"""
Strategie-vergelijking — FTMO $160k 2026
Test 3 strategieën:
  1. EMA_PULLBACK  — onze huidige bot (EMA50 pullback + twee-candle bevestiging)
  2. BB_REVERSION  — Bollinger Bands mean reversion (van trentstauff/FXBot idee)
  3. SMA_CROSS     — SMA9/21 crossover op M15

Alle 3 gebruiken dezelfde FTMO-guards, ATR-SL, positiegrootte en rapportage.
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
    # H1 trend
    h1_ema_fast:        int   = 50
    h1_ema_slow:        int   = 200
    h1_adx_period:      int   = 14
    h1_adx_min:         float = 30.0
    h1_ema_momentum:    int   = 20
    h1_slope_bars:      int   = 3
    # M15 — EMA Pullback
    m15_ema:            int   = 50
    ema_slope_bars:     int   = 2
    two_bar_confirm:    bool  = True
    body_pct_min:       float = 0.55
    m15_rsi_period:     int   = 14
    rsi_buy_lo:         float = 45.0
    rsi_buy_hi:         float = 75.0
    rsi_sell_lo:        float = 25.0
    rsi_sell_hi:        float = 55.0
    # M15 — Bollinger Bands
    bb_period:          int   = 20
    bb_dev:             float = 2.0
    # M15 — SMA Cross
    sma_fast:           int   = 9
    sma_slow:           int   = 21
    # ATR SL
    atr_period:         int   = 14
    atr_sl_mult:        float = 1.5
    rr_ratio:           float = 2.0
    # Risico
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
    session_start:      int   = 7
    session_end:        int   = 20
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

def sma(s: pd.Series, p: int) -> pd.Series:
    return s.rolling(p).mean()

def rsi_series(s: pd.Series, period: int) -> pd.Series:
    delta    = s.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs       = avg_gain / (avg_loss + 1e-9)
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
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
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
    return df

def prepare_m15(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # EMA Pullback indicatoren
    df["ema50"]   = ema(df["close"], CFG.m15_ema)
    df["rsi"]     = rsi_series(df["close"], CFG.m15_rsi_period)
    df["atr"]     = atr_series(df, CFG.atr_period)
    # Bollinger Bands
    df["bb_mid"]  = sma(df["close"], CFG.bb_period)
    bb_std        = df["close"].rolling(CFG.bb_period).std()
    df["bb_up"]   = df["bb_mid"] + CFG.bb_dev * bb_std
    df["bb_lo"]   = df["bb_mid"] - CFG.bb_dev * bb_std
    # SMA Crossover
    df["sma_f"]   = sma(df["close"], CFG.sma_fast)
    df["sma_s"]   = sma(df["close"], CFG.sma_slow)
    return df

# ---------------------------------------------------------------------------
# H1 TREND CHECKER (gedeeld door alle strategieën)
# ---------------------------------------------------------------------------

def get_h1_trend(h1: pd.DataFrame, h1_idx: int):
    """Geeft 'up', 'down' of None terug op basis van H1 EMA/ADX filters."""
    if h1_idx < CFG.h1_ema_slow + 20:
        return None
    adx_val = h1["adx"].iloc[h1_idx]
    if adx_val < CFG.h1_adx_min:
        return None
    ef      = h1["ema_fast"].iloc[h1_idx]
    es      = h1["ema_slow"].iloc[h1_idx]
    em      = h1["ema_mom"].iloc[h1_idx]
    ef_prev = h1["ema_fast"].iloc[h1_idx - CFG.h1_slope_bars]
    if ef > es:
        if em <= ef or ef <= ef_prev:
            return None
        return "up"
    elif ef < es:
        if em >= ef or ef >= ef_prev:
            return None
        return "down"
    return None

# ---------------------------------------------------------------------------
# POSITIEGROOTTE BEREKENING
# ---------------------------------------------------------------------------

def calc_lot(symbol, sl_dist, balance, info):
    risk_pct_sym = CFG.risk_pct_per_sym.get(symbol, CFG.risk_pct)
    risk_amount  = balance * (risk_pct_sym / 100.0)
    ticks_in_sl  = sl_dist / info.trade_tick_size
    loss_per_lot = ticks_in_sl * info.trade_tick_value
    if loss_per_lot <= 0:
        return None, None
    raw_lot = risk_amount / loss_per_lot
    lot     = round(round(raw_lot / info.volume_step) * info.volume_step, 8)
    lot     = max(info.volume_min, min(lot, info.volume_max))
    return lot, risk_amount

# ---------------------------------------------------------------------------
# SIMULATIE — GENERIEK
# ---------------------------------------------------------------------------

def simulate(symbol: str, h1: pd.DataFrame, m15: pd.DataFrame, strategy: str) -> list[dict]:
    trades  = []
    balance = CFG.start_balance

    info = mt5.symbol_info(symbol)
    if info is None:
        return []

    digits     = info.digits
    open_trades: list[dict] = []

    week_start_balance = balance
    current_week       = -1
    weekly_guard_hit   = False
    day_start_balance  = balance
    current_day        = None
    daily_guard_hit    = False

    cols_needed = ["open", "high", "low", "close", "ema50", "rsi", "atr",
                   "bb_up", "bb_lo", "bb_mid", "sma_f", "sma_s"]
    m15_arr = m15[cols_needed].values
    m15_idx = m15.index
    warmup  = max(CFG.m15_ema + 20, CFG.bb_period + 5, CFG.sma_slow + 5, CFG.atr_period + 5)

    for i in range(warmup, len(m15_arr) - 1):
        candle_t = m15_idx[i]
        row      = m15_arr[i]
        o, h_c, l_c, cl = row[0], row[1], row[2], row[3]
        e50, rsi_v, atr_v = row[4], row[5], row[6]
        bb_up, bb_lo  = row[7], row[8]
        sma_f, sma_s  = row[10], row[11]

        prev_row = m15_arr[i - 1]
        p_cl     = prev_row[3]
        p_e50    = prev_row[4]
        p_bb_up  = prev_row[7]
        p_bb_lo  = prev_row[8]
        p_sma_f  = prev_row[10]
        p_sma_s  = prev_row[11]

        e50_prev = m15_arr[i - CFG.ema_slope_bars][4]

        sl_dist = atr_v * CFG.atr_sl_mult
        tp_dist = sl_dist * CFG.rr_ratio

        # Week/dag reset
        iso_week = candle_t.isocalendar()[1]
        if iso_week != current_week:
            current_week       = iso_week
            week_start_balance = balance
            weekly_guard_hit   = False
        candle_day = candle_t.date()
        if candle_day != current_day:
            current_day       = candle_day
            day_start_balance = balance
            daily_guard_hit   = False

        # Check open trades SL/TP
        still_open = []
        for ot in open_trades:
            closed = False
            if ot["direction"] == "BUY":
                if l_c <= ot["sl"]:
                    ot.update(exit_time=candle_t, exit_price=ot["sl"],
                              pnl=-ot["risk"], result="LOSS")
                    balance += ot["pnl"]; trades.append(ot); closed = True
                elif h_c >= ot["tp"]:
                    ot.update(exit_time=candle_t, exit_price=ot["tp"],
                              pnl=ot["risk"] * CFG.rr_ratio, result="WIN")
                    balance += ot["pnl"]; trades.append(ot); closed = True
            else:
                if h_c >= ot["sl"]:
                    ot.update(exit_time=candle_t, exit_price=ot["sl"],
                              pnl=-ot["risk"], result="LOSS")
                    balance += ot["pnl"]; trades.append(ot); closed = True
                elif l_c <= ot["tp"]:
                    ot.update(exit_time=candle_t, exit_price=ot["tp"],
                              pnl=ot["risk"] * CFG.rr_ratio, result="WIN")
                    balance += ot["pnl"]; trades.append(ot); closed = True
            if not closed:
                still_open.append(ot)
        open_trades = still_open

        # FTMO guards
        equity = balance
        if not daily_guard_hit and day_start_balance > 0:
            if (day_start_balance - equity) / day_start_balance >= CFG.daily_loss_limit:
                daily_guard_hit = True
        if not weekly_guard_hit and week_start_balance > 0:
            if (week_start_balance - equity) / week_start_balance >= CFG.weekly_loss_limit:
                weekly_guard_hit = True
        max_dd_hit = (CFG.start_balance - equity) / CFG.start_balance >= CFG.max_drawdown_limit
        if daily_guard_hit or weekly_guard_hit or max_dd_hit:
            continue

        if len(open_trades) >= CFG.max_open_per_sym:
            continue

        # Sessiefilter
        s_start, s_end = CFG.session_per_sym.get(symbol, (CFG.session_start, CFG.session_end))
        if not (s_start <= candle_t.hour < s_end):
            continue

        # H1 trend
        h1_idx = h1.index.searchsorted(candle_t, side="right") - 1
        trend  = get_h1_trend(h1, h1_idx)
        if trend is None:
            continue

        direction = None

        # ── Signaallogica per strategie ──────────────────────────────────

        if strategy == "EMA_PULLBACK":
            touch_o, touch_h, touch_l, touch_cl, touch_e50 = (
                prev_row[0], prev_row[1], prev_row[2], p_cl, p_e50)
            if not (touch_l <= touch_e50 <= touch_h):
                continue
            crange = h_c - l_c
            if crange == 0:
                continue
            body = abs(cl - o)
            if body / crange < CFG.body_pct_min:
                continue
            if trend == "up":
                if cl > o and cl > e50 and cl > touch_cl and e50 > e50_prev:
                    if CFG.rsi_buy_lo <= rsi_v <= CFG.rsi_buy_hi:
                        direction = "BUY"
            else:
                if cl < o and cl < e50 and cl < touch_cl and e50 < e50_prev:
                    if CFG.rsi_sell_lo <= rsi_v <= CFG.rsi_sell_hi:
                        direction = "SELL"

        elif strategy == "BB_REVERSION":
            # Koop wanneer prijs de onderste BB raakt en terugkeert (in uptrend)
            # Verkoop wanneer prijs de bovenste BB raakt en terugkeert (in downtrend)
            # Bevestiging: vorige candle doorboorde de band, huidige sluit erbinnen
            if trend == "up":
                if p_cl < p_bb_lo and cl > bb_lo:
                    if CFG.rsi_buy_lo <= rsi_v <= CFG.rsi_buy_hi:
                        direction = "BUY"
            else:
                if p_cl > p_bb_up and cl < bb_up:
                    if CFG.rsi_sell_lo <= rsi_v <= CFG.rsi_sell_hi:
                        direction = "SELL"

        elif strategy == "SMA_CROSS":
            # Golden cross M15: SMA9 kruist boven SMA21 (in uptrend)
            # Death cross M15: SMA9 kruist onder SMA21 (in downtrend)
            if trend == "up":
                if p_sma_f <= p_sma_s and sma_f > sma_s:
                    if CFG.rsi_buy_lo <= rsi_v <= CFG.rsi_buy_hi:
                        direction = "BUY"
            else:
                if p_sma_f >= p_sma_s and sma_f < sma_s:
                    if CFG.rsi_sell_lo <= rsi_v <= CFG.rsi_sell_hi:
                        direction = "SELL"

        if direction is None:
            continue

        # Positiegrootte
        lot, risk_amount = calc_lot(symbol, sl_dist, balance, info)
        if lot is None:
            continue

        if direction == "BUY":
            sl_p = round(cl - sl_dist, digits)
            tp_p = round(cl + tp_dist, digits)
        else:
            sl_p = round(cl + sl_dist, digits)
            tp_p = round(cl - tp_dist, digits)

        open_trades.append({
            "symbol":      symbol,
            "direction":   direction,
            "entry_time":  candle_t,
            "entry_price": cl,
            "sl":          sl_p,
            "tp":          tp_p,
            "lot":         lot,
            "risk":        risk_amount,
            "exit_time":   None,
            "exit_price":  None,
            "pnl":         None,
            "result":      None,
        })

    # Sluit resterende trades op laatste candle
    last_close = m15["close"].iloc[-1]
    for ot in open_trades:
        tick_size  = info.trade_tick_size
        tick_value = info.trade_tick_value
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

def week_label(dt) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"

def max_drawdown(pnl_list: list, start: float) -> float:
    peak = eq = start
    mdd  = 0.0
    for p in pnl_list:
        eq  += p
        peak = max(peak, eq)
        mdd  = max(mdd, (peak - eq) / peak * 100)
    return mdd

def report_strategy(name: str, all_trades: list[dict]) -> dict:
    if not all_trades:
        print(f"\n[{name}] Geen trades.")
        return {}

    df = pd.DataFrame(all_trades)
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["week"]       = df["entry_time"].apply(week_label)
    df["win"]        = df["pnl"] > 0

    sep = "=" * 76
    print(f"\n{sep}")
    print(f"  STRATEGIE: {name}   |   {CFG.bt_start.date()} tot {CFG.bt_end.date()}   |   $160,000")
    print(sep)
    print(f"{'Week':<12} {'Trades':>7} {'Winrate':>8} {'W':>5} {'L':>5} "
          f"{'P&L ($)':>12} {'Cumulatief ($)':>16}")
    print("-" * 76)

    running = CFG.start_balance
    for wk, grp in df.groupby("week"):
        n   = len(grp)
        w   = int(grp["win"].sum())
        l   = n - w
        wr  = w / n * 100
        pnl = grp["pnl"].sum()
        running += pnl
        teken = "+" if pnl >= 0 else ""
        print(f"{wk:<12} {n:>7} {wr:>7.1f}%  {w:>5} {l:>5}  "
              f"{teken}{pnl:>10,.2f}    {running:>12,.2f}")

    total      = len(df)
    wins       = int(df["win"].sum())
    net_pnl    = df["pnl"].sum()
    end_bal    = CFG.start_balance + net_pnl
    wr_tot     = wins / total * 100 if total else 0
    df_sorted  = df.sort_values("exit_time")
    mdd        = max_drawdown(df_sorted["pnl"].tolist(), CFG.start_balance)
    gross_win  = df.loc[df["pnl"] > 0, "pnl"].sum()
    gross_loss = df.loc[df["pnl"] < 0, "pnl"].sum()
    pf         = gross_win / abs(gross_loss) if gross_loss != 0 else float("inf")
    avg_win    = df.loc[df["win"], "pnl"].mean() if wins else 0
    avg_loss   = df.loc[~df["win"], "pnl"].mean() if (total - wins) else 0

    print(sep)
    print(f"  Totaal trades    : {total}")
    print(f"  Winrate          : {wr_tot:.1f}%  ({wins}W / {total - wins}L)")
    print(f"  Gem. winst/trade : ${avg_win:+,.2f}  |  Gem. verlies: ${avg_loss:+,.2f}")
    print(f"  Netto P&L        : ${net_pnl:+,.2f}")
    print(f"  Eindbalance      : ${end_bal:,.2f}")
    print(f"  Max drawdown     : {mdd:.2f}%")
    print(f"  Profit factor    : {pf:.2f}")
    print(sep)

    print("Per symbool:")
    for sym, grp in df.groupby("symbol"):
        w2  = int(grp["win"].sum())
        wr2 = w2 / len(grp) * 100
        print(f"  {sym:<8}  {len(grp):>4} trades  winrate {wr2:.1f}%  "
              f"P&L ${grp['pnl'].sum():+,.2f}")

    return {
        "name": name,
        "trades": total,
        "winrate": wr_tot,
        "net_pnl": net_pnl,
        "end_bal": end_bal,
        "mdd": mdd,
        "pf": pf,
    }

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    connect()
    print(f"\nData ophalen: {CFG.bt_start.date()} tot {CFG.bt_end.date()}")

    # Data één keer laden per symbool
    data_cache = {}
    for sym in CFG.symbols:
        mt5.symbol_select(sym, True)
        print(f"  {sym}: H1...", end="", flush=True)
        h1_raw = get_bars(sym, mt5.TIMEFRAME_H1, CFG.bt_start, CFG.bt_end)
        print(f"{len(h1_raw)} bars  M15...", end="", flush=True)
        m15_raw = get_bars(sym, mt5.TIMEFRAME_M15, CFG.bt_start, CFG.bt_end)
        print(f"{len(m15_raw)} bars")
        if h1_raw.empty or m15_raw.empty:
            print(f"  {sym}: geen data, overgeslagen.")
            continue
        data_cache[sym] = (prepare_h1(h1_raw), prepare_m15(m15_raw))

    strategies = ["EMA_PULLBACK", "BB_REVERSION", "SMA_CROSS"]
    summary = []

    for strat in strategies:
        all_trades = []
        for sym, (h1, m15) in data_cache.items():
            trades = simulate(sym, h1, m15, strat)
            all_trades.extend(trades)
        result = report_strategy(strat, all_trades)
        if result:
            summary.append(result)

    mt5.shutdown()

    # Vergelijkingstabel
    print("\n")
    print("=" * 76)
    print("  VERGELIJKING ALLE STRATEGIEEN — $160,000 — 2026-01-01 tot 2026-06-25")
    print("=" * 76)
    print(f"{'Strategie':<16} {'Trades':>7} {'Winrate':>9} {'Netto P&L':>12} "
          f"{'Maand gem.':>11} {'Max DD':>8} {'PF':>6} {'FTMO?':>7}")
    print("-" * 76)
    for r in summary:
        maand = r["net_pnl"] / 6.0
        ftmo_ok = "JA" if r["mdd"] < 10.0 else "NEE"
        print(f"{r['name']:<16} {r['trades']:>7} {r['winrate']:>8.1f}%  "
              f"${r['net_pnl']:>10,.0f}  ${maand:>9,.0f}  {r['mdd']:>7.2f}%  "
              f"{r['pf']:>5.2f}  {ftmo_ok:>7}")
    print("=" * 76)


if __name__ == "__main__":
    main()
