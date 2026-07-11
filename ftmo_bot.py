"""
FTMO Trading Bot — productie versie
Symbolen  : XAUUSD, EURUSD, GBPUSD, USDJPY, BTCUSD
Strategie : H1 EMA trendfilter + ADX + M15 EMA50 pullback + twee-candle bevestiging
SL/TP     : ATR(14) x1.5, R:R 2.0
Config    : bot_config.ini  (MT5 credentials + modus)
"""

from __future__ import annotations

import sys as _sys, os as _os
# Alleen venv Python mag draaien (launcher spawnt interpreter met PYTHONHOME → prefix != base_prefix)
if _sys.prefix == _sys.base_prefix:
    _os._exit(0)

import configparser
import logging
import logging.handlers
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, date
from typing import Optional

import MetaTrader5 as mt5
import numpy as np
import pandas as pd
import requests
import threading

import learning

# ===========================================================================
#  CONFIG LADEN  (bot_config.ini)
# ===========================================================================

_ini = configparser.ConfigParser()
_ini_path = os.path.join(os.path.dirname(__file__), "bot_config.ini")
if not _ini.read(_ini_path, encoding="utf-8-sig"):   # utf-8-sig stript BOM automatisch
    print(f"FOUT: bot_config.ini niet gevonden op {_ini_path}")
    sys.exit(1)

MT5_LOGIN    = int(_ini["MT5"]["login"])
MT5_PASSWORD = _ini["MT5"]["password"]
MT5_SERVER   = _ini["MT5"]["server"]
MODUS        = _ini["BOT"].get("modus", "AGRESSIEF").strip().upper()
TG_TOKEN     = _ini["TELEGRAM"].get("token", "").strip()
TG_CHAT_ID   = _ini["TELEGRAM"].get("chat_id", "").strip()

if MODUS not in ("AGRESSIEF", "VEILIG"):
    print(f"FOUT: modus moet AGRESSIEF of VEILIG zijn, niet '{MODUS}'")
    sys.exit(1)

# ===========================================================================
#  CONFIGURATIES
# ===========================================================================

_CONFIGS = {
    "AGRESSIEF": {
        "symbols":            ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD"],
        "session_per_sym":    {
            "XAUUSD": (7, 20),
            "EURUSD": (7, 20),
            "GBPUSD": (7, 20),
            "USDJPY": (0, 12),
            "BTCUSD": (0, 23),  # BTC handelt 24/7
        },
        "h1_ema_fast":        50,
        "h1_ema_slow":        200,
        "h1_adx_period":      14,
        "h1_adx_min":         28.0,
        "h1_ema_momentum":    20,
        "h1_slope_bars":      3,
        "m15_ema":            50,
        "m15_rsi_period":     14,
        "rsi_buy_lo":         45.0,
        "rsi_buy_hi":         75.0,
        "rsi_sell_lo":        25.0,
        "rsi_sell_hi":        55.0,
        "ema_slope_bars":     2,
        "body_pct_min":       0.55,
        "atr_period":         14,
        "atr_sl_mult":        1.5,
        "rr_ratio":           2.5,
        "adx_min_per_sym":    {"XAUUSD": 32.0, "BTCUSD": 25.0},
        "adx_slope_bars":     2,
        "adx_slope_syms":     ["XAUUSD"],  # BTC geen slope filter (te beperkend)
        "adx_boost_min":      45.0,
        "adx_boost_mult":     2.0,
        "rr_weak_adx":        0.0,
        "rr_ratio_weak":      1.5,
        "ml_min_prob":        0.0,    # 0 = uit; 0.45 = aanbevolen na 50+ trades
        "breakeven_r":        1.0,    # SL naar entry na 1R winst
        "trail_start_r":      2.0,    # trailing start na 2R winst
        "trail_dist_r":       1.5,    # trail op 1.5R achter huidige prijs
        "max_trades_per_day": 10,     # max entries per dag (niet guards-stops)
        "risk_pct_per_sym":   {"XAUUSD": 2.0, "EURUSD": 1.5, "GBPUSD": 1.5, "USDJPY": 0.8, "BTCUSD": 1.0},
        "risk_pct_default":   0.5,
        "max_open_per_sym":   2,
        "daily_loss_limit":   0.04,
        "weekly_loss_limit":  0.03,
        "max_drawdown_limit": 0.09,
        "challenge_target":   0.10,
        "verify_target":      0.15,
    },
    "VEILIG": {
        "symbols":            ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD"],
        "session_per_sym":    {
            "XAUUSD": (7, 20),
            "EURUSD": (7, 20),
            "GBPUSD": (7, 20),
            "USDJPY": (0, 12),
            "BTCUSD": (0, 23),
        },
        "h1_ema_fast":        50,
        "h1_ema_slow":        200,
        "h1_adx_period":      14,
        "h1_adx_min":         30.0,
        "h1_ema_momentum":    20,
        "h1_slope_bars":      3,
        "m15_ema":            50,
        "m15_rsi_period":     14,
        "rsi_buy_lo":         45.0,
        "rsi_buy_hi":         75.0,
        "rsi_sell_lo":        25.0,
        "rsi_sell_hi":        55.0,
        "ema_slope_bars":     2,
        "body_pct_min":       0.55,
        "atr_period":         14,
        "atr_sl_mult":        1.5,
        "rr_ratio":           2.0,
        "adx_min_per_sym":    {"XAUUSD": 35.0, "BTCUSD": 25.0},
        "adx_slope_bars":     3,
        "adx_slope_syms":     ["XAUUSD"],  # BTC geen slope filter
        "adx_boost_min":      0.0,
        "adx_boost_mult":     1.0,
        "rr_weak_adx":        0.0,
        "rr_ratio_weak":      1.5,
        "ml_min_prob":        0.0,
        "risk_pct_per_sym":   {"XAUUSD": 0.9, "EURUSD": 0.3, "GBPUSD": 0.3, "USDJPY": 0.2, "BTCUSD": 0.5},
        "risk_pct_default":   0.2,
        "max_open_per_sym":   2,
        "daily_loss_limit":   0.03,
        "weekly_loss_limit":  0.015,
        "max_drawdown_limit": 0.08,
        "challenge_target":   0.0,
        "verify_target":      0.0,
    },
}

class C:
    pass

for _k, _v in _CONFIGS[MODUS].items():
    setattr(C, _k, _v)

# ===========================================================================
#  CONSTANTEN
# ===========================================================================

SYMBOL_CURRENCIES = {
    "XAUUSD": ["USD"],
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"],
    "BTCUSD": ["USD"],  # BTC priced in USD; FOMC/CPI events affect it
}

# Max spread als fractie van M15 ATR — voorkomt entries bij wyide spreads
MAX_SPREAD_ATR_RATIO = 0.25  # max 25% van ATR (FTMO normaal spreads ~10-15%)

# Throttle voor blokkade-meldingen: max 1x per 30 min per symbool per type
_blokkade_throttle: dict = {}  # key: (symbol, type) -> laatste melding timestamp

def _tg_blokkade(symbol: str, btype: str, msg: str):
    """Stuur Telegram blokkade-melding max 1x per 30 min per symbool+type."""
    from datetime import datetime, timezone
    key = (symbol, btype)
    now = datetime.now(tz=timezone.utc).timestamp()
    if now - _blokkade_throttle.get(key, 0) < 1800:
        return
    _blokkade_throttle[key] = now
    tg_status(msg)

# Hard maximale lot per symbool — bescherming tegen gap-risico
MAX_LOT_PER_SYM = {
    "BTCUSD": 1.0,  # max 1 BTC per trade; gap van $5k = $5k verlies max
}

H1_BARS         = 450
M15_BARS        = 200
POLL_SEC        = 15
NEWS_BUFFER_MIN = 30
NEWS_CACHE_MIN  = 60
FF_URL          = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
MAGIC           = 20260101
COMMENT         = f"FTMO_{MODUS}"
DEVIATION       = 20
MAX_RECONNECTS  = 10
RECONNECT_WAIT  = 15
HEARTBEAT_FILE  = os.path.join(os.path.dirname(__file__), "bot_heartbeat.txt")
START_BAL_FILE  = os.path.join(os.path.dirname(__file__), "bot_start_balance.txt")

# FTMO absolute limieten — altijd vast, ongeacht groeiende balans
# Bot stopt op $7.500 (niet $8.000) zodat slippage bij sluiten nooit de FTMO grens van $8.000 overschrijdt
FTMO_DAILY_LOSS_ABS = 7_500.0   # bot-stop: $7.500/dag  (FTMO grens = $8.000 — $500 buffer voor slippage)
FTMO_MAX_DD_ABS     = 14_000.0  # stop bij $14k totaal verlies (veiligheidsmarge voor $16k FTMO grens)

# MT5 terminal pad — zodat Python het zelf kan opstarten (voorkomt IPC sessie-isolatie probleem)
MT5_TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

# ===========================================================================
#  LOGGING  (roterende bestanden, max 10MB x 5)
# ===========================================================================

_log_file = os.path.join(os.path.dirname(__file__), "ftmo_bot.log")
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
log = logging.getLogger("ftmo_bot")

# ===========================================================================
#  TELEGRAM
# ===========================================================================

def tg(msg: str, silent: bool = False):
    """Stuur een Telegram bericht. Mislukt stilletjes als token niet ingesteld."""
    if not TG_TOKEN or not TG_CHAT_ID or TG_TOKEN.startswith("123456"):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id":              TG_CHAT_ID,
                "text":                 msg,
                "parse_mode":           "HTML",
                "disable_notification": silent,
            },
            timeout=8,
        )
    except Exception as exc:
        log.warning("Telegram fout: %s", exc)

_SEP = "─────────────────────"

def tg_trade_open(direction: str, symbol: str, lot: float,
                  entry: float, sl: float, tp: float, adx: float):
    arrow    = "🟢" if direction == "BUY" else "🔴"
    dir_word = "LONG" if direction == "BUY" else "SHORT"
    risk_pct = C.risk_pct_per_sym.get(symbol, C.risk_pct_default)
    balance  = get_balance()
    risk_usd = balance * risk_pct / 100
    sl_pips  = abs(entry - sl)
    tp_pips  = abs(tp - entry)
    boost    = "  ⚡ <b>BOOST</b>" if C.adx_boost_min > 0 and adx >= C.adx_boost_min else ""
    tg(
        f"{arrow} <b>{dir_word}  {symbol}</b>{boost}\n"
        f"{_SEP}\n"
        f"📍 Entry    <code>{entry:.2f}</code>\n"
        f"🛑 SL       <code>{sl:.2f}</code>   <i>(-${sl_pips * lot * 100:,.0f})</i>\n"
        f"🎯 TP       <code>{tp:.2f}</code>   <i>(+${tp_pips * lot * 100:,.0f})</i>\n"
        f"{_SEP}\n"
        f"📦 Lot      {lot:.2f}   ({risk_pct}%  =  ${risk_usd:,.0f})\n"
        f"📊 ADX      {adx:.1f}"
    )

def tg_trade_close(symbol: str, direction: str, pnl: float,
                   entry: float, close_price: float, balance: float):
    start_bal = STATE.start_balance if STATE.start_balance > 0 else balance
    total_pct = (balance - start_bal) / start_bal * 100
    day_pct   = (balance - STATE.day_start_balance) / STATE.day_start_balance * 100 if STATE.day_start_balance > 0 else 0
    dir_word  = "LONG" if direction == "BUY" else "SHORT"
    if pnl >= 0:
        emoji = "✅"; label = "WIN"
    else:
        emoji = "❌"; label = "VERLIES"
    day_icon  = "📈" if day_pct >= 0 else "📉"
    tg(
        f"{emoji} <b>{label}  {symbol}  {dir_word}</b>\n"
        f"{_SEP}\n"
        f"💰 P&amp;L     <b>${pnl:+,.0f}</b>\n"
        f"📉 Prijs    <code>{entry:.2f}</code> → <code>{close_price:.2f}</code>\n"
        f"{_SEP}\n"
        f"💼 Balans   ${balance:,.0f}   ({total_pct:+.1f}% totaal)\n"
        f"{day_icon} Vandaag   {day_pct:+.2f}%"
    )

def tg_guard(guard_type: str, loss_usd: float, loss_pct: float, equity_at_trigger: float):
    hervat = "morgen" if "DAG" in guard_type else "volgende week"
    tg(
        f"🚨 <b>{guard_type} BEREIKT</b>\n"
        f"{_SEP}\n"
        f"💸 Verlies   <b>-${loss_usd:,.0f}</b>   (-{loss_pct:.2f}%)\n"
        f"💼 Equity    ${equity_at_trigger:,.0f}\n"
        f"{_SEP}\n"
        f"✅ Alle posities gesloten\n"
        f"⏳ Trading hervat <b>{hervat}</b>"
    )

def tg_milestone(label: str, balance: float, pct: float):
    next_doel = "Verificatie (+15%)" if "CHALLENGE" in label.upper() else "Zet modus → VEILIG"
    tg(
        f"🏆 <b>{label}</b>\n"
        f"{_SEP}\n"
        f"💼 Balans    ${balance:,.0f}\n"
        f"📈 Winst     +{pct:.1f}%\n"
        f"{_SEP}\n"
        f"➡️ Volgende:  {next_doel}"
    )

def tg_daily_report(balance: float, day_pnl: float, week_pnl: float,
                    open_count: int, trades_today: int):
    start_bal = STATE.start_balance if STATE.start_balance > 0 else balance
    total_pct = (balance - start_bal) / start_bal * 100
    ch_target = start_bal * (1 + C.challenge_target) if C.challenge_target > 0 else 0
    ch_needed = max(0, ch_target - balance) if ch_target > 0 else 0
    dag_icon  = "📈" if day_pnl >= 0 else "📉"
    _now      = datetime.now(tz=timezone.utc)
    datum     = f"{_now.day} {_now.strftime('%b %Y')}"
    ch_line   = f"\n🎯 Challenge   nog ${ch_needed:,.0f} nodig" if ch_needed > 0 else ""
    tg(
        f"{dag_icon} <b>Dagrapport  {datum}</b>\n"
        f"{_SEP}\n"
        f"💼 Balans    ${balance:,.0f}   ({total_pct:+.1f}%)\n"
        f"📅 Dag P&amp;L   <b>${day_pnl:+,.0f}</b>\n"
        f"📆 Week P&amp;L  ${week_pnl:+,.0f}\n"
        f"{_SEP}\n"
        f"🔢 Trades    {trades_today} vandaag\n"
        f"📂 Open      {open_count} posities"
        f"{ch_line}"
    )

def tg_status(msg: str):
    tg(f"🤖 <b>BOT STATUS</b>\n{_SEP}\n{msg}")

# ===========================================================================
#  STATE
# ===========================================================================

class BotState:
    def __init__(self):
        self.start_balance:       float    = 0.0
        self.day_start_balance:   float    = 0.0
        self.week_start_balance:  float    = 0.0
        self.current_day:         date     = date.min
        self.current_week:        int      = -1
        self.daily_guard_hit:     bool     = False
        self.weekly_guard_hit:    bool     = False
        self.max_dd_guard_hit:    bool     = False
        self.challenge_logged:    bool     = False
        self.verify_logged:       bool     = False
        self.last_entry_candle:   dict     = {}
        self.news_cache:          list     = []
        self.news_cache_time:     Optional[datetime] = None
        self.reconnect_count:     int      = 0
        # Telegram trade tracking
        self.open_tickets:        dict     = {}  # ticket -> {sym, dir, entry, lot, signal_id}
        self.trades_today:        int      = 0
        self.daily_report_sent:   bool     = False
        # Optimizer
        self.last_optimizer_week: int      = -1
        self.last_week_report:    int      = -1
        # Telegram commando polling
        self.tg_last_update_id:   int      = 0
        self.bot_paused:          bool     = False

STATE = BotState()

# ===========================================================================
#  TELEGRAM COMMANDO THREAD  (altijd actief, ook bij MT5 disconnect)
# ===========================================================================

def _tg_command_loop():
    """Aparte thread: poll Telegram elke 3 seconden onafhankelijk van MT5."""
    while True:
        try:
            process_tg_commands()
        except Exception:
            pass
        time.sleep(3)

# ===========================================================================
#  MT5 VERBINDING + AUTO-RECONNECT
# ===========================================================================

def connect_mt5() -> bool:
    # path= zorgt dat Python zelf MT5 opstart als het niet draait — beide in dezelfde Windows sessie
    path_kwarg = {"path": MT5_TERMINAL_PATH} if os.path.exists(MT5_TERMINAL_PATH) else {}
    if not mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER, **path_kwarg):
        log.error("MT5 initialize() mislukt: %s", mt5.last_error())
        return False
    info = mt5.terminal_info()
    acc  = mt5.account_info()
    if info is None or acc is None:
        log.error("MT5 terminal of account info niet beschikbaar")
        mt5.shutdown()
        return False
    log.info("MT5 verbonden — build=%s  broker=%s  account=%d  balance=%.2f",
             info.build, acc.company, acc.login, acc.balance)
    for sym in C.symbols:
        if not mt5.symbol_select(sym, True):
            log.warning("symbol_select mislukt voor %s", sym)
    STATE.reconnect_count = 0
    return True

def is_connected() -> bool:
    info = mt5.terminal_info()
    return info is not None and info.connected

def ensure_connected() -> bool:
    if is_connected():
        return True
    log.warning("MT5 verbinding verloren — poging tot herverbinding...")
    mt5.shutdown()
    for attempt in range(1, MAX_RECONNECTS + 1):
        time.sleep(RECONNECT_WAIT)
        if connect_mt5():
            log.info("Herverbinding geslaagd na %d pogingen", attempt)
            STATE.reconnect_count += 1
            return True
        log.warning("Herverbinding %d/%d mislukt", attempt, MAX_RECONNECTS)
    log.error("Kon niet herverbinden na %d pogingen — bot stopt", MAX_RECONNECTS)
    return False

def shutdown_mt5():
    mt5.shutdown()
    log.info("MT5 verbinding gesloten.")

# ===========================================================================
#  HEARTBEAT
# ===========================================================================

def write_heartbeat():
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(datetime.now(tz=timezone.utc).isoformat())
    except OSError:
        pass

def load_start_balance() -> float:
    """Lees de persistente startbalans (overleeft herstart). Geeft 0.0 als niet gevonden."""
    try:
        with open(START_BAL_FILE, "r") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return 0.0

def save_start_balance(bal: float):
    try:
        with open(START_BAL_FILE, "w") as f:
            f.write(str(bal))
    except OSError:
        pass

# ===========================================================================
#  MT5 HELPERS
# ===========================================================================

def get_balance() -> float:
    acc = mt5.account_info()
    if acc is None:
        return 0.0
    return acc.balance

def get_equity() -> float:
    acc = mt5.account_info()
    if acc is None:
        return 0.0
    return acc.equity

def get_bars(symbol: str, timeframe: int, count: int) -> Optional[pd.DataFrame]:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        log.warning("Geen bars voor %s tf=%s", symbol, timeframe)
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df.set_index("time", inplace=True)
    return df

def open_positions(symbol: str = "") -> list:
    if symbol:
        return list(mt5.positions_get(symbol=symbol) or [])
    return list(mt5.positions_get() or [])

# ===========================================================================
#  INDICATOREN
# ===========================================================================

def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def rsi_ind(s: pd.Series, p: int) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(span=p, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(span=p, adjust=False).mean()
    return 100 - 100 / (1 + g / (l + 1e-9))

def adx_ind(df: pd.DataFrame, p: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pdm = (h - h.shift(1)).clip(lower=0)
    mdm = (l.shift(1) - l).clip(lower=0)
    pdm = pdm.where(pdm > mdm, 0.0)
    mdm = mdm.where(mdm > pdm.shift(0), 0.0)
    tr  = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    at  = tr.ewm(span=p, adjust=False).mean()
    pdi = 100 * pdm.ewm(span=p, adjust=False).mean() / at
    mdi = 100 * mdm.ewm(span=p, adjust=False).mean() / at
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.ewm(span=p, adjust=False).mean()

def atr_ind(df: pd.DataFrame, p: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(span=p, adjust=False).mean()

# ===========================================================================
#  H1 TRENDDETECTIE
# ===========================================================================

def get_h1_trend(symbol: str) -> Optional[tuple[str, float]]:
    df = get_bars(symbol, mt5.TIMEFRAME_H1, H1_BARS)
    if df is None or len(df) < C.h1_ema_slow + 20:
        return None

    ef    = ema(df["close"], C.h1_ema_fast)
    es    = ema(df["close"], C.h1_ema_slow)
    em    = ema(df["close"], C.h1_ema_momentum)
    adx_s = adx_ind(df, C.h1_adx_period)

    ef_cur  = ef.iloc[-2]
    es_cur  = es.iloc[-2]
    em_cur  = em.iloc[-2]
    ef_prev = ef.iloc[-2 - C.h1_slope_bars]
    adx_val = adx_s.iloc[-2]

    adx_min_sym = getattr(C, "adx_min_per_sym", {}).get(symbol, C.h1_adx_min)
    if adx_val < adx_min_sym:
        return None

    slope_bars = getattr(C, "adx_slope_bars", 0)
    slope_syms = getattr(C, "adx_slope_syms", [])
    if slope_bars > 0 and symbol in slope_syms:
        if len(adx_s) < slope_bars + 2:
            return None
        adx_old = adx_s.iloc[-2 - slope_bars]
        if adx_val <= adx_old:
            return None

    if ef_cur > es_cur:
        if em_cur <= ef_cur or ef_cur <= ef_prev:
            return None
        return ("up", adx_val)
    elif ef_cur < es_cur:
        if em_cur >= ef_cur or ef_cur >= ef_prev:
            return None
        return ("down", adx_val)
    return None

# ===========================================================================
#  M15 ENTRY-SIGNAAL
# ===========================================================================

def get_m15_signal(symbol: str, trend: str, adx_val: float) -> Optional[dict]:
    df = get_bars(symbol, mt5.TIMEFRAME_M15, M15_BARS)
    if df is None or len(df) < C.m15_ema + 20:
        return None

    df["ema50"] = ema(df["close"], C.m15_ema)
    df["rsi"]   = rsi_ind(df["close"], C.m15_rsi_period)
    df["atr"]   = atr_ind(df, C.atr_period)

    confirm  = df.iloc[-2]
    touch    = df.iloc[-3]
    candle_t = df.index[-2]

    if STATE.last_entry_candle.get(symbol) == candle_t:
        return None

    e_touch   = touch["ema50"]
    e_confirm = confirm["ema50"]
    e_prev    = df["ema50"].iloc[-2 - C.ema_slope_bars]
    rsi_v     = confirm["rsi"]
    atr_v     = confirm["atr"]

    if not (touch["low"] <= e_touch <= touch["high"]):
        return None

    crange = confirm["high"] - confirm["low"]
    if crange == 0:
        return None
    if abs(confirm["close"] - confirm["open"]) / crange < C.body_pct_min:
        return None

    sl_dist = atr_v * C.atr_sl_mult
    rr_weak = getattr(C, "rr_weak_adx", 0.0)
    rr      = (getattr(C, "rr_ratio_weak", C.rr_ratio)
               if (rr_weak > 0 and adx_val < rr_weak)
               else C.rr_ratio)
    tp_dist = sl_dist * rr

    tick = mt5.symbol_info_tick(symbol)
    info_sym = mt5.symbol_info(symbol)
    if tick is None or info_sym is None:
        log.warning("%s: geen tick of symbol info beschikbaar", symbol)
        return None

    # Spread-check: verwerp als spread > MAX_SPREAD_ATR_RATIO * ATR
    spread = tick.ask - tick.bid
    if spread > atr_v * MAX_SPREAD_ATR_RATIO:
        log.info("%s: spread te breed (%.5f > %.1f%% ATR=%.5f) — overgeslagen",
                 symbol, spread, MAX_SPREAD_ATR_RATIO * 100, atr_v)
        _tg_blokkade(symbol, "spread",
                     f"BLOKKADE {symbol}: spread te breed ({spread:.5f} = "
                     f"{spread/atr_v*100:.0f}% van ATR) — setup anders geldig")
        return None

    # Minimale stop-afstand die de broker vereist
    min_stop = info_sym.trade_stops_level * info_sym.point
    if min_stop > 0 and sl_dist < min_stop * 1.1:
        log.info("%s: SL-afstand %.5f onder broker minimum %.5f — overgeslagen",
                 symbol, sl_dist, min_stop)
        return None

    if trend == "up":
        if e_confirm <= e_prev:
            return None
        if not (confirm["close"] > confirm["open"] and
                confirm["close"] > e_confirm and
                confirm["close"] > touch["close"]):
            return None
        if not (C.rsi_buy_lo <= rsi_v <= C.rsi_buy_hi):
            return None
        entry      = tick.ask
        order_type = mt5.ORDER_TYPE_BUY
        sl_price   = entry - sl_dist
        tp_price   = entry + tp_dist
        if sl_price >= entry or tp_price <= entry:
            return None

    elif trend == "down":
        if e_confirm >= e_prev:
            return None
        if not (confirm["close"] < confirm["open"] and
                confirm["close"] < e_confirm and
                confirm["close"] < touch["close"]):
            return None
        if not (C.rsi_sell_lo <= rsi_v <= C.rsi_sell_hi):
            return None
        entry      = tick.bid
        order_type = mt5.ORDER_TYPE_SELL
        sl_price   = entry + sl_dist
        tp_price   = entry - tp_dist
        if sl_price <= entry or tp_price >= entry:
            return None
    else:
        return None

    boost = 1.0
    if C.adx_boost_min > 0 and adx_val >= C.adx_boost_min:
        boost = C.adx_boost_mult
        log.info("BOOST  %s  ADX=%.1f  lot x%.1f", symbol, adx_val, boost)

    return {
        "symbol":     symbol,
        "order_type": order_type,
        "entry":      entry,
        "sl":         sl_price,
        "tp":         tp_price,
        "sl_dist":    sl_dist,
        "boost":      boost,
        "candle_t":   candle_t,
        "adx_val":    adx_val,
    }

# ===========================================================================
#  POSITIEGROOTTE
# ===========================================================================

def calc_lot(symbol: str, sl_distance: float, boost: float = 1.0) -> Optional[float]:
    balance = get_balance()
    if balance <= 0:
        log.error("calc_lot: balance is 0 of negatief — trade geannuleerd")
        return None

    info = mt5.symbol_info(symbol)
    if info is None:
        return None

    rp           = C.risk_pct_per_sym.get(symbol, C.risk_pct_default)
    risk_amount  = balance * (rp / 100.0)
    ticks_sl     = sl_distance / info.trade_tick_size
    loss_per_lot = ticks_sl * info.trade_tick_value

    if loss_per_lot <= 0:
        return None

    raw_lot = (risk_amount / loss_per_lot) * boost
    step    = info.volume_step
    lot     = round(raw_lot / step) * step
    lot     = max(info.volume_min, min(lot, info.volume_max))

    # Sanity check: lot mag niet meer dan 5% van balance risico zijn
    max_risk = balance * 0.05
    if loss_per_lot * lot > max_risk:
        lot = round((max_risk / loss_per_lot) / step) * step
        lot = max(info.volume_min, lot)
        log.warning("%s: lot teruggebracht door 5%% maximumregel", symbol)

    # Hard cap per symbool — beschermt tegen gap-risico bij illiquide markten
    hard_cap = MAX_LOT_PER_SYM.get(symbol)
    if hard_cap is not None and lot > hard_cap:
        lot = round(round(hard_cap / step) * step, 8)
        lot = max(info.volume_min, lot)
        log.info("%s: lot teruggebracht naar hard cap %.2f", symbol, lot)

    return round(lot, 8)

# ===========================================================================
#  ORDER VERZENDEN  (met 1 retry)
# ===========================================================================

def get_filling_mode(symbol: str) -> int:
    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_FOK
    mask = info.filling_mode
    if mask & 1: return mt5.ORDER_FILLING_FOK
    if mask & 2: return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN

def send_order(signal: dict, lot: float, signal_id: Optional[int] = None) -> bool:
    sym  = signal["symbol"]
    info = mt5.symbol_info(sym)
    if info is None:
        return False
    dg    = info.digits
    price = round(signal["entry"], dg)
    sl    = round(signal["sl"],    dg)
    tp    = round(signal["tp"],    dg)

    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       sym,
        "volume":       lot,
        "type":         signal["order_type"],
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "deviation":    DEVIATION,
        "magic":        MAGIC,
        "comment":      COMMENT,
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode(sym),
    }

    for attempt in range(1, 3):
        result = mt5.order_send(req)
        if result is None:
            log.error("%s: order_send=None attempt=%d  %s", sym, attempt, mt5.last_error())
            time.sleep(2)
            continue
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            d = "BUY" if signal["order_type"] == mt5.ORDER_TYPE_BUY else "SELL"
            log.info("ORDER  %s %s  lot=%.2f  entry=%.5f  sl=%.5f  tp=%.5f  ticket=#%d",
                     d, sym, lot, price, sl, tp, result.order)
            STATE.open_tickets[result.order] = {
                "sym":      sym,
                "dir":      d,
                "entry":    price,
                "lot":      lot,
                "sl_dist":  signal["sl_dist"],
                "signal_id": signal_id,
            }
            STATE.trades_today += 1
            tg_trade_open(d, sym, lot, price, sl, tp, signal.get("adx_val", 0.0))
            return True
        log.error("%s: retcode=%d  %s  (attempt %d)", sym, result.retcode, result.comment, attempt)
        time.sleep(2)

    return False

def close_all_positions():
    positions = open_positions()
    if not positions:
        return
    log.warning("Alle posities sluiten (%d open)...", len(positions))
    for pos in positions:
        sym  = pos.symbol
        info = mt5.symbol_info(sym)
        if info is None:
            continue
        closed = False
        for attempt in range(1, 4):  # max 3 pogingen per positie
            tick = mt5.symbol_info_tick(sym)
            if tick is None:
                log.warning("GUARD CLOSE  #%d  %s  geen tick (poging %d)", pos.ticket, sym, attempt)
                time.sleep(1)
                continue
            if pos.type == mt5.ORDER_TYPE_BUY:
                otype = mt5.ORDER_TYPE_SELL
                price = tick.bid
            else:
                otype = mt5.ORDER_TYPE_BUY
                price = tick.ask
            req = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       sym,
                "volume":       pos.volume,
                "type":         otype,
                "position":     pos.ticket,
                "price":        round(price, info.digits),
                "deviation":    DEVIATION * 3,  # ruimere deviation bij guard
                "magic":        MAGIC,
                "comment":      "GUARD_CLOSE",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": get_filling_mode(sym),
            }
            r = mt5.order_send(req)
            if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                log.info("GUARD CLOSE  #%d  %s  OK (poging %d)", pos.ticket, sym, attempt)
                closed = True
                break
            err = r.comment if r else mt5.last_error()
            log.error("GUARD CLOSE  #%d  %s  poging %d mislukt: %s", pos.ticket, sym, attempt, err)
            time.sleep(2)
        if not closed:
            log.error("GUARD CLOSE  #%d  %s  DEFINITIEF MISLUKT na 3 pogingen!", pos.ticket, sym)
            tg(f"⚠️ <b>Positie NIET gesloten!</b>\n\n#{pos.ticket}  {sym}\nSluit handmatig in MT5!")

# ===========================================================================
#  BREAKEVEN + TRAILING STOP BEHEER
# ===========================================================================

def manage_open_positions():
    """Zet SL naar breakeven na 1R winst; trail SL na 2R — laat winnaars lopen."""
    be_r    = getattr(C, "breakeven_r",    0.0)
    tr_r    = getattr(C, "trail_start_r",  0.0)
    tr_dist = getattr(C, "trail_dist_r",   1.5)

    if be_r <= 0 and tr_r <= 0:
        return

    for ticket, info in list(STATE.open_tickets.items()):
        pos_list = mt5.positions_get(ticket=ticket)
        if not pos_list:
            continue
        pos   = pos_list[0]
        sym   = info["sym"]
        entry = info["entry"]
        sl_d  = info.get("sl_dist", 0.0)
        if sl_d <= 0:
            continue

        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            continue

        is_buy   = (pos.type == mt5.ORDER_TYPE_BUY)
        price    = tick.bid if is_buy else tick.ask
        profit_r = ((price - entry) / sl_d) if is_buy else ((entry - price) / sl_d)

        new_sl = None

        # Trailing stop (hogere prioriteit dan breakeven)
        if tr_r > 0 and profit_r >= tr_r:
            trail_sl = (price - sl_d * tr_dist) if is_buy else (price + sl_d * tr_dist)
            if is_buy  and trail_sl > pos.sl + 1e-9:
                new_sl = trail_sl
            if not is_buy and trail_sl < pos.sl - 1e-9:
                new_sl = trail_sl

        # Breakeven — alleen als SL nog op oorspronkelijk niveau staat
        elif be_r > 0 and profit_r >= be_r:
            be_sl = (entry + sl_d * 0.1) if is_buy else (entry - sl_d * 0.1)
            if is_buy  and pos.sl < be_sl - 1e-9:
                new_sl = be_sl
            if not is_buy and pos.sl > be_sl + 1e-9:
                new_sl = be_sl

        if new_sl is None:
            continue

        sym_info = mt5.symbol_info(sym)
        if sym_info is None:
            continue
        new_sl = round(new_sl, sym_info.digits)

        req = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol":   sym,
            "sl":       new_sl,
            "tp":       pos.tp,
        }
        result = mt5.order_send(req)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            action = "TRAILING" if (tr_r > 0 and profit_r >= tr_r) else "BREAKEVEN"
            log.info("%s  %s #%d  SL %.5f → %.5f  (%.1fR)", action, sym, ticket, pos.sl, new_sl, profit_r)
        else:
            err = result.comment if result else mt5.last_error()
            log.warning("SL aanpassen mislukt  %s #%d: %s", sym, ticket, err)

# ===========================================================================
#  FTMO GUARDS + MIJLPALEN
# ===========================================================================

def check_guards() -> bool:
    if STATE.max_dd_guard_hit:
        return False

    equity = get_equity()
    if equity <= 0:
        return False

    if STATE.start_balance > 0:
        dd_usd = STATE.start_balance - equity
        dd_pct = dd_usd / STATE.start_balance
        # FTMO: max $16k totaalverlies — stop bij $14k (veiligheidsmarge van $2k)
        if dd_usd >= FTMO_MAX_DD_ABS or dd_pct >= C.max_drawdown_limit:
            log.warning("MAX DD GUARD  equity=%.2f  verlies=$%.0f  (%.2f%%)", equity, dd_usd, dd_pct * 100)
            STATE.max_dd_guard_hit = True
            tg_guard("MAX DRAWDOWN", dd_usd, dd_pct * 100, equity)
            close_all_positions()
            return False

    if STATE.day_start_balance > 0:
        # FTMO: absoluut $8,000 dagverlies — niet percentage-gebaseerd
        daily_loss_usd = STATE.day_start_balance - equity
        if daily_loss_usd >= FTMO_DAILY_LOSS_ABS:
            if not STATE.daily_guard_hit:
                daily_pct = daily_loss_usd / STATE.start_balance * 100 if STATE.start_balance > 0 else 0
                log.warning("DAG LOSS GUARD  $%.0f  (%.2f%% van startkapitaal)", daily_loss_usd, daily_pct)
                STATE.daily_guard_hit = True
                tg_guard("DAGLIMIET", daily_loss_usd, daily_pct, equity)
                close_all_positions()
            return False

    if STATE.week_start_balance > 0:
        weekly_dd = (STATE.week_start_balance - equity) / STATE.week_start_balance
        if weekly_dd >= C.weekly_loss_limit:
            if not STATE.weekly_guard_hit:
                weekly_usd = STATE.week_start_balance - equity
                log.warning("WEEK LOSS GUARD  $%.0f  (%.2f%%)", weekly_usd, weekly_dd * 100)
                STATE.weekly_guard_hit = True
                tg_guard("WEEKLIMIET", weekly_usd, weekly_dd * 100, equity)
                close_all_positions()
            return False

    return True

def check_milestones():
    if C.challenge_target <= 0 or STATE.start_balance <= 0:
        return
    balance = get_balance()
    pct     = (balance - STATE.start_balance) / STATE.start_balance

    if not STATE.challenge_logged and pct >= C.challenge_target:
        STATE.challenge_logged = True
        log.info("=" * 60)
        log.info("*** CHALLENGE GEHAALD!  balance=%.2f  winst=+%.1f%%  ***", balance, pct * 100)
        log.info("*** Wacht op verificatie (+15%%), daarna MODUS=VEILIG ***")
        log.info("=" * 60)
        tg_milestone("CHALLENGE GEHAALD! (+10%)", balance, pct * 100)

    if not STATE.verify_logged and pct >= C.verify_target:
        STATE.verify_logged = True
        log.info("=" * 60)
        log.info("*** VERIFICATIE GEHAALD!  balance=%.2f  winst=+%.1f%%  ***", balance, pct * 100)
        log.info("*** STOP DE BOT EN ZET modus=VEILIG IN bot_config.ini ***")
        log.info("=" * 60)
        tg_milestone("VERIFICATIE GEHAALD! (+15%)", balance, pct * 100)

# ===========================================================================
#  DAG / WEEK RESET
# ===========================================================================

def maybe_reset_day():
    today    = date.today()
    iso_week = today.isocalendar()[1]

    if iso_week != STATE.current_week:
        bal = get_balance()
        log.info("NIEUWE WEEK %d  balance=%.2f", iso_week, bal)
        STATE.current_week       = iso_week
        STATE.week_start_balance = bal
        STATE.weekly_guard_hit   = False

    if today != STATE.current_day:
        bal = get_balance()
        log.info("NIEUWE DAG %s  balance=%.2f", today, bal)
        STATE.current_day        = today
        STATE.day_start_balance  = bal
        STATE.daily_guard_hit    = False
        STATE.last_entry_candle  = {}
        STATE.trades_today       = 0
        STATE.daily_report_sent  = False

# ===========================================================================
#  SESSIE- EN NIEUWSFILTER
# ===========================================================================

def in_session(symbol: str) -> bool:
    hour = datetime.now(tz=timezone.utc).hour
    s, e = C.session_per_sym.get(symbol, (7, 20))
    return s <= hour < e

def fetch_news() -> list:
    try:
        r = requests.get(FF_URL, timeout=10)
        r.raise_for_status()
        high = [e for e in r.json() if str(e.get("impact", "")).lower() == "high"]
        log.info("Nieuws vernieuwd — %d high-impact events", len(high))
        return high
    except Exception as exc:
        log.warning("Nieuws ophalen mislukt (%s) — trading doorgaat", exc)
        return STATE.news_cache

def get_news() -> list:
    now = datetime.now(tz=timezone.utc)
    if (STATE.news_cache_time is None or
            (now - STATE.news_cache_time).total_seconds() > NEWS_CACHE_MIN * 60):
        STATE.news_cache      = fetch_news()
        STATE.news_cache_time = now
    return STATE.news_cache

def news_blocked(symbol: str) -> bool:
    currencies = SYMBOL_CURRENCIES.get(symbol, [])
    if not currencies:
        return False
    now = datetime.now(tz=timezone.utc)
    buf = timedelta(minutes=NEWS_BUFFER_MIN)
    for ev in get_news():
        if ev.get("currency", "").upper() not in currencies:
            continue
        try:
            ev_t = datetime.strptime(ev["date"], "%m-%d-%Y %I:%M%p").replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        if abs(now - ev_t) <= buf:
            title = ev.get("title", "?")
            log.info("NIEUWS BLOKKADE  %s  '%s'", symbol, title)
            _tg_blokkade(symbol, "nieuws",
                         f"BLOKKADE {symbol}: nieuws '{title}' "
                         f"({ev.get('currency','?')}) — trading gepauzeerd")
            return True
    return False

# ===========================================================================
#  TELEGRAM COMMANDO'S VERWERKEN
# ===========================================================================

_tg_rate: dict = {"last_cmd_time": 0.0, "blocked_count": 0}
_TG_CMD_MIN_INTERVAL = 3.0  # minimaal 3 seconden tussen commando's

def process_tg_commands():
    """Poll Telegram voor inkomende commando's van de gebruiker."""
    if not TG_TOKEN or TG_TOKEN.startswith("123456"):
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": STATE.tg_last_update_id + 1, "timeout": 2},
            timeout=5,
        )
        updates = r.json().get("result", [])
        for upd in updates:
            STATE.tg_last_update_id = upd["update_id"]
            msg = upd.get("message", {})
            # Verwerp berichten van onbekende chats (security)
            sender_chat_id = str(msg.get("chat", {}).get("id", ""))
            if sender_chat_id != TG_CHAT_ID:
                log.warning("Telegram: onbekende chat_id %s geblokkeerd", sender_chat_id)
                continue
            text = msg.get("text", "").strip()
            if not text or not text.startswith("/"):
                continue

            # Rate limiting — max 1 commando per 3 seconden
            now_ts = time.time()
            if now_ts - _tg_rate["last_cmd_time"] < _TG_CMD_MIN_INTERVAL:
                _tg_rate["blocked_count"] += 1
                if _tg_rate["blocked_count"] == 1:
                    tg("⚠️ Te snel — wacht even tussen commando's.")
                continue
            _tg_rate["last_cmd_time"] = now_ts
            _tg_rate["blocked_count"] = 0

            if text == "/status":
                bal      = get_balance()
                eq       = get_equity()
                pct      = (bal - STATE.start_balance) / STATE.start_balance * 100 if STATE.start_balance > 0 else 0
                day_pct  = (bal - STATE.day_start_balance) / STATE.day_start_balance * 100 if STATE.day_start_balance > 0 else 0
                open_pos = open_positions()
                paused   = "⏸ GEPAUZEERD" if STATE.bot_paused else "▶️ ACTIEF"
                syms     = "  ".join(s[:3] for s in C.symbols)
                pos_lines = ""
                for p in open_pos:
                    p_icon = "🟢" if p.profit >= 0 else "🔴"
                    p_dir  = "BUY" if p.type == 0 else "SELL"
                    pos_lines += f"\n  {p_icon} {p.symbol}  {p_dir}  ${p.profit:+,.0f}"
                day_icon  = "📈" if day_pct >= 0 else "📉"
                tg(
                    f"📡 <b>BOT STATUS</b>  {paused}\n"
                    f"{_SEP}\n"
                    f"💼 Balans    ${bal:,.0f}   ({pct:+.1f}%)\n"
                    f"⚖️  Equity    ${eq:,.0f}\n"
                    f"{day_icon} Vandaag   {day_pct:+.2f}%\n"
                    f"{_SEP}\n"
                    f"📂 Posities  {len(open_pos)} open{pos_lines}\n"
                    f"{_SEP}\n"
                    f"⚙️  Modus     {MODUS}\n"
                    f"🎯 Symbolen  {syms}\n"
                    f"🔢 Trades    {STATE.trades_today} vandaag"
                )

            elif text == "/pause":
                STATE.bot_paused = True
                log.warning("Bot gepauzeerd via Telegram")
                tg(f"⏸ <b>Bot gepauzeerd</b>\n{_SEP}\nGeen nieuwe trades.\nStuur /resume om te hervatten.")

            elif text == "/resume":
                STATE.bot_paused = False
                log.info("Bot hervat via Telegram")
                tg(f"▶️ <b>Bot hervat</b>\n{_SEP}\nTrading weer actief. Succes! 🚀")

            elif text.startswith("/switch "):
                parts = text.split(" ", 3)
                if len(parts) == 4:
                    _, new_login, new_pass, new_server = parts
                    # Nooit wachtwoord loggen — alleen login en server
                    log.info("Telegram /switch ontvangen voor login=%s server=%s", new_login, new_server)
                    _switch_account(new_login, new_pass, new_server)
                else:
                    tg(
                        "⚠️ Gebruik:\n<code>/switch LOGIN WACHTWOORD SERVER</code>\n\n"
                        "⚠️ Let op: wachtwoord is zichtbaar in Telegram chatgeschiedenis.\n"
                        "Verwijder het bericht daarna."
                    )

            elif text == "/report":
                learning.build_week_report(tg, STATE.start_balance, get_balance())

            elif text == "/resetstart":
                bal = get_balance()
                eq  = get_equity()
                STATE.start_balance = min(bal, eq)
                save_start_balance(STATE.start_balance)
                STATE.challenge_logged = False
                STATE.verify_logged    = False
                log.info("Start balance gereset naar %.2f via Telegram", STATE.start_balance)
                tg(f"🔄 <b>Startbalans gereset</b>\n{_SEP}\n💼 Nieuw startpunt: ${STATE.start_balance:,.2f}\nGuards herberekend vanaf dit punt.")

            elif text == "/help":
                tg(
                    f"📋 <b>COMMANDO'S</b>\n"
                    f"{_SEP}\n"
                    f"📡 /status       balans &amp; open posities\n"
                    f"⏸ /pause        trading stoppen\n"
                    f"▶️  /resume       trading hervatten\n"
                    f"📊 /report       weekrapport\n"
                    f"🔄 /resetstart   DD-guard resetten\n"
                    f"🔑 /switch       account wisselen\n"
                    f"📋 /help         dit menu\n"
                    f"{_SEP}\n"
                    f"🔒 <b>FTMO LIMIETEN</b>\n"
                    f"📅 Dag       max  <b>-${FTMO_DAILY_LOSS_ABS:,.0f}</b>  <i>(FTMO grens $8.000)</i>\n"
                    f"📊 Totaal    max  <b>-${FTMO_MAX_DD_ABS:,.0f}</b>  <i>(FTMO grens $16.000)</i>"
                )

    except Exception as exc:
        log.warning("Telegram commando fout: %s", exc)

def _switch_account(login: str, password: str, server: str):
    """Wissel naar nieuw FTMO account (bijv. verificatie)."""
    global MT5_LOGIN, MT5_PASSWORD, MT5_SERVER
    try:
        # Config bestand updaten
        ini = configparser.ConfigParser()
        ini.read(_ini_path)
        ini["MT5"]["login"]    = login
        ini["MT5"]["password"] = password
        ini["MT5"]["server"]   = server
        with open(_ini_path, "w") as f:
            ini.write(f)

        # MT5 herverbinden
        mt5.shutdown()
        MT5_LOGIN    = int(login)
        MT5_PASSWORD = password
        MT5_SERVER   = server
        time.sleep(3)

        if connect_mt5():
            bal = get_balance()
            eq  = get_equity()
            STATE.start_balance      = min(bal, eq)
            STATE.day_start_balance  = min(bal, eq)
            STATE.week_start_balance = min(bal, eq)
            STATE.challenge_logged   = False
            STATE.verify_logged      = False
            save_start_balance(STATE.start_balance)
            log.info("Account gewisseld naar %s  balance=%.2f  start=%.2f", login, bal, STATE.start_balance)
            tg(
                f"✅ <b>Account gewisseld</b>\n"
                f"{_SEP}\n"
                f"🔑 Login     {login}\n"
                f"🌐 Server    {server}\n"
                f"💼 Balans    ${bal:,.0f}\n"
                f"⚙️  Modus     {MODUS}"
            )
        else:
            tg(f"❌ <b>Account wisselen mislukt</b>\n{_SEP}\nLogin {login} kon niet verbinden.")

    except Exception as exc:
        log.error("Account wissel fout: %s", exc)
        tg(f"❌ <b>Fout bij wisselen</b>\n\n{exc}")

# ===========================================================================
#  GESLOTEN TRADES DETECTEREN + DAGRAPPORT
# ===========================================================================

def check_closed_trades():
    """Vergelijk huidige open posities met vorige loop — stuur melding bij sluiting."""
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
        log.info("TRADE GESLOTEN  #%d  %s %s  pnl=%.2f", ticket, info["dir"], info["sym"], pnl)
        tg_trade_close(info["sym"], info["dir"], pnl, info["entry"], close_price, balance)
        # Uitkomst opslaan in learning database
        sig_id = info.get("signal_id")
        if sig_id:
            learning.update_signal_result(sig_id, win=(pnl > 0), pnl=pnl)

def maybe_send_daily_report():
    """Stuur dagrapport eenmalig om 20:00 UTC."""
    now = datetime.now(tz=timezone.utc)
    if now.hour == 20 and not STATE.daily_report_sent:
        balance    = get_balance()
        day_pnl    = balance - STATE.day_start_balance
        week_pnl   = balance - STATE.week_start_balance
        open_count = len(open_positions())
        tg_daily_report(balance, day_pnl, week_pnl, open_count, STATE.trades_today)
        STATE.daily_report_sent = True
        log.info("Dagrapport verstuurd via Telegram")

# ===========================================================================
#  TRADING LOGICA PER SYMBOOL
# ===========================================================================

def process_symbol(symbol: str):
    if STATE.bot_paused:
        return
    if not check_guards():
        return
    max_td = getattr(C, "max_trades_per_day", 0)
    if max_td > 0 and STATE.trades_today >= max_td:
        return
    if not in_session(symbol):
        return
    if news_blocked(symbol):
        return
    if len(open_positions(symbol)) >= C.max_open_per_sym:
        return

    trend_result = get_h1_trend(symbol)
    if trend_result is None:
        return
    trend, adx_val = trend_result

    signal = get_m15_signal(symbol, trend, adx_val)
    if signal is None:
        return

    lot = calc_lot(symbol, signal["sl_dist"], signal["boost"])
    if lot is None or lot <= 0:
        log.warning("%s: ongeldige lot berekening", symbol)
        return

    direction = "BUY" if signal["order_type"] == mt5.ORDER_TYPE_BUY else "SELL"
    log.info("SIGNAAL  %s %s  ADX=%.1f  lot=%.2f  entry=%.5f  sl=%.5f  tp=%.5f",
             direction, symbol, adx_val, lot,
             signal["entry"], signal["sl"], signal["tp"])

    # Signaal opslaan in database (voor zelf-leren)
    df_m15  = get_bars(symbol, mt5.TIMEFRAME_M15, M15_BARS)
    rsi_val = 50.0
    body_pct_val = 0.0
    if df_m15 is not None and len(df_m15) >= 2:
        rsi_s = rsi_ind(df_m15["close"], C.m15_rsi_period)
        rsi_val = float(rsi_s.iloc[-2])
        c = df_m15.iloc[-2]
        rng = c["high"] - c["low"]
        body_pct_val = abs(c["close"] - c["open"]) / rng if rng > 0 else 0
    entry_price = signal["entry"]
    ema50_dist  = abs(entry_price - (entry_price - signal["sl_dist"])) / entry_price

    # ML winkans filter — actief zodra ml_min_prob > 0 en model getraind is (50+ trades)
    ml_min = getattr(C, "ml_min_prob", 0.0)
    if ml_min > 0:
        win_prob = learning.predict_win_probability(
            adx_val=adx_val, rsi_val=rsi_val, body_pct=body_pct_val,
            session_hr=datetime.now(tz=timezone.utc).hour,
            atr_val=signal["sl_dist"] / C.atr_sl_mult,
            ema_dist=ema50_dist,
        )
        if win_prob < ml_min:
            log.info("ML filter: %s winkans %.1f%% < %.1f%% — overgeslagen",
                     symbol, win_prob * 100, ml_min * 100)
            return

    signal_id = learning.log_signal(
        symbol=symbol, direction=direction, adx_val=adx_val,
        rsi_val=rsi_val, body_pct=body_pct_val,
        session_hr=datetime.now(tz=timezone.utc).hour,
        atr_val=signal["sl_dist"] / C.atr_sl_mult,
        ema_dist=ema50_dist, traded=True,
    )

    if send_order(signal, lot, signal_id=signal_id):
        STATE.last_entry_candle[symbol] = signal["candle_t"]
        # ticket + signal_id worden al correct opgeslagen door send_order()

# ===========================================================================
#  MAIN LOOP
# ===========================================================================

_LOCK_FILE_HANDLE = None  # Win32 HANDLE
LOCK_FILE = os.path.join(os.path.dirname(__file__), "bot.lock")

def acquire_lock() -> bool:
    """Win32 CreateFileW zonder sharing — echt cross-process exclusief."""
    global _LOCK_FILE_HANDLE
    import ctypes, ctypes.wintypes
    kernel32 = ctypes.windll.kernel32
    # Zet restype op HANDLE (64-bit pointer) — anders trunceert ctypes naar 32-bit int
    # en klopt de INVALID_HANDLE_VALUE vergelijking (-1 != 18446744073709551615) niet
    kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
    # GENERIC_WRITE=0x40000000, ShareMode=0 (exclusief), OPEN_ALWAYS=4
    h = kernel32.CreateFileW(
        LOCK_FILE, 0x40000000, 0, None, 4, 0x80, None
    )
    INVALID = ctypes.wintypes.HANDLE(-1).value   # INVALID_HANDLE_VALUE als HANDLE type
    if h == INVALID or h == 0:
        err = kernel32.GetLastError()
        log.error("Bot al actief — lock bezet (err=%d). Stopt.", err)
        return False
    _LOCK_FILE_HANDLE = h
    return True

def release_lock():
    global _LOCK_FILE_HANDLE
    if _LOCK_FILE_HANDLE:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(_LOCK_FILE_HANDLE)
        _LOCK_FILE_HANDLE = None
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass

def main():
    # Alleen de venv Python mag de bot draaien — stop systeem Python direct
    _exe = sys.executable.lower()
    if r"\.venv" not in _exe and "/.venv" not in _exe:
        sys.exit(0)

    if not acquire_lock():   # voorkomt dat twee instances tegelijk draaien
        sys.exit(0)

    log.info("=" * 60)
    log.info("FTMO Bot gestart — MODUS: %s", MODUS)
    log.info("Symbolen : %s", C.symbols)
    log.info("Risico   : %s", C.risk_pct_per_sym)
    log.info("ADX min  : %.0f  |  Boost bij ADX>%.0f x%.1f",
             C.h1_adx_min, C.adx_boost_min, C.adx_boost_mult)
    log.info("Weekstop : %.1f%%  |  Max DD: %.0f%%  (FTMO: dag -$%.0f  totaal -$%.0f)",
             C.weekly_loss_limit * 100, C.max_drawdown_limit * 100,
             FTMO_DAILY_LOSS_ABS, FTMO_MAX_DD_ABS)
    if MODUS == "AGRESSIEF":
        log.info("Doelen   : Challenge=+10%%  Verificatie=+15%%")
    log.info("=" * 60)

    if not connect_mt5():
        sys.exit(1)

    bal = get_balance()
    eq  = get_equity()
    if bal <= 0:
        log.error("Balance is 0 — controleer MT5 account")
        shutdown_mt5()
        sys.exit(1)

    # Persistente startbalans: gebruik opgeslagen waarde bij herstart, sla op bij eerste start.
    # Gebruik min(balance, equity) zodat open verliezende posities de guard niet direct triggeren.
    persisted = load_start_balance()
    if persisted > 0:
        STATE.start_balance = persisted
        log.info("Startbalans (hersteld): %.2f  |  huidig balance=%.2f  equity=%.2f",
                 STATE.start_balance, bal, eq)
    else:
        STATE.start_balance = min(bal, eq)
        save_start_balance(STATE.start_balance)
        log.info("Startbalans (nieuw): %.2f  |  balance=%.2f  equity=%.2f",
                 STATE.start_balance, bal, eq)

    if eq < bal:
        open_pos = open_positions()
        log.warning("Open posities bij start: %d  |  equity (%.2f) < balance (%.2f) — DD guard tijdelijk gebaseerd op equity",
                    len(open_pos), eq, bal)

    maybe_reset_day()

    learning.init_db()
    learning.train_ml_model()  # Herlaad ML model als er al trainingsdata is

    # Telegram commando thread — altijd actief, ook bij MT5 herverbinding
    threading.Thread(target=_tg_command_loop, daemon=True).start()

    risico_lines = "   ".join(f"{s[:3]} {v}%" for s, v in C.risk_pct_per_sym.items())
    tg(
        f"🚀 <b>FTMO BOT GESTART</b>\n"
        f"{_SEP}\n"
        f"💼 Balans    ${bal:,.0f}\n"
        f"⚙️  Modus     {MODUS}\n"
        f"{_SEP}\n"
        f"🎯 {risico_lines}\n"
        f"{_SEP}\n"
        f"Typ /help voor commando's"
    )

    try:
        while True:
            write_heartbeat()

            if not ensure_connected():
                log.error("Verbinding permanent verloren — bot stopt")
                tg_status("VERBINDING VERLOREN — bot gestopt. Herstart vereist!")
                break

            try:
                maybe_reset_day()
            except Exception as exc:
                log.exception("maybe_reset_day fout: %s", exc)

            try:
                check_milestones()
            except Exception as exc:
                log.exception("check_milestones fout: %s", exc)

            try:
                check_closed_trades()
            except Exception as exc:
                log.exception("check_closed_trades fout: %s", exc)

            try:
                maybe_send_daily_report()
            except Exception as exc:
                log.exception("maybe_send_daily_report fout: %s", exc)

            # Wekelijkse optimizer — elke zondag na 22:00 UTC
            try:
                now = datetime.now(tz=timezone.utc)
                cur_week = now.isocalendar()[1]
                if (now.weekday() == 6 and now.hour >= 22
                        and STATE.last_optimizer_week != cur_week):
                    STATE.last_optimizer_week = cur_week
                    threading.Thread(
                        target=learning.run_weekly_optimizer,
                        args=(C, tg), daemon=True
                    ).start()
                    learning.build_week_report(tg, STATE.start_balance, get_balance())
                    STATE.last_week_report = cur_week
            except Exception as exc:
                log.exception("Wekelijkse optimizer fout: %s", exc)

            try:
                manage_open_positions()
            except Exception as exc:
                log.exception("manage_open_positions fout: %s", exc)

            for sym in C.symbols:
                try:
                    process_symbol(sym)
                except Exception as exc:
                    log.exception("Onverwachte fout bij %s: %s", sym, exc)
                    tg_status(f"Fout bij {sym}: {exc}")

            time.sleep(POLL_SEC)

    except KeyboardInterrupt:
        log.info("Gestopt door gebruiker (Ctrl+C)")
        tg_status("Bot gestopt door gebruiker.")
    finally:
        shutdown_mt5()
        release_lock()


if __name__ == "__main__":
    main()
