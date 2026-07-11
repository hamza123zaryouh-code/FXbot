"""
backtest_fundingpips.py — GBPUSD LBO + NY SELL onder FundingPips Zero regels
============================================================================
Doel: exact de regels naspelen die lbo_bot.py live afdwingt, over de laatste
3 maanden, en rapporteren PER WEEK + winst/verliesdagen + winstdagen (>=EUR400)
+ max drawdown, alles in euro EN procent.

Account:  FundingPips Zero  EUR 160.000  (EUR-gedenomineerd)
Data:     Dukascopy  GBPUSD + EURUSD  (M5 + H1)  — indicatief (andere feed dan FP)

COMPLIANCE die hier wordt nagespeeld (zoals in lbo_bot.py):
  - Risk-cap  EUR 500 per trade (hard, incl. commissie) — lot naar beneden afgerond
  - Max 1 positie per symbool  (LBO-positie blokkeert NY, 2e NY wacht op 1e)
  - Eigen dagstop  -EUR 1.600 (op gerealiseerd + open risk) -> stop die dag
  - FP dag-breach  EUR 4.800  (backstop, mag nooit)
  - Trailing vloer EUR 8.000 vanaf hoogste equity
  - Open-risk cap  EUR 1.400 pre-trade
  - Weekend: vrijdag geen entry >=16:00 UTC, alles dicht 20:30 UTC
  - Break-even op 1R voor LBO EN NY
  - Alleen FX (GBPUSD)

NIET nagespeeld (databeperking) -> zie caveat in de output:
  - Newsfilter (+-10 min high-impact): vereist historische kalender; live iets
    minder trades. Effect klein en licht negatief.
"""
from __future__ import annotations
import os, sys, pickle
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import numpy as np
import pandas as pd

import dukascopy_python
from dukascopy_python.instruments import (
    INSTRUMENT_FX_MAJORS_GBP_USD, INSTRUMENT_FX_MAJORS_EUR_USD,
)

# ---------------------------------------------------------------------------
#  ACCOUNT / COMPLIANCE PARAMETERS  (spiegel van lbo_bot.py)
# ---------------------------------------------------------------------------
ACCOUNT_INITIAL   = 160_000.0     # EUR
PIP_SIZE          = 0.0001
COMMISSION        = 3.5           # per lot, round-trip (EUR ~ USD, klein)
LOT_CONTRACT      = 100_000       # 1.0 lot = 100k GBP
VOL_MIN, VOL_STEP = 0.01, 0.01

RISK_EUR_MAX      = 500.0
MAX_PER_SYMBOL    = 1
DAY_STOP_EUR      = 1_600.0
FP_DAY_BREACH_EUR = 4_800.0
FP_TRAIL_EUR      = 8_000.0
OPEN_RISK_CAP_EUR = 1_400.0
PROFIT_DAY_EUR    = 400.0         # winstdag telt vanaf +EUR400 (payout-regel)

# Strategie (identiek aan lbo_bot.py)
H1_EMA_FAST, H1_EMA_SLOW, H1_ADX_MIN = 50, 200, 18.0
LBO_ENTRY_START, LBO_ENTRY_END = 7, 13
LBO_ASIAN_START, LBO_ASIAN_END = 22, 7
LBO_TP_MULT, LBO_RANGE_MIN, LBO_RANGE_MAX = 2.5, 5, 60
LBO_BE_R = 1.0
NY_START, NY_END = 14, 17
NY_EMA_FAST, NY_EMA_SLOW = 8, 21
NY_ATR_SL_MULT, NY_RR_RATIO, NY_BE_R, NY_BODY_MIN, NY_MAX = 1.5, 2.0, 1.0, 0.45, 2
FRIDAY_LAST_ENTRY_H = 16
FRIDAY_FLAT_H, FRIDAY_FLAT_M = 20, 30

MONTHS_BACK = 3
CACHE = os.path.join(os.path.dirname(__file__), "_bt_fp_cache.pkl")

# ---------------------------------------------------------------------------
#  INDICATOREN
# ---------------------------------------------------------------------------
def ema(s, p): return s.ewm(span=p, adjust=False).mean()
def rsi_ind(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(span=p, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, 1e-9))
def atr_ind(df, p=14):
    h, l, c = df["high"], df["low"], df["close"]
    return pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()],
                     axis=1).max(axis=1).ewm(span=p, adjust=False).mean()
def adx_ind(df, p=14):
    h, l = df["high"], df["low"]
    up = h.diff(); dn = -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    ndm = dn.where((dn > up) & (dn > 0), 0.0)
    a = atr_ind(df, p)
    pdi = 100 * pdm.ewm(span=p, adjust=False).mean() / a.replace(0, 1e-9)
    ndi = 100 * ndm.ewm(span=p, adjust=False).mean() / a.replace(0, 1e-9)
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi + 1e-9)
    return dx.ewm(span=p, adjust=False).mean()

# ---------------------------------------------------------------------------
#  DATA
# ---------------------------------------------------------------------------
def fetch(instrument, interval, start, end):
    return dukascopy_python.fetch(instrument, interval,
                                  dukascopy_python.OFFER_SIDE_BID, start, end)

def load_data(refetch=False):
    if os.path.exists(CACHE) and not refetch:
        with open(CACHE, "rb") as f:
            d = pickle.load(f)
        print(f"Data uit cache: M5={len(d['m5'])} H1={len(d['h1'])} "
              f"({d['m5'].index[0].date()} -> {d['m5'].index[-1].date()})")
        return d
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=30 * MONTHS_BACK + 20)   # +warmup voor EMA200/asian
    print(f"Dukascopy ophalen {start.date()} -> {end.date()} ...")
    m5  = fetch(INSTRUMENT_FX_MAJORS_GBP_USD, dukascopy_python.INTERVAL_MIN_5,  start, end)
    h1  = fetch(INSTRUMENT_FX_MAJORS_GBP_USD, dukascopy_python.INTERVAL_HOUR_1, start, end)
    eur = fetch(INSTRUMENT_FX_MAJORS_EUR_USD, dukascopy_python.INTERVAL_HOUR_1, start, end)
    for df in (m5, h1, eur):
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
    d = {"m5": m5, "h1": h1, "eur": eur}
    with open(CACHE, "wb") as f:
        pickle.dump(d, f)
    print(f"Opgehaald: M5={len(m5)} H1={len(h1)} EURUSD={len(eur)}")
    return d

# ---------------------------------------------------------------------------
#  EUR pip-waarde & sizing (EUR-account)
# ---------------------------------------------------------------------------
def eurusd_at(eur_h1, t):
    sub = eur_h1[eur_h1.index <= t]
    return float(sub["close"].iloc[-1]) if len(sub) else 1.08

def pip_value_eur(eurusd):
    # 1 lot GBPUSD: 1 pip = LOT_CONTRACT * PIP_SIZE = $10 -> EUR = 10 / EURUSD
    return (LOT_CONTRACT * PIP_SIZE) / eurusd   # EUR per pip per lot

def calc_lot(sl_dist, eurusd):
    """Lot op de EUR500-cap, naar beneden afgerond. 0.0 = skip."""
    sl_pips = sl_dist / PIP_SIZE
    pv = pip_value_eur(eurusd)
    if sl_pips <= 0 or pv <= 0:
        return 0.0
    lot = RISK_EUR_MAX / (sl_pips * pv + COMMISSION)
    import math
    lot = math.floor(lot / VOL_STEP + 1e-9) * VOL_STEP
    if lot < VOL_MIN:
        return 0.0
    return round(lot, 2)

def pnl_eur(direction, entry, exit_p, lot, eurusd):
    pips = ((exit_p - entry) if direction == "BUY" else (entry - exit_p)) / PIP_SIZE
    return pips * pip_value_eur(eurusd) * lot - COMMISSION * lot

def risk_eur_of(sl_dist, lot, eurusd):
    return (sl_dist / PIP_SIZE) * pip_value_eur(eurusd) * lot + COMMISSION * lot

# ---------------------------------------------------------------------------
#  TRADE SIMULATIE (bar-by-bar, met break-even op 1R)
# ---------------------------------------------------------------------------
def simulate(direction, entry, sl0, tp, sl_dist, be_r, future):
    """Geeft (exit_price, exit_time, reason)."""
    sl = sl0
    be = False
    for ts, bar in future.iterrows():
        h, l, c = float(bar["high"]), float(bar["low"]), float(bar["close"])
        if direction == "BUY":
            if l <= sl:  return sl, ts, ("BE" if be and sl >= entry else "SL")
            if h >= tp:  return tp, ts, "TP"
            if not be and (c - entry) >= be_r * sl_dist:
                sl = max(sl, entry); be = True
        else:  # SELL
            if h >= sl:  return sl, ts, ("BE" if be and sl <= entry else "SL")
            if l <= tp:  return tp, ts, "TP"
            if not be and (entry - c) >= be_r * sl_dist:
                sl = min(sl, entry); be = True
    last = future.iloc[-1] if len(future) else None
    if last is None:
        return entry, None, "EOD"
    return float(last["close"]), future.index[-1], "EOD"

# ---------------------------------------------------------------------------
#  BACKTEST
# ---------------------------------------------------------------------------
@dataclass
class Trade:
    strat: str; day: str; t_in: pd.Timestamp; t_out: pd.Timestamp
    direction: str; lot: float; entry: float; exit: float
    reason: str; pnl: float; bal_after: float

def run(data):
    m5, h1, eur = data["m5"], data["h1"], data["eur"]

    ef = ema(h1["close"], H1_EMA_FAST)
    es = ema(h1["close"], H1_EMA_SLOW)
    adx = adx_ind(h1)

    start_trading = (datetime.now(timezone.utc) - timedelta(days=30 * MONTHS_BACK))
    start_trading = pd.Timestamp(start_trading).normalize()

    days = sorted(d for d in m5.index.normalize().unique()
                  if d.weekday() < 5 and d >= start_trading)

    balance = ACCOUNT_INITIAL
    equity_high = ACCOUNT_INITIAL
    fp_halt = False
    trades = []
    daily_pnl = defaultdict(float)

    cur_month = None; month_start = ACCOUNT_INITIAL

    for day in days:
        if fp_halt:
            break
        dm = (day.year, day.month)
        if dm != cur_month:
            cur_month = dm; month_start = balance

        day_start_bal = balance
        day_loss_hit  = False

        def day_loss():
            return day_start_bal - balance

        # H1 trend (laatste gesloten H1 vóór de dag)
        h1_pre = h1[h1.index < day]
        n = len(h1_pre)
        if n < H1_EMA_SLOW + 5:
            continue
        ef_v, es_v, adx_v = float(ef.iloc[n-2]), float(es.iloc[n-2]), float(adx.iloc[n-2])
        if adx_v < H1_ADX_MIN:
            trend = None
        elif ef_v > es_v: trend = "up"
        elif ef_v < es_v: trend = "down"
        else: trend = None

        busy_until = None   # tijd tot wanneer een positie open is (max 1 per symbool)

        def can_open(t, new_sl_dist, new_lot, eurusd):
            """Compliance-checklist vóór entry."""
            nonlocal day_loss_hit
            if day_loss_hit or fp_halt:
                return False
            if busy_until is not None and t <= busy_until:
                return False           # positie nog open (max 1 per symbool)
            # vrijdag: geen entry >= 16:00
            if t.weekday() == 4 and t.hour >= FRIDAY_LAST_ENTRY_H:
                return False
            new_risk = risk_eur_of(new_sl_dist, new_lot, eurusd)
            if new_risk > RISK_EUR_MAX + 1:
                return False
            # open-risk cap (max 1 positie tegelijk, dus open_risk=0 hier)
            if new_risk > OPEN_RISK_CAP_EUR:
                return False
            # dagstop: mag de trade de -1600 kunnen raken?
            if day_loss() + new_risk >= DAY_STOP_EUR:
                return False
            # trailing vloer marge
            floor = equity_high - FP_TRAIL_EUR
            if (balance - floor) < new_risk + 300:
                return False
            return True

        def register(tr, eurusd):
            nonlocal balance, equity_high, busy_until, day_loss_hit, fp_halt
            balance += tr.pnl
            tr.bal_after = balance
            equity_high = max(equity_high, balance)
            daily_pnl[day.date()] += tr.pnl
            trades.append(tr)
            # dagstop na realisatie
            if day_loss() >= DAY_STOP_EUR:
                day_loss_hit = True
            if day_loss() >= FP_DAY_BREACH_EUR * 0.9:
                day_loss_hit = True
            if balance <= equity_high - FP_TRAIL_EUR + 300:
                fp_halt = True

        weekend_flat = day.replace(hour=FRIDAY_FLAT_H, minute=FRIDAY_FLAT_M) \
                       if day.weekday() == 4 else None

        # ---- LBO ochtend --------------------------------------------------
        if trend is not None and not day_loss_hit:
            prev = day - pd.Timedelta(days=1)
            asian = m5[(m5.index >= prev + pd.Timedelta(hours=LBO_ASIAN_START)) &
                       (m5.index <  day  + pd.Timedelta(hours=LBO_ASIAN_END))]
            if len(asian) >= 3:
                rh, rl = float(asian["high"].max()), float(asian["low"].min())
                rpips = (rh - rl) / PIP_SIZE
                if LBO_RANGE_MIN <= rpips <= LBO_RANGE_MAX:
                    win = m5[(m5.index >= day + pd.Timedelta(hours=LBO_ENTRY_START)) &
                             (m5.index <  day + pd.Timedelta(hours=LBO_ENTRY_END))]
                    for ts, bar in win.iterrows():
                        direction = None
                        if bar["close"] > rh and trend == "up":   direction = "BUY"
                        elif bar["close"] < rl and trend == "down": direction = "SELL"
                        if direction is None:
                            continue
                        entry = float(bar["close"])
                        sl_p  = rl if direction == "BUY" else rh
                        sl_d  = abs(entry - sl_p)
                        tp_p  = (entry + (rh - rl) * LBO_TP_MULT if direction == "BUY"
                                 else entry - (rh - rl) * LBO_TP_MULT)
                        if sl_d <= 0:
                            continue
                        eu  = eurusd_at(eur, ts)
                        lot = calc_lot(sl_d, eu)
                        if lot <= 0:
                            continue
                        if not can_open(ts, sl_d, lot, eu):
                            break   # 1 LBO-poging per dag; geblokkeerd -> stop
                        end_cap = day + pd.Timedelta(hours=20)
                        if weekend_flat is not None:
                            end_cap = min(end_cap, weekend_flat)
                        fut = m5[(m5.index > ts) & (m5.index <= end_cap)]
                        ex, tout, why = simulate(direction, entry, sl_p, tp_p, sl_d, LBO_BE_R, fut)
                        tout = tout or ts
                        pnl = pnl_eur(direction, entry, ex, lot, eu)
                        busy_until = tout
                        register(Trade("LBO", str(day.date()), ts, tout, direction,
                                       lot, entry, ex, why, pnl, 0.0), eu)
                        break   # max 1 LBO per dag

        # ---- NY middag ----------------------------------------------------
        if trend == "down" and not day_loss_hit and not fp_halt:
            ny = m5[(m5.index.normalize() == day) &
                    (m5.index.hour >= NY_START) & (m5.index.hour < NY_END)]
            if len(ny) >= 5:
                pre = m5[m5.index < day + pd.Timedelta(hours=NY_START)]
                if len(pre) >= 50:
                    ctx = pd.concat([pre.tail(100), ny])
                    e8  = ema(ctx["close"], NY_EMA_FAST)
                    e21 = ema(ctx["close"], NY_EMA_SLOW)
                    rsi = rsi_ind(ctx["close"], 14)
                    atr = atr_ind(ctx, 14)
                    ds  = len(pre.tail(100)); nd = len(ny)
                    ny_count = 0; last_i = -999
                    for i in range(2, nd - 1):
                        if ny_count >= NY_MAX or i - last_i < 3:
                            continue
                        ci  = ds + i
                        bar = ctx.iloc[ci]
                        ts  = ctx.index[ci]
                        e8c, e8p = float(e8.iloc[ci]), float(e8.iloc[ci-1])
                        e21c, e21p = float(e21.iloc[ci]), float(e21.iloc[ci-1])
                        rsi_v, atr_v = float(rsi.iloc[ci]), float(atr.iloc[ci])
                        cr = bar["high"] - bar["low"]
                        body_ok = cr > 0 and abs(bar["close"] - bar["open"]) / cr >= NY_BODY_MIN
                        if not (e8p >= e21p and e8c < e21c and bar["close"] < bar["open"]
                                and body_ok and 25 <= rsi_v <= 60 and atr_v > 0):
                            continue
                        entry = float(bar["close"])
                        sl_d  = atr_v * NY_ATR_SL_MULT
                        sl_p  = entry + sl_d
                        tp_p  = entry - sl_d * NY_RR_RATIO
                        eu    = eurusd_at(eur, ts)
                        lot   = calc_lot(sl_d, eu)
                        if lot <= 0:
                            continue
                        if not can_open(ts, sl_d, lot, eu):
                            continue   # bv. 1e NY nog open -> wacht
                        end_cap = ny.index[-1]
                        if weekend_flat is not None:
                            end_cap = min(end_cap, weekend_flat)
                        fut = ny[(ny.index > ts) & (ny.index <= end_cap)]
                        ex, tout, why = simulate("SELL", entry, sl_p, tp_p, sl_d, NY_BE_R, fut)
                        tout = tout or ts
                        pnl = pnl_eur("SELL", entry, ex, lot, eu)
                        busy_until = tout
                        ny_count += 1; last_i = i
                        register(Trade("NY", str(day.date()), ts, tout, "SELL",
                                       lot, entry, ex, why, pnl, 0.0), eu)

    return trades, balance, equity_high, dict(daily_pnl), days, fp_halt

# ---------------------------------------------------------------------------
#  RAPPORT
# ---------------------------------------------------------------------------
def report(trades, final_bal, equity_high, daily_pnl, days, fp_halt):
    if not trades:
        print("\nGEEN trades in de periode."); return
    df = pd.DataFrame([t.__dict__ for t in trades])
    df["t_out"] = pd.to_datetime(df["t_out"], utc=True)

    total_pnl = final_bal - ACCOUNT_INITIAL
    total_pct = total_pnl / ACCOUNT_INITIAL * 100

    # Equity curve op sluitvolgorde -> max DD
    df_sorted = df.sort_values("t_out")
    eq = ACCOUNT_INITIAL + df_sorted["pnl"].cumsum()
    peak = eq.cummax()
    dd_abs = (eq - peak)
    max_dd_eur = dd_abs.min()
    max_dd_pct = (dd_abs / peak).min() * 100

    wins = int((df["pnl"] > 0).sum()); losses = int((df["pnl"] < 0).sum())
    be = len(df) - wins - losses
    wr = wins / len(df) * 100
    gw = df[df["pnl"] > 0]["pnl"].sum(); gl = df[df["pnl"] < 0]["pnl"].sum()
    pf = gw / abs(gl) if gl != 0 else float("inf")

    d0, d1 = days[0].date(), days[-1].date()
    line = "=" * 78
    print("\n" + line)
    print(f"  FUNDINGPIPS ZERO BACKTEST — GBPUSD LBO + NY SELL")
    print(f"  Periode {d0} -> {d1}  ({MONTHS_BACK} mnd)  |  data: Dukascopy (indicatief)")
    print(line)
    print(f"  Startbalans   EUR {ACCOUNT_INITIAL:>12,.2f}")
    print(f"  Eindbalans    EUR {final_bal:>12,.2f}")
    print(f"  Totaal PnL    EUR {total_pnl:>+12,.2f}   ({total_pct:+.2f}%)")
    print(f"  Max drawdown  EUR {max_dd_eur:>12,.2f}   ({max_dd_pct:.2f}%)")
    print(f"  Trades        {len(df):>12}   (W {wins} / L {losses} / BE {be})")
    print(f"  Win rate      {wr:>11.1f}%   Profit factor {pf:.2f}")
    if fp_halt:
        print("  !! FP-HALT geraakt: trailing vloer / dag-breach -> gestopt")

    # ---- Per week -----------------------------------------------------------
    print("\n" + line)
    print("  PER WEEK  (op sluitdatum van de trade)")
    print(line)
    print(f"  {'Week':<12} {'dd/mm-dd/mm':<13} {'N':>2} {'W':>2} {'L':>2} "
          f"{'PnL EUR':>11} {'PnL %':>7} {'Balans':>12}")
    print("  " + "-" * 74)
    df["week"] = df["t_out"].dt.isocalendar().apply(lambda r: f"{r['year']}-W{r['week']:02d}", axis=1)
    run_bal = ACCOUNT_INITIAL
    for wk in sorted(df["week"].unique()):
        g = df[df["week"] == wk].sort_values("t_out")
        n = len(g); w = int((g["pnl"] > 0).sum()); l = int((g["pnl"] < 0).sum())
        pnl = g["pnl"].sum()
        start_bal = run_bal
        run_bal += pnl
        pct = pnl / start_bal * 100
        dr = f"{g['t_out'].min():%d/%m}-{g['t_out'].max():%d/%m}"
        print(f"  {wk:<12} {dr:<13} {n:>2} {w:>2} {l:>2} "
              f"{pnl:>+11,.0f} {pct:>+6.2f}% {run_bal:>12,.0f}")

    # ---- Dag-analyse --------------------------------------------------------
    dp = pd.Series(daily_pnl)
    win_days  = int((dp > 0).sum())
    loss_days = int((dp < 0).sum())
    flat_days = int((dp == 0).sum())
    payout_days = int((dp >= PROFIT_DAY_EUR).sum())    # >= EUR400 telt voor payout
    trading_days = len(dp)

    print("\n" + line)
    print("  DAG-ANALYSE  (dagen met minstens 1 gesloten trade)")
    print(line)
    print(f"  Handelsdagen met trades : {trading_days}")
    print(f"  Winstdagen  (PnL > 0)   : {win_days}")
    print(f"  Verliesdagen(PnL < 0)   : {loss_days}")
    print(f"  Vlakke dagen(PnL = 0)   : {flat_days}")
    print(f"  Payout-winstdagen >=EUR{PROFIT_DAY_EUR:.0f} : {payout_days}   "
          f"(FundingPips eist 7 per 30 dagen)")
    print(f"  Beste dag   : EUR {dp.max():>+10,.0f}")
    print(f"  Slechtste dag: EUR {dp.min():>+10,.0f}")

    # Rollend 30-dagen venster: haalt het de 7 payout-dagen?
    dpd = dp.copy(); dpd.index = pd.to_datetime(dpd.index)
    dpd = dpd.sort_index()
    best_30 = 0
    for d in dpd.index:
        window = dpd[(dpd.index > d - pd.Timedelta(days=30)) & (dpd.index <= d)]
        best_30 = max(best_30, int((window >= PROFIT_DAY_EUR).sum()))
    print(f"  Max payout-dagen in enig 30d-venster: {best_30}  "
          f"{'-> HAALT 7' if best_30 >= 7 else '-> HAALT GEEN 7 (payout-risico)'}")

    # ---- Per strategie ------------------------------------------------------
    print("\n" + line)
    print("  PER STRATEGIE")
    print(line)
    for s in ["LBO", "NY"]:
        g = df[df["strat"] == s]
        if not len(g): continue
        w = int((g["pnl"] > 0).sum()); l = int((g["pnl"] < 0).sum())
        wr_s = w / len(g) * 100
        gws = g[g["pnl"] > 0]["pnl"].sum(); gls = g[g["pnl"] < 0]["pnl"].sum()
        pf_s = gws / abs(gls) if gls != 0 else float("inf")
        print(f"  {s:<4} {len(g):>3} trades  WR {wr_s:>4.0f}%  PF {pf_s:>4.2f}  "
              f"PnL EUR {g['pnl'].sum():>+10,.0f}")

    # Consistency (max 15% van cycluswinst op 1 dag)
    if total_pnl > 0:
        worst = dp.max() / total_pnl * 100
        print("\n" + line)
        print("  PAYOUT-CHECKS (FundingPips Zero)")
        print(line)
        print(f"  Grootste dagwinst = {worst:.0f}% van totale winst  "
              f"(consistency-regel: max 15% per dag)  "
              f"{'OK' if worst <= 15 else 'LET OP: spreiden'}")
        cushion = ACCOUNT_INITIAL * 0.03
        print(f"  Safety cushion (3% = EUR {cushion:,.0f}): "
              f"{'opgebouwd' if total_pnl >= cushion else 'nog niet gehaald'}")
    print(line)
    print("  CAVEAT: newsfilter (+-10 min high-impact) niet in deze data nagespeeld;")
    print("  live iets minder trades. Prijzen zijn Dukascopy-feed (niet FundingPips).")
    print(line)


if __name__ == "__main__":
    refetch = "--refetch" in sys.argv
    data = load_data(refetch=refetch)
    res = run(data)
    report(*res)
