"""
Twee-fase FTMO backtest — $160,000 account 2026

FASE 1 — Challenge (agressief):
  Doel: 10% winst ($16,000) zo snel mogelijk, max 10% drawdown
  Parameters: hoog risico, meer signalen

FASE 2 — Funded (conservatief):
  Doel: stabiel $4,000/maand, absoluut veilig
  Parameters: laag risico, strenge filters
"""

import sys
from datetime import datetime, timezone
from dataclasses import dataclass, field

import MetaTrader5 as mt5
import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# CONFIGURATIES
# ---------------------------------------------------------------------------

@dataclass
class BtConfig:
    name:               str   = "ONBEKEND"
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
    two_bar_confirm:    bool  = True
    atr_period:         int   = 14
    atr_sl_mult:        float = 1.5
    rr_ratio:           float = 2.0
    body_pct_min:       float = 0.55
    risk_pct_per_sym:   dict  = field(default_factory=lambda: {
                                    "XAUUSD": 1.2, "EURUSD": 0.3,
                                    "GBPUSD": 0.3, "USDJPY": 0.3,
                                })
    risk_pct:           float = 0.5
    max_open_per_sym:   int   = 2
    daily_loss_limit:   float = 0.04
    weekly_loss_limit:  float = 0.025
    max_drawdown_limit: float = 0.09
    # Winst-doelstelling: stop als deze bereikt is
    profit_target_pct:  float = 0.0    # 0.0 = geen stop
    start_balance:      float = 160_000.0
    session_start:      int   = 7
    session_end:        int   = 20
    bt_start: datetime = field(default_factory=lambda: datetime(2026, 1, 1,  tzinfo=timezone.utc))
    bt_end:   datetime = field(default_factory=lambda: datetime(2026, 6, 25, tzinfo=timezone.utc))


# FASE 1: Challenge — agressief
CHALLENGE = BtConfig(
    name               = "FASE 1 — CHALLENGE",
    h1_adx_min         = 25.0,        # meer signalen (was 30)
    atr_sl_mult        = 1.3,         # iets kapper SL zodat R:R groter lijkt
    rr_ratio           = 2.0,
    max_open_per_sym   = 3,           # meer gelijktijdige trades (was 2)
    weekly_loss_limit  = 0.03,        # 3% wekelijks (was 2.5%)
    daily_loss_limit   = 0.04,
    max_drawdown_limit = 0.09,
    profit_target_pct  = 0.10,        # stop zodra 10% winst bereikt (challenge geslaagd)
    risk_pct_per_sym   = {
        "XAUUSD": 2.0,                # dubbel risico tov fase 2
        "EURUSD": 0.5,
        "GBPUSD": 0.5,
        "USDJPY": 0.5,
    },
    start_balance      = 160_000.0,
    bt_start           = datetime(2026, 1, 1,  tzinfo=timezone.utc),
    bt_end             = datetime(2026, 6, 25, tzinfo=timezone.utc),
)

# FASE 2: Funded — conservatief
FUNDED = BtConfig(
    name               = "FASE 2 — FUNDED",
    h1_adx_min         = 30.0,        # strenge filter
    atr_sl_mult        = 1.5,
    rr_ratio           = 2.0,
    max_open_per_sym   = 2,
    weekly_loss_limit  = 0.015,       # 1.5% wekelijks — extra voorzichtig
    daily_loss_limit   = 0.03,        # 3% dagelijks (strenger dan FTMO's 5% op funded)
    max_drawdown_limit = 0.08,        # 8% max (buffer onder FTMO's 10%)
    profit_target_pct  = 0.0,
    risk_pct_per_sym   = {
        "XAUUSD": 0.8,
        "EURUSD": 0.2,
        "GBPUSD": 0.2,
        "USDJPY": 0.2,
    },
    start_balance      = 160_000.0,
    bt_start           = datetime(2026, 1, 1,  tzinfo=timezone.utc),
    bt_end             = datetime(2026, 6, 25, tzinfo=timezone.utc),
)

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

def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def rsi_series(s, period):
    delta    = s.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs       = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))

def adx_series(df, period):
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

def atr_series_fn(df, period):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def prepare_h1(df, CFG):
    df = df.copy()
    df["ema_fast"] = ema(df["close"], CFG.h1_ema_fast)
    df["ema_slow"] = ema(df["close"], CFG.h1_ema_slow)
    df["ema_mom"]  = ema(df["close"], CFG.h1_ema_momentum)
    df["adx"]      = adx_series(df, CFG.h1_adx_period)
    return df

def prepare_m15(df, CFG):
    df = df.copy()
    df["ema50"] = ema(df["close"], CFG.m15_ema)
    df["rsi"]   = rsi_series(df["close"], CFG.m15_rsi_period)
    df["atr"]   = atr_series_fn(df, CFG.atr_period)
    return df

# ---------------------------------------------------------------------------
# SIMULATIE
# ---------------------------------------------------------------------------

def simulate(symbol, h1, m15, CFG) -> list[dict]:
    trades  = []
    balance = CFG.start_balance

    info = mt5.symbol_info(symbol)
    if info is None:
        return []

    digits     = info.digits
    open_trades = []

    week_start_balance = balance
    current_week       = -1
    weekly_guard_hit   = False
    day_start_balance  = balance
    current_day        = None
    daily_guard_hit    = False
    challenge_done     = False

    m15_arr = m15[["open", "high", "low", "close", "ema50", "rsi", "atr"]].values
    m15_idx = m15.index
    warmup  = max(CFG.m15_ema + 20, CFG.m15_rsi_period + CFG.ema_slope_bars + 5, CFG.atr_period + 5)

    for i in range(warmup, len(m15_arr) - 1):
        candle_t = m15_idx[i]
        row      = m15_arr[i]
        o, h_c, l_c, cl = row[0], row[1], row[2], row[3]
        e50, rsi_v, atr_v = row[4], row[5], row[6]
        e50_prev = m15_arr[i - CFG.ema_slope_bars][4]
        prev     = m15_arr[i - 1]
        p_o, p_h, p_l, p_cl, p_e50 = prev[0], prev[1], prev[2], prev[3], prev[4]

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

        # Winst-doelstelling bereikt? (challenge geslaagd)
        equity = balance
        if CFG.profit_target_pct > 0:
            if (equity - CFG.start_balance) / CFG.start_balance >= CFG.profit_target_pct:
                if not challenge_done:
                    challenge_done = True
                    # sluit alle open trades op marktprijs
                    last_c = cl
                    for ot in open_trades:
                        if ot["direction"] == "BUY":
                            pnl = (last_c - ot["entry_price"]) / info.trade_tick_size * info.trade_tick_value * ot["lot"]
                        else:
                            pnl = (ot["entry_price"] - last_c) / info.trade_tick_size * info.trade_tick_value * ot["lot"]
                        ot.update(exit_time=candle_t, exit_price=last_c,
                                  pnl=pnl, result="TARGET")
                        trades.append(ot)
                    open_trades = []
                break  # stop simulatie voor dit symbool

        # FTMO guards
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

        # H1 trend + ADX
        h1_idx = h1.index.searchsorted(candle_t, side="right") - 1
        if h1_idx < CFG.h1_ema_slow + 20:
            continue
        adx_val = h1["adx"].iloc[h1_idx]
        if adx_val < CFG.h1_adx_min:
            continue
        ef      = h1["ema_fast"].iloc[h1_idx]
        es      = h1["ema_slow"].iloc[h1_idx]
        em      = h1["ema_mom"].iloc[h1_idx]
        ef_prev = h1["ema_fast"].iloc[h1_idx - CFG.h1_slope_bars]

        if ef > es:
            if em <= ef or ef <= ef_prev:
                continue
            trend = "up"
        elif ef < es:
            if em >= ef or ef >= ef_prev:
                continue
            trend = "down"
        else:
            continue

        # M15 twee-candle signaal
        if not (p_l <= p_e50 <= p_h):
            continue
        crange = h_c - l_c
        if crange == 0:
            continue
        body = abs(cl - o)
        if body / crange < CFG.body_pct_min:
            continue

        direction = None
        if trend == "up":
            if cl > o and cl > e50 and cl > p_cl and e50 > e50_prev:
                if CFG.rsi_buy_lo <= rsi_v <= CFG.rsi_buy_hi:
                    direction = "BUY"
        else:
            if cl < o and cl < e50 and cl < p_cl and e50 < e50_prev:
                if CFG.rsi_sell_lo <= rsi_v <= CFG.rsi_sell_hi:
                    direction = "SELL"

        if direction is None:
            continue

        # Positiegrootte
        risk_pct_sym = CFG.risk_pct_per_sym.get(symbol, CFG.risk_pct)
        risk_amount  = balance * (risk_pct_sym / 100.0)
        ticks_in_sl  = sl_dist / info.trade_tick_size
        loss_per_lot = ticks_in_sl * info.trade_tick_value
        if loss_per_lot <= 0:
            continue
        raw_lot = risk_amount / loss_per_lot
        lot     = round(round(raw_lot / info.volume_step) * info.volume_step, 8)
        lot     = max(info.volume_min, min(lot, info.volume_max))

        if direction == "BUY":
            sl_p = round(cl - sl_dist, digits)
            tp_p = round(cl + tp_dist, digits)
        else:
            sl_p = round(cl + sl_dist, digits)
            tp_p = round(cl - tp_dist, digits)

        open_trades.append({
            "symbol": symbol, "direction": direction,
            "entry_time": candle_t, "entry_price": cl,
            "sl": sl_p, "tp": tp_p, "lot": lot, "risk": risk_amount,
            "exit_time": None, "exit_price": None, "pnl": None, "result": None,
        })

    # Sluit resterende open trades
    last_close = m15["close"].iloc[-1]
    for ot in open_trades:
        if ot["direction"] == "BUY":
            pnl = (last_close - ot["entry_price"]) / info.trade_tick_size * info.trade_tick_value * ot["lot"]
        else:
            pnl = (ot["entry_price"] - last_close) / info.trade_tick_size * info.trade_tick_value * ot["lot"]
        ot.update(exit_time=m15.index[-1], exit_price=last_close, pnl=pnl, result="OPEN_CLOSE")
        trades.append(ot)

    return trades

# ---------------------------------------------------------------------------
# RAPPORTAGE
# ---------------------------------------------------------------------------

def week_label(dt):
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"

def max_drawdown(pnl_list, start):
    peak = eq = start
    mdd  = 0.0
    for p in pnl_list:
        eq  += p
        peak = max(peak, eq)
        mdd  = max(mdd, (peak - eq) / peak * 100)
    return mdd

def report(CFG, all_trades):
    if not all_trades:
        print(f"\n[{CFG.name}] Geen trades.")
        return {}

    df = pd.DataFrame(all_trades)
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["exit_time"]  = pd.to_datetime(df["exit_time"],  utc=True)
    df["week"]       = df["entry_time"].apply(week_label)
    df["win"]        = df["pnl"] > 0

    sep = "=" * 76
    print(f"\n{sep}")
    print(f"  {CFG.name}")
    print(f"  Startbalance: ${CFG.start_balance:,.0f}  |  Periode: {CFG.bt_start.date()} tot {CFG.bt_end.date()}")
    print(f"  XAUUSD risico: {CFG.risk_pct_per_sym.get('XAUUSD',0)}%  |  ADX>={CFG.h1_adx_min}  |  "
          f"Max open: {CFG.max_open_per_sym}  |  Weekstop: {CFG.weekly_loss_limit*100:.1f}%")
    print(sep)
    print(f"{'Week':<12} {'Trades':>7} {'Winrate':>8} {'W':>5} {'L':>5} "
          f"{'P&L ($)':>12} {'Cumulatief ($)':>16}")
    print("-" * 76)

    running = CFG.start_balance
    for wk, grp in df.groupby("week"):
        n   = len(grp)
        w   = int(grp["win"].sum())
        pnl = grp["pnl"].sum()
        running += pnl
        teken = "+" if pnl >= 0 else ""
        print(f"{wk:<12} {n:>7} {w/n*100:>7.1f}%  {w:>5} {n-w:>5}  "
              f"{teken}{pnl:>10,.2f}    {running:>12,.2f}")

    total     = len(df)
    wins      = int(df["win"].sum())
    net_pnl   = df["pnl"].sum()
    end_bal   = CFG.start_balance + net_pnl
    wr_tot    = wins / total * 100 if total else 0
    df_sorted = df.sort_values("exit_time")
    mdd       = max_drawdown(df_sorted["pnl"].tolist(), CFG.start_balance)
    gross_win = df.loc[df["pnl"] > 0, "pnl"].sum()
    gross_loss= df.loc[df["pnl"] < 0, "pnl"].sum()
    pf        = gross_win / abs(gross_loss) if gross_loss != 0 else float("inf")
    avg_win   = df.loc[df["win"], "pnl"].mean() if wins else 0
    avg_loss  = df.loc[~df["win"], "pnl"].mean() if (total - wins) else 0

    # Wanneer werd 10% winst bereikt?
    running2 = CFG.start_balance
    target_week = None
    for wk, grp in df.groupby("week"):
        running2 += grp["pnl"].sum()
        if target_week is None and (running2 - CFG.start_balance) / CFG.start_balance >= 0.10:
            target_week = wk

    print(sep)
    print(f"  Totaal trades    : {total}")
    print(f"  Winrate          : {wr_tot:.1f}%  ({wins}W / {total-wins}L)")
    print(f"  Gem. winst/trade : ${avg_win:+,.2f}  |  Gem. verlies: ${avg_loss:+,.2f}")
    print(f"  Netto P&L        : ${net_pnl:+,.2f}  ({net_pnl/CFG.start_balance*100:.1f}%)")
    print(f"  Eindbalance      : ${end_bal:,.2f}")
    print(f"  Max drawdown     : {mdd:.2f}%")
    print(f"  Profit factor    : {pf:.2f}")
    if target_week:
        print(f"  10% doel bereikt : week {target_week}")
    else:
        print(f"  10% doel bereikt : NIET in deze periode")
    print(sep)

    print("Per symbool:")
    for sym, grp in df.groupby("symbol"):
        w2 = int(grp["win"].sum())
        print(f"  {sym:<8}  {len(grp):>4} trades  winrate {w2/len(grp)*100:.1f}%  "
              f"P&L ${grp['pnl'].sum():+,.2f}")

    maanden = (CFG.bt_end - CFG.bt_start).days / 30.44
    print(f"\n  Gemiddeld per maand: ${net_pnl/maanden:+,.0f}")
    print(f"  FTMO regels OK     : {'JA' if mdd < 10.0 else 'NEE — drawdown te hoog!'}")

    return {
        "name": CFG.name, "trades": total, "winrate": wr_tot,
        "net_pnl": net_pnl, "mdd": mdd, "pf": pf,
        "maand": net_pnl / maanden, "target_week": target_week,
    }

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    connect()

    # Data eenmalig laden
    print(f"\nData laden: {CHALLENGE.bt_start.date()} tot {CHALLENGE.bt_end.date()}")
    data_cache = {}
    for sym in CHALLENGE.symbols:
        mt5.symbol_select(sym, True)
        print(f"  {sym}...", end="", flush=True)
        h1_raw  = get_bars(sym, mt5.TIMEFRAME_H1,  CHALLENGE.bt_start, CHALLENGE.bt_end)
        m15_raw = get_bars(sym, mt5.TIMEFRAME_M15, CHALLENGE.bt_start, CHALLENGE.bt_end)
        print(f" {len(h1_raw)} H1 / {len(m15_raw)} M15 bars")
        if h1_raw.empty or m15_raw.empty:
            continue
        data_cache[sym] = (h1_raw, m15_raw)

    results = []
    for cfg in [CHALLENGE, FUNDED]:
        print(f"\n--- Simulatie: {cfg.name} ---")
        all_trades = []
        for sym, (h1_raw, m15_raw) in data_cache.items():
            h1  = prepare_h1(h1_raw, cfg)
            m15 = prepare_m15(m15_raw, cfg)
            trades = simulate(sym, h1, m15, cfg)
            print(f"  {sym}: {len(trades)} trades")
            all_trades.extend(trades)
        r = report(cfg, all_trades)
        if r:
            results.append(r)

    mt5.shutdown()

    # Eindvergelijking
    print("\n")
    print("=" * 76)
    print("  SAMENVATTING — CHALLENGE vs FUNDED")
    print("=" * 76)
    print(f"{'Fase':<24} {'Trades':>7} {'Winrate':>9} {'P&L':>12} "
          f"{'Maand':>10} {'Max DD':>8} {'10% doel':>12}")
    print("-" * 76)
    for r in results:
        target = r.get("target_week", "NIET bereikt")
        print(f"{r['name']:<24} {r['trades']:>7} {r['winrate']:>8.1f}%  "
              f"${r['net_pnl']:>10,.0f}  ${r['maand']:>8,.0f}  {r['mdd']:>7.2f}%  {str(target):>12}")
    print("=" * 76)
    print("\nTIP: Gebruik de CHALLENGE config om snel 10% te halen,")
    print("     schakel dan over naar FUNDED voor stabiele $4k/maand.")

if __name__ == "__main__":
    main()
