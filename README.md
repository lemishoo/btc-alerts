# BTC Alerts & Paper Trading System

Version: baseline v1  
Owner: Michal Rohrböck  
Environment: VPS (Ubuntu), Python 3.11, venv  

---

## 🎯 CIEĽ SYSTÉMU

Tento systém **neobchoduje impulzívne**.  
Je to **market-regime–driven scanner + paper trader**, ktorého cieľom je:

- čítať **BTC ako globálny režim**
- generovať **setup-only signály** pre vybrané alty
- exekvovať ich **automaticky v paper režime**
- zbierať dáta → vyhodnocovať → rezať zlé časti

Bez diskrečného zásahu.  
Bez emócií.  
Bez FOMO.

---

## 🧩 ARCHITEKTÚRA (HIGH LEVEL)


---

## 🧠 HLAVNÉ KOMPONENTY

### 1. alerts.py — MARKET SCANNER

**Zodpovednosť:**
- sleduje BTCUSDT (30s)
- vyhodnocuje MARKET REGIME
- povoľuje / zakazuje alt setupy podľa BTC režimu
- zapisuje setupy do `signals.jsonl`
- posiela info do Telegramu

**Nič neexekvuje. Len pozoruje.**

---

### 2. paper_exec.py — PAPER EXECUTOR

**Zodpovednosť:**
- číta `signals.jsonl`
- vytvára paper obchody
- riadi:
  - entry
  - SL
  - TP1 (partial)
  - TP2 (full)
- zapisuje výsledky do:
  - `paper_trades.jsonl`
  - `paper_trades.csv`

**Žiadne live API. Čistá simulácia.**

---

### 3. daily_paper_report.py — REPORTING

**Zodpovednosť:**
- denne (21:00 CET)
- sumarizuje paper výsledky
- pošle report do Telegramu

---

## 🧭 MARKET REGIME LOGIKA (BTC)

BTC je **motor reality**, nie trade setup.

Používané signály:
- price change (15m)
- funding
- OI delta (Bybit)
- range width

Príklady režimov:
- RANGE / CHOP
- TRANSITION
- DELEVERAGING
- LONG UNWIND
- SHORT SQUEEZE

---

## 🪙 ALT LOGIKA (SETUP-ONLY)

Alty:
- ETHUSDT
- SOLUSDT
- ZECUSDT *(špeciálne správanie, nie BTC-driven)*

Setup:
- MEAN REVERT (lower / upper touch)
- LEN v povolených BTC režimoch
- LEN ak range width < limit

Alt/BTC páry:
- ETHBTC
- SOLBTC
- (ZECBTC nepoužívaný – ZEC má vlastnú dynamiku)

---

## ⚠️ DÔLEŽITÉ ZÁSADY

- **Signál ≠ obchod**
- Paper trading je nadradený manuálnemu pocitu
- Zlé obchody sú cenné dáta
- Nič sa „neladí pocitovo“

---

## 🛠️ PREVÁDZKA (RUNBOOK)

### Spustenie scanneru:
```bash
./run.sh
