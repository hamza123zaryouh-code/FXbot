# FP Zero Bot — "NY Flow + Monday"

Geautomatiseerde FX-bot voor een FundingPips Zero €160K account (EUR).
Tijd-gebaseerde flow-strategie op **GBPUSD, EURUSD, USDJPY, AUDUSD** —
geen indicatoren, entries en exits op vaste UTC-tijden, SL als vangnet.

| Sleeve | Wanneer (UTC) | Trade | SL | Risk |
|---|---|---|---|---|
| MON  | ma 00:00 → 20:00 | BUY GBPUSD | 40p | €500 |
| MONA | ma 00:00 → 20:00 | BUY AUDUSD | 30p | €500 |
| H12  | ma–vr 12:00 → 13:00 | BUY GBPUSD + EURUSD | 20p | €300 |
| H14  | ma–vr 14:00 → 15:00 | BUY GBPUSD + EURUSD | 20p | €300 |
| H18  | ma–do 18:00 → 20:00 | BUY USDJPY | 20p | €500 |
| H18G | ma–do 18:00 → 20:00 | SELL GBPUSD | 20p | €300 |

Backtest (Dukascopy, spread + commissie meegerekend): 12M **+31,5%** /
laatste 3M **+9,7%**, max DD 2,9%, alle kwartalen positief.
Herbevestig de edge maandelijks: `python backtest.py --months 3`.

## Bestanden

| Bestand | Doel |
|---|---|
| `lbo_bot.py` | de live bot (strategie + volledige FP-compliance-laag) |
| `backtest.py` | backtest die de bot exact spiegelt (zelfde SLEEVES-tabel) |
| `check_mt5.py` | read-only omgevingscheck — draai dit vóór de eerste bot-start |
| `bot_config.ini` | MT5-login + Telegram (NOOIT committen — staat in .gitignore) |
| `start_bot.bat` | starter met auto-herstart voor de VPS |
| `lbo_fp_state.json` | persistente compliance-state (equity-high!) — nooit verwijderen |

## VPS-installatie (Windows VPS)

1. **VPS-eisen**: Windows Server/10/11, ≥2 GB RAM, altijd aan. Tijdzone van
   de VPS maakt niet uit — de bot rekent volledig in UTC. Kies een
   datacenter dicht bij de broker (Londen/Amsterdam) voor lage latency.
2. **MetaTrader 5** installeren — gebruik de **installer van de broker
   (FundingPips)**, niet de kale MetaQuotes-download. Terminals die op
   MetaQuotes-Demo inloggen krijgen bèta-builds en dan faalt de
   Python-koppeling met `IPC timeout`; broker-terminals draaien de
   release-build die bij het pip-package hoort. Eenmalig handmatig
   inloggen en checken dat GBPUSD, EURUSD, USDJPY, AUDUSD in Market Watch
   staan. Standaardpad `C:\Program Files\MetaTrader 5\terminal64.exe`
   aanhouden (of `MT5_TERMINAL_PATH` in `lbo_bot.py` aanpassen).
3. **Python 3.10+** installeren (python.org, vink "Add to PATH" aan), dan:
   ```
   pip install MetaTrader5 requests
   ```
   (voor de backtest ook: `pip install numpy pandas dukascopy-python`)
4. Map `FXbot` naar de VPS kopiëren en `bot_config.ini` invullen:
   ```ini
   [MT5]
   login    = 12345678
   password = ...
   server   = FundingPips-Server

   [TELEGRAM]
   token   = 123456:ABC...   ; leeg laten = geen Telegram
   chat_id = 123456789
   ```
5. **Omgevingscheck**: `python check_mt5.py` — moet `OK` tonen voor het
   account (valuta EUR!) en alle 4 symbolen, zonder waarschuwingen.
   Verwijder ook oude EA's/charts uit de terminal (bv. XAUUSD-experts) —
   FundingPips-regels: alleen FX, en maar één systeem mag handelen.
6. **Starten**: dubbelklik `start_bot.bat`, of beter — Taakplanner:
   taak "Bij aanmelden", actie = `start_bot.bat`, "Met hoogste bevoegdheden",
   en zet in VPS-instellingen automatisch aanmelden aan zodat de bot na een
   reboot vanzelf terugkomt. De bot heeft een lock-file: per ongeluk twee
   keer starten kan geen kwaad. **Twee valkuilen** (geleerd op 12 juli 2026):
   - Taakplanner-tabblad Instellingen: **"Taak stoppen als deze langer
     draait dan" UITVINKEN** — de standaard is 72 uur en dan wordt de bot
     gewoon gekilld (zo lag de oude bot er sinds vrijdagnacht uit).
   - De **Algo Trading-knop** in MT5 moet AAN staan (anders weigert de
     terminal alle bot-orders met `trade_allowed=False`). In
     `config\common.ini` onder `[Experts]`: `Enabled=1`, en zet ook
     `Account=0` + `Profile=0` zodat de knop niet vanzelf uitvalt bij een
     account- of profielwissel.
7. **Controle**: stuur `/status` via Telegram; dagrapport komt om 20:00 UTC.
   `lbo_bot_heartbeat.txt` wordt elke poll ververst — ouder dan 1 minuut
   betekent dat de bot niet draait.

## Eerst demo, dan live

Draai **minimaal 1–2 weken op een demo-account** en vergelijk de fills met
de backtest (aantal trades per dag, spread op de entry-tijden, of de
news-skips logisch zijn). Pas daarna `bot_config.ini` omzetten naar het
FundingPips-account. Bij het wisselen van account: verwijder
`lbo_fp_state.json` en `lbo_start_balance.txt` éénmalig, zodat equity-high
en dagankers op het nieuwe account verankeren.

## Compliance (afgedwongen door de bot)

- max €500 risico per trade (hard, incl. spread-buffer + commissie)
- max 2 posities tegelijk, max 1 per symbool, open-risk cap €1.400
- eigen dagstop −€1.600 (FP-breach −€4.800 wordt nooit geraakt)
- trailing vloer €8.000 onder hoogste equity (persistent over herstarts)
- newsfilter ±10 min high-impact per symbool + flatten vóór events
- vrijdag: geen entries ≥16:00, alles dicht 20:30 UTC (geen weekend-holds)
- maand circuit-breaker −3% (persistent over herstarts)

**Let op**: bij een drawdown van ~€6.300 onder de equity-piek bevriest de
bot zichzelf permanent (beter bevroren dan breached). Als `/status` een
vloer-marge onder €2.000 toont: overweeg handmatig pauzeren.
