"""
BTCUSD backtest — laatste 2 maanden (26 apr - 26 jun 2026)
Dezelfde strategie als ftmo_bot.py: EMA trend + ADX + M15 pullback
"""

import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
import configparser

@dataclass
class Cfg:
    name:              str   = ""
    symbol:            str   = "BTCUSD"
    session_start:     int   = 0
    session_end:       int   = 23
    h1_ema_fast:       int   = 50
    h1_ema_slow:       int   = 200
    h1_adx_period:     int   = 14
    h1_adx_min:        float = 30.0
    h1_ema_momentum:   int   = 20
    h1_slope_bars:     int   = 3
    m15_ema:           int   = 50
    m15_rsi_period:    int   = 14
    rsi_buy_lo:        float = 45.0
    rsi_buy_hi:        float = 75.0
    rsi_sell_lo:       float = 25.0
    rsi_sell_hi:       float = 55.0
    ema_slope_bars:    int   = 2
    body_pct_min:      float = 0.55
    atr_period:        int   = 14
    atr_sl_mult:       float = 1.5
    rr_ratio:          float = 2.0
    adx_slope_bars:    int   = 2
    adx_boost_min:     float = 45.0
    adx_boost_mult:    float = 2.0
    risk_pct:          float = 1.0
    max_open:          int   = 2
    daily_loss_limit:  float = 0.04
    weekly_loss_limit: float = 0.03
    max_drawdown_limit:float = 0.09
    start_balance:     float = 152_563.15
    bt_start: datetime = field(default_factory=lambda: datetime(2026, 4, 26, tzinfo=timezone.utc))
    bt_end:   datetime = field(default_factory=lambda: datetime(2026, 6, 26, tzinfo=timezone.utc))


def connect():
    cfg = configparser.ConfigParser()
    cfg.read(r"C:\TradingBot\FXbot\bot_config.ini")
    try:
        login = int(cfg["MT5"]["login"])
        pw    = cfg["MT5"]["password"]
        srv   = cfg["MT5"]["server"]
        ok = mt5.initialize(login=login, password=pw, server=srv)
    except Exception:
        ok = mt5.initialize()
    if not ok:
        sys.exit(f"MT5 mislukt: {mt5.last_error()}")
    print(f"MT5 verbonden — {mt5.account_info().company}")


def get_bars(sym, tf, start, end):
    r = mt5.copy_rates_range(sym, tf, start, end)
    if r is None or len(r) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(r)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("time")


def ema(s, p):  return s.ewm(span=p, adjust=False).mean()

def rsi_s(s, p):
    d = s.diff()
    g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(span=p, adjust=False).mean()
    return 100 - 100 / (1 + g / (l + 1e-9))

def adx_s(df, p):
    h, l, c = df["high"], df["low"], df["close"]
    pdm = (h - h.shift(1)).clip(lower=0)
    mdm = (l.shift(1) - l).clip(lower=0)
    pdm = pdm.where(pdm > mdm, 0.0)
    mdm = mdm.where(mdm > pdm.shift(0), 0.0)
    tr  = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    at  = tr.ewm(span=p, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=p, adjust=False).mean() / at
    mdi = 100 * mdm.ewm(span=p, adjust=False).mean() / at
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.ewm(span=p, adjust=False).mean()

def atr_s(df, p):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

def prep_h1(df, C):
    df = df.copy()
    df["ef"]  = ema(df["close"], C.h1_ema_fast)
    df["es"]  = ema(df["close"], C.h1_ema_slow)
    df["em"]  = ema(df["close"], C.h1_ema_momentum)
    df["adx"] = adx_s(df, C.h1_adx_period)
    return df

def prep_m15(df, C):
    df = df.copy()
    df["e50"] = ema(df["close"], C.m15_ema)
    df["rsi"] = rsi_s(df["close"], C.m15_rsi_period)
    df["atr"] = atr_s(df, C.atr_period)
    return df


def simulate(h1, m15, C):
    info = mt5.symbol_info(C.symbol)
    if info is None:
        print(f"BTCUSD niet gevonden op MT5!"); return []
    ts = info.trade_tick_size
    tv = info.trade_tick_value
    vs = info.volume_step
    vn = info.volume_min
    vx = info.volume_max
    dg = info.digits

    trades = []
    balance = C.start_balance
    open_t = []
    wb = balance; cw = -1; wg = False
    db = balance; cd = None; dg2 = False

    arr = m15[["open", "high", "low", "close", "e50", "rsi", "atr"]].values
    idx = m15.index
    wu  = max(C.m15_ema + 20, C.m15_rsi_period + C.ema_slope_bars + 5, C.atr_period + 5)

    for i in range(wu, len(arr) - 1):
        t = idx[i]
        o, hi, lo, cl, e50, rv, av = arr[i]
        e50p = arr[i - C.ema_slope_bars][4]
        po, phi, plo, pcl, pe50 = arr[i-1][0], arr[i-1][1], arr[i-1][2], arr[i-1][3], arr[i-1][4]

        wk = t.isocalendar()[1]
        if wk != cw: cw = wk; wb = balance; wg = False
        dy = t.date()
        if dy != cd: cd = dy; db = balance; dg2 = False

        still = []
        for ot in open_t:
            done = False
            if ot["dir"] == "BUY":
                if lo <= ot["sl"]: ot.update(xt=t, xp=ot["sl"], pnl=-ot["risk"], res="LOSS"); balance += ot["pnl"]; trades.append(ot); done = True
                elif hi >= ot["tp"]: ot.update(xt=t, xp=ot["tp"], pnl=ot["risk"] * C.rr_ratio, res="WIN"); balance += ot["pnl"]; trades.append(ot); done = True
            else:
                if hi >= ot["sl"]: ot.update(xt=t, xp=ot["sl"], pnl=-ot["risk"], res="LOSS"); balance += ot["pnl"]; trades.append(ot); done = True
                elif lo <= ot["tp"]: ot.update(xt=t, xp=ot["tp"], pnl=ot["risk"] * C.rr_ratio, res="WIN"); balance += ot["pnl"]; trades.append(ot); done = True
            if not done: still.append(ot)
        open_t = still

        eq = balance
        if not dg2 and (db - eq) / max(db, 1) >= C.daily_loss_limit: dg2 = True
        if not wg  and (wb - eq) / max(wb, 1) >= C.weekly_loss_limit: wg  = True
        if dg2 or wg: continue
        if (C.start_balance - eq) / C.start_balance >= C.max_drawdown_limit: continue
        if len(open_t) >= C.max_open: continue
        if not (C.session_start <= t.hour < C.session_end): continue

        h1i = h1.index.searchsorted(t, side="right") - 1
        if h1i < C.h1_ema_slow + 20: continue

        adx_val = h1["adx"].iloc[h1i]
        if adx_val < C.h1_adx_min: continue

        if C.adx_slope_bars > 0:
            if h1i < C.adx_slope_bars: continue
            adx_old = h1["adx"].iloc[h1i - C.adx_slope_bars]
            if adx_val <= adx_old: continue

        ef  = h1["ef"].iloc[h1i]
        es  = h1["es"].iloc[h1i]
        em  = h1["em"].iloc[h1i]
        efp = h1["ef"].iloc[h1i - C.h1_slope_bars]

        if ef > es:
            if em <= ef or ef <= efp: continue
            trend = "up"
        elif ef < es:
            if em >= ef or ef >= efp: continue
            trend = "down"
        else:
            continue

        if not (plo <= pe50 <= phi): continue
        rng = hi - lo
        if rng == 0: continue
        if abs(cl - o) / rng < C.body_pct_min: continue

        direction = None
        if trend == "up":
            if cl > o and cl > e50 and cl > pcl and e50 > e50p:
                if C.rsi_buy_lo <= rv <= C.rsi_buy_hi: direction = "BUY"
        else:
            if cl < o and cl < e50 and cl < pcl and e50 < e50p:
                if C.rsi_sell_lo <= rv <= C.rsi_sell_hi: direction = "SELL"
        if direction is None: continue

        sl_d = av * C.atr_sl_mult
        tp_d = sl_d * C.rr_ratio

        risk_amt = balance * C.risk_pct / 100
        lperlot  = (sl_d / ts) * tv
        if lperlot <= 0: continue
        raw_lot  = risk_amt / lperlot

        if C.adx_boost_min > 0 and adx_val >= C.adx_boost_min:
            raw_lot *= C.adx_boost_mult

        lot = max(vn, min(vx, round(round(raw_lot / vs) * vs, 8)))
        slp = round(cl - sl_d, dg) if direction == "BUY" else round(cl + sl_d, dg)
        tpp = round(cl + tp_d, dg) if direction == "BUY" else round(cl - tp_d, dg)

        open_t.append({
            "dir": direction, "et": t, "ep": cl, "sl": slp, "tp": tpp,
            "lot": lot, "risk": risk_amt,
            "xt": None, "xp": None, "pnl": None, "res": None,
            "adx": adx_val
        })

    lc = m15["close"].iloc[-1]
    for ot in open_t:
        pnl = (lc - ot["ep"]) / ts * tv * ot["lot"] if ot["dir"] == "BUY" else (ot["ep"] - lc) / ts * tv * ot["lot"]
        ot.update(xt=m15.index[-1], xp=lc, pnl=pnl, res="OPEN_CLOSE")
        trades.append(ot)
    return trades


def max_dd(pnls, start):
    peak = eq = start; mdd = 0.0
    for p in pnls:
        eq += p; peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak * 100)
    return mdd


def report(C, trades):
    if not trades:
        print("Geen trades gevonden."); return

    df = pd.DataFrame(trades)
    df["et"] = pd.to_datetime(df["et"], utc=True)
    df["xt"] = pd.to_datetime(df["xt"], utc=True)
    df["win"] = df["pnl"] > 0

    def wk_lbl(dt):
        iso = dt.isocalendar(); return f"{iso[0]}-W{iso[1]:02d}"
    df["week"] = df["et"].apply(wk_lbl)

    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  BTCUSD BACKTEST  —  {C.bt_start.date()} tot {C.bt_end.date()}")
    print(f"  Risico: {C.risk_pct}%  |  ADX>={C.h1_adx_min}  |  ADX slope: {C.adx_slope_bars} bars")
    print(f"  Boost bij ADX>{C.adx_boost_min}: x{C.adx_boost_mult}  |  SL: ATR×{C.atr_sl_mult}  |  RR: {C.rr_ratio}")
    print(f"  Startbalans: ${C.start_balance:,.2f}")
    print(sep)
    print(f"{'Week':<12} {'#':>4} {'WR':>7} {'W':>4} {'L':>4}  {'P&L':>11}  {'Balans':>12}  {'%':>7}")
    print("-" * 72)

    running = C.start_balance
    for wk, grp in df.groupby("week"):
        n   = len(grp)
        w   = int(grp["win"].sum())
        pnl = grp["pnl"].sum()
        running += pnl
        pct = (running - C.start_balance) / C.start_balance * 100
        teken = "+" if pnl >= 0 else ""
        print(f"{wk:<12} {n:>4} {w/n*100:>6.0f}%  {w:>4} {n-w:>4}  "
              f"{teken}{pnl:>9,.0f}   {running:>11,.2f}  {pct:>+6.2f}%")

    total = len(df); wins = int(df["win"].sum()); losses = total - wins
    net   = df["pnl"].sum()
    wr    = wins / total * 100 if total else 0
    mdd_v = max_dd(df.sort_values("xt")["pnl"].tolist(), C.start_balance)
    gw    = df.loc[df["pnl"] > 0, "pnl"].sum()
    gl    = df.loc[df["pnl"] < 0, "pnl"].sum()
    pf    = gw / abs(gl) if gl != 0 else float("inf")
    avg_w = df.loc[df["win"], "pnl"].mean() if wins else 0
    avg_l = df.loc[~df["win"], "pnl"].mean() if losses else 0
    end_bal = C.start_balance + net
    maanden = (C.bt_end - C.bt_start).days / 30.44

    print(sep)
    print(f"  Totaal  : {total} trades  ({wins}W / {losses}L)  |  Winrate: {wr:.1f}%")
    print(f"  Gem. win: ${avg_w:+,.0f}  |  Gem. verlies: ${avg_l:+,.0f}  |  PF: {pf:.2f}")
    print(f"  Netto   : ${net:+,.0f}  ({net/C.start_balance*100:+.2f}%)")
    print(f"  Eindbal.: ${end_bal:,.2f}")
    print(f"  Max DD  : {mdd_v:.2f}%  {'✓ OK' if mdd_v < C.max_drawdown_limit * 100 else '✗ BOVEN LIMIET'}")
    print(f"  Per mnd : ${net/maanden:+,.0f}")

    # Daganalyse
    df["day"] = df["xt"].dt.date
    daily = df.groupby("day")["pnl"].sum()
    print(f"\n  Dagelijkse analyse:")
    print(f"    Beste dag   : {daily.idxmax()}  ${daily.max():+,.0f}")
    print(f"    Slechtste   : {daily.idxmin()}  ${daily.min():+,.0f}")
    print(f"    Daglimiet   : ${C.start_balance * C.daily_loss_limit:,.0f}  "
          f"(slechtste dag = {abs(daily.min()) / (C.start_balance * C.daily_loss_limit) * 100:.0f}% van limiet)")

    # Boost vs geen boost
    boost_trades = df[df["adx"] >= C.adx_boost_min]
    norm_trades  = df[df["adx"] <  C.adx_boost_min]
    if len(boost_trades) > 0:
        bw = int(boost_trades["win"].sum())
        print(f"\n  ADX boost (ADX>={C.adx_boost_min:.0f}) : {len(boost_trades)} trades  "
              f"{bw/len(boost_trades)*100:.0f}% WR  ${boost_trades['pnl'].sum():+,.0f}")
    if len(norm_trades) > 0:
        nw = int(norm_trades["win"].sum())
        print(f"  Normaal (ADX<{C.adx_boost_min:.0f})  : {len(norm_trades)} trades  "
              f"{nw/len(norm_trades)*100:.0f}% WR  ${norm_trades['pnl'].sum():+,.0f}")

    # BUY vs SELL
    for d in ["BUY", "SELL"]:
        dg = df[df["dir"] == d]
        if len(dg) > 0:
            dw = int(dg["win"].sum())
            print(f"  {d:<4} : {len(dg):>3} trades  {dw/len(dg)*100:.0f}% WR  ${dg['pnl'].sum():+,.0f}")

    print(sep)

    # Beste/slechtste trades
    print(f"\n  Top 5 beste trades:")
    for _, row in df.nlargest(5, "pnl").iterrows():
        print(f"    {row['et'].strftime('%d %b %H:%M')} {row['dir']:<4}  ADX={row['adx']:.1f}  "
              f"lot={row['lot']:.2f}  P&L=${row['pnl']:+,.0f}  [{row['res']}]")

    print(f"\n  Top 5 slechtste trades:")
    for _, row in df.nsmallest(5, "pnl").iterrows():
        print(f"    {row['et'].strftime('%d %b %H:%M')} {row['dir']:<4}  ADX={row['adx']:.1f}  "
              f"lot={row['lot']:.2f}  P&L=${row['pnl']:+,.0f}  [{row['res']}]")

    print(sep)


def main():
    connect()
    C = Cfg()
    mt5.symbol_select(C.symbol, True)

    print(f"\nData laden voor BTCUSD ({C.bt_start.date()} – {C.bt_end.date()})...")
    h1r  = get_bars(C.symbol, mt5.TIMEFRAME_H1,  C.bt_start, C.bt_end)
    m15r = get_bars(C.symbol, mt5.TIMEFRAME_M15, C.bt_start, C.bt_end)

    if h1r.empty or m15r.empty:
        print("Geen data ontvangen van MT5. Controleer of BTCUSD beschikbaar is.")
        mt5.shutdown(); return

    print(f"  H1 bars : {len(h1r)}")
    print(f"  M15 bars: {len(m15r)}")
    print(f"  Periode : {h1r.index[0].date()} tot {h1r.index[-1].date()}")

    h1  = prep_h1(h1r, C)
    m15 = prep_m15(m15r, C)

    trades = simulate(h1, m15, C)
    report(C, trades)
    mt5.shutdown()


if __name__ == "__main__":
    main()
