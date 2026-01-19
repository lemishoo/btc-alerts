#!/usr/bin/env python3
import os
import re
import subprocess
import datetime as dt
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the same directory as this file
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

OUT_DIR = Path(os.getenv("REPORT_OUT_DIR", "/var/www/reports"))
SERVICE = os.getenv("REPORT_SERVICE", "btc-alerts")
REPORT_BASE_URL = os.getenv("REPORT_BASE_URL", "http://194.13.83.153/reports").rstrip("/")


def sh(cmd):
    return subprocess.check_output(cmd, text=True)


def journal_for_day(day: dt.date) -> str:
    # journalctl is picky about ISO timestamps; use "YYYY-MM-DD HH:MM:SS"
    start = dt.datetime(day.year, day.month, day.day, 0, 0, 0)
    end = start + dt.timedelta(days=1)

    since = start.strftime("%Y-%m-%d %H:%M:%S")
    until = end.strftime("%Y-%m-%d %H:%M:%S")

    return sh([
        "journalctl",
        "-u", SERVICE,
        "--since", since,
        "--until", until,
        "--no-pager",
    ])


def parse_setups(logtext: str):
    """
    We log setups like:
      "... | 🎯 SETUP | MEAN REVERT (Lower touch) → LONG | Regime: RANGE / CHOP | ..."
    or in some variants:
      "... 🎯 SETUP: BREAKOUT FAIL ..."
    We'll extract a reasonable "setup title" token for counting.
    """
    setups = []
    for line in logtext.splitlines():
        if "SETUP" not in line:
            continue

        # Prefer explicit "🎯 SETUP | <title> |"
        m = re.search(r"🎯\s*SETUP\s*\|\s*(.*?)\s*\|", line)
        if m:
            setups.append(m.group(1).strip())
            continue

        # Or "SETUP: <title>"
        m = re.search(r"SETUP:\s*(.*)$", line)
        if m:
            setups.append(m.group(1).strip())
            continue

        # Fallback: keep the whole line (rare)
        setups.append(line.strip())

    return setups


def parse_regimes(logtext: str):
    """
    We log regimes like:
      "... | 🧭 MARKET REGIME | TRANSITION | px15m ... "
    We'll extract the regime label right after MARKET REGIME.
    """
    regs = []
    for line in logtext.splitlines():
        if "MARKET REGIME" not in line:
            continue

        parts = [p.strip() for p in line.split("|")]
        # means: "...", "🧭 MARKET REGIME", "TRANSITION", "px15m ...", ...
        try:
            i = parts.index("🧭 MARKET REGIME")
            if i + 1 < len(parts):
                reg = parts[i + 1].strip()
                if reg:
                    regs.append(reg)
        except ValueError:
            # fallback try without emoji
            try:
                i = parts.index("MARKET REGIME")
                if i + 1 < len(parts):
                    reg = parts[i + 1].strip()
                    if reg:
                        regs.append(reg)
            except ValueError:
                pass

    return regs


def render_html(day: dt.date, setups, regimes) -> str:
    setup_counts = Counter(setups)
    regime_counts = Counter(regimes)

    def rows(counter: Counter):
        out = []
        for k, v in counter.most_common():
            out.append(f"<tr><td>{escape(k)}</td><td style='text-align:right'>{v}</td></tr>")
        return "\n".join(out) if out else "<tr><td colspan='2'><i>no data</i></td></tr>"

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>BTC Alerts Daily Report — {day.isoformat()}</title>
  <style>
    body {{ font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; margin: 24px; }}
    h1 {{ margin: 0 0 8px 0; }}
    .muted {{ color: #666; margin-bottom: 18px; }}
    .grid {{ display: grid; grid-template-columns: 1fr; gap: 18px; max-width: 980px; }}
    @media (min-width: 900px) {{ .grid {{ grid-template-columns: 1fr 1fr; }} }}
    .card {{ border: 1px solid #eee; border-radius: 12px; padding: 14px; background: #fff; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #f0f0f0; padding: 8px 6px; vertical-align: top; }}
    th {{ text-align: left; font-weight: 600; }}
    code {{ background:#fafafa; padding:2px 6px; border-radius:6px; }}
    .small {{ font-size: 12px; color: #777; }}
  </style>
</head>
<body>
  <h1>BTC Alerts — Daily report</h1>
  <div class="muted">Day: <code>{day.isoformat()}</code> · Service: <code>{escape(SERVICE)}</code></div>

  <div class="grid">
    <div class="card">
      <h2 style="margin:0 0 10px 0;">Setups</h2>
      <table>
        <thead><tr><th>Setup</th><th style="text-align:right">Count</th></tr></thead>
        <tbody>
          {rows(setup_counts)}
        </tbody>
      </table>
      <div class="small" style="margin-top:10px;">Parsed from journal lines containing <code>SETUP</code>.</div>
    </div>

    <div class="card">
      <h2 style="margin:0 0 10px 0;">Market regimes</h2>
      <table>
        <thead><tr><th>Regime</th><th style="text-align:right">Count</th></tr></thead>
        <tbody>
          {rows(regime_counts)}
        </tbody>
      </table>
      <div class="small" style="margin-top:10px;">Parsed from <code>🧭 MARKET REGIME</code> lines.</div>
    </div>
  </div>

  <div class="card" style="max-width:980px; margin-top:18px;">
    <h2 style="margin:0 0 10px 0;">Raw notes</h2>
    <div class="small">
      This report is a quick sanity check (counts + overview).
      Next step: compute “success” (TP/SL logic) once we define how to evaluate per setup.
    </div>
  </div>

</body>
</html>
"""


def escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&#39;"))


def main():
    # default = yesterday (server local time)
    if len(os.sys.argv) >= 2:
        # allow YYYY-MM-DD
        day = dt.date.fromisoformat(os.sys.argv[1])
    else:
        day = (dt.datetime.now() - dt.timedelta(days=1)).date()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    logtext = journal_for_day(day)
    setups = parse_setups(logtext)
    regimes = parse_regimes(logtext)

    html = render_html(day, setups, regimes)
    out_file = OUT_DIR / f"{day.isoformat()}.html"
    out_file.write_text(html, encoding="utf-8")

    # Print URL for caller (notify_report.py will read this)
    print(f"{REPORT_BASE_URL}/{out_file.name}")


if __name__ == "__main__":
    main()
