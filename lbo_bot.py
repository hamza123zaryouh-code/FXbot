"""
FundingPips Zero Bot — strategie "NY Flow + Monday"
===================================================
Tijd-gebaseerde flow-strategie op GBPUSD/EURUSD/USDJPY/AUDUSD, gevalideerd
over 12 maanden (backtest.py: 12M +31,5%, laatste 3M +9,7%, max DD 2,9%,
alle kwartalen positief, spread-stress tot 1,2 pip ruim winstgevend).

  STRATEGIE (alle tijden UTC):
    MON   maandag BUY GBPUSD         00:00 -> 20:00   SL 40 pips  risk EUR 500
    MONA  maandag BUY AUDUSD         00:00 -> 20:00   SL 30 pips  risk EUR 500
    H12   werkdag BUY GBPUSD+EURUSD  12:00 -> 13:00   SL 20 pips  risk EUR 300
    H14   werkdag BUY GBPUSD+EURUSD  14:00 -> 15:00   SL 20 pips  risk EUR 300
    H18   ma-do   BUY USDJPY         18:00 -> 20:00   SL 20 pips  risk EUR 500
    H18G  ma-do   SELL GBPUSD        18:00 -> 20:00   SL 20 pips  risk EUR 300
  Exits zijn tijd-gebaseerd (of SL). Geen TP, geen indicatoren, geen breakeven.
  Korte vensters worden geskipt als er een high-impact event op het symbool in
  het houd-venster valt (anders zou de news-flatten de trade direct afkappen).

  COMPLIANCE — FundingPips Zero €160K (EUR):
    max €500 risk/trade (hard cap)  |  max 2 posities, 1 per symbool
    eigen dagstop -€1.600 (equity)  |  FP dag-breach €4.800 (backstop)
    trailing vloer 5% = €8.000 vanaf hoogste equity (persistent)
    open-risk cap €1.400 pre-trade  |  newsfilter ±10 min high-impact
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
import queue
import sys
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, date
from typing import Optional

import MetaTrader5 as mt5
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
#  STRATEGIE — NY Flow + Monday (alle tijden UTC)
# ===========================================================================

@dataclass(frozen=True)
class Sleeve:
    name: str
    symbols: tuple          # symbolen waarop deze sleeve handelt
    weekdays: tuple         # 0=ma ... 4=vr
    direction: str          # "BUY" / "SELL"
    entry_h: int
    entry_m: int
    exit_h: int
    exit_m: int
    sl_pips: float
    risk_eur: float         # risico per trade (<= RISK_EUR_MAX)

SYMBOLS = ("GBPUSD", "EURUSD", "USDJPY", "AUDUSD")   # alleen FX — goud geweigerd

SLEEVES = (
    Sleeve("MON",  ("GBPUSD",),          (0,),            "BUY",  0, 0, 20, 0, 40.0, 500.0),
    Sleeve("MONA", ("AUDUSD",),          (0,),            "BUY",  0, 0, 20, 0, 30.0, 500.0),
    Sleeve("H12",  ("GBPUSD", "EURUSD"), (0, 1, 2, 3, 4), "BUY",  12, 0, 13, 0, 20.0, 300.0),
    Sleeve("H14",  ("GBPUSD", "EURUSD"), (0, 1, 2, 3, 4), "BUY",  14, 0, 15, 0, 20.0, 300.0),
    Sleeve("H18",  ("USDJPY",),          (0, 1, 2, 3),    "BUY",  18, 0, 20, 0, 20.0, 500.0),
    Sleeve("H18G", ("GBPUSD",),          (0, 1, 2, 3),    "SELL", 18, 0, 20, 0, 20.0, 300.0),
)
ENTRY_GRACE_MIN = 3        # entry alleen binnen [entry, entry+3min] — daarna skip

COMMISSION    = 3.5        # round-trip per lot (meegenomen in sizing)
SLIPPAGE_PIPS = 2.0        # sizing-buffer: fill kan tot DEVIATION slechter zijn

def pip_size(symbol: str) -> float:
    return 0.01 if "JPY" in symbol.upper() else 0.0001

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

# News filter — geen trades ±10 min rond high-impact news op het symbool
NEWS_CCY            = ("GBP", "USD", "EUR", "JPY", "AUD")
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
COMMENT          = "FP_BOT"
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

def tg_open(symbol: str, strat: str, direction: str, lot: float,
            entry: float, sl: float, exit_dt: datetime):
    arrow = "🟢" if direction == "BUY" else "🔴"
    sl_pips = abs(entry - sl) / pip_size(symbol)
    tg(
        f"{arrow} <b>{direction}  {symbol}  [{strat}]</b>\n"
        f"{_SEP}\n"
        f"📍 Entry  <code>{entry:.5f}</code>\n"
        f"🛑 SL     <code>{sl:.5f}</code>   <i>({sl_pips:.1f} pips)</i>\n"
        f"⏰ Exit   {exit_dt.strftime('%H:%M')} UTC (tijd-exit)\n"
        f"{_SEP}\n"
        f"📦 Lot    {lot:.2f}    Risico ≤ €{RISK_EUR_MAX:,.0f}"
    )

def tg_close(symbol: str, direction: str, pnl: float,
             entry: float, close_price: float, balance: float, start_bal: float):
    total_pct = (balance - start_bal) / start_bal * 100 if start_bal > 0 else 0
    label = "WIN" if pnl >= 0 else "VERLIES"
    emoji = "✅" if pnl >= 0 else "❌"
    tg(
        f"{emoji} <b>{label}  {symbol}  {direction}</b>\n"
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
    news_events:       list  = field(default_factory=list)  # UTC-tijden high-impact events
    news_fetched_at:   float = 0.0
    news_warned:       bool  = False
    news_flat_done:    set   = field(default_factory=set)   # events waarvoor al geflattened

    # Payout tracking
    day_pnl_hist:      dict  = field(default_factory=dict)  # "YYYY-MM-DD" → dag-P&L (€)
    last_close_date:   str   = ""      # laatste gesloten trade (inactivity-regel 30d)
    friday_flat_done:  bool  = False

    # Trade tracking
    open_tickets:      dict  = field(default_factory=dict)  # ticket → info dict
    sleeve_done:       set   = field(default_factory=set)   # (sleeve, symbool, "YYYY-MM-DD")
    trades_today:      int   = 0
    daily_report_sent: bool  = False

    # Telegram
    tg_last_update_id: int   = 0
    bot_paused:        bool  = False

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
    for sym in SYMBOLS:
        mt5.symbol_select(sym, True)
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

def pip_val(symbol: str) -> float:
    """Accountvaluta (EUR) per pip per lot — via MT5 tick value."""
    info = mt5.symbol_info(symbol)
    if info and info.trade_tick_size > 0:
        return info.trade_tick_value * (pip_size(symbol) / info.trade_tick_size)
    return 10.0

def calc_lot(symbol: str, sl_dist: float, risk_eur: float) -> float:
    """Lot-sizing: risico (SL-afstand + slippage-buffer + commissie) blijft
    ≤ min(risk_eur, harde cap), ook als de fill DEVIATION slechter is.
    Rondt af naar BENEDEN; 0.0 = trade skippen (risico wordt nooit opgerekt)."""
    pv = pip_val(symbol)
    sl_pips = sl_dist / pip_size(symbol)
    if sl_pips <= 0 or pv <= 0:
        return 0.0
    lot  = min(risk_eur, RISK_EUR_MAX) / ((sl_pips + SLIPPAGE_PIPS) * pv + COMMISSION)
    info = mt5.symbol_info(symbol)
    if info is None:
        return 0.0
    if info.volume_step > 0:
        lot = math.floor(lot / info.volume_step + 1e-9) * info.volume_step
    lot = min(lot, info.volume_max)
    if lot <= 0 or lot < info.volume_min:
        return 0.0
    return round(lot, 2)

def trade_risk_eur(symbol: str, sl_dist: float, lot: float) -> float:
    """Risico in € van een order: SL-afstand + slippage-buffer + commissie —
    dezelfde formule als calc_lot, zodat sizing en pre-trade check niet
    kunnen divergeren."""
    return (sl_dist / pip_size(symbol) + SLIPPAGE_PIPS) * pip_val(symbol) * lot + COMMISSION * lot

def get_filling_mode(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
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
    # Maand-anker terugzetten zodat een herstart de circuit breaker niet reset
    now = datetime.now(tz=timezone.utc)
    if d.get("month") == f"{now.year}-{now.month:02d}":
        STATE.current_month       = (now.year, now.month)
        STATE.month_start_balance = float(d.get("month_start_balance", 0.0))
        STATE.monthly_cb_hit      = bool(d.get("monthly_cb_hit", False))
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
                "equity_high":         STATE.equity_high,
                "day_pnl_hist":        STATE.day_pnl_hist,
                "last_close_date":     STATE.last_close_date,
                "day_date":            STATE.current_day.isoformat(),
                "day_start_balance":   STATE.day_start_balance,
                "day_start_anchor":    STATE.day_start_anchor,
                "daily_guard_hit":     STATE.daily_guard_hit,
                "month":               f"{STATE.current_month[0]}-{STATE.current_month[1]:02d}",
                "month_start_balance": STATE.month_start_balance,
                "monthly_cb_hit":      STATE.monthly_cb_hit,
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
#  NEWS FILTER (high-impact GBP/USD/EUR — ForexFactory kalender)
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
            events.append((t, ev["country"]))
        STATE.news_events     = sorted(events)
        STATE.news_fetched_at = time.time()
        STATE.news_warned     = False
        log.info("Newskalender ververst — %d high-impact %s events deze week",
                 len(events), "/".join(NEWS_CCY))
    except Exception as exc:
        log.warning("Newskalender ophalen mislukt: %s", exc)

def news_data_ok() -> bool:
    return (time.time() - STATE.news_fetched_at) < NEWS_MAX_AGE_SEC

def _ccy_matches(ccy: str, symbol: Optional[str]) -> bool:
    """Raakt een event-valuta dit symbool? (symbol=None: elk symbool)"""
    return symbol is None or ccy in (symbol[:3], symbol[3:])

def news_event_between(a: datetime, b: datetime, symbol: Optional[str] = None) -> bool:
    """Valt er een high-impact event op dit symbool in [a, b]?"""
    return any(a <= t <= b and _ccy_matches(c, symbol) for t, c in STATE.news_events)

def in_news_window(now: Optional[datetime] = None, symbol: Optional[str] = None) -> bool:
    now = now or datetime.now(tz=timezone.utc)
    return news_event_between(now - timedelta(minutes=NEWS_BLOCK_AFTER),
                              now + timedelta(minutes=NEWS_BLOCK_BEFORE), symbol)

def next_news_flatten(symbol: Optional[str] = None) -> Optional[datetime]:
    """Eerstvolgende event op dit symbool waarvoor open posities dicht moeten
    (binnen NEWS_FLATTEN_BEFORE min)."""
    now = datetime.now(tz=timezone.utc)
    for t, c in STATE.news_events:
        if timedelta(0) <= (t - now) <= timedelta(minutes=NEWS_FLATTEN_BEFORE) \
                and _ccy_matches(c, symbol):
            return t
    return None

# ===========================================================================
#  ORDERS
# ===========================================================================

# Retcodes waarbij de order zeker NIET is uitgevoerd — alleen dan is een
# retry veilig. Bij alle andere uitkomsten (timeout, partial, onbekend) kan de
# order tóch gevuld zijn: dan eerst de positie zoeken in plaats van opnieuw
# sturen (voorkomt dubbele posities).
_RETRY_SAFE_RETCODES = {
    getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004),
    getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10020),
    getattr(mt5, "TRADE_RETCODE_PRICE_OFF", 10021),
    getattr(mt5, "TRADE_RETCODE_TOO_MANY_REQUESTS", 10024),
    getattr(mt5, "TRADE_RETCODE_NO_QUOTES", 10030),
}

def _register_open(ticket: int, symbol: str, strat: str, direction: str,
                   entry: float, sl: float, lot: float, exit_dt: Optional[datetime]):
    STATE.open_tickets[ticket] = {
        "symbol":    symbol,
        "strat":     strat,
        "direction": direction,
        "entry":     entry,
        "exit_dt":   exit_dt.isoformat() if exit_dt else "",
    }
    STATE.trades_today += 1
    log.info("ORDER  %s %s %s  lot=%.2f  entry=%.5f  sl=%.5f  exit=%s  #%d",
             strat, direction, symbol, lot, entry, sl,
             exit_dt.strftime("%H:%M") if exit_dt else "-", ticket)
    if exit_dt:
        tg_open(symbol, strat, direction, lot, entry, sl, exit_dt)

def _find_unregistered_position(symbol: str) -> Optional[object]:
    """Bot-positie op dit symbool die nog niet in open_tickets staat (bv. na
    een ambigue order-uitkomst zoals timeout of partial fill)."""
    for pos in open_positions(symbol):
        if pos.magic == MAGIC and pos.ticket not in STATE.open_tickets:
            return pos
    return None

def send_order(symbol: str, order_type: int, sl_dist: float, lot: float,
               strat: str, exit_dt: Optional[datetime] = None) -> Optional[int]:
    """Marktorder met SL op sl_dist van de actuele prijs (geen TP; tijd-exit).
    De SL wordt hier — vlak vóór verzenden — van een verse tick afgeleid zodat
    de SL-afstand klopt, ook als de prijs sinds de signaalcheck bewoog."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return None
    direction = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"

    for attempt in range(1, 3):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            time.sleep(2); continue
        price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
        sl = price - sl_dist if order_type == mt5.ORDER_TYPE_BUY else price + sl_dist
        dg = info.digits
        req = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       lot,
            "type":         order_type,
            "price":        round(price, dg),
            "sl":           round(sl, dg),
            "tp":           0.0,
            "deviation":    DEVIATION,
            "magic":        MAGIC,
            "comment":      f"{COMMENT}_{strat}",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": get_filling_mode(symbol),
        }
        result = mt5.order_send(req)

        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            fill = result.price if result.price > 0 else price
            _register_open(result.order, symbol, strat, direction, fill, sl, lot, exit_dt)
            return result.order

        retcode = result.retcode if result is not None else -1
        log.error("order_send  retcode=%s  %s  attempt=%d", retcode,
                  result.comment if result is not None else mt5.last_error(), attempt)

        if retcode not in _RETRY_SAFE_RETCODES:
            # Ambigue uitkomst (timeout/partial/onbekend): order kan gevuld
            # zijn — positie zoeken en adopteren in plaats van opnieuw sturen.
            time.sleep(2)
            pos = _find_unregistered_position(symbol)
            if pos is not None:
                log.warning("Order-uitkomst ambigu maar positie #%d gevonden — geadopteerd", pos.ticket)
                _register_open(pos.ticket, symbol, strat, direction,
                               pos.price_open, pos.sl, pos.volume, exit_dt)
                return pos.ticket
            return None
        time.sleep(2)
    return None

def _close_position(pos) -> bool:
    """Sluit één positie tegen marktprijs (3 pogingen)."""
    info = mt5.symbol_info(pos.symbol)
    if info is None:
        return False
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
            "comment":      "BOT_CLOSE",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": get_filling_mode(pos.symbol),
        }
        r = mt5.order_send(req)
        if r and r.retcode == mt5.TRADE_RETCODE_DONE:
            log.info("CLOSE  #%d  %s  OK", pos.ticket, pos.symbol)
            return True
        log.error("CLOSE  #%d  poging %d mislukt", pos.ticket, attempt)
        time.sleep(2)
    return False

def close_ticket(ticket: int) -> bool:
    pos_list = mt5.positions_get(ticket=ticket)
    if not pos_list:
        return True   # al dicht
    return _close_position(pos_list[0])

def close_all_positions():
    positions = open_positions()
    if not positions:
        return
    log.warning("Alle posities sluiten (%d open)...", len(positions))
    for pos in positions:
        _close_position(pos)

# ===========================================================================
#  PRE-TRADE COMPLIANCE CHECK (FundingPips Zero)
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

def pretrade_ok(symbol: str, new_risk_eur: float = RISK_EUR_MAX) -> tuple[bool, str]:
    """Volledige checklist vóór elke order — bij twijfel: skip."""
    now = datetime.now(tz=timezone.utc)

    if STATE.fp_halt:
        return False, "FP-halt actief (trailing vloer / dag-breach)"
    if STATE.daily_guard_hit:
        return False, "dagstop bereikt — vandaag geen trades meer"
    if STATE.monthly_cb_hit:
        return False, "maand circuit-breaker actief"
    if now.weekday() >= 5:
        return False, "weekend"
    if now.weekday() == 4 and now.hour >= FRIDAY_LAST_ENTRY_H:
        return False, "vrijdag — geen nieuwe trades meer"

    positions = open_positions()
    if len(positions) >= MAX_TOTAL_POS:
        return False, f"al {MAX_TOTAL_POS} posities open"
    if sum(1 for p in positions if p.symbol == symbol) >= MAX_PER_SYMBOL:
        return False, "symbool al open (max 1 per symbool)"

    if not news_data_ok():
        if not STATE.news_warned:
            STATE.news_warned = True
            tg("⚠️ <b>Newskalender niet beschikbaar</b>\nNieuwe trades geblokkeerd (fail-safe) tot de kalender weer laadt.")
        return False, "geen actuele newsdata (fail-safe)"
    if in_news_window(now, symbol) or next_news_flatten(symbol) is not None:
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

    # News guard: flatten vóórdat het 10-min venster ingaat — alleen de
    # posities op symbolen die het event raakt
    for pos in positions:
        ev = next_news_flatten(pos.symbol)
        if ev is None:
            continue
        key = (ev.isoformat(), pos.symbol)
        if key in STATE.news_flat_done:
            continue
        STATE.news_flat_done.add(key)
        log.warning("NEWS GUARD — high-impact event %s UTC raakt %s: positie dicht",
                    ev.strftime("%H:%M"), pos.symbol)
        tg(f"📰 <b>News guard</b>\n{_SEP}\nHigh-impact news om {ev.strftime('%H:%M')} UTC — "
           f"{pos.symbol} vooraf gesloten.")
        _close_position(pos)

# ===========================================================================
#  FUNDINGPIPS GUARDS (doorlopend)
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
        save_fp_state()

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
        STATE.trades_today       = 0
        STATE.daily_report_sent  = False
        STATE.friday_flat_done   = False
        STATE.news_flat_done.clear()
        # sleeve-registratie van oude dagen opruimen
        cutoff_iso = (today - timedelta(days=2)).isoformat()
        STATE.sleeve_done = {k for k in STATE.sleeve_done if k[2] >= cutoff_iso}
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
        log.info("TRADE GESLOTEN  #%d  %s %s %s  pnl=%.2f",
                 ticket, info["direction"], info.get("symbol", "?"), info["strat"], pnl)
        STATE.last_close_date = datetime.now(tz=timezone.utc).date().isoformat()
        save_fp_state()
        tg_close(info.get("symbol", "?"), info["direction"], pnl,
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
#  STRATEGIE — NY Flow + Monday
# ===========================================================================

def _sleeve_times(sv: Sleeve, now: datetime) -> tuple[datetime, datetime]:
    entry_dt = now.replace(hour=sv.entry_h, minute=sv.entry_m, second=0, microsecond=0)
    exit_dt  = now.replace(hour=sv.exit_h,  minute=sv.exit_m,  second=0, microsecond=0)
    return entry_dt, exit_dt

def check_strategy():
    """Sleeve-entries: BUY op vaste UTC-tijden, exit op vaste tijd of SL."""
    now = datetime.now(tz=timezone.utc)
    today_iso = now.date().isoformat()

    for sv in SLEEVES:
        if now.weekday() not in sv.weekdays:
            continue
        entry_dt, exit_dt = _sleeve_times(sv, now)
        if not (entry_dt <= now < entry_dt + timedelta(minutes=ENTRY_GRACE_MIN)):
            continue

        for symbol in sv.symbols:
            key = (sv.name, symbol, today_iso)
            if key in STATE.sleeve_done:
                continue
            # één poging per sleeve per dag — ongeacht de uitkomst (zoals backtest)
            STATE.sleeve_done.add(key)

            # korte vensters (houd-tijd <= ~2u): skip als er news op dit symbool
            # in het houd-venster valt (anders kapt de news-flatten de trade na
            # een paar minuten af); lange holds (MON/MONA) vertrouwen op de
            # news-flatten-guard zelf
            if (exit_dt - now <= timedelta(hours=2, minutes=5)
                    and news_event_between(now, exit_dt + timedelta(minutes=NEWS_BLOCK_AFTER),
                                           symbol)):
                log.info("SKIP  %s %s — high-impact event in houd-venster", sv.name, symbol)
                continue

            sl_dist = sv.sl_pips * pip_size(symbol)
            lot     = calc_lot(symbol, sl_dist, sv.risk_eur)
            if lot <= 0:
                log.info("SKIP  %s %s — lot=0", sv.name, symbol)
                continue

            risk = trade_risk_eur(symbol, sl_dist, lot)
            ok, reden = pretrade_ok(symbol, risk)
            if not ok:
                log.info("SKIP  %s %s — %s", sv.name, symbol, reden)
                continue

            order_type = (mt5.ORDER_TYPE_BUY if sv.direction == "BUY"
                          else mt5.ORDER_TYPE_SELL)
            send_order(symbol, order_type, sl_dist, lot, sv.name, exit_dt)

def manage_time_exits():
    """Sluit sleeve-posities zodra hun exit-tijd bereikt is."""
    if not STATE.open_tickets:
        return
    now = datetime.now(tz=timezone.utc)
    for ticket, info in list(STATE.open_tickets.items()):
        exit_iso = info.get("exit_dt", "")
        if not exit_iso:
            continue
        try:
            exit_dt = datetime.fromisoformat(exit_iso)
        except ValueError:
            continue
        if now >= exit_dt:
            log.info("TIJD-EXIT  #%d  %s %s", ticket, info["strat"], info.get("symbol", ""))
            close_ticket(ticket)

def server_utc_offset() -> timedelta:
    """MT5 geeft tijden als broker-servertijd-epochs (vaak UTC+2/+3). Bepaal de
    offset dynamisch via een verse tick; buiten markturen (geen verse tick) is
    de meting onbetrouwbaar en vallen we terug op 0."""
    now_ts = datetime.now(tz=timezone.utc).timestamp()
    for sym in SYMBOLS:
        tick = mt5.symbol_info_tick(sym)
        if tick and tick.time > 0:
            off = tick.time - now_ts
            if abs(off) <= 12 * 3600:
                return timedelta(seconds=round(off / 1800) * 1800)
    return timedelta(0)

def recover_open_state():
    """Na (her)start: eigen open posities heradopteren en sleeves die vandaag
    al gedraaid hebben markeren, zodat er geen dubbele entries komen."""
    now = datetime.now(tz=timezone.utc)
    today_iso = now.date().isoformat()
    by_name = {sv.name: sv for sv in SLEEVES}
    srv_off = server_utc_offset()

    for pos in open_positions():
        if pos.magic != MAGIC:
            continue
        strat = pos.comment.replace(COMMENT + "_", "", 1) if pos.comment else ""
        sv = by_name.get(strat)
        if sv is None:
            log.warning("Onbekende bot-positie #%d (%s) — wordt gesloten (compliance)",
                        pos.ticket, pos.comment)
            _close_position(pos)
            continue
        # pos.time is servertijd-epoch → naar echte UTC corrigeren
        opened = datetime.fromtimestamp(pos.time, tz=timezone.utc) - srv_off
        exit_dt = opened.replace(hour=sv.exit_h, minute=sv.exit_m,
                                 second=0, microsecond=0)
        STATE.open_tickets[pos.ticket] = {
            "symbol":    pos.symbol,
            "strat":     strat,
            "direction": "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
            "entry":     pos.price_open,
            "exit_dt":   exit_dt.isoformat(),
        }
        STATE.sleeve_done.add((strat, pos.symbol, opened.date().isoformat()))
        log.info("HERADOPTEERD  #%d  %s %s  exit=%s",
                 pos.ticket, strat, pos.symbol, exit_dt.strftime("%H:%M"))

    # sleeves die vandaag al een deal hadden (positie inmiddels dicht);
    # queryvenster in servertijd, met marge voor de offset-afronding
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    deals = mt5.history_deals_get(day_start + srv_off - timedelta(hours=1),
                                  now + srv_off + timedelta(hours=1)) or []
    for d in deals:
        if d.magic != MAGIC or not d.comment.startswith(COMMENT + "_"):
            continue
        strat = d.comment.replace(COMMENT + "_", "", 1)
        if strat in by_name:
            STATE.sleeve_done.add((strat, d.symbol, today_iso))

# ===========================================================================
#  TELEGRAM COMMANDO'S
# ===========================================================================

_tg_rate = {"last": 0.0, "blocked": 0}
# Commando's worden door de poll-thread alleen GEQUEUED en door de hoofdloop
# uitgevoerd: de MetaTrader5-API is niet thread-safe, dus alle MT5-calls
# blijven in één thread.
_TG_CMD_QUEUE: "queue.Queue[str]" = queue.Queue()

def _tg_poll_updates():
    """Draait in de achtergrond-thread: berichten ophalen, valideren, queuen."""
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

            _TG_CMD_QUEUE.put(text)
    except Exception as exc:
        log.warning("Telegram poll fout: %s", exc)

def process_tg_queue():
    """Draait in de hoofdloop: gequeueude commando's uitvoeren (met MT5-toegang)."""
    while True:
        try:
            text = _TG_CMD_QUEUE.get_nowait()
        except queue.Empty:
            return
        try:
            _handle_command(text)
        except Exception as exc:
            log.exception("Telegram commando '%s' fout: %s", text, exc)

def _handle_command(text: str):
    if text == "/status":
        bal      = get_balance()
        eq       = get_equity()
        pct      = (bal - STATE.start_balance) / STATE.start_balance * 100 if STATE.start_balance > 0 else 0
        day_loss = STATE.day_start_anchor - eq if STATE.day_start_anchor > 0 else 0.0
        paused   = "⏸ GEPAUZEERD" if STATE.bot_paused else "▶️ ACTIEF"
        pos_list = open_positions()
        pos_lines = ""
        open_risk = 0.0
        for p in pos_list:
            ico = "🟢" if p.profit >= 0 else "🔴"
            d   = "SELL" if p.type == mt5.ORDER_TYPE_SELL else "BUY"
            strat = STATE.open_tickets.get(p.ticket, {}).get("strat", "?")
            pos_lines += f"\n  {ico} {d} {p.symbol} [{strat}]  €{p.profit:+,.0f}"
            open_risk += position_risk_eur(p)
        floor = STATE.equity_high - FP_TRAIL_EUR
        warn  = ""
        if STATE.fp_halt:         warn += "\n🚨 FP-HALT actief — handmatige controle vereist!"
        if STATE.daily_guard_hit: warn += "\n🛑 Dagstop bereikt — vandaag geen trades meer"
        if STATE.monthly_cb_hit:  warn += "\n⚡ Circuit breaker (maand) actief"
        if not news_data_ok():    warn += "\n📰 Newsdata verouderd — entries geblokkeerd (fail-safe)"
        tg(
            f"📡 <b>FP ZERO BOT STATUS</b>  {paused}\n"
            f"{_SEP}\n"
            f"💼 Balans     €{bal:,.0f}   ({pct:+.1f}%)\n"
            f"⚖️ Equity     €{eq:,.0f}\n"
            f"📅 Dagverlies €{day_loss:,.0f} / stop €{DAY_STOP_EUR:,.0f}\n"
            f"🧱 Vloer      €{floor:,.0f}   (marge €{eq - floor:,.0f})\n"
            f"🛡 Open risk  €{open_risk:,.0f} / cap €{OPEN_RISK_CAP_EUR:,.0f}\n"
            f"📈 Winstdagen 30d: {profitable_days_30d()}/{PROFIT_DAYS_MIN}\n"
            f"{_SEP}\n"
            f"📂 Posities  {len(pos_list)} open{pos_lines}\n"
            f"🏦 {' + '.join(SYMBOLS)}  [NY Flow + Monday]\n"
            f"🔢 Trades vandaag: {STATE.trades_today}"
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

    elif text == "/help":
        tg(
            f"📋 <b>COMMANDO'S  FP Zero Bot</b>\n"
            f"{_SEP}\n"
            f"📡 /status   balans &amp; posities\n"
            f"⏸ /pause    nieuwe trades stoppen\n"
            f"▶️  /resume   trading hervatten\n"
            f"🔒 /close    alle posities sluiten\n"
            f"📋 /help     dit menu\n"
            f"{_SEP}\n"
            f"🎯 <b>STRATEGIE  NY Flow + Monday</b>\n"
            + "".join(f"{sv.name}  {sv.direction} {'+'.join(s[:3] for s in sv.symbols)} "
                      f"{sv.entry_h:02d}-{sv.exit_h:02d}u (SL {sv.sl_pips:.0f}p, €{sv.risk_eur:.0f})\n"
                      for sv in SLEEVES) +
            f"{_SEP}\n"
            f"🔒 <b>FUNDINGPIPS ZERO LIMIETEN</b>\n"
            f"🎯 Trade    max <b>€{RISK_EUR_MAX:,.0f}</b> risk (hard cap)\n"
            f"📅 Dagstop  <b>-€{DAY_STOP_EUR:,.0f}</b>  (breach: -€{FP_DAY_BREACH_EUR:,.0f})\n"
            f"🧱 Trailing <b>-€{FP_TRAIL_EUR:,.0f}</b> vanaf equity-high\n"
            f"🛡 Open risk max <b>€{OPEN_RISK_CAP_EUR:,.0f}</b> pre-trade\n"
            f"📰 News ±10 min  |  🛑 vrijdag weekend-flatten\n"
            f"📆 Maand    max <b>-{MONTHLY_CB_PCT*100:.0f}%</b> (circuit breaker)"
        )

def _tg_command_loop():
    while True:
        try:
            _tg_poll_updates()
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
    for sym in SYMBOLS:
        if any(x in sym.upper() for x in ("XAU", "GOLD")):
            log.error("Symbool %s is goud — uitgesloten per compliance-config (alleen FX)", sym)
            release_lock()
            sys.exit(1)

    log.info("=" * 60)
    log.info("FundingPips Zero Bot — strategie NY Flow + Monday")
    log.info("Account     : FundingPips Zero €160K  |  symbolen %s", " + ".join(SYMBOLS))
    for sv in SLEEVES:
        log.info("Sleeve %-5s: %s %s %02d:%02d->%02d:%02d  SL %.0fp  risk €%.0f  [%s]",
                 sv.name, sv.direction, "+".join(sv.symbols),
                 sv.entry_h, sv.entry_m, sv.exit_h, sv.exit_m,
                 sv.sl_pips, sv.risk_eur,
                 "".join("mdwdv"[d] for d in sv.weekdays))
    log.info("Risico      : max €%.0f/trade (hard cap)", RISK_EUR_MAX)
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
    recover_open_state()
    save_fp_state()

    threading.Thread(target=_tg_command_loop, daemon=True).start()

    tg(
        f"🚀 <b>FP ZERO BOT GESTART</b>\n"
        f"{_SEP}\n"
        f"💼 Balans    €{bal:,.0f}\n"
        f"🏦 Symbolen  {' + '.join(SYMBOLS)}\n"
        f"🎯 Strategie NY Flow + Monday (tijd-exits)\n"
        f"🛡 Risk      max €{RISK_EUR_MAX:,.0f}/trade  |  dagstop -€{DAY_STOP_EUR:,.0f}\n"
        f"📰 News ±10 min  |  🛑 weekend-flatten\n"
        f"{_SEP}\n"
        f"Typ /help voor commando's"
    )

    try:
        while True:
            write_heartbeat()
            if not ensure_connected():
                log.error("Verbinding permanent verloren — bot stopt")
                tg_status("VERBINDING VERLOREN — herstart vereist!")
                break

            try:
                process_tg_queue()
            except Exception as exc:
                log.exception("process_tg_queue fout: %s", exc)

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
                manage_time_exits()
            except Exception as exc:
                log.exception("manage_time_exits fout: %s", exc)

            if not STATE.bot_paused and check_guards():
                try:
                    check_strategy()
                except Exception as exc:
                    log.exception("check_strategy fout: %s", exc)
                    tg_status(f"Strategie fout: {exc}")

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
