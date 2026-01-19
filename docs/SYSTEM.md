# SYSTEM OVERVIEW – BTC Alerts & Paper Trading

## Purpose
Market-regime–driven scanner + paper executor.
No discretionary trading.
No emotions.
Data first.

---

## High-level flow

1. alerts.py
   - scans BTCUSDT every 30s
   - computes market regime
   - gates alt scans by BTC regime
   - emits setup signals to:
     - Telegram
     - signals.jsonl

2. paper_exec.py
   - tails signals.jsonl
   - creates paper trades
   - manages SL / TP / BE
   - writes:
     - paper_trades.csv
     - paper_state.json

3. reporting
   - daily paper report (21:00)
   - market regime messages (intraday)

---

## BTC as global regime engine

BTC defines:
- whether alts are tradable
- which setup types are allowed
- risk posture of the system

ALT trades NEVER override BTC regime.

---

## What this system is NOT

- not a signal spammer
- not discretionary
- not optimized for winrate
- not optimized for dopamine

It is a **filtering and learning system**.
