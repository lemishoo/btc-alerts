# Configuration Reference (btc-alerts)

Tento dokument popisuje **všetky konfiguračné vstupy systému**  
(.env, environment variables, timers).

---

## 📦 .env – HLAVNÁ KONFIGURÁCIA

### 🔐 Telegram
TELEGRAM_BOT_TOKEN  
TELEGRAM_CHAT_ID – market regime channel  
TELEGRAM_SIGNALS_CHAT_ID – setup / signals channel  

---

### 🧭 MARKET REGIME (BTC)

SYMBOL=BTCUSDT  
INTERVAL_SEC=30  

MEAN_REVERT_MAX_WIDTH_PCT  
- max šírka range pre mean-revert setupy  
- aktuálne: **0.45**

ALT_ENABLED_BTC_REGIMES  
- režimy, v ktorých sú povolené alt setupy  
- typicky: RANGE, DELEVERAGING, LONG_UNWIND, SHORT_SQUEEZE

---

### 🪙 ALT SETUPS

SETUP_SYMBOLS  
- alt USDT páry, ktoré môžu generovať setup  
- aktuálne: ETHUSDT, SOLUSDT  
- ZEC **dočasne vypnutý**

WATCH_ALT_BTC  
- ALT/BTC páry používané ako filter  
- aktuálne: ETHBTC, SOLBTC  

---

## 📄 signals.jsonl

- výstup z alerts.py  
- vstup pre paper_exec.py  
- append-only (história setupov)

---

## 🧪 PAPER TRADING

PAPER_START_EQUITY=1000  
PAPER_LEVERAGE=5  
PAPER_RISK_PCT=0.005  

ENTRY_TIMEOUT_SEC=1800  

TP1_CLOSE_FRAC=0.50  
MOVE_SL_TO_BE_ON_TP1=1  

STOP_FILL_MODE=CAP  
BE_BUFFER_PCT=0.0  

---

## ⏱ TIMERS (systemd --user)

paper-report.timer  
- denne 21:00 CET  
- posiela paper trading report do Telegramu

market regime  
- beží kontinuálne (alerts.py)

---

## 🔒 FILOZOFIA
- alerts.py **nikdy neobchoduje**
- paper_exec.py **nikdy neposiela signály**
- BTC = globálny filter
- alts = setup-only
- žiadne manuálne zásahy
