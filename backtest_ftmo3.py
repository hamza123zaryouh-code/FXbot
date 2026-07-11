"""
3-Fase FTMO Backtest — $160,000
  Fase 1 — Challenge  : +10% ($16,000) zo snel mogelijk, max 10% DD
  Fase 2 — Verificatie: +5%  ($8,000)  in ~3 weken, max 5% DD
  Fase 3 — Funded     : stabiel $4-5k/maand, max 5% DD
"""

import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field

import MetaTrader5 as mt5
import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

@dataclass
class BtConfig:
    name:               str   = ""
    symbols:            list  = field(default_factory=lambda: ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY"])
    pip_size:           dict  = field(default_factory=lambda: {
                                    "XAUUSD": 0.10, "EURUSD": 0.0001,
                                    "GBPUSD": 0.0001, "USDJPY": 0.01,
                                })
    session_per_sym:    dict  = field(default_factory=lambda: {
                                    "XAUUSD": (7, 20), "EURUSD": (7, 20),
                                    "GBPUSD": (7, 20), "USDJPY": (0, 12),
                                })
    h1_ema_fast:        int   = 50
    h1_ema_slow:        int   = 200
    h1_adx_period:      int   = 14
    h1_adx_min:         float = 30.0
    h1_ema_momentum:    int   = 20
    h1_slope_bars:      int   = 3
    m15_ema:            int   = 50
    m15_rsi_period:     int   = 14
    rsi_buy_lo:         float = 45.0
    rsi_buy_hi:         float = 75.0
    rsi_sell_lo:        float = 25.0
    rsi_sell_hi:        float = 55.0
    ema_slope_bars:     int   = 2
    body_pct_min:       float = 0.55
    atr_period:         int   = 14
    atr_sl_mult:        float = 1.5
    rr_ratio:           float = 2.0
    risk_pct_per_sym:   dict  = field(default_factory=lambda: {
                                    "XAUUSD": 1.0, "EURUSD": 0.3,
                                    "GBPUSD": 0.3, "USDJPY": 0.3,
                                })
    max_open_per_sym:   int   = 2
    daily_loss_limit:   float = 0.04
    weekly_loss_limit:  float = 0.025
    max_drawdown_limit: float = 0.09
    profit_target_pct:  float = 0.0
    start_balance:      float = 160_000.0
    session_start:      int   = 7
    session_end:        int   = 20
    bt_start: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    bt_end:   datetime = field(default_factory=lambda: datetime(2026, 6, 25, tzinfo=timezone.utc))


# ── FASE 1: CHALLENGE — Agressief ─────────────────────────────────────────
CHALLENGE = BtConfig(
    name               = "FASE 1 — CHALLENGE (+10% = $16k)",
    h1_adx_min         = 22.0,        # meer signalen
    atr_sl_mult        = 1.3,         # kapper SL = grotere lot
    rr_ratio           = 2.0,
    max_open_per_sym   = 3,
    weekly_loss_limit  = 0.035,       # 3.5% per week
    daily_loss_limit   = 0.04,
    max_drawdown_limit = 0.09,
    profit_target_pct  = 0.10,
    risk_pct_per_sym   = {
        "XAUUSD": 2.5,               # hoofdmotor
        "EURUSD": 0.5,
        "GBPUSD": 0.5,
        "USDJPY": 0.4,
    },
    start_balance      = 160_000.0,
    bt_start           = datetime(2026, 1, 1,  tzinfo=timezone.utc),
    bt_end             = datetime(2026, 6, 25, tzinfo=timezone.utc),
)

# ── FASE 2: VERIFICATIE — Medium ──────────────────────────────────────────
VERIFICATIE = BtConfig(
    name               = "FASE 2 — VERIFICATIE (+5% = $8k)",
    h1_adx_min         = 27.0,
    atr_sl_mult        = 1.4,
    rr_ratio           = 2.0,
    max_open_per_sym   = 2,
    weekly_loss_limit  = 0.025,
    daily_loss_limit   = 0.04,
    max_drawdown_limit = 0.05,        # strenger: max 5% DD in verificatie
    profit_target_pct  = 0.05,
    risk_pct_per_sym   = {
        "XAUUSD": 1.5,
        "EURUSD": 0.35,
        "GBPUSD": 0.35,
        "USDJPY": 0.3,
    },
    start_balance      = 160_000.0,
    bt_start           = datetime(2026, 1, 1,  tzinfo=timezone.utc),
    bt_end             = datetime(2026, 6, 25, tzinfo=timezone.utc),
)

# ── FASE 3: FUNDED — Conservatief ─────────────────────────────────────────
FUNDED = BtConfig(
    name               = "FASE 3 — FUNDED ($4-5k/maand)",
    h1_adx_min         = 30.0,
    atr_sl_mult        = 1.5,
    rr_ratio           = 2.0,
    max_open_per_sym   = 2,
    weekly_loss_limit  = 0.015,       # 1.5% per week — beschermt kapitaal
    daily_loss_limit   = 0.03,
    max_drawdown_limit = 0.08,
    profit_target_pct  = 0.0,
    risk_pct_per_sym   = {
        "XAUUSD": 0.9,
        "EURUSD": 0.2,
        "GBPUSD": 0.2,
        "USDJPY": 0.2,
    },
    start_balance      = 160_000.0,
    bt_start           = datetime(2026, 1, 1,  tzinfo=timezone.utc),
    bt_end             = datetime(2026, 6, 25, tzinfo=timezone.utc),
)

# ---------------------------------------------------------------------------
# MT5 / HELPERS
# ---------------------------------------------------------------------------

def connect():
    if not mt5.initialize():
        sys.exit(f"MT5 mislukt: {mt5.last_error()}")
    print(f"MT5 verbonden — {mt5.account_info().company}")

def get_bars(symbol, tf, start, end):
    rates = mt5.copy_rates_range(symbol, tf, start, end)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    return df

def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def rsi_series(s, period):
    d = s.diff()
    g = d.clip(lower=0).ewm(span=period, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(span=period, adjust=False).mean()
    return 100 - 100 / (1 + g / (l + 1e-9))

def adx_series(df, period):
    h, l, c = df["high"], df["low"], df["close"]
    pdm = (h - h.shift(1)).clip(lower=0)
    mdm = (l.shift(1) - l).clip(lower=0)
    pdm = pdm.where(pdm > mdm, 0.0)
    mdm = mdm.where(mdm > pdm.shift(0), 0.0)
    tr  = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / atr
    mdi = 100 * mdm.ewm(span=period, adjust=False).mean() / atr
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.ewm(span=period, adjust=False).mean()

def atr_fn(df, period):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def prep_h1(df, C):
    df = df.copy()
    df["ef"]  = ema(df["close"], C.h1_ema_fast)
    df["es"]  = ema(df["close"], C.h1_ema_slow)
    df["em"]  = ema(df["close"], C.h1_ema_momentum)
    df["adx"] = adx_series(df, C.h1_adx_period)
    return df

def prep_m15(df, C):
    df = df.copy()
    df["e50"] = ema(df["close"], C.m15_ema)
    df["rsi"] = rsi_series(df["close"], C.m15_rsi_period)
    df["atr"] = atr_fn(df, C.atr_period)
    return df

# ---------------------------------------------------------------------------
# SIMULATIE
# ---------------------------------------------------------------------------

def simulate(symbol, h1, m15, C):
    info = mt5.symbol_info(symbol)
    if info is None:
        return []

    trades   = []
    balance  = C.start_balance
    digits   = info.digits
    ts       = info.trade_tick_size
    tv       = info.trade_tick_value
    vstep    = info.volume_step
    vmin     = info.volume_min
    vmax     = info.volume_max

    open_trades = []
    week_bal    = balance;  cur_week    = -1;   wk_guard = False
    day_bal     = balance;  cur_day     = None; day_guard = False

    arr = m15[["open","high","low","close","e50","rsi","atr"]].values
    idx = m15.index
    wu  = max(C.m15_ema+20, C.m15_rsi_period+C.ema_slope_bars+5, C.atr_period+5)

    for i in range(wu, len(arr)-1):
        t = idx[i]
        o, hi, lo, cl, e50, rv, av = arr[i]
        e50p = arr[i - C.ema_slope_bars][4]
        po, phi, plo, pcl, pe50 = arr[i-1][0], arr[i-1][1], arr[i-1][2], arr[i-1][3], arr[i-1][4]

        sl_d = av * C.atr_sl_mult
        tp_d = sl_d * C.rr_ratio

        # Week/dag reset
        wk = t.isocalendar()[1]
        if wk != cur_week:
            cur_week = wk; week_bal = balance; wk_guard = False
        dy = t.date()
        if dy != cur_day:
            cur_day = dy; day_bal = balance; day_guard = False

        # SL/TP check
        still = []
        for ot in open_trades:
            done = False
            if ot["dir"] == "BUY":
                if lo <= ot["sl"]:
                    ot.update(xt=t, xp=ot["sl"], pnl=-ot["risk"], res="LOSS")
                    balance += ot["pnl"]; trades.append(ot); done = True
                elif hi >= ot["tp"]:
                    ot.update(xt=t, xp=ot["tp"], pnl=ot["risk"]*C.rr_ratio, res="WIN")
                    balance += ot["pnl"]; trades.append(ot); done = True
            else:
                if hi >= ot["sl"]:
                    ot.update(xt=t, xp=ot["sl"], pnl=-ot["risk"], res="LOSS")
                    balance += ot["pnl"]; trades.append(ot); done = True
                elif lo <= ot["tp"]:
                    ot.update(xt=t, xp=ot["tp"], pnl=ot["risk"]*C.rr_ratio, res="WIN")
                    balance += ot["pnl"]; trades.append(ot); done = True
            if not done:
                still.append(ot)
        open_trades = still

        eq = balance

        # Winst-doelstelling bereikt
        if C.profit_target_pct > 0 and (eq - C.start_balance) / C.start_balance >= C.profit_target_pct:
            for ot in open_trades:
                pnl = (cl - ot["ep"]) / ts * tv * ot["lot"] if ot["dir"] == "BUY" \
                      else (ot["ep"] - cl) / ts * tv * ot["lot"]
                ot.update(xt=t, xp=cl, pnl=pnl, res="TARGET")
                trades.append(ot)
            open_trades = []
            break

        # FTMO guards
        if not day_guard and (day_bal - eq) / max(day_bal, 1) >= C.daily_loss_limit:
            day_guard = True
        if not wk_guard and (week_bal - eq) / max(week_bal, 1) >= C.weekly_loss_limit:
            wk_guard = True
        if day_guard or wk_guard:
            continue
        if (C.start_balance - eq) / C.start_balance >= C.max_drawdown_limit:
            continue
        if len(open_trades) >= C.max_open_per_sym:
            continue

        # Sessie
        ss, se = C.session_per_sym.get(symbol, (C.session_start, C.session_end))
        if not (ss <= t.hour < se):
            continue

        # H1 trend
        hi1 = h1.index.searchsorted(t, side="right") - 1
        if hi1 < C.h1_ema_slow + 20:
            continue
        if h1["adx"].iloc[hi1] < C.h1_adx_min:
            continue
        ef  = h1["ef"].iloc[hi1]
        es  = h1["es"].iloc[hi1]
        em  = h1["em"].iloc[hi1]
        efp = h1["ef"].iloc[hi1 - C.h1_slope_bars]

        if ef > es:
            if em <= ef or ef <= efp:
                continue
            trend = "up"
        elif ef < es:
            if em >= ef or ef >= efp:
                continue
            trend = "down"
        else:
            continue

        # M15 twee-candle bevestiging
        if not (plo <= pe50 <= phi):
            continue
        rng = hi - lo
        if rng == 0:
            continue
        if abs(cl - o) / rng < C.body_pct_min:
            continue

        direction = None
        if trend == "up":
            if cl > o and cl > e50 and cl > pcl and e50 > e50p:
                if C.rsi_buy_lo <= rv <= C.rsi_buy_hi:
                    direction = "BUY"
        else:
            if cl < o and cl < e50 and cl < pcl and e50 < e50p:
                if C.rsi_sell_lo <= rv <= C.rsi_sell_hi:
                    direction = "SELL"

        if direction is None:
            continue

        # Positiegrootte
        rp       = C.risk_pct_per_sym.get(symbol, 0.5)
        risk_amt = balance * rp / 100
        lperlot  = (sl_d / ts) * tv
        if lperlot <= 0:
            continue
        lot = max(vmin, min(vmax, round(round(risk_amt / lperlot / vstep) * vstep, 8)))

        slp = round(cl - sl_d, digits) if direction == "BUY" else round(cl + sl_d, digits)
        tpp = round(cl + tp_d, digits) if direction == "BUY" else round(cl - tp_d, digits)

        open_trades.append({
            "sym": symbol, "dir": direction,
            "et": t, "ep": cl, "sl": slp, "tp": tpp,
            "lot": lot, "risk": risk_amt,
            "xt": None, "xp": None, "pnl": None, "res": None,
        })

    # Sluit resterende trades
    lc = m15["close"].iloc[-1]
    for ot in open_trades:
        pnl = (lc - ot["ep"]) / ts * tv * ot["lot"] if ot["dir"] == "BUY" \
              else (ot["ep"] - lc) / ts * tv * ot["lot"]
        ot.update(xt=m15.index[-1], xp=lc, pnl=pnl, res="OPEN_CLOSE")
        trades.append(ot)

    return trades

# ---------------------------------------------------------------------------
# RAPPORTAGE
# ---------------------------------------------------------------------------

def wk_label(dt):
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"

def max_dd(pnl_list, start):
    peak = eq = start; mdd = 0.0
    for p in pnl_list:
        eq += p; peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100)
    return mdd

def report(C, raw_trades):
    if not raw_trades:
        print(f"\n  [{C.name}] Geen trades.")
        return {}

    # Hervorm keys naar leesbare namen
    rows = []
    for t in raw_trades:
        rows.append({
            "symbol":      t["sym"],
            "direction":   t["dir"],
            "entry_time":  pd.to_datetime(t["et"], utc=True),
            "exit_time":   pd.to_datetime(t["xt"], utc=True),
            "entry_price": t["ep"],
            "sl":          t["sl"],
            "tp":          t["tp"],
            "lot":         t["lot"],
            "pnl":         t["pnl"],
            "result":      t["res"],
        })
    df       = pd.DataFrame(rows)
    df["week"] = df["entry_time"].apply(wk_label)
    df["win"]  = df["pnl"] > 0

    sep = "=" * 78
    print(f"\n{sep}")
    print(f"  {C.name}")
    print(f"  Balance: ${C.start_balance:,.0f}  |  "
          f"XAU risico: {C.risk_pct_per_sym.get('XAUUSD')}%  |  "
          f"ADX>={C.h1_adx_min}  |  Max open: {C.max_open_per_sym}  |  "
          f"ATR x{C.atr_sl_mult}")
    print(sep)
    print(f"{'Week':<12} {'Trades':>6} {'Winrate':>8} {'W':>4} {'L':>4} "
          f"{'P&L ($)':>12} {'Balans ($)':>13}  {'Winst%':>7}")
    print("-" * 78)

    running = C.start_balance
    wk_reached = None
    for wk, grp in df.groupby("week"):
        n   = len(grp); w = int(grp["win"].sum()); pnl = grp["pnl"].sum()
        running += pnl
        pct = (running - C.start_balance) / C.start_balance * 100
        teken = "+" if pnl >= 0 else ""
        flag = ""
        if C.profit_target_pct > 0 and wk_reached is None and pct >= C.profit_target_pct * 100:
            wk_reached = wk; flag = " *** DOEL BEREIKT ***"
        print(f"{wk:<12} {n:>6} {w/n*100:>7.1f}%  {w:>4} {n-w:>4}  "
              f"{teken}{pnl:>10,.0f}    {running:>11,.0f}  {pct:>+6.1f}%{flag}")

    total     = len(df)
    wins      = int(df["win"].sum())
    net_pnl   = df["pnl"].sum()
    end_bal   = C.start_balance + net_pnl
    wr        = wins / total * 100 if total else 0
    mdd_val   = max_dd(df.sort_values("exit_time")["pnl"].tolist(), C.start_balance)
    gw        = df.loc[df["pnl"] > 0, "pnl"].sum()
    gl        = df.loc[df["pnl"] < 0, "pnl"].sum()
    pf        = gw / abs(gl) if gl != 0 else float("inf")
    avg_w     = df.loc[df["win"],  "pnl"].mean() if wins else 0
    avg_l     = df.loc[~df["win"], "pnl"].mean() if (total-wins) else 0
    maanden   = (C.bt_end - C.bt_start).days / 30.44

    print(sep)
    print(f"  Trades        : {total}   ({wins}W / {total-wins}L)   Winrate: {wr:.1f}%")
    print(f"  Gem. winst    : ${avg_w:+,.0f}  |  Gem. verlies : ${avg_l:+,.0f}")
    print(f"  Netto P&L     : ${net_pnl:+,.0f}  ({net_pnl/C.start_balance*100:.1f}%)")
    print(f"  Eindbalance   : ${end_bal:,.0f}")
    print(f"  Max drawdown  : {mdd_val:.2f}%  {'OK' if mdd_val < C.max_drawdown_limit*100 else 'OVER LIMIET!'}")
    print(f"  Profit factor : {pf:.2f}")
    print(f"  Gem. per maand: ${net_pnl/maanden:+,.0f}")
    if C.profit_target_pct > 0:
        if wk_reached:
            # bereken weeknummer
            wk_nr  = int(wk_reached.split("-W")[1])
            start_wk = int(wk_label(C.bt_start).split("-W")[1])
            weken_nodig = wk_nr - start_wk + 1
            print(f"  Doel bereikt  : {wk_reached}  (na ca. {weken_nodig} weken)")
        else:
            print(f"  Doel bereikt  : NIET in deze periode")
    print(sep)

    print("  Per symbool:")
    for sym, grp in df.groupby("symbol"):
        w2 = int(grp["win"].sum())
        print(f"    {sym:<8}  {len(grp):>3} trades  {w2/len(grp)*100:.1f}%  "
              f"P&L ${grp['pnl'].sum():+,.0f}")

    return {
        "name": C.name, "trades": total, "wr": wr,
        "pnl": net_pnl, "mdd": mdd_val, "pf": pf,
        "maand": net_pnl / maanden,
        "doel_week": wk_reached,
    }

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    connect()

    # Data eenmalig laden
    print(f"\nData laden...")
    cache = {}
    for sym in CHALLENGE.symbols:
        mt5.symbol_select(sym, True)
        h1r  = get_bars(sym, mt5.TIMEFRAME_H1,  CHALLENGE.bt_start, CHALLENGE.bt_end)
        m15r = get_bars(sym, mt5.TIMEFRAME_M15, CHALLENGE.bt_start, CHALLENGE.bt_end)
        if not h1r.empty and not m15r.empty:
            cache[sym] = (h1r, m15r)
            print(f"  {sym}: {len(h1r)} H1 / {len(m15r)} M15 bars geladen")

    results = []
    for C in [CHALLENGE, VERIFICATIE, FUNDED]:
        all_trades = []
        for sym, (h1r, m15r) in cache.items():
            h1  = prep_h1(h1r, C)
            m15 = prep_m15(m15r, C)
            sym_trades = simulate(sym, h1, m15, C)
            all_trades.extend(sym_trades)
        r = report(C, all_trades)
        if r:
            results.append(r)

    mt5.shutdown()

    # Eindoverzicht
    print("\n\n" + "=" * 78)
    print("  EINDOVERZICHT — 3 FASES FTMO $160,000")
    print("=" * 78)
    hdrs = ["Fase", "Trades", "Winrate", "Netto P&L", "Per maand", "Max DD", "Doel bereikt"]
    print(f"{'Fase':<26} {'Trades':>6} {'Winrate':>8} {'P&L':>10} "
          f"{'Maand':>9} {'Max DD':>7}  {'Doel'})")
    print("-" * 78)
    for r in results:
        doel = str(r.get("doel_week") or "n.v.t.")
        print(f"{r['name']:<26} {r['trades']:>6} {r['wr']:>7.1f}%  "
              f"${r['pnl']:>9,.0f}  ${r['maand']:>8,.0f}  {r['mdd']:>6.2f}%   {doel}")
    print("=" * 78)
    print()
    print("  PLAN:")
    print("  1) Start met CHALLENGE parameters")
    print("     -> Zodra balance $176,000+ bereikt: challenge geslaagd")
    print("  2) Schakel naar VERIFICATIE parameters")
    print("     -> Zodra balance $184,000+ bereikt: verificatie geslaagd")
    print("  3) Schakel naar FUNDED parameters voor stabiel inkomen")
    print()

if __name__ == "__main__":
    main()
