import subprocess
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import alerts

BASE = "/home/michal/apps/btc-alerts"
PY = BASE + "/.venv/bin/python"
REPORT = BASE + "/daily_report.py"

def main():
    url = subprocess.check_output(
        [PY, REPORT],
        cwd=BASE,
        text=True
    ).strip()

    alerts.send_regime(f"📊 Daily report ready:\n{url}")

if __name__ == "__main__":
    main()


