"""
backtest.py — "NY Flow + Monday" onder FundingPips Zero regels
==============================================================
Event-gedreven backtest (M5, chronologisch) die de compliance-laag van
lbo_bot.py exact naspeelt en per week rapporteert: posities / wins / verlies,
plus max drawdown over de hele periode en de payout-checks.

Strategie (identiek aan lbo_bot.py):
  MON   maandag BUY GBPUSD   00:05 -> 20:00 UTC                 SL 40p  risk EUR 500
  MONA  maandag BUY AUDUSD   00:05 -> 20:00 UTC                 SL 30p  risk EUR 500
  H12   werkdag BUY GBPUSD+EURUSD   12:00 -> 13:00 UTC          SL 20p  risk EUR 300
  H14   werkdag BUY GBPUSD+EURUSD   14:00 -> 15:00 UTC          SL 20p  risk EUR 300
  H18   ma-do   BUY USDJPY          18:00 -> 20:00 UTC          SL 20p  risk EUR 500
  H18G  ma-do   SELL GBPUSD         18:00 -> 20:00 UTC          SL 20p  risk EUR 300

Compliance (spiegel van lbo_bot.py):
  risk-cap EUR 500/trade | max 2 posities, 1 per symbool | dagstop -EUR 1.600
  FP dag-breach EUR 4.800 (backstop) | trailing vloer EUR 8.000 vanaf equity-high
  open-risk cap EUR 1.400 | vrijdag: geen entry >=16:00, flatten 20:30 UTC

Gebruik:
  python backtest.py              # laatste 3 maanden (cache indien aanwezig)
  python backtest.py --months 12  # langere periode
  python backtest.py --refetch    # cache verversen via Dukascopy

Data: Dukascopy bid-prijzen (indicatief — andere feed dan FundingPips).
Caveat: de newsfilter (±10 min high-impact) is niet naspeelbaar zonder
historische kalender; live worden sommige window-trades geskipt/afgekapt.
"""
from __future__ import annotations

import argparse
import math
import os
import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
#  PARAMETERS  (spiegel van lbo_bot.py)
# ---------------------------------------------------------------------------
ACCOUNT_INITIAL   = 160_000.0    # EUR
PIP               = 0.0001
LOT_CONTRACT      = 100_000
VOL_MIN, VOL_STEP = 0.01, 0.01
COMMISSION        = 3.5          # EUR per lot round-trip (conservatief)
SPREAD_PIPS       = 0.5          # round-trip spreadkosten (bid-data)

RISK_EUR_MAX      = 500.0
MAX_TOTAL_POS     = 2
DAY_STOP_EUR      = 1_600.0
FP_DAY_BREACH_EUR = 4_800.0
FP_TRAIL_EUR      = 8_000.0
TRAIL_MARGIN_EUR  = 1_200.0
OPEN_RISK_CAP_EUR = 1_400.0
PROFIT_DAY_EUR    = 400.0        # payout-winstdag
FRIDAY_LAST_ENTRY_H = 16
FRIDAY_FLAT = (20, 30)

# Sleeves: (naam, symbolen, weekdagen, richting, entry-minuut, exit-minuut, SL pips, risk EUR)
SLEEVES = (
    ("MON",  ("GBPUSD",),          (0,),            "BUY",  5, 20 * 60, 40.0, 500.0),
    ("MONA", ("AUDUSD",),          (0,),            "BUY",  5, 20 * 60, 30.0, 500.0),
    ("H12",  ("GBPUSD", "EURUSD"), (0, 1, 2, 3, 4), "BUY",  12 * 60, 13 * 60, 20.0, 300.0),
    ("H14",  ("GBPUSD", "EURUSD"), (0, 1, 2, 3, 4), "BUY",  14 * 60, 15 * 60, 20.0, 300.0),
    ("H18",  ("USDJPY",),          (0, 1, 2, 3),    "BUY",  18 * 60, 20 * 60, 20.0, 500.0),
    ("H18G", ("GBPUSD",),          (0, 1, 2, 3),    "SELL", 18 * 60, 20 * 60, 20.0, 300.0),
)

SYMBOLS = ("GBPUSD", "EURUSD", "USDJPY", "AUDUSD")

def pip_of(symbol: str) -> float:
    return 0.01 if "JPY" in symbol else 0.0001

CACHE = os.path.join(os.path.dirname(__file__), "_bt_data_cache.pkl")
LONG_CACHE = os.path.join(os.path.dirname(__file__), "_bt_long_cache.pkl")

# ---------------------------------------------------------------------------
#  DATA
# ---------------------------------------------------------------------------
def load_data(months: int, refetch: bool) -> dict:
    if not refetch and os.path.exists(LONG_CACHE):
        with open(LONG_CACHE, "rb") as f:
            d = pickle.load(f)
        print(f"Data uit cache {os.path.basename(LONG_CACHE)}: "
              f"GBP M5={len(d['gbp_m5'])}  ({d['gbp_m5'].index[0].date()} -> "
              f"{d['gbp_m5'].index[-1].date()})")
        return d
    if not refetch and os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            return pickle.load(f)

    import dukascopy_python
    from dukascopy_python.instruments import (
        INSTRUMENT_FX_MAJORS_GBP_USD, INSTRUMENT_FX_MAJORS_EUR_USD,
        INSTRUMENT_FX_MAJORS_USD_JPY, INSTRUMENT_FX_MAJORS_AUD_USD)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30 * months + 14)
    print(f"Dukascopy ophalen {start.date()} -> {end.date()} ...")

    def fetch(instr, iv):
        df = dukascopy_python.fetch(instr, iv, dukascopy_python.OFFER_SIDE_BID, start, end)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        return df

    d = {
        "gbp_m5": fetch(INSTRUMENT_FX_MAJORS_GBP_USD, dukascopy_python.INTERVAL_MIN_5),
        "eur_m5": fetch(INSTRUMENT_FX_MAJORS_EUR_USD, dukascopy_python.INTERVAL_MIN_5),
        "jpy_m5": fetch(INSTRUMENT_FX_MAJORS_USD_JPY, dukascopy_python.INTERVAL_MIN_5),
        "aud_m5": fetch(INSTRUMENT_FX_MAJORS_AUD_USD, dukascopy_python.INTERVAL_MIN_5),
        "eur_h1": fetch(INSTRUMENT_FX_MAJORS_EUR_USD, dukascopy_python.INTERVAL_HOUR_1),
        "jpy_h1": fetch(INSTRUMENT_FX_MAJORS_USD_JPY, dukascopy_python.INTERVAL_HOUR_1),
    }
    with open(CACHE, "wb") as f:
        pickle.dump(d, f)
    print(f"Opgehaald: GBP M5={len(d['gbp_m5'])}  EUR M5={len(d['eur_m5'])}")
    return d

# ---------------------------------------------------------------------------
#  SIMULATIE
# ---------------------------------------------------------------------------
@dataclass
class Pos:
    symbol: str; strat: str; direction: str
    entry: float; sl: float; lot: float
    t_in: pd.Timestamp; exit_ts: pd.Timestamp


@dataclass
class Trade:
    symbol: str; strat: str; direction: str
    t_in: pd.Timestamp; t_out: pd.Timestamp
    entry: float; exit: float; lot: float
    reason: str; pnl: float


class SymData:
    def __init__(self, m5: pd.DataFrame):
        self.t = m5.index.values
        self.h = m5["high"].to_numpy(float)
        self.l = m5["low"].to_numpy(float)
        self.c = m5["close"].to_numpy(float)


def run(data: dict, start: pd.Timestamp, end: pd.Timestamp):
    symbols = SYMBOLS
    key = {"GBPUSD": "gbp", "EURUSD": "eur", "USDJPY": "jpy", "AUDUSD": "aud"}
    syms = {s: SymData(data[f"{key[s]}_m5"]) for s in symbols}
    eur_t = data["eur_h1"].index.values
    eur_v = data["eur_h1"]["close"].to_numpy(float)
    jpy_t = data["jpy_h1"].index.values
    jpy_v = data["jpy_h1"]["close"].to_numpy(float)

    def pipval(ts64, symbol):
        """EUR per pip per lot."""
        i = np.searchsorted(eur_t, ts64, side="right") - 1
        eurusd = eur_v[i] if i >= 0 else 1.08
        if "JPY" in symbol:
            # USDJPY: 1 pip (0.01) per lot = 1000 JPY -> USD -> EUR
            j = np.searchsorted(jpy_t, ts64, side="right") - 1
            usdjpy = jpy_v[j] if j >= 0 else 150.0
            return (LOT_CONTRACT * 0.01) / usdjpy / eurusd
        return (LOT_CONTRACT * PIP) / eurusd   # quote-valuta USD

    all_t = np.unique(np.concatenate([syms[s].t for s in symbols]))
    all_t = all_t[(all_t >= start.to_datetime64()) & (all_t <= end.to_datetime64())]
    ptr = {s: int(np.searchsorted(syms[s].t, all_t[0])) for s in symbols}

    balance = ACCOUNT_INITIAL
    equity_high = ACCOUNT_INITIAL
    fp_halt = False
    positions: dict[str, Pos] = {}
    trades: list[Trade] = []
    daily_pnl = defaultdict(float)
    sleeve_done: set = set()          # (naam, symbool, datum)

    cur_day = None
    day_anchor = balance
    day_stop_hit = False
    max_dd_eur = 0.0
    max_dd_pct = 0.0

    def floating(ts64):
        tot = 0.0
        for s, pos in positions.items():
            px = syms[s].c[min(ptr[s], len(syms[s].c) - 1)]
            pips = (px - pos.entry) if pos.direction == "BUY" else (pos.entry - px)
            tot += (pips / pip_of(s)) * pipval(ts64, s) * pos.lot
        return tot

    def open_risk(ts64):
        tot = 0.0
        for s, pos in positions.items():
            d = (pos.entry - pos.sl) if pos.direction == "BUY" else (pos.sl - pos.entry)
            if d > 0:
                tot += (d / pip_of(s)) * pipval(ts64, s) * pos.lot + COMMISSION * pos.lot
        return tot

    def close_pos(s, px, ts, reason):
        nonlocal balance
        pos = positions.pop(s)
        pips = ((px - pos.entry) if pos.direction == "BUY" else (pos.entry - px)) / pip_of(s)
        pv = pipval(ts.to_datetime64(), s)
        pnl = pips * pv * pos.lot - (COMMISSION + SPREAD_PIPS * pv) * pos.lot
        balance += pnl
        daily_pnl[ts.date()] += pnl
        trades.append(Trade(s, pos.strat, pos.direction, pos.t_in, ts,
                            pos.entry, px, pos.lot, reason, pnl))

    def flatten(ts, reason):
        for s in list(positions.keys()):
            close_pos(s, syms[s].c[min(ptr[s], len(syms[s].c) - 1)], ts, reason)

    def calc_lot(sl_pips, ts64, risk, symbol):
        pv = pipval(ts64, symbol)
        lot = min(risk, RISK_EUR_MAX) / ((sl_pips + SPREAD_PIPS) * pv + COMMISSION)
        lot = math.floor(lot / VOL_STEP + 1e-9) * VOL_STEP
        return round(lot, 2) if lot >= VOL_MIN else 0.0

    def pretrade_ok(ts, new_risk, ts64, equity):
        if fp_halt or day_stop_hit:
            return False
        if ts.weekday() >= 5:
            return False
        if ts.weekday() == 4 and ts.hour >= FRIDAY_LAST_ENTRY_H:
            return False
        if len(positions) >= MAX_TOTAL_POS:
            return False
        if open_risk(ts64) + new_risk > OPEN_RISK_CAP_EUR:
            return False
        if equity - (equity_high - FP_TRAIL_EUR) < TRAIL_MARGIN_EUR + new_risk:
            return False
        if (day_anchor - equity) + new_risk >= DAY_STOP_EUR:
            return False
        return True

    for ts64 in all_t:
        ts = pd.Timestamp(ts64, tz="UTC")

        if cur_day != ts.date():
            cur_day = ts.date()
            day_anchor = max(balance, balance + floating(ts64))
            day_stop_hit = False

        # exits (SL / tijd) per symbool
        for s in symbols:
            sy = syms[s]
            i = ptr[s]
            while i < len(sy.t) and sy.t[i] < ts64:
                i += 1
            if i >= len(sy.t) or sy.t[i] != ts64:
                # geen bar op dit tijdstip: pointer op de LAATSTE bekende bar
                # laten staan (geen lookahead naar toekomstige prijzen)
                ptr[s] = max(i - 1, 0)
                continue
            ptr[s] = i
            pos = positions.get(s)
            if pos is None:
                continue
            hi, lo, cl = sy.h[i], sy.l[i], sy.c[i]
            if pos.direction == "BUY" and lo <= pos.sl:
                close_pos(s, pos.sl, ts, "SL")
            elif pos.direction == "SELL" and hi >= pos.sl:
                close_pos(s, pos.sl, ts, "SL")
            elif ts >= pos.exit_ts:
                close_pos(s, cl, ts, "TIME")

        # equity, drawdown en guards (equity_high = piek voor DD én FP-vloer)
        equity = balance + floating(ts64)
        equity_high = max(equity_high, equity)
        dd = equity_high - equity
        if dd > max_dd_eur:
            max_dd_eur, max_dd_pct = dd, dd / equity_high * 100

        if not fp_halt and equity <= equity_high - FP_TRAIL_EUR + 300:
            fp_halt = True
            flatten(ts, "FP_HALT")
        if not day_stop_hit and (day_anchor - equity) >= DAY_STOP_EUR:
            day_stop_hit = True
            flatten(ts, "DAYSTOP")
        if ts.weekday() == 4 and (ts.hour, ts.minute) >= FRIDAY_FLAT:
            flatten(ts, "WEEKEND")
        if fp_halt:
            break

        # sleeve-entries (op close van de bar die op entry-tijd eindigt)
        mod = ts.hour * 60 + ts.minute
        wd = ts.weekday()
        for name, sl_syms, days, direction, entry_mod, exit_mod, sl_pips, risk in SLEEVES:
            if mod != entry_mod - 5 or wd not in days:
                continue
            for s in sl_syms:
                skey = (name, s, cur_day)
                if s in positions or skey in sleeve_done:
                    continue
                sy = syms[s]
                i = ptr[s]
                if i >= len(sy.t) or sy.t[i] != ts64:
                    continue
                sleeve_done.add(skey)
                entry = sy.c[i]
                lot = calc_lot(sl_pips, ts64, risk, s)
                if lot <= 0:
                    continue
                new_risk = (sl_pips + SPREAD_PIPS) * pipval(ts64, s) * lot + COMMISSION * lot
                if not pretrade_ok(ts, new_risk, ts64, equity):
                    continue
                sl_px = (entry - sl_pips * pip_of(s) if direction == "BUY"
                         else entry + sl_pips * pip_of(s))
                positions[s] = Pos(
                    s, name, direction, entry, sl_px, lot, ts,
                    ts.normalize() + pd.Timedelta(minutes=exit_mod - 5))

    if positions:
        flatten(pd.Timestamp(all_t[-1], tz="UTC"), "END")

    return {"trades": trades, "balance": balance, "fp_halt": fp_halt,
            "daily_pnl": dict(daily_pnl), "max_dd_eur": max_dd_eur,
            "max_dd_pct": max_dd_pct}

# ---------------------------------------------------------------------------
#  RAPPORT
# ---------------------------------------------------------------------------
def report(res, start, end):
    tr = res["trades"]
    if not tr:
        print("\nGEEN trades in de periode.")
        return
    df = pd.DataFrame([t.__dict__ for t in tr])
    total = res["balance"] - ACCOUNT_INITIAL
    wins = int((df["pnl"] > 0).sum())
    losses = int((df["pnl"] < 0).sum())
    gw = df.loc[df["pnl"] > 0, "pnl"].sum()
    gl = df.loc[df["pnl"] < 0, "pnl"].sum()
    pf = gw / abs(gl) if gl else float("inf")

    line = "=" * 78
    print("\n" + line)
    print("  BACKTEST — NY FLOW + MONDAY  |  FundingPips Zero EUR 160K  |  " + "+".join(SYMBOLS))
    print(f"  Periode {start.date()} -> {end.date()}  |  data: Dukascopy (indicatief)")
    print(line)
    print(f"  Startbalans   EUR {ACCOUNT_INITIAL:>12,.2f}")
    print(f"  Eindbalans    EUR {res['balance']:>12,.2f}")
    print(f"  Totaal PnL    EUR {total:>+12,.2f}   ({total / ACCOUNT_INITIAL * 100:+.2f}%)")
    print(f"  Max drawdown  EUR {-res['max_dd_eur']:>12,.2f}   ({-res['max_dd_pct']:.2f}%)  [equity, M5]")
    print(f"  Trades        {len(df):>12}   (W {wins} / L {losses})")
    print(f"  Win rate      {wins / len(df) * 100:>11.1f}%   Profit factor {pf:.2f}")
    if res["fp_halt"]:
        print("  !! FP-HALT geraakt — trailing vloer/dag-breach")

    print("\n" + line)
    print("  PER WEEK  (op sluitdatum)")
    print(line)
    print(f"  {'Week':<10} {'ma-vr':<13} {'pos':>4} {'wins':>5} {'verlies':>8} "
          f"{'PnL EUR':>10} {'balans':>12}")
    print("  " + "-" * 68)
    iso = df["t_out"].dt.isocalendar()
    df["week"] = [f"{y}-W{w:02d}" for y, w in zip(iso["year"], iso["week"])]
    bal = ACCOUNT_INITIAL
    for wk in sorted(df["week"].unique()):
        g = df[df["week"] == wk]
        n = len(g)
        w = int((g["pnl"] > 0).sum())
        l = int((g["pnl"] < 0).sum())
        pnl = g["pnl"].sum()
        bal += pnl
        mon = g["t_out"].min().normalize()
        mon -= pd.Timedelta(days=mon.weekday())
        rng = f"{mon:%d/%m}-{mon + pd.Timedelta(days=4):%d/%m}"
        print(f"  {wk:<10} {rng:<13} {n:>4} {w:>5} {l:>8} {pnl:>+10,.0f} {bal:>12,.0f}")

    dp = pd.Series(res["daily_pnl"]).sort_index()
    payout = int((dp >= PROFIT_DAY_EUR).sum())
    dpd = dp.copy()
    dpd.index = pd.to_datetime(dpd.index)
    best30 = None
    for d in dpd.index:
        n30 = int((dpd[(dpd.index > d - pd.Timedelta(days=30)) & (dpd.index <= d)]
                   >= PROFIT_DAY_EUR).sum())
        best30 = n30 if best30 is None else max(best30, n30)

    print("\n" + line)
    print("  DAGEN & PAYOUT-CHECKS (FundingPips Zero)")
    print(line)
    print(f"  Handelsdagen: {len(dp)}   winstdagen {int((dp > 0).sum())}  "
          f"verliesdagen {int((dp < 0).sum())}")
    print(f"  Beste dag EUR {dp.max():+,.0f}   slechtste dag EUR {dp.min():+,.0f}")
    print(f"  Payout-winstdagen (>= EUR {PROFIT_DAY_EUR:.0f}): {payout}  |  "
          f"max in 30d-venster: {best30}  ({'HAALT 7' if best30 >= 7 else 'GEEN 7'})")
    if total > 0:
        big = dp.max() / total * 100
        print(f"  Grootste dagwinst = {big:.0f}% van totale winst "
              f"(consistency-regel max 15%: {'OK' if big <= 15 else 'LET OP'})")

    print("\n" + line)
    print("  PER SLEEVE")
    print(line)
    for (st, sym), g in df.groupby(["strat", "symbol"]):
        w = int((g["pnl"] > 0).sum())
        print(f"  {st:<5} {sym:<7} {len(g):>4} trades  W {w:>3}/{len(g) - w:<3}  "
              f"PnL EUR {g['pnl'].sum():>+10,.0f}")
    print(line)
    print("  CAVEAT: newsfilter niet naspeelbaar in backtest; live skipt de bot")
    print("  window-entries als er een high-impact event in het houd-venster valt.")
    print(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    data = load_data(args.months, args.refetch)
    end = pd.Timestamp(data["gbp_m5"].index[-1])
    start = end - pd.Timedelta(days=int(30.4 * args.months))
    res = run(data, start, end)
    report(res, start, end)


if __name__ == "__main__":
    main()
