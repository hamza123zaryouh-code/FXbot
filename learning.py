"""
Self-learning module voor FTMO bot.
Slaat elk signaal en trade op in SQLite database.
Elke week analyseert de optimizer welke parameters het beste werken
en past de live config automatisch aan.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    _SKLEARN_OK = True
except ImportError:
    _SKLEARN_OK = False

log = logging.getLogger("ftmo_bot")

# Globale ML model state (wordt wekelijks hertraind)
_ml_model: Optional["LogisticRegression"] = None
_ml_scaler: Optional["StandardScaler"] = None

DB_PATH = Path(__file__).parent / "bot_learning.db"

# ===========================================================================
#  DATABASE SETUP
# ===========================================================================

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            symbol      TEXT    NOT NULL,
            direction   TEXT    NOT NULL,
            adx_val     REAL,
            rsi_val     REAL,
            body_pct    REAL,
            session_hr  INTEGER,
            atr_val     REAL,
            ema_dist    REAL,
            traded      INTEGER DEFAULT 0,
            win         INTEGER,
            pnl         REAL,
            closed_ts   TEXT
        );

        CREATE TABLE IF NOT EXISTS params_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            symbol      TEXT,
            param       TEXT    NOT NULL,
            old_val     REAL,
            new_val     REAL,
            reason      TEXT
        );

        CREATE TABLE IF NOT EXISTS weekly_stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            week        TEXT    NOT NULL,
            symbol      TEXT    NOT NULL,
            trades      INTEGER,
            wins        INTEGER,
            winrate     REAL,
            total_pnl   REAL,
            avg_adx     REAL,
            best_hour   INTEGER
        );
    """)
    con.commit()
    con.close()

# ===========================================================================
#  SIGNAAL LOGGEN
# ===========================================================================

def log_signal(symbol: str, direction: str, adx_val: float, rsi_val: float,
               body_pct: float, session_hr: int, atr_val: float,
               ema_dist: float, traded: bool) -> int:
    """Sla elk gegenereerd signaal op. Geeft row-id terug voor later updaten."""
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        """INSERT INTO signals
           (ts, symbol, direction, adx_val, rsi_val, body_pct, session_hr,
            atr_val, ema_dist, traded)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now(tz=timezone.utc).isoformat(), symbol, direction,
         adx_val, rsi_val, body_pct, session_hr, atr_val, ema_dist,
         1 if traded else 0)
    )
    row_id = cur.lastrowid
    con.commit()
    con.close()
    return row_id

def update_signal_result(row_id: int, win: bool, pnl: float):
    """Koppel trade-uitkomst aan het originele signaal."""
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "UPDATE signals SET win=?, pnl=?, closed_ts=? WHERE id=?",
        (1 if win else 0, pnl, datetime.now(tz=timezone.utc).isoformat(), row_id)
    )
    con.commit()
    con.close()

# ===========================================================================
#  ML MODEL  (Logistic Regression, geïnspireerd op trentstauff/FXBot)
# ===========================================================================

def train_ml_model() -> bool:
    """
    Train een logistic regression model op historische trade data uit de database.
    Features: adx, rsi, body_pct, session_hr, atr, ema_dist  →  target: win (1/0).
    Geeft True als training geslaagd, False als te weinig data of sklearn ontbreekt.
    """
    global _ml_model, _ml_scaler
    if not _SKLEARN_OK:
        log.warning("scikit-learn niet gevonden — ML model uitgeschakeld. pip install scikit-learn")
        return False

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT adx_val, rsi_val, body_pct, session_hr, atr_val, ema_dist, win
        FROM signals
        WHERE traded=1 AND win IS NOT NULL
          AND adx_val IS NOT NULL AND rsi_val IS NOT NULL
          AND body_pct IS NOT NULL AND session_hr IS NOT NULL
          AND atr_val IS NOT NULL AND ema_dist IS NOT NULL
        ORDER BY ts DESC
        LIMIT 500
    """)
    rows = cur.fetchall()
    con.close()

    if len(rows) < 30:
        log.info("ML model: te weinig trades (%d < 30) — training overgeslagen", len(rows))
        return False

    X = np.array([[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows], dtype=float)
    y = np.array([r[6] for r in rows], dtype=int)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = LogisticRegression(C=1e6, max_iter=500, random_state=42)
    model.fit(X_scaled, y)

    _ml_model = model
    _ml_scaler = scaler

    acc = model.score(X_scaled, y)
    wins_in_data = int(y.sum())
    log.info("ML model getraind — %d trades  wins=%d  accuracy=%.1f%%",
             len(rows), wins_in_data, acc * 100)
    return True


def predict_win_probability(adx_val: float, rsi_val: float, body_pct: float,
                             session_hr: int, atr_val: float, ema_dist: float) -> float:
    """
    Voorspel winkans (0.0–1.0) voor een signaal op basis van het ML model.
    Geeft 0.5 terug als geen model beschikbaar (neutraal = geen filter).
    """
    if _ml_model is None or _ml_scaler is None:
        return 0.5
    try:
        X = np.array([[adx_val, rsi_val, body_pct, session_hr, atr_val, ema_dist]], dtype=float)
        X_scaled = _ml_scaler.transform(X)
        proba = _ml_model.predict_proba(X_scaled)[0]
        classes = list(_ml_model.classes_)
        win_idx = classes.index(1) if 1 in classes else -1
        return float(proba[win_idx]) if win_idx >= 0 else 0.5
    except Exception as exc:
        log.warning("ML predict fout: %s", exc)
        return 0.5

# ===========================================================================
#  WEKELIJKSE ANALYSE
# ===========================================================================

def analyze_last_weeks(weeks: int = 4) -> dict:
    """
    Analyseer de laatste N weken en geef aanbevelingen per symbool.
    Returns dict met per-symbool statistieken en aanbevolen aanpassingen.
    """
    con   = sqlite3.connect(DB_PATH)
    cur   = con.cursor()
    result = {}

    symbols = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD"]
    for sym in symbols:
        # Laatste N weken trades voor dit symbool
        cur.execute("""
            SELECT adx_val, rsi_val, body_pct, session_hr, win, pnl
            FROM signals
            WHERE symbol=? AND traded=1 AND win IS NOT NULL
              AND ts >= datetime('now', ?)
            ORDER BY ts DESC
        """, (sym, f"-{weeks * 7} days"))
        rows = cur.fetchall()

        if len(rows) < 5:
            result[sym] = {"trades": len(rows), "skip": True}
            continue

        trades    = len(rows)
        wins      = sum(1 for r in rows if r[4] == 1)
        winrate   = wins / trades
        total_pnl = sum(r[5] for r in rows if r[5] is not None)
        avg_adx   = sum(r[0] for r in rows if r[0]) / trades

        # Beste uur analyse
        hour_pnl = {}
        for r in rows:
            hr = r[3]
            if hr is not None:
                hour_pnl[hr] = hour_pnl.get(hr, 0) + (r[5] or 0)
        best_hour = max(hour_pnl, key=hour_pnl.get) if hour_pnl else None

        # ADX winrate analyse — splits in laag/hoog
        low_adx  = [r for r in rows if r[0] and r[0] < 38]
        high_adx = [r for r in rows if r[0] and r[0] >= 38]
        low_wr   = sum(1 for r in low_adx  if r[4]==1) / len(low_adx)  if low_adx  else None
        high_wr  = sum(1 for r in high_adx if r[4]==1) / len(high_adx) if high_adx else None

        result[sym] = {
            "trades":    trades,
            "winrate":   winrate,
            "total_pnl": total_pnl,
            "avg_adx":   avg_adx,
            "best_hour": best_hour,
            "low_adx_wr":  low_wr,
            "high_adx_wr": high_wr,
            "skip": False,
        }

    con.close()
    return result

# ===========================================================================
#  AUTO-OPTIMIZER
# ===========================================================================

def run_weekly_optimizer(C, tg_func) -> list[str]:
    """
    Analyseer performance en pas C parameters automatisch aan.
    Geeft lijst van toegepaste wijzigingen terug.
    """
    stats   = analyze_last_weeks(weeks=4)
    changes = []
    con     = sqlite3.connect(DB_PATH)

    for sym, s in stats.items():
        if s.get("skip"):
            continue

        wr  = s["winrate"]
        pnl = s["total_pnl"]

        # 1. Risico aanpassen op basis van winrate + P&L
        current_risk = C.risk_pct_per_sym.get(sym, 0.5)
        new_risk     = current_risk

        if wr < 0.32 and pnl < 0:
            # Slecht presterende periode → risico verlagen met 20%
            new_risk = round(max(0.3, current_risk * 0.80), 2)
            reason = f"winrate {wr:.0%} en negatieve P&L"
        elif wr > 0.50 and pnl > 0:
            # Goed presterende periode → risico verhogen met 10%
            max_risk = 2.0 if sym == "XAUUSD" else 1.8
            new_risk = round(min(max_risk, current_risk * 1.10), 2)
            reason = f"winrate {wr:.0%} en positieve P&L"
        else:
            reason = None

        if new_risk != current_risk and reason:
            C.risk_pct_per_sym[sym] = new_risk
            msg = f"{sym}: risico {current_risk}% → {new_risk}% ({reason})"
            changes.append(msg)
            log.info("OPTIMIZER %s", msg)
            con.execute(
                "INSERT INTO params_history (ts, symbol, param, old_val, new_val, reason) VALUES (?,?,?,?,?,?)",
                (datetime.now(tz=timezone.utc).isoformat(), sym, "risk_pct", current_risk, new_risk, reason)
            )

        # 2. ADX drempel aanpassen op basis van low vs high ADX winrate
        low_wr  = s.get("low_adx_wr")
        high_wr = s.get("high_adx_wr")
        if low_wr is not None and high_wr is not None:
            current_adx = C.adx_min_per_sym.get(sym, C.h1_adx_min)
            if high_wr > low_wr + 0.15 and low_wr < 0.35:
                # Hoge ADX veel beter dan lage ADX → drempel verhogen
                new_adx = round(min(40.0, current_adx + 2.0), 1)
                if new_adx != current_adx:
                    C.adx_min_per_sym[sym] = new_adx
                    msg = f"{sym}: ADX min {current_adx} → {new_adx} (hoge ADX beter: {high_wr:.0%} vs {low_wr:.0%})"
                    changes.append(msg)
                    log.info("OPTIMIZER %s", msg)
                    con.execute(
                        "INSERT INTO params_history (ts, symbol, param, old_val, new_val, reason) VALUES (?,?,?,?,?,?)",
                        (datetime.now(tz=timezone.utc).isoformat(), sym, "adx_min", current_adx, new_adx, msg)
            )
            elif low_wr > high_wr - 0.05 and current_adx > C.h1_adx_min:
                # Lage en hoge ADX vergelijkbaar → drempel mogen verlagen
                new_adx = round(max(C.h1_adx_min, current_adx - 1.0), 1)
                if new_adx != current_adx:
                    C.adx_min_per_sym[sym] = new_adx
                    msg = f"{sym}: ADX min {current_adx} → {new_adx} (meer signalen toelaten)"
                    changes.append(msg)
                    con.execute(
                        "INSERT INTO params_history (ts, symbol, param, old_val, new_val, reason) VALUES (?,?,?,?,?,?)",
                        (datetime.now(tz=timezone.utc).isoformat(), sym, "adx_min", current_adx, new_adx, msg)
                    )

    con.commit()
    con.close()

    if changes:
        msg = "🤖 <b>WEKELIJKSE OPTIMIZER</b>\n\nParameters aangepast:\n" + "\n".join(f"• {c}" for c in changes)
        tg_func(msg)
        log.info("Optimizer klaar — %d wijzigingen", len(changes))
    else:
        log.info("Optimizer klaar — geen wijzigingen nodig")

    train_ml_model()
    return changes

# ===========================================================================
#  WEEKRAPPORT
# ===========================================================================

def build_week_report(tg_func, start_balance: float, current_balance: float):
    """Stuur wekelijks uitgebreid rapport via Telegram."""
    stats    = analyze_last_weeks(weeks=1)
    total_pnl = current_balance - start_balance
    total_pct = total_pnl / start_balance * 100 if start_balance > 0 else 0

    lines = [f"📈 <b>WEEKRAPPORT</b>\n"]
    lines.append(f"Balans  : ${current_balance:,.0f}  ({total_pct:+.1f}% totaal)\n")

    for sym, s in stats.items():
        if s.get("skip"):
            lines.append(f"{sym}: te weinig data")
            continue
        wr  = s['winrate']
        pnl = s['total_pnl']
        tr  = s['trades']
        emoji = "✅" if pnl > 0 else "❌"
        lines.append(f"{emoji} {sym}: {tr} trades  {wr:.0%} WR  ${pnl:+,.0f}")

    tg_func("\n".join(lines))
