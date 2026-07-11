"""
Vergelijkende backtest: Origineel vs. Momentum-filter
Haalt H1 + M15 data op uit MT5 en draait de simulatie tweemaal:
  A) Originele strategie (geen momentum filter)
  B) Met H1 momentum filter (log-returns rolling window, window=8)

Doel: beoordelen of de toevoeging winstgevend is.
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
    momentum_window:    int   = 8    # H1 bars voor log-returns rolling mean
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
                                    "XAUUSD": 1.2, "EURUSD": 0.3,
                                    "GBPUSD": 0.3, "USDJPY": 0.3,
                                })
    risk_pct:           float = 0.5
    max_open_per_sym:   int   = 2
    daily_loss_limit:   float = 0.04
    weekly_loss_limit:  float = 0.025
    max_drawdown_limit: float = 0.09
    start_balance:      float = 160_000.0
    bt_start: datetime = field(default_factory=lambda: datetime(2026, 1, 1,  tzinfo=timezone.utc))
    bt_end:   datetime = field(default_factory=lambda: datetime(2026, 6, 25, tzinfo=timezone.utc))

CFG = BtConfig()

# ---------------------------------------------------------------------------
# MT5
# ---------------------------------------------------------------------------

def connect():
    if not mt5.initialize():
        print(f"MT5 initialize() mislukt: {mt5.last_error()}")
        sys.exit(1)
    print(f"MT5 verbonden — {mt5.account_info().company}")

def get_bars(symbol, timeframe, start, end) -> pd.DataFrame:
    rates = mt5.copy_rates_range(symbol, timeframe, start, end)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    return df

# ---------------------------------------------------------------------------
# INDICATOREN
# ---------------------------------------------------------------------------

def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def rsi_series(s: pd.Series, period: int) -> pd.Series:
    delta    = s.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))

def adx_series(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c  = df["high"], df["low"], df["close"]
    plus_dm  = (h - h.shift(1)).clip(lower=0)
    minus_dm = (l.shift(1) - l).clip(lower=0)
    plus_dm  = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm = minus_dm.where(minus_dm > plus_dm.shift(0), 0.0)
    tr       = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_s    = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr_s
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
    return dx.ewm(span=period, adjust=False).mean()

def atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

# ---------------------------------------------------------------------------
# DATASETS VOORBEREIDEN
# ---------------------------------------------------------------------------

def prepare_h1(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = ema(df["close"], CFG.h1_ema_fast)
    df["ema_slow"] = ema(df["close"], CFG.h1_ema_slow)
    df["ema_mom"]  = ema(df["close"], CFG.h1_ema_momentum)
    df["adx"]      = adx_series(df, CFG.h1_adx_period)
    # Momentum: rolling mean van H1 log-returns (trentstauff/FXBot idee)
    log_ret = np.log(df["close"] / df["close"].shift(1))
    df["momentum"] = log_ret.rolling(CFG.momentum_window).mean()
    return df

def prepare_m15(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema50"] = ema(df["close"], CFG.m15_ema)
    df["rsi"]   = rsi_series(df["close"], CFG.m15_rsi_period)
    df["atr"]   = atr_series(df, CFG.atr_period)
    return df

# ---------------------------------------------------------------------------
# SIMULATIE
# ---------------------------------------------------------------------------

def simulate(symbol: str, h1: pd.DataFrame, m15: pd.DataFrame,
             use_momentum: bool = False) -> list[dict]:
    trades  = []
    balance = CFG.start_balance

    info = mt5.symbol_info(symbol)
    if info is None:
        print(f"  {symbol}: symboolinfo niet gevonden")
        return []

    tick_size  = info.trade_tick_size
    tick_value = info.trade_tick_value
    vol_step   = info.volume_step
    vol_min    = info.volume_min
    vol_max    = info.volume_max
    digits     = info.digits

    open_trades:      list[dict] = []
    week_start_bal    = balance
    current_week      = -1
    weekly_guard_hit  = False
    day_start_bal     = balance
    current_day       = None
    daily_guard_hit   = False

    warmup = max(CFG.m15_ema + 20, CFG.m15_rsi_period + CFG.ema_slope_bars + 5, CFG.atr_period + 5)

    for i in range(warmup, len(m15) - 1):
        candle_t = m15.index[i]
        o  = m15["open"].iat[i]
        hc = m15["high"].iat[i]
        lc = m15["low"].iat[i]
        cl = m15["close"].iat[i]
        e50     = m15["ema50"].iat[i]
        rsi_v   = m15["rsi"].iat[i]
        atr_v   = m15["atr"].iat[i]
        e50_prev = m15["ema50"].iat[i - CFG.ema_slope_bars]

        sl_dist = atr_v * CFG.atr_sl_mult
        tp_dist = sl_dist * CFG.rr_ratio

        # Raakcandle (i-1) voor twee-candle bevestiging
        p_o  = m15["open"].iat[i - 1]
        p_h  = m15["high"].iat[i - 1]
        p_l  = m15["low"].iat[i - 1]
        p_cl = m15["close"].iat[i - 1]
        p_e50 = m15["ema50"].iat[i - 1]

        # ── Week/dag reset ───────────────────────────────────────────────
        iso_week = candle_t.isocalendar()[1]
        if iso_week != current_week:
            current_week     = iso_week
            week_start_bal   = balance
            weekly_guard_hit = False

        candle_day = candle_t.date()
        if candle_day != current_day:
            current_day      = candle_day
            day_start_bal    = balance
            daily_guard_hit  = False

        # ── Open trades afhandelen (SL/TP) ───────────────────────────────
        still_open = []
        for ot in open_trades:
            closed = False
            if ot["direction"] == "BUY":
                if lc <= ot["sl"]:
                    ot.update(exit_time=candle_t, exit_price=ot["sl"],
                              pnl=-ot["risk"], result="LOSS")
                    balance += ot["pnl"]; trades.append(ot); closed = True
                elif hc >= ot["tp"]:
                    ot.update(exit_time=candle_t, exit_price=ot["tp"],
                              pnl=ot["risk"] * CFG.rr_ratio, result="WIN")
                    balance += ot["pnl"]; trades.append(ot); closed = True
            else:
                if hc >= ot["sl"]:
                    ot.update(exit_time=candle_t, exit_price=ot["sl"],
                              pnl=-ot["risk"], result="LOSS")
                    balance += ot["pnl"]; trades.append(ot); closed = True
                elif lc <= ot["tp"]:
                    ot.update(exit_time=candle_t, exit_price=ot["tp"],
                              pnl=ot["risk"] * CFG.rr_ratio, result="WIN")
                    balance += ot["pnl"]; trades.append(ot); closed = True
            if not closed:
                still_open.append(ot)
        open_trades = still_open

        # ── FTMO guards ──────────────────────────────────────────────────
        equity = balance
        if not daily_guard_hit and day_start_bal > 0:
            if (day_start_bal - equity) / day_start_bal >= CFG.daily_loss_limit:
                daily_guard_hit = True
        if not weekly_guard_hit and week_start_bal > 0:
            if (week_start_bal - equity) / week_start_bal >= CFG.weekly_loss_limit:
                weekly_guard_hit = True
        max_dd_hit = (CFG.start_balance - equity) / CFG.start_balance >= CFG.max_drawdown_limit
        if daily_guard_hit or weekly_guard_hit or max_dd_hit:
            continue

        if len(open_trades) >= CFG.max_open_per_sym:
            continue

        # ── Sessiefilter ─────────────────────────────────────────────────
        s_start, s_end = CFG.session_per_sym.get(symbol, (7, 20))
        if not (s_start <= candle_t.hour < s_end):
            continue

        # ── H1 trend + ADX ───────────────────────────────────────────────
        h1_idx = h1.index.searchsorted(candle_t, side="right") - 1
        if h1_idx < CFG.h1_ema_slow + 20:
            continue

        adx_val = h1["adx"].iat[h1_idx]
        if adx_val < CFG.h1_adx_min:
            continue

        ef      = h1["ema_fast"].iat[h1_idx]
        es      = h1["ema_slow"].iat[h1_idx]
        em      = h1["ema_mom"].iat[h1_idx]
        ef_prev = h1["ema_fast"].iat[h1_idx - CFG.h1_slope_bars]

        if ef > es:
            trend = "up"
            if em <= ef or ef <= ef_prev:
                continue
        elif ef < es:
            trend = "down"
            if em >= ef or ef >= ef_prev:
                continue
        else:
            continue

        # ── MOMENTUM FILTER (nieuw) ───────────────────────────────────────
        if use_momentum:
            mom = h1["momentum"].iat[h1_idx]
            if pd.isna(mom):
                continue
            if trend == "up"   and mom <= 0:
                continue
            if trend == "down" and mom >= 0:
                continue

        # ── M15 twee-candle signaal ──────────────────────────────────────
        if not (p_l <= p_e50 <= p_h):      # raakcandle raakt EMA
            continue
        crange = hc - lc
        if crange == 0:
            continue
        if abs(cl - o) / crange < CFG.body_pct_min:
            continue

        if trend == "up":
            if not (cl > o and cl > e50 and cl > p_cl):
                continue
            if e50 <= e50_prev:
                continue
            if not (CFG.rsi_buy_lo <= rsi_v <= CFG.rsi_buy_hi):
                continue
            direction = "BUY"
            entry_p   = cl
            sl_p      = round(entry_p - sl_dist, digits)
            tp_p      = round(entry_p + tp_dist, digits)
        elif trend == "down":
            if not (cl < o and cl < e50 and cl < p_cl):
                continue
            if e50 >= e50_prev:
                continue
            if not (CFG.rsi_sell_lo <= rsi_v <= CFG.rsi_sell_hi):
                continue
            direction = "SELL"
            entry_p   = cl
            sl_p      = round(entry_p + sl_dist, digits)
            tp_p      = round(entry_p - tp_dist, digits)
        else:
            continue

        # ── Positiegrootte ───────────────────────────────────────────────
        risk_pct_sym = CFG.risk_pct_per_sym.get(symbol, CFG.risk_pct)
        risk_amount  = balance * (risk_pct_sym / 100.0)
        ticks_in_sl  = sl_dist / tick_size
        loss_per_lot = ticks_in_sl * tick_value
        if loss_per_lot <= 0:
            continue
        raw_lot = risk_amount / loss_per_lot
        lot     = round(round(raw_lot / vol_step) * vol_step, 8)
        lot     = max(vol_min, min(lot, vol_max))

        open_trades.append({
            "symbol":      symbol,
            "direction":   direction,
            "entry_time":  candle_t,
            "entry_price": entry_p,
            "sl":          sl_p,
            "tp":          tp_p,
            "lot":         lot,
            "risk":        risk_amount,
            "exit_time":   None, "exit_price": None,
            "pnl":         None, "result":      None,
        })

    # ── Forceer sluiting open trades ─────────────────────────────────────
    last_close = m15["close"].iloc[-1]
    for ot in open_trades:
        if ot["direction"] == "BUY":
            pnl = (last_close - ot["entry_price"]) / tick_size * tick_value * ot["lot"]
        else:
            pnl = (ot["entry_price"] - last_close) / tick_size * tick_value * ot["lot"]
        ot.update(exit_time=m15.index[-1], exit_price=last_close,
                  pnl=pnl, result="OPEN_CLOSE")
        trades.append(ot)

    return trades

# ---------------------------------------------------------------------------
# RAPPORTAGE
# ---------------------------------------------------------------------------

def max_drawdown(pnl_list: list, start: float) -> float:
    peak = eq = start
    mdd  = 0.0
    for p in pnl_list:
        eq  += p
        peak = max(peak, eq)
        mdd  = max(mdd, (peak - eq) / peak * 100)
    return mdd

def stats(all_trades: list[dict], label: str) -> dict:
    if not all_trades:
        return {}
    df         = pd.DataFrame(all_trades)
    df["win"]  = df["pnl"] > 0
    total      = len(df)
    wins       = int(df["win"].sum())
    net_pnl    = df["pnl"].sum()
    end_bal    = CFG.start_balance + net_pnl
    wr         = wins / total * 100 if total else 0
    df_s       = df.sort_values("exit_time")
    mdd        = max_drawdown(df_s["pnl"].tolist(), CFG.start_balance)
    gross_win  = df.loc[df["pnl"] > 0, "pnl"].sum()
    gross_loss = df.loc[df["pnl"] < 0, "pnl"].sum()
    pf         = gross_win / abs(gross_loss) if gross_loss != 0 else float("inf")
    avg_win    = df.loc[df["win"],  "pnl"].mean() if wins else 0
    avg_loss   = df.loc[~df["win"], "pnl"].mean() if (total - wins) else 0
    return dict(label=label, total=total, wins=wins, wr=wr, net_pnl=net_pnl,
                end_bal=end_bal, mdd=mdd, pf=pf, avg_win=avg_win, avg_loss=avg_loss,
                df=df)

def print_stats(s: dict):
    sep = "-" * 60
    print(f"\n{'=' * 60}")
    print(f"  {s['label']}")
    print(sep)
    print(f"  Trades          : {s['total']}  ({s['wins']}W / {s['total'] - s['wins']}L)")
    print(f"  Winrate         : {s['wr']:.1f}%")
    print(f"  Netto P&L       : ${s['net_pnl']:+,.2f}")
    print(f"  Eindbalance     : ${s['end_bal']:,.2f}  ({(s['end_bal']/CFG.start_balance - 1)*100:+.2f}%)")
    print(f"  Max Drawdown    : {s['mdd']:.2f}%")
    print(f"  Profit Factor   : {s['pf']:.2f}")
    print(f"  Gem. winst      : ${s['avg_win']:+,.2f}")
    print(f"  Gem. verlies    : ${s['avg_loss']:+,.2f}")

    df = s["df"]
    print(f"\n  Per symbool:")
    for sym, grp in df.groupby("symbol"):
        w  = int((grp["pnl"] > 0).sum())
        wr = w / len(grp) * 100
        print(f"    {sym:<8}  {len(grp):>4} trades  WR {wr:.1f}%  P&L ${grp['pnl'].sum():+,.2f}")

def print_comparison(a: dict, b: dict):
    if not a or not b:
        return
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  VERGELIJKING: {a['label']}  vs  {b['label']}")
    print(sep)
    print(f"  {'Metric':<22} {'Origineel':>15} {'+ Momentum':>15} {'Delta':>10}")
    print("-" * 64)

    def row(label, key, fmt="{:+.2f}", inv=False):
        va = a[key]; vb = b[key]
        d  = vb - va
        sign = "+" if d > 0 else ""
        better = (">" if not inv else "<")
        arrow = " <" if d < 0 else " >"
        print(f"  {label:<22} {va:>15.2f} {vb:>15.2f}  {sign}{d:.2f}{arrow}")

    print(f"  {'Trades':<22} {a['total']:>15} {b['total']:>15}  {b['total']-a['total']:+}")
    print(f"  {'Winrate (%)':<22} {a['wr']:>15.1f} {b['wr']:>15.1f}  {b['wr']-a['wr']:+.1f}")
    print(f"  {'Netto P&L ($)':<22} {a['net_pnl']:>15,.2f} {b['net_pnl']:>15,.2f}  {b['net_pnl']-a['net_pnl']:+,.2f}")
    print(f"  {'Eindbalance ($)':<22} {a['end_bal']:>15,.2f} {b['end_bal']:>15,.2f}  {b['end_bal']-a['end_bal']:+,.2f}")
    print(f"  {'Max Drawdown (%)':<22} {a['mdd']:>15.2f} {b['mdd']:>15.2f}  {b['mdd']-a['mdd']:+.2f}")
    print(f"  {'Profit Factor':<22} {a['pf']:>15.2f} {b['pf']:>15.2f}  {b['pf']-a['pf']:+.2f}")
    print(f"  {'Gem. winst ($)':<22} {a['avg_win']:>15.2f} {b['avg_win']:>15.2f}  {b['avg_win']-a['avg_win']:+.2f}")
    print(sep)

    # Conclusie
    pnl_beter   = b["net_pnl"] > a["net_pnl"]
    mdd_beter   = b["mdd"]     < a["mdd"]
    wr_beter    = b["wr"]      > a["wr"]
    pf_beter    = b["pf"]      > a["pf"]
    score = sum([pnl_beter, mdd_beter, wr_beter, pf_beter])
    print(f"\n  Momentum filter wint op {score}/4 metrics:")
    print(f"    P&L beter?        {'JA' if pnl_beter  else 'NEE'}")
    print(f"    Drawdown lager?   {'JA' if mdd_beter  else 'NEE'}")
    print(f"    Winrate hoger?    {'JA' if wr_beter   else 'NEE'}")
    print(f"    Profit Factor?    {'JA' if pf_beter   else 'NEE'}")
    if score >= 3:
        print("\n  CONCLUSIE: Momentum filter VERBETERT de strategie.")
    elif score == 2:
        print("\n  CONCLUSIE: Gemengd resultaat — nader onderzoek nodig.")
    else:
        print("\n  CONCLUSIE: Momentum filter VERSLECHTERT de strategie.")
    print(sep)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    connect()
    print(f"\nBacktest: {CFG.bt_start.date()} tot {CFG.bt_end.date()}")
    print(f"Symbolen: {CFG.symbols}")
    print(f"Momentum window: {CFG.momentum_window} H1-bars\n")

    trades_a = []  # Origineel
    trades_b = []  # + Momentum filter

    for sym in CFG.symbols:
        mt5.symbol_select(sym, True)
        print(f"{sym}: data laden...", end="", flush=True)
        h1_raw  = get_bars(sym, mt5.TIMEFRAME_H1,  CFG.bt_start, CFG.bt_end)
        m15_raw = get_bars(sym, mt5.TIMEFRAME_M15, CFG.bt_start, CFG.bt_end)
        if h1_raw.empty or m15_raw.empty:
            print(" geen data, overgeslagen.")
            continue
        print(f" H1={len(h1_raw)} M15={len(m15_raw)}", end="", flush=True)

        h1  = prepare_h1(h1_raw)
        m15 = prepare_m15(m15_raw)

        t_a = simulate(sym, h1, m15, use_momentum=False)
        t_b = simulate(sym, h1, m15, use_momentum=True)
        print(f"  |  Origineel: {len(t_a)} trades  |  + Momentum: {len(t_b)} trades")
        trades_a.extend(t_a)
        trades_b.extend(t_b)

    mt5.shutdown()

    s_a = stats(trades_a, f"ORIGINEEL  (geen momentum filter)")
    s_b = stats(trades_b, f"+ MOMENTUM FILTER (window={CFG.momentum_window} H1-bars)")

    print_stats(s_a)
    print_stats(s_b)
    print_comparison(s_a, s_b)


if __name__ == "__main__":
    main()
