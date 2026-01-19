#!/usr/bin/env python3
import os, json, math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

import requests

PAPER_JSONL = os.path.expanduser(os.getenv("OUT_JSONL", "~/apps/btc-alerts/paper_trades.jsonl"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PAPER_REPORT_CHAT_ID = os.getenv("PAPER_REPORT_CHAT_ID", "").strip()

TZ = os.getenv("TZ_LOCAL", "Europe/Bratislava")
LOCAL_TZ = ZoneInfo(TZ)

# Report window: "today" in local time
def local_day_bounds(dt_local: datetime) -> Tuple[datetime, datetime]:
    start = dt_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end

def parse_iso_any(s: str) -> Optional[datetime]:
    if not s:
        return None
    # handles 2026-01-14T16:16:00+01:00 or with seconds
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def tg_send(chat_id: str, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=20)
        if r.status_code != 200:
            print(f"[report] telegram failed HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[report] telegram error: {e}")

def load_trades(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
    return out

def fmt(x: float, nd: int = 2) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.{nd}f}"

def main() -> None:
    now_local = datetime.now(LOCAL_TZ)
    start_local, end_local = local_day_bounds(now_local)

    trades = load_trades(PAPER_JSONL)

    # Filter: closed trades with ts_closed within today local
    today: List[Dict[str, Any]] = []
    for t in trades:
        ts_closed = parse_iso_any(str(t.get("ts_closed", "")))
        if not ts_closed:
            continue
        ts_closed_local = ts_closed.astimezone(LOCAL_TZ)
        if start_local <= ts_closed_local < end_local:
            today.append(t)

    n = len(today)
    pnl = sum(float(t.get("pnl_usdt", 0.0) or 0.0) for t in today)
    wins = sum(1 for t in today if float(t.get("pnl_usdt", 0.0) or 0.0) > 0)
    losses = sum(1 for t in today if float(t.get("pnl_usdt", 0.0) or 0.0) < 0)
    flats = n - wins - losses
    winrate = (wins / n * 100.0) if n else 0.0

    avg = (pnl / n) if n else 0.0

    # Group by symbol
    by_sym: Dict[str, Dict[str, float]] = {}
    for t in today:
        sym = str(t.get("symbol", "UNKNOWN"))
        by_sym.setdefault(sym, {"n": 0, "pnl": 0.0})
        by_sym[sym]["n"] += 1
        by_sym[sym]["pnl"] += float(t.get("pnl_usdt", 0.0) or 0.0)

    top = sorted(by_sym.items(), key=lambda kv: kv[1]["pnl"], reverse=True)[:5]
    bottom = sorted(by_sym.items(), key=lambda kv: kv[1]["pnl"])[:5]

    date_str = start_local.strftime("%Y-%m-%d")
    header = f"📒 PAPER DAILY REPORT ({TZ})\n{date_str}"
    summary = (
        f"\n\nTrades: {n} | Wins: {wins} Losses: {losses} Flat: {flats} | Winrate: {fmt(winrate,1)}%\n"
        f"PnL: {fmt(pnl,2)} USDT | Avg/trade: {fmt(avg,2)} USDT"
    )

    def bucket(title: str, rows: List[Tuple[str, Dict[str, float]]]) -> str:
        if not rows:
            return f"\n\n{title}\n(none)"
        lines = [f"{sym}: {int(v['n'])} | {fmt(v['pnl'],2)} USDT" for sym, v in rows]
        return "\n\n" + title + "\n" + "\n".join(lines)

    text = header + summary + bucket("Top symbols", top) + bucket("Bottom symbols", bottom)

    chat = PAPER_REPORT_CHAT_ID or TELEGRAM_CHAT_ID
    if not chat:
        print("[report] no TELEGRAM_CHAT_ID / PAPER_REPORT_CHAT_ID set")
        print(text)
        return

    tg_send(chat, text)
    print("[report] sent")

if __name__ == "__main__":
    main()
