"""
FundingPips LBO Bot — GBPUSD London Breakout + NY EMA SELL
===========================================================
Strategie (gebaseerd op backtest: +24.4% in 4 mnd, MaxDD -4.0%, PF 2.01):

  OCHTEND — London Breakout (07:00–13:00 UTC)
    Asian range: 22:00–07:00 UTC  →  breakout in H1 trendrichting
    H1: EMA50 > EMA200 = uptrend (BUY), EMA50 < EMA200 = downtrend (SELL)
    H1: ADX > 18  (trending markt)
    TP = entry ± range × 2.5     SL = andere kant van range
    Max 1 LBO trade per dag

  MIDDAG — NY EMA SELL (14:00–17:00 UTC)
    Alleen SELL in H1 downtrend + ADX > 18
    M5: EMA8 kruist omlaag door EMA21
    Trigger: bearish candle (body ≥ 45%) + RSI 25–60
    SL = ATR × 1.5    TP = SL × 2.0    Break-even na 1×SL
    Max 2 NY trades per sessie

  COMPLIANCE — FundingPips Zero €160K (EUR):
    max €500 risk/trade (hard cap)  |  max 2 posities, 1 per symbool
    eigen dagstop -€1.600 (equity)  |  FP dag-breach €4.800 (backstop)
    trailing vloer 5% = €8.000 vanaf hoogste equity (persistent)
    open-risk cap €1.400 pre-trade  |  newsfilter ±10 min high-impact GBP/USD
    vrijdag flatten vóór weekend    |  winstdagen-teller (≥€400, doel 7/30d)
    alleen FX — goud (XAUUSD) uitgesloten
"""

from __future__ import annotations

import configparser
import json
import logging
import logging.handlers
import math
import os
import sys
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, date
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd
import requests

# ===========================================================================
#  CONFIG
# ===========================================================================

_ini = configparser.RawConfigParser()   # RawConfigParser: % in wachtwoorden werkt zonder escaping
_ini_path = os.path.join(os.path.dirname(__file__), "bot_config.ini")
if not _ini.read(_ini_path, encoding="utf-8-sig"):
    print(f"FOUT: bot_config.ini niet gevonden op {_ini_path}")
    sys.exit(1)

MT5_LOGIN    = int(_ini["MT5"]["login"])
MT5_PASSWORD = _ini["MT5"]["password"]
MT5_SERVER   = _ini["MT5"]["server"]
TG_TOKEN     = _ini["TELEGRAM"].get("token", "").strip()
TG_CHAT_ID   = _ini["TELEGRAM"].get("chat_id", "").strip()

# ===========================================================================
#  STRATEGIE PARAMETERS
# ===========================================================================

SYMBOL        = "GBPUSD"
PIP_SIZE      = 0.0001
COMMISSION    = 3.5       # round-trip per lot

# H1 trendfilter
H1_EMA_FAST   = 50
H1_EMA_SLOW   = 200
H1_ADX_MIN    = 18.0

# London Breakout
LBO_ASIAN_START = 22      # 22:00 UTC vorige dag
LBO_ASIAN_END   = 7       # 07:00 UTC vandaag
LBO_ENTRY_START = 7       # entry venster start
LBO_ENTRY_END   = 13      # entry venster einde (uitgebreid — beste backtest resultaat)
LBO_TP_MULT     = 2.5
LBO_RANGE_MIN   = 5       # pips
LBO_RANGE_MAX   = 60      # pips

# NY EMA SELL
NY_START        = 14      # 14:00 UTC
NY_END          = 17      # 17:00 UTC
NY_EMA_FAST     = 8
NY_EMA_SLOW     = 21
NY_ATR_SL_MULT  = 1.5
NY_RR_RATIO     = 2.0
NY_BE_R         = 1.0     # break-even na 1×SL winst
NY_BODY_MIN     = 0.45
NY_MAX_TRADES   = 2

# FundingPips Zero guards — account €160.000 (alle bedragen in accountvaluta EUR)
RISK_EUR_MAX          = 500.0     # harde cap risico per trade (incl. commissie)
MAX_TOTAL_POS         = 2         # max gelijktijdige posities (alle symbolen)
MAX_PER_SYMBOL        = 1         # max 1 positie per symbool
DAY_STOP_EUR          = 1_600.0   # eigen dagstop (1%) → flatten + stop voor de dag
FP_DAY_BREACH_EUR     = 4_800.0   # FundingPips 3% dag-breach (equity) — noodstop backstop
FP_TRAIL_EUR          = 8_000.0   # 5% trailing loss vanaf hoogste equity
TRAIL_MARGIN_EUR      = 1_200.0   # pre-trade marge boven trailing vloer
TRAIL_HALT_BUFFER_EUR = 300.0     # binnen deze afstand tot de vloer → halt + flatten
OPEN_RISK_CAP_EUR     = 1_400.0   # pre-trade cap som floating risk (breach = €1.600)
PROFIT_DAY_EUR        = 400.0     # winstdag telt vanaf ≥ €400 (0,25%)
PROFIT_DAYS_MIN       = 7         # min winstdagen per 30 dagen (payout-regel)
MONTHLY_CB_PCT        = 0.03      # eigen circuit breaker: stop bij 3% maandverlies

# News filter — geen trades ±10 min rond high-impact news op GBP/USD
NEWS_CCY            = ("GBP", "USD")
NEWS_BLOCK_BEFORE   = 12        # min vóór event geen nieuwe trades (regel 10 + marge)
NEWS_BLOCK_AFTER    = 11        # min ná event geen nieuwe trades (regel 10 + marge)
NEWS_FLATTEN_BEFORE = 14        # open posities dicht zoveel min vóór event
NEWS_URL            = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_REFRESH_SEC    = 4 * 3600  # kalender elke 4 uur verversen
NEWS_MAX_AGE_SEC    = 24 * 3600 # data ouder dan dit → fail-safe: geen nieuwe trades

# Weekend guard — geen posities het weekend in
FRIDAY_LAST_ENTRY_H = 16        # vrijdag geen nieuwe trades vanaf 16:00 UTC
FRIDAY_FLATTEN_H    = 20        # vrijdag alles dicht om 20:30 UTC (markt sluit 21/22 UTC)
FRIDAY_FLATTEN_M    = 30

# Bot gedrag
POLL_SEC         = 10
MAGIC            = 20260701
COMMENT          = "LBO_BOT"
DEVIATION        = 20
MAX_RECONNECTS   = 10
RECONNECT_WAIT   = 15
MT5_TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
HEARTBEAT_FILE   = os.path.join(os.path.dirname(__file__), "lbo_bot_heartbeat.txt")
START_BAL_FILE   = os.path.join(os.path.dirname(__file__), "lbo_start_balance.txt")
FP_STATE_FILE    = os.path.join(os.path.dirname(__file__), "lbo_fp_state.json")
LOCK_FILE        = os.path.join(os.path.dirname(__file__), "lbo_bot.lock")

# ===========================================================================
#  LOGGING
# ===========================================================================

_log_file = os.path.join(os.path.dirname(__file__), "lbo_bot.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            _log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        ),
    ],
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
log = logging.getLogger("lbo_bot")

# ===========================================================================
#  TELEGRAM
# ===========================================================================

_SEP = "─────────────────────"

def tg(msg: str, silent: bool = False):
    if not TG_TOKEN or not TG_CHAT_ID or TG_TOKEN.startswith("123456"):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg,
                  "parse_mode": "HTML", "disable_notification": silent},
            timeout=8,
        )
    except Exception as exc:
        log.warning("Telegram fout: %s", exc)

def tg_open(strat: str, direction: str, lot: float,
            entry: float, sl: float, tp: float, extra: str = ""):
    arrow = "🟢" if direction == "BUY" else "🔴"
    sl_pips = abs(entry - sl) / PIP_SIZE
    tp_pips = abs(tp - entry) / PIP_SIZE
    tg(
        f"{arrow} <b>{direction}  GBPUSD  [{strat}]</b>\n"
        f"{_SEP}\n"
        f"📍 Entry  <code>{entry:.5f}</code>\n"
        f"🛑 SL     <code>{sl:.5f}</code>   <i>({sl_pips:.1f} pips)</i>\n"
        f"🎯 TP     <code>{tp:.5f}</code>   <i>({tp_pips:.1f} pips)</i>\n"
        f"{_SEP}\n"
        f"📦 Lot    {lot:.2f}    Risico ≤ €{RISK_EUR_MAX:,.0f}\n"
        f"{extra}"
    )

def tg_close(ticket: int, direction: str, pnl: float,
             entry: float, close_price: float, balance: float, start_bal: float):
    total_pct = (balance - start_bal) / start_bal * 100 if start_bal > 0 else 0
    label = "WIN" if pnl >= 0 else "VERLIES"
    emoji = "✅" if pnl >= 0 else "❌"
    tg(
        f"{emoji} <b>{label}  GBPUSD  {direction}</b>\n"
        f"{_SEP}\n"
        f"💰 P&amp;L    <b>€{pnl:+,.0f}</b>\n"
        f"📉 Prijs  <code>{entry:.5f}</code> → <code>{close_price:.5f}</code>\n"
        f"{_SEP}\n"
        f"💼 Balans  €{balance:,.0f}   ({total_pct:+.1f}% totaal)"
    )

def tg_guard(gtype: str, loss_eur: float, equity: float):
    if "DAGSTOP" in gtype:
        hervat = "morgen"
    elif "MAAND" in gtype or "CIRCUIT" in gtype:
        hervat = "volgende maand"
    else:
        hervat = "NIET automatisch — handmatige controle vereist"
    tg(
        f"🚨 <b>{gtype} BEREIKT</b>\n"
        f"{_SEP}\n"
        f"💸 Verlies  <b>-€{loss_eur:,.0f}</b>\n"
        f"⚖️ Equity   €{equity:,.0f}\n"
        f"⏳ Trading hervat: <b>{hervat}</b>"
    )

def tg_status(msg: str):
    tg(f"🤖 <b>BOT STATUS</b>\n{_SEP}\n{msg}")

# ===========================================================================
#  STATE
# ===========================================================================

@dataclass
class BotState:
    start_balance:      float = 0.0
    day_start_balance:  float = 0.0
    month_start_balance: float = 0.0
    current_day:        date  = date.min
    current_month:      tuple = (-1, -1)

    # FundingPips guards
    daily_guard_hit:   bool  = False   # eigen dagstop -€1.600 geraakt
    fp_halt:           bool  = False   # trailing vloer / dag-breach → definitief stoppen
    monthly_cb_hit:    bool  = False   # circuit breaker voor deze maand
    day_start_anchor:  float = 0.0     # max(balance, equity) bij dagstart — dagverlies-anker
    equity_high:       float = 0.0     # hoogste equity ooit → trailing vloer (persistent)

    # News filter
    news_events:       list  = field(default_factory=list)  # UTC-tijden high-impact GBP/USD
    news_fetched_at:   float = 0.0
    news_warned:       bool  = False
    news_flat_done:    set   = field(default_factory=set)   # events waarvoor al geflattened

    # Payout tracking
    day_pnl_hist:      dict  = field(default_factory=dict)  # "YYYY-MM-DD" → dag-P&L (€)
    last_close_date:   str   = ""      # laatste gesloten trade (inactivity-regel 30d)
    friday_flat_done:  bool  = False

    # Trade tracking
    open_tickets:      dict  = field(default_factory=dict)  # ticket → info dict
    trades_today:      int   = 0
    daily_report_sent: bool  = False

    # LBO: max 1 per dag
    lbo_traded_today:  bool  = False
    # NY: max 2 per sessie
    ny_count_today:    int   = 0
    ny_session_active: bool  = False   # True = 14:00-17:00 UTC actief

    # Telegram
    tg_last_update_id: int   = 0
    bot_paused:        bool  = False

    # EMA cross tracking (voorkomt meerdere entries op zelfde cross)
    last_ny_cross_bar: Optional[datetime] = None

STATE = BotState()

# ===========================================================================
#  LOCK FILE (voorkomt twee instanties)
# ===========================================================================

_LOCK_HANDLE = None

def acquire_lock() -> bool:
    global _LOCK_HANDLE
    import ctypes, ctypes.wintypes
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
    h = kernel32.CreateFileW(LOCK_FILE, 0x40000000, 0, None, 4, 0x80, None)
    INVALID = ctypes.wintypes.HANDLE(-1).value
    if h == INVALID or h == 0:
        log.error("Bot al actief — lock bezet. Stopt.")
        return False
    _LOCK_HANDLE = h
    return True

def release_lock():
    global _LOCK_HANDLE
    if _LOCK_HANDLE:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(_LOCK_HANDLE)
        _LOCK_HANDLE = None
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass

# ===========================================================================
#  MT5 VERBINDING
# ===========================================================================

def connect_mt5() -> bool:
    path_kwarg = {"path": MT5_TERMINAL_PATH} if os.path.exists(MT5_TERMINAL_PATH) else {}
    if not mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD,
                          server=MT5_SERVER, **path_kwarg):
        log.error("MT5 initialize() mislukt: %s", mt5.last_error())
        return False
    info = mt5.terminal_info()
    acc  = mt5.account_info()
    if info is None or acc is None:
        mt5.shutdown(); return False
    log.info("MT5 verbonden — account=%d  balance=%.2f", acc.login, acc.balance)
    mt5.symbol_select(SYMBOL, True)
    STATE.reconnect_count = 0 if hasattr(STATE, "reconnect_count") else None
    return True

def is_connected() -> bool:
    info = mt5.terminal_info()
    return info is not None and info.connected

def ensure_connected() -> bool:
    if is_connected():
        return True
    log.warning("MT5 verbinding verloren — herverbinding...")
    mt5.shutdown()
    for attempt in range(1, MAX_RECONNECTS + 1):
        time.sleep(RECONNECT_WAIT)
        if connect_mt5():
            log.info("Herverbinding geslaagd na %d pogingen", attempt)
            return True
        log.warning("Herverbinding %d/%d mislukt", attempt, MAX_RECONNECTS)
    return False

# ===========================================================================
#  HELPERS
# ===========================================================================

def get_balance() -> float:
    acc = mt5.account_info()
    return acc.balance if acc else 0.0

def get_equity() -> float:
    acc = mt5.account_info()
    return acc.equity if acc else 0.0

def open_positions(symbol: str = "") -> list:
    if symbol:
        return list(mt5.positions_get(symbol=symbol) or [])
    return list(mt5.positions_get() or [])

def get_bars(timeframe: int, count: int) -> Optional[pd.DataFrame]:
    rates = mt5.copy_rates_from_pos(SYMBOL, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    return df

def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def atr_ind(df: pd.DataFrame, p: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    return pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()],
                     axis=1).max(axis=1).ewm(span=p, adjust=False).mean()

def adx_ind(df: pd.DataFrame, p: int = 14) -> pd.Series:
    h, l = df["high"], df["low"]
    up = h.diff(); dn = -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    ndm = dn.where((dn > up) & (dn > 0), 0.0)
    atr_ = atr_ind(df, p)
    pdi = 100 * pdm.ewm(span=p, adjust=False).mean() / atr_.replace(0, 1e-9)
    ndi = 100 * ndm.ewm(span=p, adjust=False).mean() / atr_.replace(0, 1e-9)
    dx  = 100 * (pdi - ndi).abs() / (pdi + ndi + 1e-9)
    return dx.ewm(span=p, adjust=False).mean()

def rsi_ind(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(span=p, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, 1e-9))

def pip_val() -> float:
    info = mt5.symbol_info(SYMBOL)
    if info and info.trade_tick_size > 0:
        return info.trade_tick_value * (PIP_SIZE / info.trade_tick_size)
    return 10.0

def calc_lot(sl_dist: float) -> float:
    """Lot-sizing op de harde cap: risico (SL-afstand + commissie) ≤ €RISK_EUR_MAX.
    Rondt af naar BENEDEN; 0.0 = trade skippen (risico wordt nooit opgerekt)."""
    pv = pip_val()
    sl_pips = sl_dist / PIP_SIZE
    if sl_pips <= 0 or pv <= 0:
        return 0.0
    lot  = RISK_EUR_MAX / (sl_pips * pv + COMMISSION)
    info = mt5.symbol_info(SYMBOL)
    if info is None:
        return 0.0
    if info.volume_step > 0:
        lot = math.floor(lot / info.volume_step + 1e-9) * info.volume_step
    lot = min(lot, info.volume_max)
    if lot <= 0 or lot < info.volume_min:
        return 0.0
    return round(lot, 2)

def get_filling_mode() -> int:
    info = mt5.symbol_info(SYMBOL)
    if info is None:
        return mt5.ORDER_FILLING_FOK
    mask = info.filling_mode
    if mask & 1: return mt5.ORDER_FILLING_FOK
    if mask & 2: return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN

def load_start_balance() -> float:
    try:
        with open(START_BAL_FILE) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0

def save_start_balance(bal: float):
    try:
        with open(START_BAL_FILE, "w") as f:
            f.write(str(bal))
    except OSError:
        pass

def write_heartbeat():
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(datetime.now(tz=timezone.utc).isoformat())
    except OSError:
        pass

# ===========================================================================
#  FUNDINGPIPS STATE (equity-high, dag-anker, winstdagen — persistent)
# ===========================================================================

def load_fp_state():
    try:
        with open(FP_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return
    STATE.equity_high     = float(d.get("equity_high", 0.0))
    STATE.day_pnl_hist    = {k: float(v) for k, v in d.get("day_pnl_hist", {}).items()}
    STATE.last_close_date = d.get("last_close_date", "")
    # Dag-anker terugzetten zodat een herstart de dagstop-meting niet reset
    if d.get("day_date") == datetime.now(tz=timezone.utc).date().isoformat():
        STATE.current_day       = date.fromisoformat(d["day_date"])
        STATE.day_start_balance = float(d.get("day_start_balance", 0.0))
        STATE.day_start_anchor  = float(d.get("day_start_anchor", 0.0))
        STATE.daily_guard_hit   = bool(d.get("daily_guard_hit", False))

def save_fp_state():
    try:
        with open(FP_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "equity_high":       STATE.equity_high,
                "day_pnl_hist":      STATE.day_pnl_hist,
                "last_close_date":   STATE.last_close_date,
                "day_date":          STATE.current_day.isoformat(),
                "day_start_balance": STATE.day_start_balance,
                "day_start_anchor":  STATE.day_start_anchor,
                "daily_guard_hit":   STATE.daily_guard_hit,
            }, f, indent=2)
    except OSError:
        pass

def profitable_days_30d() -> int:
    cutoff = datetime.now(tz=timezone.utc).date() - timedelta(days=30)
    return sum(1 for d, pnl in STATE.day_pnl_hist.items()
               if date.fromisoformat(d) >= cutoff and pnl >= PROFIT_DAY_EUR)

def days_since_last_close() -> Optional[int]:
    if not STATE.last_close_date:
        return None
    return (datetime.now(tz=timezone.utc).date()
            - date.fromisoformat(STATE.last_close_date)).days

# ===========================================================================
#  NEWS FILTER (high-impact GBP/USD — ForexFactory kalender)
# ===========================================================================

def refresh_news():
    if time.time() - STATE.news_fetched_at < NEWS_REFRESH_SEC:
        return
    try:
        r = requests.get(NEWS_URL, timeout=10)
        r.raise_for_status()
        events = []
        for ev in r.json():
            if ev.get("impact") != "High" or ev.get("country") not in NEWS_CCY:
                continue
            try:
                t = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
            except (KeyError, ValueError, TypeError):
                continue
            events.append(t)
        STATE.news_events     = sorted(events)
        STATE.news_fetched_at = time.time()
        STATE.news_warned     = False
        log.info("Newskalender ververst — %d high-impact GBP/USD events deze week", len(events))
    except Exception as exc:
        log.warning("Newskalender ophalen mislukt: %s", exc)

def news_data_ok() -> bool:
    return (time.time() - STATE.news_fetched_at) < NEWS_MAX_AGE_SEC

def in_news_window(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(tz=timezone.utc)
    for t in STATE.news_events:
        if (t - timedelta(minutes=NEWS_BLOCK_BEFORE)
                <= now <= t + timedelta(minutes=NEWS_BLOCK_AFTER)):
            return True
    return False

def next_news_flatten() -> Optional[datetime]:
    """Eerstvolgende event waarvoor open posities dicht moeten (binnen NEWS_FLATTEN_BEFORE min)."""
    now = datetime.now(tz=timezone.utc)
    for t in STATE.news_events:
        if timedelta(0) <= (t - now) <= timedelta(minutes=NEWS_FLATTEN_BEFORE):
            return t
    return None

# ===========================================================================
#  H1 TREND + ADX
# ===========================================================================

def get_h1_context() -> Optional[tuple[str, float]]:
    """Geeft (trend, adx) of None als geen duidelijke trend."""
    df = get_bars(mt5.TIMEFRAME_H1, 300)
    if df is None or len(df) < 220:
        return None
    ef  = ema(df["close"], H1_EMA_FAST)
    es  = ema(df["close"], H1_EMA_SLOW)
    adx = adx_ind(df)
    ef_v  = float(ef.iloc[-2])
    es_v  = float(es.iloc[-2])
    adx_v = float(adx.iloc[-2])
    if adx_v < H1_ADX_MIN:
        return None
    if ef_v > es_v:
        return ("up", adx_v)
    elif ef_v < es_v:
        return ("down", adx_v)
    return None

# ===========================================================================
#  ORDER VERZENDEN
# ===========================================================================

def send_order(order_type: int, sl: float, tp: float,
               lot: float, strat: str, extra_log: str = "") -> Optional[int]:
    info = mt5.symbol_info(SYMBOL)
    tick = mt5.symbol_info_tick(SYMBOL)
    if info is None or tick is None:
        return None

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    dg    = info.digits

    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       lot,
        "type":         order_type,
        "price":        round(price, dg),
        "sl":           round(sl, dg),
        "tp":           round(tp, dg),
        "deviation":    DEVIATION,
        "magic":        MAGIC,
        "comment":      f"{COMMENT}_{strat}",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode(),
    }

    for attempt in range(1, 3):
        result = mt5.order_send(req)
        if result is None:
            log.error("order_send=None attempt=%d  %s", attempt, mt5.last_error())
            time.sleep(2); continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            direction = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
            log.info("ORDER  %s %s  lot=%.2f  entry=%.5f  sl=%.5f  tp=%.5f  #%d  %s",
                     strat, direction, lot, price, sl, tp, result.order, extra_log)
            STATE.open_tickets[result.order] = {
                "strat":     strat,
                "direction": direction,
                "entry":     price,
                "sl_dist":   abs(price - sl),
                "sl_orig":   sl,
            }
            STATE.trades_today += 1
            tg_open(strat, direction, lot, price, sl, tp, extra_log)
            return result.order
        log.error("retcode=%d  %s  attempt=%d", result.retcode, result.comment, attempt)
        time.sleep(2)
    return None

def close_all_positions():
    positions = open_positions()
    if not positions:
        return
    log.warning("Alle posities sluiten (%d open)...", len(positions))
    for pos in positions:
        info = mt5.symbol_info(pos.symbol)
        if info is None:
            continue
        for attempt in range(1, 4):
            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is None:
                time.sleep(1); continue
            otype = (mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY
                     else mt5.ORDER_TYPE_BUY)
            price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
            req = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       pos.symbol,
                "volume":       pos.volume,
                "type":         otype,
                "position":     pos.ticket,
                "price":        round(price, info.digits),
                "deviation":    DEVIATION * 3,
                "magic":        MAGIC,
                "comment":      "GUARD_CLOSE",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": get_filling_mode(),
            }
            r = mt5.order_send(req)
            if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                log.info("GUARD CLOSE  #%d  OK", pos.ticket)
                break
            log.error("GUARD CLOSE  #%d  poging %d mislukt", pos.ticket, attempt)
            time.sleep(2)

# ===========================================================================
#  PRE-TRADE COMPLIANCE CHECK (FundingPips Zero — sectie 5 van de spec)
# ===========================================================================

def position_risk_eur(pos) -> float:
    """Floating risk in € als de SL van deze positie geraakt wordt (vanaf entry)."""
    info = mt5.symbol_info(pos.symbol)
    if info is None or info.trade_tick_size <= 0:
        return RISK_EUR_MAX
    if pos.sl <= 0:
        log.warning("Positie #%d zonder SL — telt als onbeperkt risico, blokkeert entries", pos.ticket)
        return 999_999.0
    dist = ((pos.price_open - pos.sl) if pos.type == mt5.POSITION_TYPE_BUY
            else (pos.sl - pos.price_open))
    if dist <= 0:
        return 0.0   # SL op of voorbij break-even
    return (dist / info.trade_tick_size) * info.trade_tick_value * pos.volume

def pretrade_ok(new_risk_eur: float = RISK_EUR_MAX) -> tuple[bool, str]:
    """Volledige checklist vóór elke order — bij twijfel: skip."""
    now = datetime.now(tz=timezone.utc)

    if STATE.fp_halt:
        return False, "FP-halt actief (trailing vloer / dag-breach)"
    if STATE.daily_guard_hit:
        return False, "dagstop bereikt — vandaag geen trades meer"
    if now.weekday() >= 5:
        return False, "weekend"
    if now.weekday() == 4 and now.hour >= FRIDAY_LAST_ENTRY_H:
        return False, "vrijdag — geen nieuwe trades meer"

    positions = open_positions()
    if len(positions) >= MAX_TOTAL_POS:
        return False, f"al {MAX_TOTAL_POS} posities open"
    if len(open_positions(SYMBOL)) >= MAX_PER_SYMBOL:
        return False, "symbool al open (max 1 per symbool)"

    if not news_data_ok():
        if not STATE.news_warned:
            STATE.news_warned = True
            tg("⚠️ <b>Newskalender niet beschikbaar</b>\nNieuwe trades geblokkeerd (fail-safe) tot de kalender weer laadt.")
        return False, "geen actuele newsdata (fail-safe)"
    if in_news_window(now) or next_news_flatten() is not None:
        return False, "high-impact news window"

    open_risk = sum(position_risk_eur(p) for p in positions)
    if open_risk + new_risk_eur > OPEN_RISK_CAP_EUR:
        return False, f"open risk €{open_risk + new_risk_eur:.0f} > cap €{OPEN_RISK_CAP_EUR:.0f}"

    equity = get_equity()
    floor  = STATE.equity_high - FP_TRAIL_EUR
    if equity - floor < TRAIL_MARGIN_EUR + new_risk_eur:
        return False, "te dicht bij trailing-vloer"

    if STATE.day_start_anchor > 0:
        day_loss = STATE.day_start_anchor - equity
        if day_loss + new_risk_eur >= DAY_STOP_EUR:
            return False, "trade zou de dagstop kunnen raken"

    return True, ""

def manage_flatten_guards():
    """Vrijdag- en news-flatten: posities dicht vóór weekend / high-impact news."""
    positions = open_positions()
    if not positions:
        return
    now = datetime.now(tz=timezone.utc)

    # Weekend guard: vrijdag alles dicht ruim vóór marktsluiting
    if (now.weekday() == 4 and not STATE.friday_flat_done
            and (now.hour, now.minute) >= (FRIDAY_FLATTEN_H, FRIDAY_FLATTEN_M)):
        STATE.friday_flat_done = True
        log.warning("WEEKEND GUARD — vrijdag %02d:%02d UTC: alle posities dicht", now.hour, now.minute)
        tg(f"🛑 <b>Weekend guard</b>\n{_SEP}\nVrijdag — alle posities gesloten vóór het weekend.")
        close_all_positions()
        return

    # News guard: flatten vóórdat het 10-min venster ingaat
    ev = next_news_flatten()
    if ev is not None:
        key = ev.isoformat()
        if key not in STATE.news_flat_done:
            STATE.news_flat_done.add(key)
            log.warning("NEWS GUARD — high-impact event %s UTC: posities dicht", ev.strftime("%H:%M"))
            tg(f"📰 <b>News guard</b>\n{_SEP}\nHigh-impact news om {ev.strftime('%H:%M')} UTC — posities vooraf gesloten.")
            close_all_positions()

# ===========================================================================
#  BREAK-EVEN BEHEER
# ===========================================================================

def manage_breakeven():
    """Zet SL naar break-even zodra 1×SL winst bereikt is (NY én LBO — verlaagt open risk)."""
    if in_news_window():
        return   # geen SL-wijzigingen binnen het newsvenster
    for ticket, info in list(STATE.open_tickets.items()):
        pos_list = mt5.positions_get(ticket=ticket)
        if not pos_list:
            continue
        pos     = pos_list[0]
        entry   = info["entry"]
        sl_dist = info.get("sl_dist", 0.0)
        if sl_dist <= 0:
            continue

        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            continue

        is_sell  = (pos.type == mt5.ORDER_TYPE_SELL)
        price    = tick.bid if is_sell else tick.ask
        profit_r = ((entry - price) / sl_dist) if is_sell else ((price - entry) / sl_dist)

        if profit_r < NY_BE_R:
            continue

        # Controleer of SL al op break-even staat
        be_sl = (entry - sl_dist * 0.1) if is_sell else (entry + sl_dist * 0.1)
        sym_info = mt5.symbol_info(SYMBOL)
        if sym_info is None:
            continue
        be_sl = round(be_sl, sym_info.digits)

        already_be = ((is_sell and pos.sl <= entry + sl_dist * 0.15) or
                      (not is_sell and pos.sl >= entry - sl_dist * 0.15))
        if already_be:
            continue

        req = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket,
               "symbol": SYMBOL, "sl": be_sl, "tp": pos.tp}
        result = mt5.order_send(req)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("BREAKEVEN  #%d  SL %.5f → %.5f  (%.1fR)", ticket, pos.sl, be_sl, profit_r)
        else:
            err = result.comment if result else mt5.last_error()
            log.warning("Breakeven mislukt  #%d: %s", ticket, err)

# ===========================================================================
#  FUNDINGPIPS GUARDS (doorlopend — sectie 2 + 6 van de spec)
# ===========================================================================

def check_guards() -> bool:
    if STATE.fp_halt:
        return False

    equity  = get_equity()
    balance = get_balance()
    if equity <= 0:
        return False

    # Trailing vloer: hoogste equity − €8.000 (schuift alleen omhoog, persistent)
    if equity > STATE.equity_high:
        STATE.equity_high = equity
        save_fp_state()
    floor = STATE.equity_high - FP_TRAIL_EUR
    if equity <= floor + TRAIL_HALT_BUFFER_EUR:
        log.critical("TRAILING VLOER  equity=%.2f  vloer=%.2f  high=%.2f",
                     equity, floor, STATE.equity_high)
        STATE.fp_halt = True
        tg_guard("TRAILING VLOER (5%)", STATE.equity_high - equity, equity)
        close_all_positions()
        return False

    # Dagverlies op EQUITY t.o.v. dag-anker (max(balance, equity) bij dagstart)
    if STATE.day_start_anchor > 0:
        day_loss = STATE.day_start_anchor - equity

        # Backstop: de FundingPips 3%-breach mag NOOIT geraakt worden
        if day_loss >= FP_DAY_BREACH_EUR * 0.9:
            log.critical("FP DAG-BREACH NABIJ  verlies=€%.0f — noodstop", day_loss)
            STATE.fp_halt = True
            tg_guard("FP DAG-BREACH (noodstop)", day_loss, equity)
            close_all_positions()
            return False

        # Eigen dagstop €1.600: flatten + geen trades tot morgen
        if day_loss >= DAY_STOP_EUR:
            if not STATE.daily_guard_hit:
                log.warning("DAGSTOP  verlies=€%.0f", day_loss)
                STATE.daily_guard_hit = True
                save_fp_state()
                tg_guard("DAGSTOP", day_loss, equity)
                close_all_positions()
            return False
    if STATE.daily_guard_hit:
        return False

    # Maandelijkse circuit breaker (eigen extra bescherming)
    if STATE.monthly_cb_hit:
        return False
    if STATE.month_start_balance > 0:
        month_loss = STATE.month_start_balance - balance
        if month_loss >= STATE.month_start_balance * MONTHLY_CB_PCT:
            log.warning("CIRCUIT BREAKER  maandverlies=€%.0f (%.1f%%)",
                        month_loss, month_loss / STATE.month_start_balance * 100)
            STATE.monthly_cb_hit = True
            tg_guard("CIRCUIT BREAKER (maand)", month_loss, equity)
            return False

    return True

# ===========================================================================
#  DAG / MAAND RESET
# ===========================================================================

def maybe_reset():
    now   = datetime.now(tz=timezone.utc)
    today = now.date()
    month = (now.year, now.month)

    # Nieuwe maand
    if month != STATE.current_month:
        bal = get_balance()
        log.info("NIEUWE MAAND %d-%02d  balance=%.2f", now.year, now.month, bal)
        STATE.current_month       = month
        STATE.month_start_balance = bal
        STATE.monthly_cb_hit      = False

    # Nieuwe dag (UTC)
    if today != STATE.current_day:
        bal = get_balance()
        eq  = get_equity()

        # Vorige dag afsluiten in de winstdagen-historie (payout: ≥€400 telt)
        if STATE.current_day != date.min and STATE.day_start_balance > 0:
            prev_pnl = round(bal - STATE.day_start_balance, 2)
            STATE.day_pnl_hist[STATE.current_day.isoformat()] = prev_pnl
            cutoff = (today - timedelta(days=45)).isoformat()
            STATE.day_pnl_hist = {k: v for k, v in STATE.day_pnl_hist.items() if k >= cutoff}
            if prev_pnl >= PROFIT_DAY_EUR:
                log.info("WINSTDAG geregistreerd: €%.0f  (30d: %d/%d)",
                         prev_pnl, profitable_days_30d(), PROFIT_DAYS_MIN)

        log.info("NIEUWE DAG %s  balance=%.2f  equity=%.2f", today, bal, eq)
        STATE.current_day        = today
        STATE.day_start_balance  = bal
        STATE.day_start_anchor   = max(bal, eq)
        STATE.daily_guard_hit    = False
        STATE.lbo_traded_today   = False
        STATE.ny_count_today     = 0
        STATE.ny_session_active  = False
        STATE.last_ny_cross_bar  = None
        STATE.trades_today       = 0
        STATE.daily_report_sent  = False
        STATE.friday_flat_done   = False
        STATE.news_flat_done.clear()
        save_fp_state()

# ===========================================================================
#  GESLOTEN TRADES DETECTEREN
# ===========================================================================

def check_closed_trades():
    current_tickets = {p.ticket for p in open_positions()}
    closed = set(STATE.open_tickets.keys()) - current_tickets
    for ticket in closed:
        info = STATE.open_tickets.pop(ticket, None)
        if info is None:
            continue
        deals = mt5.history_deals_get(position=ticket)
        if deals:
            pnl         = sum(d.profit + d.swap + d.commission for d in deals)
            close_price = deals[-1].price
        else:
            pnl         = 0.0
            close_price = 0.0
        balance = get_balance()
        log.info("TRADE GESLOTEN  #%d  %s %s  pnl=%.2f",
                 ticket, info["direction"], info["strat"], pnl)
        STATE.last_close_date = datetime.now(tz=timezone.utc).date().isoformat()
        save_fp_state()
        tg_close(ticket, info["direction"], pnl,
                 info["entry"], close_price, balance, STATE.start_balance)

def maybe_send_daily_report():
    now = datetime.now(tz=timezone.utc)
    if now.hour == 20 and not STATE.daily_report_sent:
        balance  = get_balance()
        day_pnl  = balance - STATE.day_start_balance
        total_pct = (balance - STATE.start_balance) / STATE.start_balance * 100 if STATE.start_balance > 0 else 0
        day_icon  = "📈" if day_pnl >= 0 else "📉"
        pdays     = profitable_days_30d()
        pday_now  = "✅" if day_pnl >= PROFIT_DAY_EUR else f"⏳ (telt vanaf +€{PROFIT_DAY_EUR:.0f})"
        extra = ""
        idle = days_since_last_close()
        if idle is not None and idle >= 25:
            extra += f"\n⚠️ Al {idle} dagen geen gesloten trade (inactivity-regel: 30d)"
        month_profit = balance - STATE.month_start_balance if STATE.month_start_balance > 0 else 0.0
        if day_pnl > 0 and month_profit > 0 and day_pnl / month_profit > 0.15:
            extra += (f"\n⚠️ Dagwinst = {day_pnl / month_profit * 100:.0f}% van maandwinst "
                      f"— consistency-regel: max 15% per dag, winst spreiden")
        tg(
            f"{day_icon} <b>Dagrapport  {now.day} {now.strftime('%b %Y')}</b>\n"
            f"{_SEP}\n"
            f"💼 Balans    €{balance:,.0f}   ({total_pct:+.1f}%)\n"
            f"📅 Dag P&amp;L   <b>€{day_pnl:+,.0f}</b>   winstdag: {pday_now}\n"
            f"📈 Winstdagen 30d: <b>{pdays}/{PROFIT_DAYS_MIN}</b>\n"
            f"🔢 Trades    {STATE.trades_today} vandaag"
            f"{extra}"
        )
        STATE.daily_report_sent = True

# ===========================================================================
#  LONDON BREAKOUT STRATEGIE
# ===========================================================================

def check_lbo():
    """Zoek London Breakout entry. Max 1 per dag."""
    if STATE.lbo_traded_today:
        return

    now = datetime.now(tz=timezone.utc)
    hour = now.hour
    if not (LBO_ENTRY_START <= hour < LBO_ENTRY_END):
        return

    ctx = get_h1_context()
    if ctx is None:
        return
    h1_trend, adx_v = ctx

    # Haal M5 data op
    m5 = get_bars(mt5.TIMEFRAME_M5, 500)
    if m5 is None or len(m5) < 50:
        return

    # Bepaal Asian range: gisterenavond 22:00 → vandaag 07:00 UTC
    today     = now.replace(hour=0, minute=0, second=0, microsecond=0)
    prev      = today - timedelta(days=1)
    a_start   = prev.replace(hour=LBO_ASIAN_START, tzinfo=timezone.utc)
    a_end     = today.replace(hour=LBO_ASIAN_END,  tzinfo=timezone.utc)
    asian     = m5[(m5.index >= a_start) & (m5.index < a_end)]

    if len(asian) < 3:
        log.debug("LBO: te weinig Asian bars (%d)", len(asian))
        return

    rh    = float(asian["high"].max())
    rl    = float(asian["low"].min())
    rpips = (rh - rl) / PIP_SIZE

    if not (LBO_RANGE_MIN <= rpips <= LBO_RANGE_MAX):
        log.debug("LBO: range %.1f pips buiten filter (%d–%d)", rpips, LBO_RANGE_MIN, LBO_RANGE_MAX)
        return

    # Huidig prijsniveau van tick
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return

    # Gebruik de laatste gesloten M5 bar voor entry trigger
    last_bar = m5.iloc[-2]
    bar_time = m5.index[-2]

    # Controleer of deze bar al in het LBO venster valt
    bar_hour = bar_time.hour
    if not (LBO_ENTRY_START <= bar_hour < LBO_ENTRY_END):
        return

    # Breakout detectie: slotkoers boven/onder range
    direction = None
    if last_bar["close"] > rh and h1_trend == "up":
        direction = "BUY"
    elif last_bar["close"] < rl and h1_trend == "down":
        direction = "SELL"

    if direction is None:
        return

    # Volledige compliance-checklist (posities, news, open risk, dagstop, vloer)
    ok, reason = pretrade_ok()
    if not ok:
        log.debug("LBO: pre-trade check — %s", reason)
        return

    entry   = tick.ask if direction == "BUY" else tick.bid
    sl_p    = rl if direction == "BUY" else rh
    sl_dist = abs(entry - sl_p)
    tp_p    = (entry + (rh - rl) * LBO_TP_MULT if direction == "BUY"
               else entry - (rh - rl) * LBO_TP_MULT)

    if sl_dist <= 0:
        return

    lot = calc_lot(sl_dist)
    if lot <= 0:
        return

    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    extra = f"Range: {rpips:.1f} pips  ADX: {adx_v:.1f}  TP: {LBO_TP_MULT}×range"

    ticket = send_order(order_type, sl_p, tp_p, lot, "LBO", extra)
    if ticket:
        STATE.lbo_traded_today = True
        log.info("LBO %s  range=%.1fp  sl=%.5f  tp=%.5f  lot=%.2f",
                 direction, rpips, sl_p, tp_p, lot)

# ===========================================================================
#  NY EMA SELL STRATEGIE
# ===========================================================================

def check_ny_sell():
    """Zoek NY EMA cross SELL entry. Max 2 per sessie."""
    now  = datetime.now(tz=timezone.utc)
    hour = now.hour

    # Reset ny_count bij begin nieuwe sessie
    if hour == NY_START and not STATE.ny_session_active:
        STATE.ny_session_active = True
        STATE.ny_count_today    = 0
        STATE.last_ny_cross_bar = None

    if hour >= NY_END:
        STATE.ny_session_active = False
        return

    if not (NY_START <= hour < NY_END):
        return

    if STATE.ny_count_today >= NY_MAX_TRADES:
        return

    ctx = get_h1_context()
    if ctx is None:
        return
    h1_trend, _ = ctx
    if h1_trend != "down":
        return

    # M5 data — neem genoeg voor indicators
    m5 = get_bars(mt5.TIMEFRAME_M5, 150)
    if m5 is None or len(m5) < 30:
        return

    e8  = ema(m5["close"], NY_EMA_FAST)
    e21 = ema(m5["close"], NY_EMA_SLOW)
    rsi = rsi_ind(m5["close"], 14)
    atr = atr_ind(m5, 14)

    # Gebruik de laatste gesloten bar (index -2) voor signaal
    bar      = m5.iloc[-2]
    bar_time = m5.index[-2]
    e8c      = float(e8.iloc[-2]);  e8p  = float(e8.iloc[-3])
    e21c     = float(e21.iloc[-2]); e21p = float(e21.iloc[-3])
    rsi_v    = float(rsi.iloc[-2])
    atr_v    = float(atr.iloc[-2])

    # Voorkom dubbele entries op dezelfde bar
    if STATE.last_ny_cross_bar == bar_time:
        return

    # Baruur moet in NY sessie liggen
    if not (NY_START <= bar_time.hour < NY_END):
        return

    # EMA cross omlaag
    if not (e8p >= e21p and e8c < e21c):
        return

    # Bearish candle
    if bar["close"] >= bar["open"]:
        return

    # Body percentage
    c_range = bar["high"] - bar["low"]
    if c_range <= 0:
        return
    if abs(bar["close"] - bar["open"]) / c_range < NY_BODY_MIN:
        return

    # RSI filter
    if not (25 <= rsi_v <= 60):
        return

    # Volledige compliance-checklist (posities, news, open risk, dagstop, vloer)
    ok, reason = pretrade_ok()
    if not ok:
        log.debug("NY: pre-trade check — %s", reason)
        return

    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return

    entry   = tick.bid
    sl_dist = atr_v * NY_ATR_SL_MULT
    tp_dist = sl_dist * NY_RR_RATIO
    sl_p    = entry + sl_dist
    tp_p    = entry - tp_dist

    lot = calc_lot(sl_dist)
    if lot <= 0:
        return

    extra = f"EMA8={e8c:.5f} EMA21={e21c:.5f}  RSI={rsi_v:.1f}  ATR={atr_v:.5f}"

    ticket = send_order(mt5.ORDER_TYPE_SELL, sl_p, tp_p, lot, "NY", extra)
    if ticket:
        STATE.ny_count_today  += 1
        STATE.last_ny_cross_bar = bar_time
        log.info("NY SELL  RSI=%.1f  sl=%.5f  tp=%.5f  lot=%.2f  [%d/%d]",
                 rsi_v, sl_p, tp_p, lot, STATE.ny_count_today, NY_MAX_TRADES)

# ===========================================================================
#  TELEGRAM COMMANDO'S
# ===========================================================================

_tg_rate = {"last": 0.0, "blocked": 0}

def process_tg_commands():
    if not TG_TOKEN or TG_TOKEN.startswith("123456"):
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": STATE.tg_last_update_id + 1, "timeout": 2},
            timeout=5,
        )
        for upd in r.json().get("result", []):
            STATE.tg_last_update_id = upd["update_id"]
            msg  = upd.get("message", {})
            cid  = str(msg.get("chat", {}).get("id", ""))
            if cid != TG_CHAT_ID:
                log.warning("Telegram: onbekende chat_id %s geblokkeerd", cid)
                continue
            text = msg.get("text", "").strip()
            if not text or not text.startswith("/"):
                continue

            now_ts = time.time()
            if now_ts - _tg_rate["last"] < 3.0:
                _tg_rate["blocked"] += 1
                if _tg_rate["blocked"] == 1:
                    tg("⚠️ Te snel — wacht even tussen commando's.")
                continue
            _tg_rate["last"] = now_ts
            _tg_rate["blocked"] = 0

            _handle_command(text)
    except Exception as exc:
        log.warning("Telegram poll fout: %s", exc)

def _handle_command(text: str):
    if text == "/status":
        bal      = get_balance()
        eq       = get_equity()
        pct      = (bal - STATE.start_balance) / STATE.start_balance * 100 if STATE.start_balance > 0 else 0
        day_loss = STATE.day_start_anchor - eq if STATE.day_start_anchor > 0 else 0.0
        paused   = "⏸ GEPAUZEERD" if STATE.bot_paused else "▶️ ACTIEF"
        pos_list = open_positions(SYMBOL)
        pos_lines = ""
        open_risk = 0.0
        for p in pos_list:
            ico = "🟢" if p.profit >= 0 else "🔴"
            d   = "SELL" if p.type == mt5.ORDER_TYPE_SELL else "BUY"
            pos_lines += f"\n  {ico} {d}  €{p.profit:+,.0f}"
            open_risk += position_risk_eur(p)
        floor = STATE.equity_high - FP_TRAIL_EUR
        warn  = ""
        if STATE.fp_halt:         warn += "\n🚨 FP-HALT actief — handmatige controle vereist!"
        if STATE.daily_guard_hit: warn += "\n🛑 Dagstop bereikt — vandaag geen trades meer"
        if STATE.monthly_cb_hit:  warn += "\n⚡ Circuit breaker (maand) actief"
        if not news_data_ok():    warn += "\n📰 Newsdata verouderd — entries geblokkeerd (fail-safe)"
        tg(
            f"📡 <b>LBO BOT STATUS</b>  {paused}\n"
            f"{_SEP}\n"
            f"💼 Balans     €{bal:,.0f}   ({pct:+.1f}%)\n"
            f"⚖️ Equity     €{eq:,.0f}\n"
            f"📅 Dagverlies €{day_loss:,.0f} / stop €{DAY_STOP_EUR:,.0f}\n"
            f"🧱 Vloer      €{floor:,.0f}   (marge €{eq - floor:,.0f})\n"
            f"🛡 Open risk  €{open_risk:,.0f} / cap €{OPEN_RISK_CAP_EUR:,.0f}\n"
            f"📈 Winstdagen 30d: {profitable_days_30d()}/{PROFIT_DAYS_MIN}\n"
            f"{_SEP}\n"
            f"📂 Posities  {len(pos_list)} open{pos_lines}\n"
            f"🏦 GBPUSD  |  LBO 07–13h  |  NY SELL 14–17h\n"
            f"🔢 Trades vandaag: {STATE.trades_today}"
            f"  (LBO: {'✅' if STATE.lbo_traded_today else '⏳'}  NY: {STATE.ny_count_today}/{NY_MAX_TRADES})"
            f"{warn}"
        )

    elif text == "/pause":
        STATE.bot_paused = True
        log.warning("Bot gepauzeerd via Telegram")
        tg(f"⏸ <b>Bot gepauzeerd</b>\n{_SEP}\nGeen nieuwe trades.\nStuur /resume om te hervatten.")

    elif text == "/resume":
        STATE.bot_paused = False
        log.info("Bot hervat via Telegram")
        tg(f"▶️ <b>Bot hervat</b>\n{_SEP}\nTrading weer actief. 🚀")

    elif text == "/close":
        tg(f"🔒 <b>Posities sluiten...</b>")
        close_all_positions()
        tg(f"✅ Alle posities gesloten.")

    elif text.startswith("/switch "):
        parts = text.split(" ", 3)
        if len(parts) == 4:
            _, new_login, new_pass, new_server = parts
            log.info("Telegram /switch login=%s server=%s", new_login, new_server)
            _switch_account(new_login, new_pass, new_server)
        else:
            tg("⚠️ Gebruik:\n<code>/switch LOGIN WACHTWOORD SERVER</code>\n\n⚠️ Verwijder dit bericht daarna.")

    elif text == "/help":
        tg(
            f"📋 <b>COMMANDO'S  LBO Bot</b>\n"
            f"{_SEP}\n"
            f"📡 /status   balans &amp; posities\n"
            f"⏸ /pause    nieuwe trades stoppen\n"
            f"▶️  /resume   trading hervatten\n"
            f"🔒 /close    alle posities sluiten\n"
            f"🔑 /switch   account wisselen\n"
            f"📋 /help     dit menu\n"
            f"{_SEP}\n"
            f"🔒 <b>FUNDINGPIPS ZERO LIMIETEN</b>\n"
            f"🎯 Trade    max <b>€{RISK_EUR_MAX:,.0f}</b> risk (hard cap)\n"
            f"📅 Dagstop  <b>-€{DAY_STOP_EUR:,.0f}</b>  (breach: -€{FP_DAY_BREACH_EUR:,.0f})\n"
            f"🧱 Trailing <b>-€{FP_TRAIL_EUR:,.0f}</b> vanaf equity-high\n"
            f"🛡 Open risk max <b>€{OPEN_RISK_CAP_EUR:,.0f}</b> pre-trade\n"
            f"📰 News ±10 min  |  🛑 vrijdag weekend-flatten\n"
            f"📆 Maand    max <b>-{MONTHLY_CB_PCT*100:.0f}%</b> (circuit breaker)"
        )

def _switch_account(login: str, password: str, server: str):
    global MT5_LOGIN, MT5_PASSWORD, MT5_SERVER
    try:
        ini = configparser.RawConfigParser()
        ini.read(_ini_path)
        ini["MT5"]["login"]    = login
        ini["MT5"]["password"] = password
        ini["MT5"]["server"]   = server
        with open(_ini_path, "w") as f:
            ini.write(f)
        mt5.shutdown()
        MT5_LOGIN    = int(login)
        MT5_PASSWORD = password
        MT5_SERVER   = server
        time.sleep(3)
        if connect_mt5():
            bal = get_balance()
            STATE.start_balance = bal
            save_start_balance(bal)
            # FP-compliance state resetten voor het nieuwe account
            STATE.equity_high       = max(bal, get_equity())
            STATE.day_start_balance = bal
            STATE.day_start_anchor  = STATE.equity_high
            STATE.day_pnl_hist      = {}
            STATE.last_close_date   = ""
            STATE.daily_guard_hit   = False
            STATE.fp_halt           = False
            save_fp_state()
            log.info("Account gewisseld naar %s  balance=%.2f", login, bal)
            tg(f"✅ <b>Account gewisseld</b>\n{_SEP}\n🔑 Login {login}\n🌐 Server {server}\n💼 €{bal:,.0f}")
        else:
            tg(f"❌ <b>Account wisselen mislukt</b> — login {login} niet bereikbaar.")
    except Exception as exc:
        log.error("Account wissel fout: %s", exc)
        tg(f"❌ Fout: {exc}")

def _tg_command_loop():
    while True:
        try:
            process_tg_commands()
        except Exception:
            pass
        time.sleep(3)

# ===========================================================================
#  MAIN LOOP
# ===========================================================================

def main():
    if not acquire_lock():
        sys.exit(0)

    # Compliance: alleen FX — goud is uitgesloten
    if any(x in SYMBOL.upper() for x in ("XAU", "GOLD")):
        log.error("Symbool %s is goud — uitgesloten per compliance-config (alleen FX)", SYMBOL)
        release_lock()
        sys.exit(1)

    log.info("=" * 60)
    log.info("LBO Bot gestart — GBPUSD London Breakout + NY EMA SELL")
    log.info("Account     : FundingPips Zero €160K (compliance-modus)")
    log.info("LBO venster : %02d:00–%02d:00 UTC  TP=%.1f× range", LBO_ENTRY_START, LBO_ENTRY_END, LBO_TP_MULT)
    log.info("NY venster  : %02d:00–%02d:00 UTC  SELL-only  max %d/sessie", NY_START, NY_END, NY_MAX_TRADES)
    log.info("ADX min     : %.0f  |  Risico: max €%.0f/trade (hard cap)", H1_ADX_MIN, RISK_EUR_MAX)
    log.info("FP guards   : dagstop=€%.0f  breach=€%.0f  trailing=€%.0f  open-risk-cap=€%.0f",
             DAY_STOP_EUR, FP_DAY_BREACH_EUR, FP_TRAIL_EUR, OPEN_RISK_CAP_EUR)
    log.info("News filter : ±10 min high-impact %s  |  weekend-flatten vr %02d:%02d UTC",
             "/".join(NEWS_CCY), FRIDAY_FLATTEN_H, FRIDAY_FLATTEN_M)
    log.info("=" * 60)

    if not connect_mt5():
        sys.exit(1)

    bal = get_balance()
    eq  = get_equity()
    if bal <= 0:
        log.error("Balance is 0 — controleer MT5 account")
        mt5.shutdown(); sys.exit(1)

    persisted = load_start_balance()
    if persisted > 0:
        STATE.start_balance = persisted
        log.info("Startbalans (hersteld): %.2f  |  huidig=%.2f", STATE.start_balance, bal)
    else:
        STATE.start_balance = min(bal, eq)
        save_start_balance(STATE.start_balance)
        log.info("Startbalans (nieuw): %.2f", STATE.start_balance)

    # FP-compliance state (equity-high, dag-anker, winstdagen) herstellen
    load_fp_state()
    if STATE.equity_high <= 0:
        STATE.equity_high = max(bal, eq)
    log.info("Equity-high : %.2f  →  trailing vloer %.2f",
             STATE.equity_high, STATE.equity_high - FP_TRAIL_EUR)

    # Newskalender direct laden (zonder actuele data geen entries — fail-safe)
    refresh_news()

    maybe_reset()
    save_fp_state()

    threading.Thread(target=_tg_command_loop, daemon=True).start()

    tg(
        f"🚀 <b>LBO BOT GESTART</b>  (FundingPips Zero)\n"
        f"{_SEP}\n"
        f"💼 Balans    €{bal:,.0f}\n"
        f"🏦 Symbool   GBPUSD (alleen FX)\n"
        f"{_SEP}\n"
        f"⏰ LBO       07:00–13:00 UTC\n"
        f"⏰ NY SELL   14:00–17:00 UTC\n"
        f"🎯 Risk      max €{RISK_EUR_MAX:,.0f}/trade  |  dagstop -€{DAY_STOP_EUR:,.0f}\n"
        f"📰 News ±10 min  |  🛑 weekend-flatten\n"
        f"{_SEP}\n"
        f"Typ /help voor commando's"
    )

    _loop_count = 0
    try:
        while True:
            _loop_count += 1
            write_heartbeat()
            if not ensure_connected():
                log.error("Verbinding permanent verloren — bot stopt")
                tg_status("VERBINDING VERLOREN — herstart vereist!")
                break

            try:
                maybe_reset()
            except Exception as exc:
                log.exception("maybe_reset fout: %s", exc)

            try:
                check_closed_trades()
            except Exception as exc:
                log.exception("check_closed_trades fout: %s", exc)

            try:
                maybe_send_daily_report()
            except Exception as exc:
                log.exception("daily_report fout: %s", exc)

            try:
                refresh_news()
            except Exception as exc:
                log.exception("refresh_news fout: %s", exc)

            try:
                manage_flatten_guards()
            except Exception as exc:
                log.exception("manage_flatten_guards fout: %s", exc)

            try:
                manage_breakeven()
            except Exception as exc:
                log.exception("manage_breakeven fout: %s", exc)

            if not STATE.bot_paused and check_guards():
                try:
                    check_lbo()
                except Exception as exc:
                    log.exception("check_lbo fout: %s", exc)
                    tg_status(f"LBO fout: {exc}")

                try:
                    check_ny_sell()
                except Exception as exc:
                    log.exception("check_ny_sell fout: %s", exc)
                    tg_status(f"NY fout: {exc}")

            time.sleep(POLL_SEC)

    except KeyboardInterrupt:
        log.info("Gestopt door gebruiker (Ctrl+C)")
        tg_status("Bot gestopt door gebruiker.")
    except BaseException as exc:
        log.critical("BOT GESTOPT DOOR ONVERWACHT: %s (%s)", exc, type(exc).__name__)
        tg_status(f"BOT CRASH: {type(exc).__name__}: {exc}")
        raise
    finally:
        log.info("Bot afsluiten — mt5.shutdown() + lock vrijgeven")
        mt5.shutdown()
        release_lock()


if __name__ == "__main__":
    main()
