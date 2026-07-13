"""
check_mt5.py — read-only verificatie van de MT5-omgeving (GEEN orders).
Draai dit op de VPS ná installatie en vóór de eerste bot-start:

    python check_mt5.py

Controleert: terminal-verbinding, account (valuta/balans), de 4 symbolen,
pip-values, spreads en de servertijd-offset.

Bekende valkuil: een terminal die op MetaQuotes-Demo is ingelogd krijgt
BÈTA-builds en dan faalt de Python-koppeling met 'IPC timeout'. Gebruik
altijd de MT5-installatie van de broker (FundingPips) met een release-build.
"""
import configparser
import os
import sys
from datetime import datetime, timezone

import MetaTrader5 as mt5

INI = os.path.join(os.path.dirname(__file__), "bot_config.ini")
TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
SYMBOLS = ("GBPUSD", "EURUSD", "USDJPY", "AUDUSD")

def main() -> int:
    ini = configparser.RawConfigParser()
    if not ini.read(INI, encoding="utf-8-sig"):
        print(f"FAIL: {INI} niet gevonden"); return 1

    kw = {"path": TERMINAL} if os.path.exists(TERMINAL) else {}
    print(f"terminal-pad aanwezig: {os.path.exists(TERMINAL)}")
    print(f"MetaTrader5 package : {mt5.__version__}")

    if not mt5.initialize(login=int(ini["MT5"]["login"]),
                          password=ini["MT5"]["password"],
                          server=ini["MT5"]["server"],
                          timeout=120_000, **kw):
        print(f"FAIL: initialize: {mt5.last_error()}")
        print("  -> Check: broker-terminal (geen MetaQuotes-Demo bèta),")
        print("     zelfde Windows-gebruiker, login/wachtwoord/server correct.")
        return 1

    ok = True
    acc = mt5.account_info()
    term = mt5.terminal_info()
    print(f"OK  login={acc.login}  server={acc.server}")
    print(f"    balance={acc.balance:,.2f} {acc.currency}  equity={acc.equity:,.2f}  "
          f"leverage=1:{acc.leverage}")
    print(f"    terminal connected={term.connected}  trade_allowed={term.trade_allowed}")
    if acc.currency != "EUR":
        print("LET OP: accountvaluta is geen EUR — de €-caps in lbo_bot.py kloppen dan niet!")
        ok = False
    if not term.trade_allowed:
        print("LET OP: algo-trading staat uit in de terminal (knop 'Algo Trading').")
        ok = False

    for sym in SYMBOLS:
        if not mt5.symbol_select(sym, True):
            print(f"FAIL {sym}: niet beschikbaar (heeft de broker een suffix-naam?)")
            ok = False
            continue
        info = mt5.symbol_info(sym)
        tick = mt5.symbol_info_tick(sym)
        pipsz = 0.01 if "JPY" in sym else 0.0001
        pv = info.trade_tick_value * (pipsz / info.trade_tick_size) \
            if info.trade_tick_size else 0.0
        if tick and tick.time > 0:
            spread_p = (tick.ask - tick.bid) / pipsz
            off_h = (tick.time - datetime.now(timezone.utc).timestamp()) / 3600
            print(f"OK  {sym}: pip_val={pv:.2f} {acc.currency}/lot  "
                  f"spread={spread_p:.1f}p  servertijd-offset={off_h:+.1f}u")
        else:
            print(f"OK  {sym}: pip_val={pv:.2f} (markt dicht — geen verse tick)")

    mt5.shutdown()
    print("KLAAR — geen orders verstuurd." + ("" if ok else "  (met waarschuwingen!)"))
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
