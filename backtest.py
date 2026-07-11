"""
Backtest FTMO Bot — identieke strategie als ftmo_bot.py
  H1 : EMA50/EMA200 trend  +  EMA20 momentum  +  ADX filter
  M15: EMA50 pullback (touch + confirm)  +  RSI  +  candle body
  SL : ATR(14) × 1.5
  TP : SL × 2.5  (R:R 2.5)
  Breakeven  : na 1R winst → SL naar entry
  Trailing   : na 2R winst → trail 1.5R achter koers
"""

import warnings
warnings.filterwarnings("ignore")

import yfinance as yf
import pandas as pd
import numpy as np
from collections import defaultdict

# ==============================================================================
#  CONFIG  (zelfde als AGRESSIEF modus in ftmo_bot.py)
# ==============================================================================

START_BAL    = 160_194.37

RISK_PCT = {
    "XAUUSD": 2.0,
    "EURUSD": 1.5,
    "GBPUSD": 1.5,
    "USDJPY": 0.8,
    "BTCUSD": 1.0,
}

ATR_SL_MULT  = 1.5
RR_RATIO     = 2.5
BREAKEVEN_R  = 1.0
TRAIL_R      = 2.0
TRAIL_DIST_R = 1.5

H1_EMA_FAST  = 50
H1_EMA_SLOW  = 200
H1_EMA_MOM   = 20
H1_ADX_P     = 14
H1_SLOPE_B   = 3
ADX_SLOPE_B  = 2
ADX_MIN_GLB  = 28.0
ADX_MIN_SYM  = {"XAUUSD": 32.0, "BTCUSD": 25.0}
ADX_BOOST    = 45.0
ADX_BOOST_X  = 2.0

M15_EMA_P    = 50
M15_RSI_P    = 14
ATR_P        = 14
EMA_SLOPE_B  = 2
BODY_PCT_MIN = 0.55

RSI_BUY_LO,  RSI_BUY_HI  = 45.0, 75.0
RSI_SELL_LO, RSI_SELL_HI = 25.0, 55.0

MAX_OPEN_SYM       = 2
MAX_TRADES_PER_DAY = 10

FTMO_DAILY_LIMIT = 7_500.0
FTMO_TOTAL_LIMIT = 14_000.0

SESSION = {
    "XAUUSD": (7, 20),
    "EURUSD": (7, 20),
    "GBPUSD": (7, 20),
    "USDJPY": (0, 12),
    "BTCUSD": (0, 23),
}

TICKERS = {
    "XAUUSD": "GC=F",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "BTCUSD": "BTC-USD",
}

# ==============================================================================
#  INDICATOREN  (identiek aan ftmo_bot.py)
# ==============================================================================

def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def rsi_ind(s, p):
    d  = s.diff()
    g  = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    lo = (-d).clip(lower=0).ewm(span=p, adjust=False).mean()
    return 100 - 100 / (1 + g / (lo + 1e-9))

def adx_ind(df, p):
    h, l, c = df["High"], df["Low"], df["Close"]
    pdm = (h - h.shift(1)).clip(lower=0)
    mdm = (l.shift(1) - l).clip(lower=0)
    pdm = pdm.where(pdm > mdm, 0.0)
    mdm = mdm.where(mdm > pdm, 0.0)
    tr  = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    at  = tr.ewm(span=p, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=p, adjust=False).mean() / at
    mdi = 100 * mdm.ewm(span=p, adjust=False).mean() / at
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.ewm(span=p, adjust=False).mean()

def atr_ind(df, p):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

# ==============================================================================
#  DATA DOWNLOAD
# ==============================================================================

def _flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def download_all():
    data_h1, data_m15 = {}, {}
    for sym, ticker in TICKERS.items():
        print(f"  {sym:8s} ({ticker}) ...", end=" ", flush=True)
        try:
            h1  = _flatten(yf.download(ticker, period="400d", interval="1h",
                                        progress=False, auto_adjust=True))
            m15 = _flatten(yf.download(ticker, period="60d",  interval="15m",
                                        progress=False, auto_adjust=True))
            if h1.empty or m15.empty:
                print("geen data"); continue
            for df in [h1, m15]:
                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC")
                else:
                    df.index = df.index.tz_convert("UTC")
            data_h1[sym]  = h1
            data_m15[sym] = m15
            print(f"H1:{len(h1)}bars  M15:{len(m15)}bars  "
                  f"({m15.index[0].date()} → {m15.index[-1].date()})")
        except Exception as e:
            print(f"fout: {e}")
    return data_h1, data_m15

# ==============================================================================
#  H1 TREND
# ==============================================================================

def precompute_h1(df):
    out = df.copy()
    out["ef"]  = ema(out["Close"], H1_EMA_FAST)
    out["es"]  = ema(out["Close"], H1_EMA_SLOW)
    out["em"]  = ema(out["Close"], H1_EMA_MOM)
    out["adx"] = adx_ind(out, H1_ADX_P)
    return out

def h1_trend_at(sym, h1, t):
    cutoff = t - pd.Timedelta(hours=1)
    sub    = h1[h1.index <= cutoff]
    if len(sub) < H1_EMA_SLOW + H1_SLOPE_B + ADX_SLOPE_B + 5:
        return None

    adx_min = ADX_MIN_SYM.get(sym, ADX_MIN_GLB)
    ef      = float(sub["ef"].iloc[-1])
    es      = float(sub["es"].iloc[-1])
    em      = float(sub["em"].iloc[-1])
    ef_prev = float(sub["ef"].iloc[-1 - H1_SLOPE_B])
    adx_val = float(sub["adx"].iloc[-1])

    if adx_val < adx_min:
        return None

    if sym == "XAUUSD" and len(sub) >= ADX_SLOPE_B + 2:
        if adx_val <= float(sub["adx"].iloc[-1 - ADX_SLOPE_B]):
            return None

    if ef > es:
        if em <= ef or ef <= ef_prev:
            return None
        return ("up", adx_val)
    elif ef < es:
        if em >= ef or ef >= ef_prev:
            return None
        return ("down", adx_val)
    return None

# ==============================================================================
#  TRADE SIMULATIE  (bar-by-bar)
# ==============================================================================

def simulate_trade(m15, start_idx, direction, entry, sl_init, tp, sl_dist):
    sl_cur  = sl_init
    be_done = False

    for i in range(start_idx, len(m15)):
        hi = float(m15.iloc[i]["High"])
        lo = float(m15.iloc[i]["Low"])
        cl = float(m15.iloc[i]["Close"])

        if direction == "BUY":
            sl_hit = lo <= sl_cur
            tp_hit = hi >= tp
            if sl_hit and tp_hit:
                return (sl_cur - entry) / sl_dist, i, "SL"
            if sl_hit:
                return (sl_cur - entry) / sl_dist, i, "SL"
            if tp_hit:
                return RR_RATIO, i, "TP"
            if not be_done and (cl - entry) / sl_dist >= BREAKEVEN_R:
                new_sl = entry + sl_dist * 0.1
                if new_sl > sl_cur:
                    sl_cur  = new_sl
                    be_done = True
            if TRAIL_R > 0 and (cl - entry) / sl_dist >= TRAIL_R:
                trail_sl = cl - sl_dist * TRAIL_DIST_R
                if trail_sl > sl_cur:
                    sl_cur = trail_sl

        else:  # SELL
            sl_hit = hi >= sl_cur
            tp_hit = lo <= tp
            if sl_hit and tp_hit:
                return (entry - sl_cur) / sl_dist, i, "SL"
            if sl_hit:
                return (entry - sl_cur) / sl_dist, i, "SL"
            if tp_hit:
                return RR_RATIO, i, "TP"
            if not be_done and (entry - cl) / sl_dist >= BREAKEVEN_R:
                new_sl = entry - sl_dist * 0.1
                if new_sl < sl_cur:
                    sl_cur  = new_sl
                    be_done = True
            if TRAIL_R > 0 and (entry - cl) / sl_dist >= TRAIL_R:
                trail_sl = cl + sl_dist * TRAIL_DIST_R
                if trail_sl < sl_cur:
                    sl_cur = trail_sl

    # Einde data
    cl = float(m15.iloc[-1]["Close"])
    r  = (cl - entry) / sl_dist if direction == "BUY" else (entry - cl) / sl_dist
    return r, len(m15) - 1, "EOD"

# ==============================================================================
#  BACKTEST HOOFD LOOP
# ==============================================================================

def run_backtest():
    print("\n" + "=" * 65)
    print("  FTMO BOT — BACKTEST  (60 dagen M15 data)")
    print("=" * 65)
    print(f"  Startbalans : ${START_BAL:,.2f}")
    print(f"  R:R         : {RR_RATIO}  |  SL: ATR×{ATR_SL_MULT}")
    print(f"  Breakeven   : >{BREAKEVEN_R}R  |  Trailing: >{TRAIL_R}R @ {TRAIL_DIST_R}R achter")
    print("=" * 65)
    print("\nData ophalen...")
    data_h1, data_m15 = download_all()

    if not data_h1 or not data_m15:
        print("Geen data beschikbaar — afgebroken.")
        return [], {}, START_BAL

    h1_ind = {sym: precompute_h1(df) for sym, df in data_h1.items()}

    # Alle M15 bars gesorteerd op tijd
    all_bars = []
    for sym, df in data_m15.items():
        for t in df.index:
            all_bars.append((t, sym))
    all_bars.sort()

    balance      = START_BAL
    day_bal      = START_BAL
    cur_day      = None
    cur_week     = None
    trades_today = 0
    daily_hit    = False
    total_hit    = False

    trades   = []
    weekly   = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "be": 0,
                                     "pnl": 0.0, "pnl_r": 0.0,
                                     "start": 0.0, "end": 0.0})
    last_entry_bar = {}
    open_per_sym   = defaultdict(int)
    seen_signals   = set()

    for t, sym in all_bars:
        if sym not in h1_ind or sym not in data_m15:
            continue
        m15     = data_m15[sym]
        bar_idx = m15.index.get_loc(t)

        if bar_idx < max(M15_EMA_P, ATR_P, M15_RSI_P) + 25:
            continue

        # Dag/week reset
        t_date   = t.date()
        iso      = t.isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"

        if t_date != cur_day:
            cur_day      = t_date
            day_bal      = balance
            trades_today = 0
            daily_hit    = False

        if (iso[0], iso[1]) != cur_week:
            if cur_week:
                weekly[f"{cur_week[0]}-W{cur_week[1]:02d}"]["end"] = balance
            cur_week = (iso[0], iso[1])
            if weekly[week_key]["start"] == 0.0:
                weekly[week_key]["start"] = balance

        # Guards
        if total_hit or daily_hit:
            continue
        if (START_BAL - balance) >= FTMO_TOTAL_LIMIT:
            total_hit = True; continue
        if (day_bal - balance) >= FTMO_DAILY_LIMIT:
            daily_hit = True; continue
        if trades_today >= MAX_TRADES_PER_DAY:
            continue

        # Sessie + weekend
        s_lo, s_hi = SESSION.get(sym, (0, 24))
        if not (s_lo <= t.hour < s_hi):
            continue
        if t.weekday() >= 5:
            continue

        # Max open per symbool
        if open_per_sym[sym] >= MAX_OPEN_SYM:
            continue

        # Geen dubbel signaal
        if (sym, bar_idx) in seen_signals:
            continue
        if last_entry_bar.get(sym) == bar_idx:
            continue

        # H1 trend
        tr = h1_trend_at(sym, h1_ind[sym], t)
        if tr is None:
            continue
        trend, adx_val = tr

        # M15 indicatoren
        lo_s = max(0, bar_idx - M15_EMA_P - 30)
        sub  = m15.iloc[lo_s : bar_idx + 1]
        if len(sub) < M15_EMA_P + 10:
            continue

        ema50_s = ema(sub["Close"], M15_EMA_P)
        rsi_s   = rsi_ind(sub["Close"], M15_RSI_P)
        atr_s   = atr_ind(sub, ATR_P)

        touch   = sub.iloc[-3]
        confirm = sub.iloc[-2]
        e_touch   = float(ema50_s.iloc[-3])
        e_confirm = float(ema50_s.iloc[-2])
        e_prev    = float(ema50_s.iloc[-2 - EMA_SLOPE_B])
        rsi_v     = float(rsi_s.iloc[-2])
        atr_v     = float(atr_s.iloc[-2])

        if atr_v <= 0 or np.isnan(atr_v):
            continue

        # EMA50 touch op touch-bar
        if not (float(touch["Low"]) <= e_touch <= float(touch["High"])):
            continue

        # Candle body filter
        crange = float(confirm["High"]) - float(confirm["Low"])
        if crange == 0:
            continue
        if abs(float(confirm["Close"]) - float(confirm["Open"])) / crange < BODY_PCT_MIN:
            continue

        sl_dist = atr_v * ATR_SL_MULT
        tp_dist = sl_dist * RR_RATIO
        boost   = ADX_BOOST_X if adx_val >= ADX_BOOST else 1.0

        # Richting
        direction = None
        if trend == "up":
            if e_confirm <= e_prev:
                continue
            if not (float(confirm["Close"]) > float(confirm["Open"]) and
                    float(confirm["Close"]) > e_confirm and
                    float(confirm["Close"]) > float(touch["Close"])):
                continue
            if not (RSI_BUY_LO <= rsi_v <= RSI_BUY_HI):
                continue
            direction = "BUY"
        elif trend == "down":
            if e_confirm >= e_prev:
                continue
            if not (float(confirm["Close"]) < float(confirm["Open"]) and
                    float(confirm["Close"]) < e_confirm and
                    float(confirm["Close"]) < float(touch["Close"])):
                continue
            if not (RSI_SELL_LO <= rsi_v <= RSI_SELL_HI):
                continue
            direction = "SELL"

        if direction is None:
            continue

        # Entry op open van volgende bar
        next_idx = bar_idx + 1
        if next_idx >= len(m15):
            continue
        entry = float(m15.iloc[next_idx]["Open"])

        sl = (entry - sl_dist) if direction == "BUY" else (entry + sl_dist)
        tp = (entry + tp_dist) if direction == "BUY" else (entry - tp_dist)

        rp       = RISK_PCT.get(sym, 0.5)
        risk_usd = balance * (rp / 100.0) * boost

        # Simuleer trade
        open_per_sym[sym] += 1
        pnl_r, exit_idx, reason = simulate_trade(
            m15, next_idx + 1, direction, entry, sl, tp, sl_dist
        )
        open_per_sym[sym] -= 1

        pnl_usd = pnl_r * risk_usd

        # Clamp aan daginlimiet
        ruimte = FTMO_DAILY_LIMIT - (day_bal - balance)
        if pnl_usd < -ruimte:
            pnl_usd   = -ruimte
            reason    = "GUARD"
            daily_hit = True

        balance      += pnl_usd
        trades_today += 1

        exit_t    = m15.index[exit_idx] if exit_idx < len(m15) else t
        exit_iso  = exit_t.isocalendar()
        exit_week = f"{exit_iso[0]}-W{exit_iso[1]:02d}"

        trade = {
            "sym":    sym,
            "dir":    direction,
            "t_in":   t,
            "t_out":  exit_t,
            "pnl_r":  pnl_r,
            "pnl":    pnl_usd,
            "adx":    adx_val,
            "boost":  boost,
            "reason": reason,
            "bal":    balance,
            "week":   exit_week,
        }
        trades.append(trade)

        w = weekly[exit_week]
        w["n"]    += 1
        w["pnl"]  += pnl_usd
        w["pnl_r"] += pnl_r
        if   pnl_r >  0.1: w["wins"]   += 1
        elif pnl_r < -0.1: w["losses"] += 1
        else:               w["be"]     += 1
        if w["start"] == 0.0:
            w["start"] = balance - pnl_usd
        w["end"] = balance

        seen_signals.add((sym, bar_idx))
        last_entry_bar[sym] = bar_idx

    if cur_week:
        weekly[f"{cur_week[0]}-W{cur_week[1]:02d}"]["end"] = balance

    return trades, weekly, balance

# ==============================================================================
#  RESULTATEN
# ==============================================================================

def print_results(trades, weekly, final_balance):
    if not trades:
        print("\nGeen trades gevonden in de backtest periode.")
        return

    LINE = "-" * 80

    print("\n" + "=" * 80)
    print("  WEEKRESULTATEN")
    print("=" * 80)
    print(f"  {'Week':<12} {'N':>3}  {'W':>3} {'L':>3} {'BE':>2}  {'WR':>5}  "
          f"{'P&L $':>11}  {'P&L R':>6}  {'Balans $':>12}  {'Week%':>6}")
    print(LINE)

    for wk in sorted(weekly.keys()):
        w = weekly[wk]
        if w["n"] == 0:
            continue
        decided = w["wins"] + w["losses"]
        wr  = w["wins"] / decided * 100 if decided > 0 else 0.0
        end = w["end"] if w["end"] != 0.0 else (w["start"] + w["pnl"])
        pct = (end - w["start"]) / w["start"] * 100 if w["start"] > 0 else 0.0
        sp  = "+" if w["pnl"] >= 0 else ""
        pp  = "+" if pct >= 0 else ""
        print(f"  {wk:<12} {w['n']:>3}  {w['wins']:>3} {w['losses']:>3} {w['be']:>2}  "
              f"{wr:>4.0f}%  {sp}{w['pnl']:>10,.0f}  "
              f"{w['pnl_r']:>+5.1f}R  ${end:>11,.0f}  "
              f"{pp}{pct:>5.1f}%")

    print(LINE)

    total   = len(trades)
    wins    = sum(1 for t in trades if t["pnl_r"] >  0.1)
    losses  = sum(1 for t in trades if t["pnl_r"] < -0.1)
    be      = total - wins - losses
    wr      = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    tot_pnl = final_balance - START_BAL
    tot_pct = tot_pnl / START_BAL * 100
    avg_r   = sum(t["pnl_r"] for t in trades) / total

    sp = "+" if tot_pnl >= 0 else ""
    print(f"  {'TOTAAL':<12} {total:>3}  {wins:>3} {losses:>3} {be:>2}  "
          f"{wr:>4.0f}%  {sp}{tot_pnl:>10,.0f}  "
          f"{avg_r:>+5.2f}R  ${final_balance:>11,.0f}  "
          f"{sp}{tot_pct:>5.1f}%")

    # Max drawdown
    peak   = START_BAL
    cur_b  = START_BAL
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x["t_out"]):
        cur_b  = t["bal"]
        peak   = max(peak, cur_b)
        max_dd = max(max_dd, (peak - cur_b) / peak * 100)

    gross_w = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_l = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")

    print("\n" + "=" * 80)
    print("  STATISTIEKEN")
    print("=" * 80)
    w_r = sum(t["pnl_r"] for t in trades if t["pnl_r"] >  0.1) / max(wins,  1)
    l_r = sum(t["pnl_r"] for t in trades if t["pnl_r"] < -0.1) / max(losses, 1)
    print(f"  Profit factor    : {pf:.2f}")
    print(f"  Gem. R/trade     : {avg_r:+.3f}R")
    print(f"  Gem. win         : +{w_r:.2f}R   |   Gem. verlies: {l_r:.2f}R")
    print(f"  Max drawdown     : -{max_dd:.2f}%")

    # Per symbool
    print("\n" + "=" * 80)
    print("  PER SYMBOOL")
    print("=" * 80)
    sym_s = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "r": 0.0})
    for t in trades:
        s = sym_s[t["sym"]]
        s["n"]   += 1
        s["pnl"] += t["pnl"]
        s["r"]   += t["pnl_r"]
        if   t["pnl_r"] >  0.1: s["wins"]   += 1
        elif t["pnl_r"] < -0.1: s["losses"] += 1
    for sym, s in sorted(sym_s.items(), key=lambda x: -x[1]["pnl"]):
        d    = s["wins"] + s["losses"]
        wr_s = s["wins"] / d * 100 if d > 0 else 0
        sp_s = "+" if s["pnl"] >= 0 else ""
        print(f"  {sym:<8}  {s['n']:>3} trades  WR:{wr_s:>4.0f}%  "
              f"P&L: {sp_s}${s['pnl']:>9,.0f}   gem. R: {s['r']/s['n']:>+.2f}R")

    # Exit redenen
    print("\n  Exit redenen:")
    reason_s = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for t in trades:
        reason_s[t["reason"]]["n"]   += 1
        reason_s[t["reason"]]["pnl"] += t["pnl"]
    for reason, r in sorted(reason_s.items()):
        sp_r = "+" if r["pnl"] >= 0 else ""
        pct_n = r["n"] / total * 100
        print(f"    {reason:<6}  {r['n']:>3}x ({pct_n:>4.0f}%)   P&L: {sp_r}${r['pnl']:>9,.0f}")

    # FTMO progress
    target_10  = START_BAL * 0.10
    target_15  = START_BAL * 0.15
    reached    = max(0, tot_pnl)
    daily_vals = defaultdict(float)
    for t in trades:
        daily_vals[t["t_out"].date()] += t["pnl"]
    max_day_loss = abs(min(daily_vals.values())) if daily_vals else 0.0

    print("\n" + "=" * 80)
    print("  FTMO DOELEN")
    print("=" * 80)
    print(f"  Challenge   (+10%) : nodig ${target_10:>8,.0f}  |  gehaald ${reached:>8,.0f}"
          f"  ({min(reached/target_10*100, 100):>5.1f}%)")
    print(f"  Verificatie (+15%) : nodig ${target_15:>8,.0f}  |  gehaald ${reached:>8,.0f}"
          f"  ({min(reached/target_15*100, 100):>5.1f}%)")
    print(f"  Max dag verlies    : grens $7,500  |  grootste verlies: ${max_day_loss:>,.0f}")
    print("=" * 80)


if __name__ == "__main__":
    trades, weekly, final_bal = run_backtest()
    print_results(trades, weekly, final_bal)
