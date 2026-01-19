#!/usr/bin/env python3
# === BTC MM ALERT ENGINE (HYBRID + SETUPS) + BTC→ALT FILTER + ALT/BTC GATE ===
# BTC: price + funding + klines + (Bybit OIΔ15m) -> MARKET REGIME engine
# ALTs (ETH/SOL default): setup-only scanning gated by BTC regime_key
# Extra: ALT/BTC bias gate (ETHBTC,SOLBTC) for higher-quality entries
#
# ENV:
#   SYMBOL=BTCUSDT
#   INTERVAL_SEC=30
#   TELEGRAM_BOT_TOKEN=...
#   TELEGRAM_CHAT_ID=...                 (regime channel)
#   TELEGRAM_SIGNALS_CHAT_ID=...         (signals channel, optional; fallback to TELEGRAM_CHAT_ID)
#   STATE_PATH=./state.json
#   SETUP_COOLDOWN_SEC=600
#
#   SETUP_SYMBOLS=ETHUSDT,SOLUSDT        (ZEC removed from default; can add back via env)
#   ALT_ENABLED_BTC_REGIMES=RANGE,DELEVERAGING,LONG_UNWIND,SHORT_SQUEEZE
#
#   MEAN_REVERT_MAX_WIDTH_PCT=0.45       (block mean-revert if zone width too wide)
#   OI_CONTRA_FILTER=1                   (1=on, 0=off)
#   OI_CONTRA_MIN_ABS=250                (min abs OIΔ(15m) for contra confirmation)
#
#   WATCH_ALT_BTC=ETHBTC,SOLBTC          (ALT/BTC symbols to use as bias gate)
#   ALT_BTC_BIAS_PCT=0.03                (tolerance: +/- bias percent)
#
# Paper-trade output:
#   SIGNALS_FILE=~/apps/btc-alerts/signals.jsonl
#
# Anti-spam:
#   Degraded alert only if API down continuously > 180s; recovered once.

import os
import time
import json
import math
import random
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("btc-alerts")

# -------------------- ENV --------------------
SYMBOL = os.getenv("SYMBOL", "BTCUSDT").upper()
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "30"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_SIGNALS_CHAT_ID = os.getenv("TELEGRAM_SIGNALS_CHAT_ID", "").strip()

STATE_PATH = os.getenv("STATE_PATH", "./state.json")
SETUP_COOLDOWN_SEC = int(os.getenv("SETUP_COOLDOWN_SEC", "600"))

# Default: ZEC out (not BTC-driven); user can add back in .env if needed.
SETUP_SYMBOLS = [
    s.strip().upper()
    for s in os.getenv("SETUP_SYMBOLS", "ETHUSDT,SOLUSDT").split(",")
    if s.strip()
]
ALT_ENABLED_BTC_REGIMES = {
    s.strip().upper()
    for s in os.getenv("ALT_ENABLED_BTC_REGIMES", "RANGE,DELEVERAGING,LONG_UNWIND,SHORT_SQUEEZE").split(",")
    if s.strip()
}

# Mean-revert quality filters
MEAN_REVERT_MAX_WIDTH_PCT = float(os.getenv("MEAN_REVERT_MAX_WIDTH_PCT", "0.45"))
OI_CONTRA_FILTER = os.getenv("OI_CONTRA_FILTER", "1").strip() == "1"
OI_CONTRA_MIN_ABS = float(os.getenv("OI_CONTRA_MIN_ABS", "250"))

# ALT/BTC bias gate (spot pairs on Binance)
WATCH_ALT_BTC = [s.strip().upper() for s in os.getenv("WATCH_ALT_BTC", "ETHBTC,SOLBTC").split(",") if s.strip()]
ALT_BTC_BIAS_PCT = float(os.getenv("ALT_BTC_BIAS_PCT", "0.03"))

# Paper-trade signal output file (JSONL)
SIGNALS_FILE = os.path.expanduser(os.getenv("SIGNALS_FILE", "~/apps/btc-alerts/signals.jsonl"))

BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_SPOT = "https://api.binance.com"
BYBIT_API = "https://api.bybit.com"

# -------------------- Paper signal emitter --------------------
def to_ccxt_swap_symbol(binance_symbol: str) -> str:
    """
    Convert Binance-style 'ETHUSDT' -> CCXT swap style 'ETH/USDT:USDT'
    """
    s = (binance_symbol or "").upper().strip()
    if not s.endswith("USDT") or len(s) <= 4:
        return s
    base = s[:-4]
    return f"{base}/USDT:USDT"

def emit_signal_event(event: Dict[str, Any]) -> None:
    """
    Append one JSON line to SIGNALS_FILE for paper_exec.py.
    Safe: errors are swallowed to not break alerts loop.
    """
    try:
        os.makedirs(os.path.dirname(SIGNALS_FILE), exist_ok=True)
        event = dict(event)
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")  # local tz
        with open(SIGNALS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass

# -------------------- HTTP session with retry --------------------
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=8, connect=8, read=8, status=8,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": "btc-alerts/1.4 (+systemd; requests)",
        "Accept": "application/json,text/plain,*/*",
        "Connection": "keep-alive",
    })
    return s

SESSION = make_session()

# -------------------- Telegram (2 channels) --------------------
def post_form(url: str, data: Dict[str, Any], timeout: float = 12.0) -> Tuple[int, str]:
    try:
        r = SESSION.post(url, data=data, timeout=timeout)
        return int(r.status_code), (r.text or "")
    except Exception as e:
        return 0, str(e)

def tg_send(chat_id: str, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    code, body = post_form(url, {"chat_id": chat_id, "text": text}, timeout=20)
    if code != 200:
        log.warning(f"Telegram send failed HTTP {code}: {str(body)[:200]}")

def send_regime(text: str) -> None:
    tg_send(TELEGRAM_CHAT_ID, text)

def send_signal(text: str) -> None:
    chat = TELEGRAM_SIGNALS_CHAT_ID or TELEGRAM_CHAT_ID
    tg_send(chat, text)

# -------------------- Anti-spam API health gate (DEGRADED/RECOVERED) --------------------
_HEALTH: Dict[str, Dict[str, Any]] = {
    "Binance fapi": {"down_since": None, "degraded_sent": False, "last_degraded_sent": None},
    "Binance spot": {"down_since": None, "degraded_sent": False, "last_degraded_sent": None},
    "Bybit api":    {"down_since": None, "degraded_sent": False, "last_degraded_sent": None},
}

DEGRADED_AFTER_S = 180     # 3 min continuous failure => 1x degraded
DEGRADED_GAP_S = 1800      # then no more degraded for 30 min

def _src_from_url(url: str) -> str:
    u = (url or "").lower()
    if "fapi.binance.com" in u:
        return "Binance fapi"
    if "api.binance.com" in u:
        return "Binance spot"
    if "api.bybit.com" in u:
        return "Bybit api"
    return "HTTP"

def _health_fail(src: str) -> None:
    if src not in _HEALTH:
        return
    st = _HEALTH[src]
    now = time.time()

    if st["down_since"] is None:
        st["down_since"] = now

    down_for = now - st["down_since"]
    if down_for < DEGRADED_AFTER_S:
        return

    if st["degraded_sent"]:
        return

    last = st["last_degraded_sent"]
    if last is not None and (now - last) < DEGRADED_GAP_S:
        return

    send_regime(f"⚠️ DATA DEGRADED: {src} timeouts/connection issues. Retrying…")
    st["degraded_sent"] = True
    st["last_degraded_sent"] = now

def _health_ok(src: str) -> None:
    if src not in _HEALTH:
        return
    st = _HEALTH[src]
    if st["down_since"] is None:
        return

    down_for = int(time.time() - st["down_since"])
    send_regime(f"✅ DATA OK again: {src} recovered after {down_for}s")

    st["down_since"] = None
    st["degraded_sent"] = False

def fetch_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: float = 12.0, max_tries: int = 3) -> Optional[Any]:
    src = _src_from_url(url)

    for attempt in range(1, max_tries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)

            if r.status_code >= 400:
                if r.status_code in (429, 500, 502, 503, 504):
                    _health_fail(src)
                    wait = min(30.0, (2 ** (attempt - 1)) + random.random())
                    log.warning(f"HTTP {r.status_code} transient ({src}) {url} | try {attempt}/{max_tries} | sleep {wait:.1f}s")
                    time.sleep(wait)
                    continue
                else:
                    log.warning(f"HTTP {r.status_code} hard ({src}) GET {url}")
                    return None

            data = r.json()
            _health_ok(src)
            return data

        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError) as e:
            _health_fail(src)
            wait = min(30.0, (2 ** (attempt - 1)) + random.random())
            log.warning(f"NET error ({src}) (try {attempt}/{max_tries}) {type(e).__name__}: {e} | sleep {wait:.1f}s")
            time.sleep(wait)

        except ValueError:
            log.warning(f"Bad/non-JSON response ({src}) from {url}")
            return None

        except Exception as e:
            log.exception(f"Unexpected GET error ({src}) {url}: {e}")
            return None

    return None

# -------------------- State --------------------
def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"Failed to save state: {e}")

# -------------------- Helpers --------------------
def safe_float(v: Any, default: float = float("nan")) -> float:
    try:
        return float(v)
    except Exception:
        return default

def fmt_int(x: float) -> str:
    return f"{int(round(x)):,}".replace(",", " ")

def fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"

# -------------------- Binance: premiumIndex + klines --------------------
def binance_premium_index(symbol: str) -> Optional[Dict[str, Any]]:
    data = fetch_json(f"{BINANCE_FAPI}/fapi/v1/premiumIndex", params={"symbol": symbol})
    return data if isinstance(data, dict) else None

def binance_fapi_klines(symbol: str, interval: str, limit: int) -> Optional[List[List[Any]]]:
    data = fetch_json(f"{BINANCE_FAPI}/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
    return data if isinstance(data, list) else None

def binance_spot_klines(symbol: str, interval: str, limit: int) -> Optional[List[List[Any]]]:
    data = fetch_json(f"{BINANCE_SPOT}/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
    return data if isinstance(data, list) else None

def px_change_15m_from_1m(kl_1m: List[List[Any]]) -> Optional[float]:
    if len(kl_1m) < 20:
        return None
    close_now = safe_float(kl_1m[-1][4])
    close_15m = safe_float(kl_1m[-16][4])
    if close_now <= 0 or close_15m <= 0:
        return None
    return (close_now / close_15m - 1.0) * 100.0

def atr_from_15m(kl_15m: List[List[Any]], period: int = 14) -> Optional[float]:
    if len(kl_15m) < period + 2:
        return None
    highs = [safe_float(k[2]) for k in kl_15m]
    lows = [safe_float(k[3]) for k in kl_15m]
    closes = [safe_float(k[4]) for k in kl_15m]
    trs: List[float] = []
    for i in range(1, len(kl_15m)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    recent = trs[-period:]
    return (sum(recent) / len(recent)) if recent else None

# -------------------- Bybit: OI history -> OIΔ(15m) --------------------
def bybit_oi_delta_15m(symbol: str) -> Optional[float]:
    url = f"{BYBIT_API}/v5/market/open-interest"
    params = {"category": "linear", "symbol": symbol, "intervalTime": "5min", "limit": 4}
    data = fetch_json(url, params=params, timeout=12)
    if not isinstance(data, dict):
        return None
    result = data.get("result", {})
    lst = result.get("list", None)
    if not isinstance(lst, list) or len(lst) < 2:
        return None
    rows: List[Tuple[int, float]] = []
    for it in lst:
        if not isinstance(it, dict):
            continue
        ts_raw = it.get("timestamp", it.get("time"))
        oi_raw = it.get("openInterest", it.get("open_interest", it.get("oi")))
        ts = int(safe_float(ts_raw, default=float("nan"))) if ts_raw is not None else None
        oi = safe_float(oi_raw)
        if ts is None or math.isnan(oi):
            continue
        rows.append((ts, oi))
    if len(rows) < 2:
        return None
    rows.sort(key=lambda x: x[0])
    return rows[-1][1] - rows[0][1]

# -------------------- Zones --------------------
@dataclass
class Zones:
    upper_lo: float
    upper_hi: float
    lower_lo: float
    lower_hi: float
    width_pct: float

def compute_zones(kl_1m: List[List[Any]], atr: Optional[float], lookback_min: int = 180) -> Optional[Zones]:
    if not kl_1m:
        return None
    n = min(len(kl_1m), max(60, lookback_min))
    sample = kl_1m[-n:]
    highs = [safe_float(k[2]) for k in sample]
    lows = [safe_float(k[3]) for k in sample]
    closes = [safe_float(k[4]) for k in sample if safe_float(k[4]) > 0]
    if not closes:
        return None
    recent_high = max(highs)
    recent_low = min(lows)
    mid = closes[-1]
    pad = (atr * 0.25) if (atr and atr > 0) else 0.0
    pad = max(pad, mid * 0.0007)
    upper_hi = recent_high
    upper_lo = recent_high - pad
    lower_lo = recent_low
    lower_hi = recent_low + pad
    width = max(0.0, upper_lo - lower_hi)
    width_pct = (width / mid) * 100.0 if mid > 0 else 0.0
    return Zones(upper_lo, upper_hi, lower_lo, lower_hi, width_pct)

# -------------------- Regime --------------------
def classify_regime(px15m: Optional[float], oi15m: Optional[float], width_pct: Optional[float]) -> str:
    if px15m is None or width_pct is None:
        return "UNKNOWN"
    if oi15m is None:
        if width_pct <= 0.30 and abs(px15m) <= 0.25:
            return "RANGE / CHOP (OI n/a)"
        return "TRANSITION (OI n/a)"
    if oi15m < -250 and px15m < -0.20:
        return "DELEVERAGING / LONG UNWIND"
    if oi15m < -250 and px15m > +0.20:
        return "SHORT COVER / SQUEEZE"
    if width_pct <= 0.30 and abs(px15m) <= 0.25 and abs(oi15m) <= 200:
        return "RANGE / CHOP"
    return "TRANSITION"

def norm_regime(regime_raw: str) -> str:
    r = (regime_raw or "").upper()
    if "RANGE" in r:
        return "RANGE"
    if "TRANSITION" in r:
        return "TRANSITION"
    if "SQUEEZE" in r or "SHORT COVER" in r:
        return "SHORT_SQUEEZE"
    if "LONG UNWIND" in r:
        return "LONG_UNWIND"
    if "DELEVERAGING" in r:
        return "DELEVERAGING"
    return "UNKNOWN"

# -------------------- SETUP Detection --------------------
@dataclass
class SetupSignal:
    key: str
    title: str
    direction: str
    info: str

def detect_setups(regime_key: str, kl_1m: List[List[Any]], z: Zones) -> List[SetupSignal]:
    if len(kl_1m) < 3:
        return []

    h1 = safe_float(kl_1m[-1][2]); l1 = safe_float(kl_1m[-1][3]); c1 = safe_float(kl_1m[-1][4])
    h0 = safe_float(kl_1m[-2][2]); l0 = safe_float(kl_1m[-2][3])

    sigs: List[SetupSignal] = []

    # A) Sweep + reclaim (upper -> short)
    swept_up = (h1 > z.upper_hi) or (h0 > z.upper_hi)
    reclaimed_down = (c1 < z.upper_lo) and (h1 > z.upper_lo)
    if swept_up and reclaimed_down:
        sigs.append(SetupSignal(
            key="SWEEP_RECLAIM_SHORT",
            title="SWEEP + RECLAIM (Upper) → SHORT",
            direction="SHORT",
            info=f"Upper: {fmt_int(z.upper_lo)}–{fmt_int(z.upper_hi)} | close={fmt_int(c1)}"
        ))

    # B) Sweep + reclaim (lower -> long)
    swept_down = (l1 < z.lower_lo) or (l0 < z.lower_lo)
    reclaimed_up = (c1 > z.lower_hi) and (l1 < z.lower_hi)
    if swept_down and reclaimed_up:
        sigs.append(SetupSignal(
            key="SWEEP_RECLAIM_LONG",
            title="SWEEP + RECLAIM (Lower) → LONG",
            direction="LONG",
            info=f"Lower: {fmt_int(z.lower_lo)}–{fmt_int(z.lower_hi)} | close={fmt_int(c1)}"
        ))

    # C) Mean revert (RANGE only)
    if regime_key == "RANGE":
        touch_upper = h1 >= z.upper_lo
        back_inside_upper = c1 < z.upper_lo
        if touch_upper and back_inside_upper:
            sigs.append(SetupSignal(
                key="MEAN_REVERT_SHORT",
                title="MEAN REVERT (Upper touch) → SHORT",
                direction="SHORT",
                info=f"Upper_lo={fmt_int(z.upper_lo)} | close={fmt_int(c1)}"
            ))
        touch_lower = l1 <= z.lower_hi
        back_inside_lower = c1 > z.lower_hi
        if touch_lower and back_inside_lower:
            sigs.append(SetupSignal(
                key="MEAN_REVERT_LONG",
                title="MEAN REVERT (Lower touch) → LONG",
                direction="LONG",
                info=f"Lower_hi={fmt_int(z.lower_hi)} | close={fmt_int(c1)}"
            ))

    return sigs

ALLOWED_SETUPS = {
    "SHORT_SQUEEZE": {"SWEEP_RECLAIM_SHORT"},
    "LONG_UNWIND":   {"SWEEP_RECLAIM_LONG"},
    "RANGE":         {"SWEEP_RECLAIM_SHORT", "SWEEP_RECLAIM_LONG", "MEAN_REVERT_SHORT", "MEAN_REVERT_LONG"},
    "DELEVERAGING":  {"SWEEP_RECLAIM_SHORT", "SWEEP_RECLAIM_LONG", "MEAN_REVERT_SHORT", "MEAN_REVERT_LONG"},
    "TRANSITION":    set(),
    "UNKNOWN":       set(),
}

def build_regime_message(regime: str, px15m: float, funding: float, oi15m: Optional[float], z: Zones) -> str:
    oi_s = "n/a" if oi15m is None else f"{oi15m:+.2f} (Bybit)"
    return (
        "🧭 MARKET REGIME\n"
        f"{regime}\n"
        f"px15m {fmt_pct(px15m)}, funding {funding:+.6f}, width~{z.width_pct:.2f}%\n\n"
        f"Funding: {funding:+.6f}\n"
        f"OIΔ(15m): {oi_s}\n\n"
        f"Upper zone: {fmt_int(z.upper_lo)} – {fmt_int(z.upper_hi)}\n"
        f"Lower zone: {fmt_int(z.lower_lo)} – {fmt_int(z.lower_hi)}"
    )

def build_setup_message(sig: SetupSignal, regime_raw: str, z: Zones, extra: str = "") -> str:
    tail = f"\n\n{extra}" if extra else ""
    return (
        "🎯 SETUP\n"
        f"{sig.title}\n"
        f"Regime: {regime_raw}\n\n"
        f"{sig.info}\n"
        f"Upper: {fmt_int(z.upper_lo)}–{fmt_int(z.upper_hi)}\n"
        f"Lower: {fmt_int(z.lower_lo)}–{fmt_int(z.lower_hi)}"
        f"{tail}"
    )

# -------------------- Filters --------------------
def mean_revert_ok(z: Zones, oi15m: Optional[float], direction: str) -> Tuple[bool, str]:
    # 1) Width filter
    if z.width_pct > MEAN_REVERT_MAX_WIDTH_PCT:
        return False, f"blocked: width {z.width_pct:.2f}% > {MEAN_REVERT_MAX_WIDTH_PCT:.2f}%"

    # 2) OI contra filter (optional)
    if not OI_CONTRA_FILTER or oi15m is None:
        return True, "ok"

    if abs(oi15m) < OI_CONTRA_MIN_ABS:
        return False, f"blocked: |OIΔ| {abs(oi15m):.0f} < {OI_CONTRA_MIN_ABS:.0f}"

    # CONTRA logic:
    # LONG: prefer OI decreasing (flush / long unwind) => oi15m <= -min
    # SHORT: prefer OI increasing (crowding / long build) => oi15m >= +min
    if direction == "LONG" and oi15m > -OI_CONTRA_MIN_ABS:
        return False, f"blocked: OIΔ {oi15m:+.0f} not <= -{OI_CONTRA_MIN_ABS:.0f}"
    if direction == "SHORT" and oi15m < +OI_CONTRA_MIN_ABS:
        return False, f"blocked: OIΔ {oi15m:+.0f} not >= +{OI_CONTRA_MIN_ABS:.0f}"

    return True, "ok"

def altbtc_bias_ok(alt_symbol_usdt: str, direction: str) -> Tuple[bool, str, Optional[float]]:
    """
    If ETHUSDT -> uses ETHBTC (if listed in WATCH_ALT_BTC).
    Bias idea: for LONG, prefer ALT/BTC not negative; for SHORT, prefer ALT/BTC not positive.
    """
    s = (alt_symbol_usdt or "").upper().strip()
    if not s.endswith("USDT"):
        return True, "ok", None
    base = s[:-4]
    altbtc = f"{base}BTC"
    if altbtc not in WATCH_ALT_BTC:
        return True, "ok", None

    kl = binance_spot_klines(altbtc, "1m", 240)
    if not kl:
        return True, "ok (altbtc n/a)", None

    ch = px_change_15m_from_1m(kl)
    if ch is None:
        return True, "ok (altbtc n/a)", None

    # tolerance
    if direction == "LONG" and ch < -ALT_BTC_BIAS_PCT:
        return False, f"blocked: {altbtc} px15m {ch:+.2f}% < -{ALT_BTC_BIAS_PCT:.2f}%", ch
    if direction == "SHORT" and ch > +ALT_BTC_BIAS_PCT:
        return False, f"blocked: {altbtc} px15m {ch:+.2f}% > +{ALT_BTC_BIAS_PCT:.2f}%", ch

    return True, "ok", ch

# -------------------- Main --------------------
def main() -> None:
    state = load_state()
    last_regime_sent = state.get("last_regime_sent")
    last_regime_alert_ts = float(state.get("last_regime_alert_ts", 0.0))
    last_ok_ts = float(state.get("last_ok_ts", 0.0))

    last_setup_ts: Dict[str, float] = state.get("last_setup_ts", {}) if isinstance(state.get("last_setup_ts", {}), dict) else {}

    log.info(
        "Starting btc-alerts | symbol=%s interval=%ss | telegram=%s | setups=on | setup_symbols=%s | alt_enabled_btc_regimes=%s | signals_file=%s | mr_max_width=%.2f | oi_contra=%s | watch_alt_btc=%s",
        SYMBOL, INTERVAL_SEC,
        "on" if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else "off",
        ",".join(SETUP_SYMBOLS) if SETUP_SYMBOLS else "(none)",
        ",".join(sorted(ALT_ENABLED_BTC_REGIMES)) if ALT_ENABLED_BTC_REGIMES else "(none)",
        SIGNALS_FILE,
        MEAN_REVERT_MAX_WIDTH_PCT,
        "on" if OI_CONTRA_FILTER else "off",
        ",".join(WATCH_ALT_BTC) if WATCH_ALT_BTC else "(none)",
    )

    while True:
        t0 = time.time()

        # --- BTC MASTER FETCH
        prem = binance_premium_index(SYMBOL)
        kl_1m = binance_fapi_klines(SYMBOL, "1m", 240)
        kl_15m = binance_fapi_klines(SYMBOL, "15m", 80)
        oi15m = bybit_oi_delta_15m(SYMBOL)

        if not isinstance(prem, dict) or not kl_1m or not kl_15m:
            if time.time() - last_ok_ts > 180:
                log.warning("No successful BTC price/funding bundle for >180s (API unstable?)")
            time.sleep(INTERVAL_SEC)
            continue

        funding = safe_float(prem.get("lastFundingRate"))
        px15m = px_change_15m_from_1m(kl_1m)
        atr = atr_from_15m(kl_15m, period=14)
        z = compute_zones(kl_1m, atr=atr, lookback_min=180)

        if px15m is None or z is None or math.isnan(funding):
            time.sleep(INTERVAL_SEC)
            continue

        regime_raw = classify_regime(px15m=px15m, oi15m=oi15m, width_pct=z.width_pct)
        regime_key = norm_regime(regime_raw)

        # --- REGIME log (always)
        msg_reg = build_regime_message(regime_raw, px15m, funding, oi15m, z)
        log.info(msg_reg.replace("\n", " | "))

        # --- REGIME telegram: only on change + heartbeat
        now = time.time()
        heartbeat_sec = 600
        if (last_regime_sent != regime_raw) or (now - last_regime_alert_ts >= heartbeat_sec):
            send_regime(msg_reg)
            last_regime_sent = regime_raw
            last_regime_alert_ts = now

        # --- BTC SETUPS
        btc_sigs = detect_setups(regime_key=regime_key, kl_1m=kl_1m, z=z)
        btc_allowed = ALLOWED_SETUPS.get(regime_key, set())

        for sig in btc_sigs:
            if sig.key not in btc_allowed:
                continue

            # mean revert filters on BTC too
            if sig.key in ("MEAN_REVERT_LONG", "MEAN_REVERT_SHORT"):
                ok, reason = mean_revert_ok(z, oi15m, sig.direction)
                if not ok:
                    log.info(f"SETUP blocked BTC {sig.key}: {reason}")
                    continue

            key = f"{SYMBOL}:{sig.key}"
            prev = float(last_setup_ts.get(key, 0.0))
            if (now - prev) < SETUP_COOLDOWN_SEC:
                continue

            setup_msg = f"🪙 {SYMBOL}\n" + build_setup_message(sig, regime_raw, z)
            log.info(("SETUP | " + setup_msg.replace("\n", " | ")))
            send_signal(setup_msg)

            # --- PAPER EVENT (mean revert both directions)
            close_px = safe_float(kl_1m[-1][4])
            if sig.key == "MEAN_REVERT_LONG":
                emit_signal_event({
                    "exchange": "mexc",
                    "market": "futures",
                    "symbol": to_ccxt_swap_symbol(SYMBOL),
                    "symbol_raw": SYMBOL,
                    "setup": "MEAN_REVERT_LOWER_TOUCH_LONG",
                    "regime": "RANGE_CHOP" if regime_key == "RANGE" else regime_key,
                    "lower": [z.lower_lo, z.lower_hi],
                    "upper": [z.upper_lo, z.upper_hi],
                    "close": close_px,
                    "width_pct": z.width_pct,
                    "oi15m": oi15m,
                })
            elif sig.key == "MEAN_REVERT_SHORT":
                emit_signal_event({
                    "exchange": "mexc",
                    "market": "futures",
                    "symbol": to_ccxt_swap_symbol(SYMBOL),
                    "symbol_raw": SYMBOL,
                    "setup": "MEAN_REVERT_UPPER_TOUCH_SHORT",
                    "regime": "RANGE_CHOP" if regime_key == "RANGE" else regime_key,
                    "lower": [z.lower_lo, z.lower_hi],
                    "upper": [z.upper_lo, z.upper_hi],
                    "close": close_px,
                    "width_pct": z.width_pct,
                    "oi15m": oi15m,
                })

            last_setup_ts[key] = now

        # --- ALT SETUP-ONLY SCAN (gated by BTC regime_key)
        if regime_key in ALT_ENABLED_BTC_REGIMES and SETUP_SYMBOLS:
            for sym in SETUP_SYMBOLS:
                alt_1m = binance_fapi_klines(sym, "1m", 240)
                alt_15m = binance_fapi_klines(sym, "15m", 80)
                if not alt_1m or not alt_15m:
                    continue

                alt_atr = atr_from_15m(alt_15m, period=14)
                alt_z = compute_zones(alt_1m, atr=alt_atr, lookback_min=180)
                if alt_z is None:
                    continue

                alt_sigs = detect_setups(regime_key=regime_key, kl_1m=alt_1m, z=alt_z)
                alt_allowed = ALLOWED_SETUPS.get(regime_key, set())

                for sig in alt_sigs:
                    if sig.key not in alt_allowed:
                        continue

                    # mean revert filters on alts
                    extra_parts: List[str] = []
                    if sig.key in ("MEAN_REVERT_LONG", "MEAN_REVERT_SHORT"):
                        ok, reason = mean_revert_ok(alt_z, oi15m, sig.direction)
                        if not ok:
                            log.info(f"ALT SETUP blocked {sym} {sig.key}: {reason}")
                            continue
                        extra_parts.append(f"filters: width<= {MEAN_REVERT_MAX_WIDTH_PCT:.2f}% + OI_contra={'on' if OI_CONTRA_FILTER else 'off'}")

                        # ALT/BTC bias gate (ETHBTC/SOLBTC)
                        ok2, reason2, ch = altbtc_bias_ok(sym, sig.direction)
                        if not ok2:
                            log.info(f"ALT SETUP blocked {sym} {sig.key}: {reason2}")
                            continue
                        if ch is not None:
                            base = sym[:-4]
                            extra_parts.append(f"{base}BTC px15m {ch:+.2f}% (bias ok)")

                    key = f"{sym}:{sig.key}"
                    prev = float(last_setup_ts.get(key, 0.0))
                    if (now - prev) < SETUP_COOLDOWN_SEC:
                        continue

                    extra = " | ".join(extra_parts)
                    setup_msg = f"🪙 {sym}\n" + build_setup_message(sig, regime_raw, alt_z, extra=extra)
                    log.info(("ALT SETUP | " + setup_msg.replace("\n", " | ")))
                    send_signal(setup_msg)

                    # --- PAPER EVENT (mean revert both directions)
                    close_px = safe_float(alt_1m[-1][4])
                    if sig.key == "MEAN_REVERT_LONG":
                        emit_signal_event({
                            "exchange": "mexc",
                            "market": "futures",
                            "symbol": to_ccxt_swap_symbol(sym),
                            "symbol_raw": sym,
                            "setup": "MEAN_REVERT_LOWER_TOUCH_LONG",
                            "regime": "RANGE_CHOP" if regime_key == "RANGE" else regime_key,
                            "lower": [alt_z.lower_lo, alt_z.lower_hi],
                            "upper": [alt_z.upper_lo, alt_z.upper_hi],
                            "close": close_px,
                            "width_pct": alt_z.width_pct,
                            "oi15m": oi15m,
                        })
                    elif sig.key == "MEAN_REVERT_SHORT":
                        emit_signal_event({
                            "exchange": "mexc",
                            "market": "futures",
                            "symbol": to_ccxt_swap_symbol(sym),
                            "symbol_raw": sym,
                            "setup": "MEAN_REVERT_UPPER_TOUCH_SHORT",
                            "regime": "RANGE_CHOP" if regime_key == "RANGE" else regime_key,
                            "lower": [alt_z.lower_lo, alt_z.lower_hi],
                            "upper": [alt_z.upper_lo, alt_z.upper_hi],
                            "close": close_px,
                            "width_pct": alt_z.width_pct,
                            "oi15m": oi15m,
                        })

                    last_setup_ts[key] = now

        # save state
        last_ok_ts = now
        save_state({
            "symbol": SYMBOL,
            "last_regime": regime_raw,
            "last_regime_sent": last_regime_sent,
            "last_regime_alert_ts": last_regime_alert_ts,
            "last_ok_ts": last_ok_ts,
            "last_setup_ts": last_setup_ts,
        })

        elapsed = time.time() - t0
        time.sleep(max(1.0, INTERVAL_SEC - elapsed))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped by user")
