"""
Backtest: XAUUSD ORB STRAK — FTMO $160k
Gebaseerd op strategy_xauusd_orb_strak.pine

Logica:
  London : range bouwen 09:00-10:00 AMS, handelen 10:00-11:00 AMS
  NY     : range bouwen 15:30-16:30 AMS, handelen 16:30-17:30 AMS
  Entry  : eerste close boven/onder range + buffer (binnen trade-venster)
  SL     : range-tegenkant (min 0.5×ATR) of ATR×1.5
  TP     : R × 1.5
  BE     : SL naar entry bij +1R
  Posities blijven open na het venster — SL/TP op alle bars

Tijdzone : Europe/Amsterdam via zoneinfo (DST-bewust)
Timeframe: M5
"""

import sys
from datetime import datetime, timezone, date, timedelta
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5
import pandas as pd
import numpy as np

AMS = ZoneInfo("Europe/Amsterdam")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

@dataclass
class OrbConfig:
    symbol:          str   = "XAUUSD"
    # Sessies (Amsterdam-minuten)
    use_asia:     bool  = True
    asia_build:   tuple = (2*60,      5*60)     # 02:00–05:00 (Tokyo)
    asia_trade:   tuple = (5*60,      8*60)     # 05:00–08:00
    use_frankfurt: bool = True
    fra_build:    tuple = (6*60,      8*60)     # 06:00–08:00 (pre-Frankfurt)
    fra_trade:    tuple = (8*60,      9*60+30)  # 08:00–09:30 (Frankfurt open)
    lon_build:    tuple = (8*60,      10*60)    # 08:00–10:00
    lon_trade:    tuple = (10*60,     14*60)    # 10:00–14:00
    use_ny:       bool  = True
    ny_build:     tuple = (15*60+30,  16*60+30) # 15:30–16:30
    ny_trade:     tuple = (16*60+30,  18*60)    # 16:30–18:00
    use_lon_pm:   bool  = False
    lon_pm_build: tuple = (13*60,     15*60)
    lon_pm_trade: tuple = (15*60,     16*60+30)
    # Entry
    break_buf:  float = 0.5
    # SL / TP
    sl_mode:    str   = "range"
    atr_len:    int   = 14
    sl_mult:    float = 1.5
    tp_rr:      float = 2.0
    use_be:     bool  = True
    risk_pct:   float = 1.05   # 1.05% → max 3×1.05=3.15% dagverlies, binnen FTMO 4%
    # Filters
    trend_filter:  bool  = True
    adx_min:       float = 20.0
    adx_min_buy:   float = 25.0
    adx_min_ny:    float = 25.0
    min_range_atr: float = 0.2    # range moet ≥ 20% van ATR zijn
    buy_above_ema: bool  = True
    buy_ema_slope: bool  = True
    # Dag-limiet
    max_per_day:   int   = 3
    # FTMO guards — vaste bedragen gebaseerd op startbalans $160k
    daily_loss_limit: float = 8_000.0    # max $8k verlies per dag (5% van $160k)
    max_total_loss:   float = 16_000.0   # max $16k totaal verlies (10% van $160k)
    target_pct:       float = 10.0
    use_target:       bool  = True
    # Account
    start_bal: float = 160_000.0
    bt_start: datetime = field(default_factory=lambda: datetime(2026, 1, 1,  tzinfo=timezone.utc))
    bt_end:   datetime = field(default_factory=lambda: datetime(2026, 6, 25, tzinfo=timezone.utc))

CFG = OrbConfig()

# ---------------------------------------------------------------------------
# MT5
# ---------------------------------------------------------------------------

def connect():
    if not mt5.initialize():
        print(f"MT5 mislukt: {mt5.last_error()}")
        sys.exit(1)
    print(f"MT5 verbonden — {mt5.account_info().company}")

def get_bars(symbol, tf, start, end) -> pd.DataFrame:
    rates = mt5.copy_rates_range(symbol, tf, start, end)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    return df

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def ams_minutes(ts: pd.Timestamp) -> int:
    """Geef Amsterdam uur*60+minuut voor een UTC-timestamp."""
    return ts.astimezone(AMS).hour * 60 + ts.astimezone(AMS).minute

def ams_date(ts: pd.Timestamp):
    return ts.astimezone(AMS).date()

def atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def calc_lot(balance: float, sl_dist: float, info) -> float:
    risk_amt     = balance * (CFG.risk_pct / 100.0)
    ticks_in_sl  = sl_dist / info.trade_tick_size
    loss_per_lot = ticks_in_sl * info.trade_tick_value
    if loss_per_lot <= 0:
        return info.volume_min
    raw  = risk_amt / loss_per_lot
    step = info.volume_step
    lot  = round(round(raw / step) * step, 8)
    return max(info.volume_min, min(lot, info.volume_max))

# ---------------------------------------------------------------------------
# HOOFD-SIMULATIE  (volledig bar-voor-bar)
# ---------------------------------------------------------------------------

def adx_series(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c  = df["high"], df["low"], df["close"]
    pdm = (h - h.shift(1)).clip(lower=0)
    mdm = (l.shift(1) - l).clip(lower=0)
    pdm = pdm.where(pdm > mdm, 0.0)
    mdm = mdm.where(mdm > pdm.shift(0), 0.0)
    tr  = pd.concat([h-l,(h-c.shift(1)).abs(),(l-c.shift(1)).abs()],axis=1).max(axis=1)
    at  = tr.ewm(span=period, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=period, adjust=False).mean() / at
    mdi = 100 * mdm.ewm(span=period, adjust=False).mean() / at
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.ewm(span=period, adjust=False).mean()


def simulate(df: pd.DataFrame, info, dfH: pd.DataFrame) -> list[dict]:
    """
    Itereert alle M5-bars in volgorde.
    Entries alleen binnen trade-vensters; exits op ALLE bars daarna.
    H1 data wordt gebruikt voor trend- en ADX-filter.
    """
    df = df.copy()
    df["atr"]      = atr_series(df, CFG.atr_len)
    df["ams_min"]  = df.index.map(ams_minutes)
    df["ams_date"] = df.index.map(ams_date)

    # H1 indicatoren voor trend- en ADX-filter
    dfH = dfH.copy()
    dfH["ema50"]  = dfH["close"].ewm(span=50,  adjust=False).mean()
    dfH["ema200"] = dfH["close"].ewm(span=200, adjust=False).mean()
    dfH["adx"]    = adx_series(dfH, 14)

    balance    = CFG.start_bal
    trades: list[dict] = []

    # FTMO state
    day_start_bal = balance
    daily_hit     = False
    acct_hit      = False
    target_hit    = False

    pos      = None
    last_day = None

    # Range-state per sessie (reset elke dag)
    lon_h = lon_l = lon_atr = 0.0
    ny_h  = ny_l  = ny_atr  = 0.0
    asi_h = asi_l = asi_atr = 0.0
    fra_h = fra_l = fra_atr = 0.0
    lpm_h = lpm_l = lpm_atr = 0.0
    lon_range_set = ny_range_set = asi_range_set = fra_range_set = lpm_range_set = False

    day_count = 0   # aantal trades vandaag
    digits    = info.digits

    for i in range(CFG.atr_len + 5, len(df)):
        row     = df.iloc[i]
        ts      = df.index[i]
        day     = row["ams_date"]
        m       = int(row["ams_min"])
        hc      = row["high"];  lc = row["low"]
        cl      = row["close"]
        atr_val = row["atr"]

        # ── Dag-reset ────────────────────────────────────────────────────
        if day != last_day:
            last_day       = day
            lon_h = lon_l  = 0.0; lon_range_set = False
            ny_h  = ny_l   = 0.0; ny_range_set  = False
            asi_h = asi_l  = 0.0; asi_range_set = False
            fra_h = fra_l  = 0.0; fra_range_set = False
            lpm_h = lpm_l  = 0.0; lpm_range_set = False
            day_start_bal  = balance
            daily_hit      = False
            day_count      = 0

        if acct_hit or target_hit:
            break

        # ── H1 indicatoren ───────────────────────────────────────────────
        h1i = dfH.index.searchsorted(ts, side="right") - 1
        if h1i < 200:
            continue
        h1_e50   = dfH["ema50"].iat[h1i]
        h1_e200  = dfH["ema200"].iat[h1i]
        h1_adx   = dfH["adx"].iat[h1i]
        h1_close = dfH["close"].iat[h1i]
        h1_slope = h1_e50 - dfH["ema50"].iat[max(0, h1i - 5)]
        h1_trend = ("up" if h1_e50 > h1_e200 else ("down" if h1_e50 < h1_e200 else None)) if CFG.trend_filter else None

        # ── Beheer open positie ───────────────────────────────────────────
        if pos is not None:
            entry = pos["entry"]; sl = pos["sl"]; tp = pos["tp"]; one_r = pos["one_r"]

            if pos["dir"] == "BUY":
                if CFG.use_be and not pos["be_armed"] and hc >= entry + one_r:
                    pos["sl"] = round(entry, digits); pos["be_armed"] = True; sl = pos["sl"]
                if lc <= sl:
                    pnl    = 0.0 if pos["be_armed"] else -pos["risk"]
                    result = "BE" if pos["be_armed"] else "LOSS"
                    trades.append({**pos, "exit_price": sl, "pnl": pnl, "result": result})
                    balance += pnl; day_count += 1; pos = None
                    if (day_start_bal - balance) >= CFG.daily_loss_limit:
                        daily_hit = True
                    if (CFG.start_bal - balance) >= CFG.max_total_loss:
                        acct_hit = True
                    continue
                if hc >= tp:
                    pnl = pos["risk"] * CFG.tp_rr
                    trades.append({**pos, "exit_price": tp, "pnl": pnl, "result": "WIN"})
                    balance += pnl; day_count += 1; pos = None
                    continue
            else:  # SELL
                if CFG.use_be and not pos["be_armed"] and lc <= entry - one_r:
                    pos["sl"] = round(entry, digits); pos["be_armed"] = True; sl = pos["sl"]
                if hc >= sl:
                    pnl    = 0.0 if pos["be_armed"] else -pos["risk"]
                    result = "BE" if pos["be_armed"] else "LOSS"
                    trades.append({**pos, "exit_price": sl, "pnl": pnl, "result": result})
                    balance += pnl; day_count += 1; pos = None
                    if (day_start_bal - balance) >= CFG.daily_loss_limit:
                        daily_hit = True
                    if (CFG.start_bal - balance) >= CFG.max_total_loss:
                        acct_hit = True
                    continue
                if lc <= tp:
                    pnl = pos["risk"] * CFG.tp_rr
                    trades.append({**pos, "exit_price": tp, "pnl": pnl, "result": "WIN"})
                    balance += pnl; day_count += 1; pos = None
                    continue

        if daily_hit or pos is not None:
            continue

        # ── Dag-limiet ───────────────────────────────────────────────────
        if day_count >= CFG.max_per_day:
            continue

        adx_ok = (CFG.adx_min <= 0) or (h1_adx >= CFG.adx_min)

        # ── Range bouwen ─────────────────────────────────────────────────
        if CFG.use_asia and CFG.asia_build[0] <= m < CFG.asia_build[1]:
            if not asi_range_set:
                asi_h = hc; asi_l = lc; asi_range_set = True
            else:
                asi_h = max(asi_h, hc); asi_l = min(asi_l, lc)
            asi_atr = atr_val

        if CFG.use_frankfurt and CFG.fra_build[0] <= m < CFG.fra_build[1]:
            if not fra_range_set:
                fra_h = hc; fra_l = lc; fra_range_set = True
            else:
                fra_h = max(fra_h, hc); fra_l = min(fra_l, lc)
            fra_atr = atr_val

        if CFG.lon_build[0] <= m < CFG.lon_build[1]:
            if not lon_range_set:
                lon_h = hc; lon_l = lc; lon_range_set = True
            else:
                lon_h = max(lon_h, hc); lon_l = min(lon_l, lc)
            lon_atr = atr_val

        if CFG.use_lon_pm and CFG.lon_pm_build[0] <= m < CFG.lon_pm_build[1]:
            if not lpm_range_set:
                lpm_h = hc; lpm_l = lc; lpm_range_set = True
            else:
                lpm_h = max(lpm_h, hc); lpm_l = min(lpm_l, lc)
            lpm_atr = atr_val

        if CFG.use_ny and CFG.ny_build[0] <= m < CFG.ny_build[1]:
            if not ny_range_set:
                ny_h = hc; ny_l = lc; ny_range_set = True
            else:
                ny_h = max(ny_h, hc); ny_l = min(ny_l, lc)
            ny_atr = atr_val

        # ── Entry ────────────────────────────────────────────────────────
        in_asi = CFG.use_asia      and (CFG.asia_trade[0]   <= m < CFG.asia_trade[1])
        in_fra = CFG.use_frankfurt and (CFG.fra_trade[0]    <= m < CFG.fra_trade[1])
        in_lon = CFG.lon_trade[0] <= m < CFG.lon_trade[1]
        in_lpm = CFG.use_lon_pm   and (CFG.lon_pm_trade[0] <= m < CFG.lon_pm_trade[1])
        in_ny  = CFG.use_ny       and (CFG.ny_trade[0]     <= m < CFG.ny_trade[1])

        session, rh, rl, ratr = None, 0.0, 0.0, 0.0
        if in_asi and asi_range_set:
            session, rh, rl, ratr = "Asia",      asi_h, asi_l, asi_atr
        elif in_fra and fra_range_set:
            session, rh, rl, ratr = "Frankfurt", fra_h, fra_l, fra_atr
        elif in_lon and lon_range_set:
            session, rh, rl, ratr = "London",    lon_h, lon_l, lon_atr
        elif in_lpm and lpm_range_set:
            session, rh, rl, ratr = "London-PM", lpm_h, lpm_l, lpm_atr
        elif in_ny and ny_range_set:
            session, rh, rl, ratr = "NY",        ny_h,  ny_l,  ny_atr

        if session:
            # Per-sessie ADX drempel
            adx_thresh = CFG.adx_min_ny if session == "NY" else CFG.adx_min
            if (adx_thresh <= 0) or (h1_adx >= adx_thresh):
                pos = _try_entry(cl, rh, rl, ratr, balance, info, digits,
                                 session, day, h1_trend, h1_adx, h1_close, h1_slope)

    # Forceer sluiting van nog open positie aan einde van data
    if pos is not None:
        cl_last = df["close"].iloc[-1]
        if pos["dir"] == "BUY":
            pnl = (cl_last - pos["entry"]) / info.trade_tick_size * info.trade_tick_value * pos["lot"]
        else:
            pnl = (pos["entry"] - cl_last) / info.trade_tick_size * info.trade_tick_value * pos["lot"]
        trades.append({**pos, "exit_price": cl_last, "pnl": pnl, "result": "TEST_END"})

    return trades


# Guard-check (update globals via closure-achtige aanpak)
_acct_hit   = False
_target_hit = False

def _check_guards(balance: float, day_start_bal: float):
    pass   # guards worden inline in simulate() afgehandeld


def _try_entry(cl: float, rh: float, rl: float, atr_val: float,
               balance: float, info, digits: int,
               session: str, day,
               h1_trend: str | None, h1_adx: float,
               h1_close: float, h1_slope: float) -> dict | None:
    buf = CFG.break_buf

    # Range te klein → nep-breakout risico
    if CFG.min_range_atr > 0 and atr_val > 0 and (rh - rl) < atr_val * CFG.min_range_atr:
        return None

    if cl > rh + buf:
        if CFG.trend_filter and h1_trend != "up":
            return None
        if CFG.adx_min_buy > 0 and h1_adx < CFG.adx_min_buy:
            return None
        if CFG.buy_above_ema and h1_close < rh:
            return None
        if CFG.buy_ema_slope and h1_slope <= 0:
            return None
        entry   = cl
        sl_dist = max(entry - rl, atr_val * 0.5) if CFG.sl_mode == "range" else atr_val * CFG.sl_mult
        return dict(dir="BUY", entry=entry,
                    sl=round(entry - sl_dist, digits),
                    tp=round(entry + sl_dist * CFG.tp_rr, digits),
                    one_r=sl_dist, lot=calc_lot(balance, sl_dist, info),
                    risk=balance*(CFG.risk_pct/100.0),
                    be_armed=False, session=session, date=str(day))

    if cl < rl - buf:
        if CFG.trend_filter and h1_trend != "down":
            return None
        entry   = cl
        sl_dist = max(rh - entry, atr_val * 0.5) if CFG.sl_mode == "range" else atr_val * CFG.sl_mult
        return dict(dir="SELL", entry=entry,
                    sl=round(entry + sl_dist, digits),
                    tp=round(entry - sl_dist * CFG.tp_rr, digits),
                    one_r=sl_dist, lot=calc_lot(balance, sl_dist, info),
                    risk=balance*(CFG.risk_pct/100.0),
                    be_armed=False, session=session, date=str(day))

    return None

# ---------------------------------------------------------------------------
# RAPPORT
# ---------------------------------------------------------------------------

def week_label(dt_str: str) -> str:
    d   = date.fromisoformat(dt_str)
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"

def max_drawdown(pnl_list: list, start: float) -> float:
    peak = eq = start
    mdd  = 0.0
    for p in pnl_list:
        eq  += p; peak = max(peak, eq)
        mdd  = max(mdd, (peak - eq) / peak * 100)
    return mdd

def report(trades: list[dict]) -> None:
    if not trades:
        print("\nGeen trades gegenereerd.")
        return

    df = pd.DataFrame(trades)
    df["win"]  = df["pnl"] > 0
    df["week"] = df["date"].apply(week_label)

    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  ORB BACKTEST  XAUUSD  {CFG.bt_start.date()} – {CFG.bt_end.date()}")
    print(f"  Buffer {CFG.break_buf} pts   SL: {CFG.sl_mode}   TP: {CFG.tp_rr}R   "
          f"BE: {'aan' if CFG.use_be else 'uit'}   Risico: {CFG.risk_pct}%")
    print(sep)

    print(f"\n{'Week':<12} {'Trades':>7} {'WR':>8} {'W':>5} {'L':>5} "
          f"{'P&L ($)':>12} {'Cumulatief':>14}")
    print("-" * 72)
    running = CFG.start_bal
    for wk, grp in df.groupby("week"):
        n   = len(grp); w = int(grp["win"].sum()); l = n - w
        wr  = w / n * 100; pnl = grp["pnl"].sum(); running += pnl
        teken = "+" if pnl >= 0 else ""
        print(f"{wk:<12} {n:>7} {wr:>7.1f}%  {w:>5} {l:>5}  "
              f"{teken}{pnl:>10,.2f}    {running:>12,.2f}")

    total      = len(df)
    wins       = int(df["win"].sum())
    net_pnl    = df["pnl"].sum()
    end_bal    = CFG.start_bal + net_pnl
    wr_tot     = wins / total * 100 if total else 0
    mdd        = max_drawdown(df["pnl"].tolist(), CFG.start_bal)
    gross_win  = df.loc[df["pnl"] > 0, "pnl"].sum()
    gross_loss = df.loc[df["pnl"] < 0, "pnl"].sum()
    pf         = gross_win / abs(gross_loss) if gross_loss != 0 else float("inf")
    avg_win    = df.loc[df["win"],  "pnl"].mean() if wins else 0
    avg_loss   = df.loc[~df["win"], "pnl"].mean() if (total - wins) else 0
    avg_rr     = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    # Uitsplitsing resultaten
    result_counts = df["result"].value_counts().to_dict()

    print(sep)
    print(f"  Totaal trades    : {total}  ({wins}W / {total - wins}L)")
    print(f"  Winrate          : {wr_tot:.1f}%")
    print(f"  Netto P&L        : ${net_pnl:+,.2f}")
    print(f"  Eindbalance      : ${end_bal:,.2f}  ({(end_bal/CFG.start_bal - 1)*100:+.2f}%)")
    print(f"  Max Drawdown     : {mdd:.2f}%")
    print(f"  Profit Factor    : {pf:.2f}")
    print(f"  Gem. winst       : ${avg_win:+,.2f}  |  Gem. verlies: ${avg_loss:+,.2f}")
    print(f"  Reëel R:R        : {avg_rr:.2f}")
    print(f"\n  Sluitingen:")
    for r, cnt in sorted(result_counts.items()):
        print(f"    {r:<18}: {cnt}")

    print(f"\n  Per sessie:")
    for sess, grp in df.groupby("session"):
        n2  = len(grp); w2 = int((grp["pnl"] > 0).sum())
        wr2 = w2 / n2 * 100
        gw2 = grp.loc[grp["pnl"] > 0, "pnl"].sum()
        gl2 = grp.loc[grp["pnl"] < 0, "pnl"].sum()
        pf2 = gw2 / abs(gl2) if gl2 != 0 else float("inf")
        print(f"    {sess:<8}  {n2:>4} trades  WR {wr2:.1f}%  "
              f"P&L ${grp['pnl'].sum():+,.2f}  PF {pf2:.2f}")

    print(f"\n  Per richting:")
    for dirr, grp in df.groupby("dir"):
        n3  = len(grp); w3 = int((grp["pnl"] > 0).sum())
        wr3 = w3 / n3 * 100
        gw3 = grp.loc[grp["pnl"] > 0, "pnl"].sum()
        gl3 = grp.loc[grp["pnl"] < 0, "pnl"].sum()
        pf3 = gw3 / abs(gl3) if gl3 != 0 else float("inf")
        print(f"    {dirr:<6}  {n3:>4} trades  WR {wr3:.1f}%  "
              f"P&L ${grp['pnl'].sum():+,.2f}  PF {pf3:.2f}")

    print(f"\n  Laatste 15 trades:")
    print(f"  {'Datum':<12} {'Sess':<8} {'Dir':<5} {'Entry':>8} "
          f"{'SL':>8} {'TP':>8} {'Exit':>8} {'Result':<12} {'P&L ($)':>10}")
    print("  " + "-" * 84)
    for _, row in df.tail(15).iterrows():
        teken = "+" if row["pnl"] >= 0 else ""
        print(f"  {row['date']:<12} {row['session']:<8} {row['dir']:<5} "
              f"{row['entry']:>8.2f} {row['sl']:>8.2f} {row['tp']:>8.2f} "
              f"{row['exit_price']:>8.2f} {row['result']:<12} "
              f"{teken}{row['pnl']:>9,.2f}")
    print(sep)

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    connect()
    sym = CFG.symbol
    mt5.symbol_select(sym, True)

    info = mt5.symbol_info(sym)
    if info is None:
        print(f"Symbool {sym} niet gevonden")
        mt5.shutdown(); sys.exit(1)

    print(f"\nData ophalen ({sym}, M5): {CFG.bt_start.date()} – {CFG.bt_end.date()}")
    df = get_bars(sym, mt5.TIMEFRAME_M5, CFG.bt_start, CFG.bt_end)
    if df.empty:
        print("Geen data ontvangen."); mt5.shutdown(); sys.exit(1)

    print(f"  {len(df)} M5-bars geladen.")
    print(f"Data ophalen ({sym}, H1) voor trend/ADX filter...")
    dfH = get_bars(sym, mt5.TIMEFRAME_H1, CFG.bt_start, CFG.bt_end)
    if dfH.empty:
        print("Geen H1-data ontvangen."); mt5.shutdown(); sys.exit(1)
    print(f"  {len(dfH)} H1-bars geladen.")
    print("Simulatie starten...")

    trades = simulate(df, info, dfH)
    mt5.shutdown()

    print(f"  {len(trades)} trades gegenereerd.")
    report(trades)


if __name__ == "__main__":
    main()
