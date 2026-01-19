#!/usr/bin/env python3
import os, json, time, csv, math
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple, List

import ccxt  # pip install ccxt

# -----------------------------
# Config (ENV overridable)
# -----------------------------
SIGNALS_FILE = os.path.expanduser(os.getenv("SIGNALS_FILE", "~/apps/btc-alerts/signals.jsonl"))
STATE_FILE   = os.path.expanduser(os.getenv("PAPER_STATE_FILE", "~/apps/btc-alerts/paper_state.json"))

OUT_JSONL    = os.path.expanduser(os.getenv("PAPER_OUT_JSONL", "~/apps/btc-alerts/paper_trades.jsonl"))
OUT_CSV      = os.path.expanduser(os.getenv("PAPER_OUT_CSV", "~/apps/btc-alerts/paper_trades.csv"))

EXCHANGE_ID = os.getenv("PAPER_EXCHANGE_ID", "mexc").strip()
DEFAULT_LEVERAGE = int(os.getenv("PAPER_LEVERAGE", "5"))
START_EQUITY = float(os.getenv("PAPER_START_EQUITY", "1000.0"))

RISK_PCT = float(os.getenv("PAPER_RISK_PCT", "0.005"))  # 0.5% equity risk per trade
POLL_SEC = float(os.getenv("PAPER_POLL_SEC", "2.0"))

MAX_OPEN_TRADES_PER_SYMBOL = int(os.getenv("PAPER_MAX_OPEN_PER_SYMBOL", "1"))

# Entry logic
ENTRY_TIMEOUT_SEC = int(os.getenv("PAPER_ENTRY_TIMEOUT_SEC", "1800"))  # 30 min
ENTRY_PRICE_MODE = os.getenv("PAPER_ENTRY_PRICE_MODE", "ZONE").upper()
# ZONE: entry = lower_hi (LONG) / upper_lo (SHORT)  (viac fillov)
# LOHI: entry = lower_lo (LONG) / upper_hi (SHORT)  (menej fillov)

# Take profit logic
TP1_CLOSE_FRAC = float(os.getenv("TP1_CLOSE_FRAC", "0.50"))  # close 50% at TP1
MOVE_SL_TO_BE_ON_TP1 = os.getenv("MOVE_SL_TO_BE_ON_TP1", "1").strip() in ("1","true","TRUE","yes","YES")

# Stop fill realism
STOP_FILL_MODE = os.getenv("PAPER_STOP_FILL_MODE", "CAP").upper()
# CAP: keď cena preletí stop, fill na SL cene (neprestrelí stratu)
# MARKET: fill na aktuálnej cene (môže prestreliť)

# Small BE buffer (fees)
BE_BUFFER_PCT = float(os.getenv("PAPER_BE_BUFFER_PCT", "0.0"))  # napr 0.00005 = +0.005%

# Safety: ignore duplicated events (same ts+symbol+setup)
DEDUP_WINDOW = int(os.getenv("PAPER_DEDUP_WINDOW", "4000"))

# -----------------------------
# Helpers
# -----------------------------
def now_iso() -> str:
    # local time with tz offset
    return datetime.now().astimezone().isoformat(timespec="seconds")

def safe_float(v, default=float("nan")) -> float:
    try:
        return float(v)
    except Exception:
        return default

def ensure_parent(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)

def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, obj: Any) -> None:
    ensure_parent(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def write_jsonl(path: str, row: Dict[str, Any]) -> None:
    ensure_parent(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

def ensure_csv_header(path: str, header: List[str]) -> None:
    ensure_parent(path)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)

def append_csv(path: str, header: List[str], row: Dict[str, Any]) -> None:
    ensure_csv_header(path, header)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([row.get(k, "") for k in header])

# -----------------------------
# Exchange
# -----------------------------
def make_exchange() -> ccxt.Exchange:
    klass = getattr(ccxt, EXCHANGE_ID)
    ex = klass({
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",
        }
    })
    ex.load_markets()
    return ex

# -----------------------------
# Data model
# -----------------------------
@dataclass
class Trade:
    ts_created: str
    ts_closed: str
    trade_id: str
    exchange: str
    market: str
    symbol: str
    symbol_raw: str
    setup: str
    regime: str
    side: str          # LONG / SHORT
    status: str        # OPEN / CLOSED / CANCELED
    leverage: int

    entry: float
    sl: float
    tp1: float
    tp2: float
    qty: float

    filled_entry: float
    filled_qty: float

    tp1_hit: bool
    tp1_qty_closed: float

    pnl_usdt: float
    pnl_pct_equity: float
    close_reason: str

# -----------------------------
# Planning logic from signal event
# -----------------------------
def plan_from_event(evt: Dict[str, Any], equity: float, leverage: int) -> Optional[Trade]:
    sym = str(evt.get("symbol","")).strip()
    sym_raw = str(evt.get("symbol_raw","")).strip()
    setup = str(evt.get("setup","")).strip()
    regime = str(evt.get("regime","")).strip()
    market = str(evt.get("market","futures")).strip()
    exchange = str(evt.get("exchange", EXCHANGE_ID)).strip()

    lower = evt.get("lower")
    upper = evt.get("upper")
    if not sym or not isinstance(lower, list) or not isinstance(upper, list) or len(lower) != 2 or len(upper) != 2:
        return None

    lower_lo = safe_float(lower[0]); lower_hi = safe_float(lower[1])
    upper_lo = safe_float(upper[0]); upper_hi = safe_float(upper[1])
    if any(math.isnan(x) for x in (lower_lo, lower_hi, upper_lo, upper_hi)):
        return None

    close_px = safe_float(evt.get("close"))
    if math.isnan(close_px) or close_px <= 0:
        return None

    # only implement the current strategy: mean revert lower touch LONG
    # (do not accidentally start trading other things)
    side = "LONG"
    if "SHORT" in setup.upper():
        side = "SHORT"

    # Entry choice (fill-rate tuning)
    if ENTRY_PRICE_MODE == "LOHI":
        entry = lower_lo if side == "LONG" else upper_hi
    else:
        entry = lower_hi if side == "LONG" else upper_lo

    # SL/TP based on zone geometry (simple, stable)
    # Use zone height as "unit"
    zone_h = max(1e-9, (lower_hi - lower_lo))
    zone_u = max(1e-9, (upper_hi - upper_lo))
    pad = max(0.5, (zone_h if side=="LONG" else zone_u) * 1.5)

    if side == "LONG":
        sl = lower_lo - pad
        tp1 = min(upper_lo, entry + (upper_lo - entry) * 0.35)  # modest TP1
        tp2 = upper_lo  # target the upper boundary
        if tp2 <= entry:
            tp2 = max(entry * 1.001, entry + abs(entry - sl) * 1.2)
        if tp1 <= entry:
            tp1 = entry + abs(entry - sl) * 0.6
    else:
        sl = upper_hi + pad
        tp1 = max(lower_hi, entry - (entry - lower_hi) * 0.35)
        tp2 = lower_hi
        if tp2 >= entry:
            tp2 = min(entry * 0.999, entry - abs(entry - sl) * 1.2)
        if tp1 >= entry:
            tp1 = entry - abs(entry - sl) * 0.6

    # risk sizing
    per_unit_risk = abs(entry - sl)
    if per_unit_risk <= 0:
        return None
    risk_usdt = max(0.0, equity * RISK_PCT)
    qty = (risk_usdt / per_unit_risk) * leverage
    if not math.isfinite(qty) or qty <= 0:
        return None

    # Round qty a bit for readability (paper)
    qty = float(f"{qty:.6f}")

    trade_id = f"{int(time.time())}-{sym_raw}-{setup[:16]}"

    t = Trade(
        ts_created=now_iso(),
        ts_closed="",
        trade_id=trade_id,
        exchange=exchange,
        market=market,
        symbol=sym,
        symbol_raw=sym_raw,
        setup=setup,
        regime=regime,
        side=side,
        status="OPEN",
        leverage=leverage,
        entry=float(f"{entry:.4f}"),
        sl=float(f"{sl:.4f}"),
        tp1=float(f"{tp1:.4f}"),
        tp2=float(f"{tp2:.4f}"),
        qty=qty,
        filled_entry=0.0,
        filled_qty=0.0,
        tp1_hit=False,
        tp1_qty_closed=0.0,
        pnl_usdt=0.0,
        pnl_pct_equity=0.0,
        close_reason="",
    )
    return t

# -----------------------------
# PnL calc
# -----------------------------
def pnl_for_move(side: str, entry: float, exit_px: float, qty: float, leverage: int) -> float:
    # Paper PnL in USDT for linear swap approximation.
    if qty <= 0:
        return 0.0
    if side == "LONG":
        return (exit_px - entry) * qty
    else:
        return (entry - exit_px) * qty

# -----------------------------
# Tick / fill simulation
# -----------------------------
def should_fill_entry(side: str, entry: float, px: float) -> bool:
    # If price touched/passed entry
    if side == "LONG":
        return px <= entry
    else:
        return px >= entry

def stop_triggered(side: str, sl: float, px: float) -> bool:
    if side == "LONG":
        return px <= sl
    else:
        return px >= sl

def tp1_triggered(side: str, tp1: float, px: float) -> bool:
    if side == "LONG":
        return px >= tp1
    else:
        return px <= tp1

def tp2_triggered(side: str, tp2: float, px: float) -> bool:
    if side == "LONG":
        return px >= tp2
    else:
        return px <= tp2

def apply_be_move(tr: Trade) -> None:
    if not MOVE_SL_TO_BE_ON_TP1:
        return
    if tr.filled_qty <= 0:
        return
    if tr.side == "LONG":
        be = tr.filled_entry * (1.0 + BE_BUFFER_PCT)
        tr.sl = float(f"{be:.4f}")
    else:
        be = tr.filled_entry * (1.0 - BE_BUFFER_PCT)
        tr.sl = float(f"{be:.4f}")

def stop_fill_price(tr: Trade, px: float) -> float:
    if STOP_FILL_MODE == "MARKET":
        return px
    # CAP mode
    return tr.sl

# -----------------------------
# IO state / offsets
# -----------------------------
def load_state() -> Dict[str, Any]:
    st = load_json(STATE_FILE, {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("offset", 0)
    st.setdefault("equity", START_EQUITY)
    st.setdefault("open_trades", {})
    st.setdefault("dedup", [])
    return st

def save_state(st: Dict[str, Any]) -> None:
    save_json(STATE_FILE, st)

def read_signals_tail(path: str, start_offset: int) -> Tuple[int, List[Dict[str, Any]]]:
    if not os.path.exists(path):
        return start_offset, []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        f.seek(start_offset)
        while True:
            line = f.readline()
            if not line:
                break
            try:
                evt = json.loads(line)
                if isinstance(evt, dict):
                    out.append(evt)
            except Exception:
                pass
        end = f.tell()
    return end, out

def dedup_key(evt: Dict[str, Any]) -> str:
    # stable-ish identity
    return f'{evt.get("ts","")}|{evt.get("symbol_raw","")}|{evt.get("setup","")}'

def dedup_accept(st: Dict[str, Any], evt: Dict[str, Any]) -> bool:
    key = dedup_key(evt)
    arr = st.get("dedup", [])
    if not isinstance(arr, list):
        arr = []
    if key in arr:
        return False
    arr.append(key)
    if len(arr) > DEDUP_WINDOW:
        arr = arr[-DEDUP_WINDOW:]
    st["dedup"] = arr
    return True

# -----------------------------
# CSV header
# -----------------------------
CSV_HEADER = [
    "ts_created","ts_closed","trade_id","exchange","market","symbol","symbol_raw","setup","regime",
    "side","status","leverage","entry","sl","tp1","tp2","qty","filled_entry","filled_qty",
    "tp1_hit","tp1_qty_closed","pnl_usdt","pnl_pct_equity","close_reason"
]

# -----------------------------
# Main loop
# -----------------------------
def main() -> None:
    ex = make_exchange()

    st = load_state()
    equity = float(st.get("equity", START_EQUITY))
    offset = int(st.get("offset", 0))

    open_trades: Dict[str, Dict[str, Any]] = st.get("open_trades", {})
    if not isinstance(open_trades, dict):
        open_trades = {}

    print(f"[paper] start equity={equity:.2f} USDT, leverage={DEFAULT_LEVERAGE}x, risk={RISK_PCT*100:.2f}%", flush=True)
    print(f"[paper] reading signals from {SIGNALS_FILE}", flush=True)
    print(f"[paper] state={STATE_FILE} offset={offset} poll={POLL_SEC}s entry_mode={ENTRY_PRICE_MODE} stop_fill={STOP_FILL_MODE} be_on_tp1={'on' if MOVE_SL_TO_BE_ON_TP1 else 'off'}", flush=True)

    ensure_csv_header(OUT_CSV, CSV_HEADER)

    while True:
        # 1) ingest new signals
        offset2, evts = read_signals_tail(SIGNALS_FILE, offset)
        offset = offset2
        st["offset"] = offset

        for evt in evts:
            if not dedup_accept(st, evt):
                continue

            sym = str(evt.get("symbol","")).strip()
            sym_raw = str(evt.get("symbol_raw","")).strip()
            if not sym:
                continue

            # enforce per-symbol max open
            per_sym = [k for k,v in open_trades.items() if isinstance(v, dict) and v.get("symbol") == sym and v.get("status")=="OPEN"]
            if len(per_sym) >= MAX_OPEN_TRADES_PER_SYMBOL:
                continue

            t = plan_from_event(evt, equity=equity, leverage=DEFAULT_LEVERAGE)
            if not t:
                continue

            open_trades[t.trade_id] = asdict(t)
            print(f"[paper] PLAN {t.symbol} entry={t.entry:.4f} sl={t.sl:.4f} tp1={t.tp1:.4f} tp2={t.tp2:.4f} qty={t.qty:.6f}", flush=True)

        # 2) tick all open trades
        # cache ticker per symbol to avoid hammering
        symbols = sorted({v.get("symbol") for v in open_trades.values() if isinstance(v, dict) and v.get("status")=="OPEN" and v.get("symbol")})
        px_map: Dict[str, float] = {}

        for sym in symbols:
            try:
                tkr = ex.fetch_ticker(sym)
                px = safe_float(tkr.get("last"))
                if math.isfinite(px) and px > 0:
                    px_map[sym] = px
            except Exception:
                continue

        # iterate trades
        for trade_id, raw in list(open_trades.items()):
            if not isinstance(raw, dict) or raw.get("status") != "OPEN":
                continue

            sym = raw.get("symbol")
            px = px_map.get(sym)
            if px is None:
                continue

            # rebuild Trade from raw dict
            tr = Trade(**raw)

            created_dt = datetime.fromisoformat(tr.ts_created)
            age = (datetime.now().astimezone() - created_dt).total_seconds()

            # ENTRY fill
            if tr.filled_qty <= 0:
                if should_fill_entry(tr.side, tr.entry, px):
                    tr.filled_entry = tr.entry
                    tr.filled_qty = tr.qty
                else:
                    if age >= ENTRY_TIMEOUT_SEC:
                        tr.status = "CANCELED"
                        tr.ts_closed = now_iso()
                        tr.close_reason = f"ENTRY_TIMEOUT_{ENTRY_TIMEOUT_SEC}s"
                        tr.pnl_usdt = 0.0
                        tr.pnl_pct_equity = 0.0

                        row = asdict(tr)
                        write_jsonl(OUT_JSONL, row)
                        append_csv(OUT_CSV, CSV_HEADER, row)

                        open_trades[trade_id] = row
                        print(f"[paper] CANCEL {sym} reason={tr.close_reason} entry={tr.entry:.4f}", flush=True)
                    else:
                        # keep waiting
                        open_trades[trade_id] = asdict(tr)
                continue

            # From here: position open (filled)
            # SL check first (protect)
            if stop_triggered(tr.side, tr.sl, px):
                exit_px = stop_fill_price(tr, px)
                pnl = pnl_for_move(tr.side, tr.filled_entry, exit_px, tr.filled_qty, tr.leverage)

                tr.pnl_usdt += pnl
                tr.status = "CLOSED"
                tr.ts_closed = now_iso()
                tr.close_reason = "SL"

                tr.pnl_pct_equity = (tr.pnl_usdt / equity) * 100.0 if equity > 0 else 0.0
                equity += tr.pnl_usdt

                row = asdict(tr)
                write_jsonl(OUT_JSONL, row)
                append_csv(OUT_CSV, CSV_HEADER, row)

                open_trades[trade_id] = row
                print(f"[paper] CLOSE {sym} SL hit px={px:.4f} fill={exit_px:.4f} pnl={tr.pnl_usdt:.2f}USDT", flush=True)
                continue

            # TP1
            if (not tr.tp1_hit) and tp1_triggered(tr.side, tr.tp1, px):
                qty_close = tr.filled_qty * TP1_CLOSE_FRAC
                qty_close = max(0.0, min(qty_close, tr.filled_qty))

                pnl_add = pnl_for_move(tr.side, tr.filled_entry, tr.tp1, qty_close, tr.leverage)
                tr.pnl_usdt += pnl_add

                tr.filled_qty -= qty_close
                tr.tp1_hit = True
                tr.tp1_qty_closed += qty_close

                # move SL to BE after TP1
                apply_be_move(tr)

                print(f"[paper] TP1 {sym} px={px:.4f} close_qty={qty_close:.6f} pnl_add={pnl_add:.2f} new_sl={tr.sl:.4f}", flush=True)

                # If fully closed at TP1 (edge case)
                if tr.filled_qty <= 1e-12:
                    tr.status = "CLOSED"
                    tr.ts_closed = now_iso()
                    tr.close_reason = "TP1_FULL"
                    tr.pnl_pct_equity = (tr.pnl_usdt / equity) * 100.0 if equity > 0 else 0.0
                    equity += tr.pnl_usdt

                    row = asdict(tr)
                    write_jsonl(OUT_JSONL, row)
                    append_csv(OUT_CSV, CSV_HEADER, row)

                    open_trades[trade_id] = row
                    print(f"[paper] CLOSE {sym} TP1_FULL pnl={tr.pnl_usdt:.2f}USDT", flush=True)
                else:
                    open_trades[trade_id] = asdict(tr)
                continue

            # TP2
            if tp2_triggered(tr.side, tr.tp2, px):
                qty_close = tr.filled_qty
                pnl_add = pnl_for_move(tr.side, tr.filled_entry, tr.tp2, qty_close, tr.leverage)
                tr.pnl_usdt += pnl_add
                tr.filled_qty = 0.0

                tr.status = "CLOSED"
                tr.ts_closed = now_iso()
                tr.close_reason = "TP2"
                tr.pnl_pct_equity = (tr.pnl_usdt / equity) * 100.0 if equity > 0 else 0.0
                equity += tr.pnl_usdt

                row = asdict(tr)
                write_jsonl(OUT_JSONL, row)
                append_csv(OUT_CSV, CSV_HEADER, row)

                open_trades[trade_id] = row
                print(f"[paper] CLOSE {sym} TP2 hit px={px:.4f} pnl={tr.pnl_usdt:.2f}USDT", flush=True)
                continue

            # still open
            open_trades[trade_id] = asdict(tr)

        # persist state
        st["equity"] = equity
        st["open_trades"] = open_trades
        save_state(st)

        time.sleep(POLL_SEC)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("[paper] stopped by user", flush=True)
