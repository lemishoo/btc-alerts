# HOWTO – Step by step (idiot-proof)

## Fresh VPS setup

1. Login
ssh user@server

2. Clone repo
git clone https://github.com/lemishoo/btc-alerts.git
cd btc-alerts

3. Create venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

4. Configure env
cp .env.example .env
nano .env

5. Start system
./restart.sh

6. Verify
./scripts/doctor.sh

---

## Common checks

- Is scanner running?
ps aux | grep alerts.py

- Is paper executor running?
ps aux | grep paper_exec.py

- Logs
tail -n 100 run.log
tail -n 100 paper_exec.log

---

## If something breaks

1. Stop everything
./stop.sh

2. Check logs
./scripts/doctor.sh

3. Restart
./restart.sh

Never debug live with emotions.
