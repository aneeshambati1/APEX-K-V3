"""
APEX-K V14.3.9 — Final Production Implementation

All P0 and P1 fixes from the V14.3.8 audit are implemented:
- P0-1: Pagination uses 'until' instead of 'after'
- P0-2: Asymmetric gates (allow_exits vs allow_entries)
- P0-3: Verify cancellation is terminal (204 ≠ cancelled)
- P0-4: Remove auto-DEAD, replace with quarantine
- P0-5: Stop cancel + exit not atomic (refresh snapshot)
- P0-6: Non-overlapping stop coverage
- P0-7: Strict fill quantity parsing
- P0-8: Follow replaced_by chain
- P0-9: Disable shorts explicitly
- P1: GitHub 409 merge-not-overwrite
- P1: Trading-day holding period

WARNING: The strategy is momentum-based and has negative expected value.
This is infrastructure only. Do not deploy live capital.
"""

import os
import sys
import json
import base64
import math
import time
import io
import logging
import hashlib
import fcntl
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Any, Tuple, Set
from dataclasses import dataclass, field, asdict
from contextlib import contextmanager
from enum import Enum

import requests
import yfinance as yf
import pandas as pd
import numpy as np

# ==============================================================================
# CONFIGURATION
# ==============================================================================

DEFAULT_TIMEOUT = 15
BASE_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
LOCK_FILE = "/tmp/apex_k_cycle.lock"
MAX_PREDICTIONS = 500
MEMORY_VERSION = 4
FRED_MAX_AGE_DAYS = 7
RECON_MAX_ATTEMPTS = 40
MAX_ORDER_PAGES = 10
MAX_HOLD_DAYS_ABSOLUTE = 15

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger("apex")

# ==============================================================================
# ENUMS
# ==============================================================================

class PostStatus(Enum):
    SUCCESS = "success"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

class DeleteStatus(Enum):
    SUCCESS = "success"
    REJECTED = "rejected"
    UNKNOWN = "unknown"

class LookupStatus(Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"

class ReconStatus(Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    FAILED = "failed"

# Terminal statuses
ENTRY_TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected"}
EXIT_TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected"}

# ==============================================================================
# CYCLE LOCK
# ==============================================================================

@contextmanager
def cycle_lock():
    """Prevent concurrent cycle execution via file lock."""
    lock_fd = None
    acquired = False
    try:
        lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield True
    except BlockingIOError:
        log.warning("Another cycle is already running. Exiting.")
        yield False
    finally:
        if lock_fd is not None:
            if acquired:
                try:
                    fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            lock_fd.close()

# ==============================================================================
# PREDICTION MODEL + STATUS DERIVATION
# ==============================================================================

@dataclass
class Prediction:
    order_id: str = ""
    client_order_id: str = ""
    ticker: str = ""
    direction: str = ""
    entry_price: float = 0.0
    qty_submitted: int = 0
    qty_filled: int = 0
    qty_exited: int = 0
    qty_exited_by_order: Dict[str, int] = field(default_factory=dict)
    atr: float = 0.0
    max_hold_days: int = 3
    date: str = ""
    strategy: str = ""

    entry_terminal: bool = False
    entry_cancel_pending: bool = False
    reject_reason: Optional[str] = None

    exit_client_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    exit_reason: Optional[str] = None
    exit_request_qty: int = 0
    stop_order_ids: List[str] = field(default_factory=list)

    status: str = "PENDING"
    closed_at: Optional[str] = None

    reconciliation_attempts: int = 0
    last_reconciliation_error: Optional[str] = None
    validation_error: Optional[str] = None
    raw_snapshot: Optional[Dict] = None

    @property
    def remaining_qty(self) -> int:
        return max(0, self.qty_filled - self.qty_exited)

    def is_open(self) -> bool:
        if not self.entry_terminal and (self.order_id or self.client_order_id):
            return True
        if self.entry_terminal and self.qty_filled > 0 and self.qty_exited < self.qty_filled:
            return True
        return self.status == "RECONCILIATION_REQUIRED"

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'Prediction':
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

def _refresh_status(pred: Prediction) -> None:
    """Recompute pred.status from lifecycle fields. Only place status is written."""
    if pred.validation_error:
        pred.status = "RECONCILIATION_REQUIRED"
        return

    if not pred.entry_terminal:
        if pred.entry_cancel_pending and pred.order_id:
            pred.status = "ENTRY_CANCEL_PENDING"
        elif pred.order_id and pred.qty_filled > 0:
            pred.status = "ENTRY_PARTIALLY_FILLED"
        elif pred.order_id:
            pred.status = "PENDING"
        elif pred.client_order_id:
            pred.status = "INTENT_PENDING"
        else:
            pred.status = "RECONCILIATION_REQUIRED"
        return

    if pred.qty_filled <= 0:
        pred.status = "DEAD"
        return

    if pred.qty_exited >= pred.qty_filled:
        pred.status = "CLOSED"
        if not pred.closed_at:
            pred.closed_at = datetime.now(timezone.utc).isoformat()
        return

    exit_active = bool(pred.exit_client_order_id or pred.exit_order_id)
    if exit_active:
        pred.status = "PARTIALLY_EXITED" if pred.qty_exited > 0 else "EXIT_PENDING"
    else:
        pred.status = "PARTIALLY_EXITED" if pred.qty_exited > 0 else "FILLED"

# ==============================================================================
# MARKET DATA CACHE
# ==============================================================================

class MarketDataCache:
    INDEX_TICKERS = {"^VIX", "^VIX3M", "^TNX", "DX-Y.NYB"}

    def __init__(self):
        self.cache: Dict[Tuple[str, str], 'MarketData'] = {}

    def get(self, ticker: str, period: str = "3mo") -> Optional['MarketData']:
        period = self._normalize_period(period)
        cache_key = (ticker, period)

        if cache_key in self.cache:
            return self.cache[cache_key]

        try:
            df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
            if df is None or df.empty:
                log.warning(f"No data for {ticker} ({period})")
                return None

            close_series = self._extract_close_series(df, ticker)
            if close_series is None:
                return None

            is_index = ticker in self.INDEX_TICKERS

            if is_index:
                min_bars = 10 if period in ("5d", "1mo") else 20
                if len(close_series) < min_bars:
                    return None
                current_price = float(close_series.iloc[-1])
                if not (math.isfinite(current_price) and current_price > 0):
                    return None
                data = MarketData(
                    ticker=ticker, close_series=close_series, ohlcv=df,
                    atr=0.0, avg_volume=0.0, current_price=current_price,
                    is_index=True,
                )
            else:
                if len(close_series) < 30:
                    return None
                current_price = float(close_series.iloc[-1])
                high = self._extract_series(df, 'High', ticker)
                low = self._extract_series(df, 'Low', ticker)
                volume = self._extract_series(df, 'Volume', ticker)
                if high is None or low is None or volume is None:
                    return None
                tr = pd.concat([
                    high - low,
                    (high - close_series.shift()).abs(),
                    (low - close_series.shift()).abs(),
                ], axis=1).max(axis=1)
                atr = float(tr.rolling(14).mean().iloc[-1])
                avg_volume = float(volume.tail(30).mean())

                if not (math.isfinite(atr) and atr > 0):
                    return None
                if not (math.isfinite(avg_volume) and avg_volume > 0):
                    return None
                if not (math.isfinite(current_price) and current_price > 0):
                    return None

                data = MarketData(
                    ticker=ticker, close_series=close_series, ohlcv=df,
                    atr=atr, avg_volume=avg_volume, current_price=current_price,
                    is_index=False,
                )

            self.cache[cache_key] = data
            return data

        except Exception as e:
            log.error(f"Failed to fetch data for {ticker}: {e}")
            return None

    def _normalize_period(self, period: str) -> str:
        period_map = {
            "10d": "1mo", "5d": "5d", "1mo": "1mo",
            "3mo": "3mo", "6mo": "6mo", "1y": "1y",
            "2y": "2y", "5y": "5y",
        }
        return period_map.get(period, "3mo")

    def _extract_close_series(self, df: pd.DataFrame, ticker: str) -> Optional[pd.Series]:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if ticker in df.columns.get_level_values(0):
                    return df[ticker]['Close'].dropna()
                if ticker in df.columns.get_level_values(1):
                    return df.xs(ticker, axis=1, level=1)['Close'].dropna()
                return None
            return df['Close'].dropna() if 'Close' in df.columns else None
        except Exception:
            return None

    def _extract_series(self, df: pd.DataFrame, field_name: str, ticker: str) -> Optional[pd.Series]:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if ticker in df.columns.get_level_values(0):
                    return df[ticker][field_name]
                if ticker in df.columns.get_level_values(1):
                    return df.xs(ticker, axis=1, level=1)[field_name]
                return None
            return df[field_name] if field_name in df.columns else None
        except Exception:
            return None

@dataclass
class MarketData:
    ticker: str
    close_series: pd.Series
    ohlcv: pd.DataFrame
    atr: float
    avg_volume: float
    current_price: float
    is_index: bool = False

    @property
    def is_valid_for_trading(self) -> bool:
        if self.is_index:
            return False
        return (
            len(self.close_series) >= 30
            and math.isfinite(self.atr) and self.atr > 0
            and math.isfinite(self.avg_volume) and self.avg_volume > 0
            and math.isfinite(self.current_price) and self.current_price > 0
        )

    def is_bar_fresh(self, max_age_hours: int = 26) -> bool:
        """Check if latest bar is recent enough. Handles tz-aware indices."""
        if len(self.close_series) == 0:
            return False
        last_idx = self.close_series.index[-1]
        if isinstance(last_idx, pd.Timestamp):
            ts = (last_idx.tz_localize(timezone.utc)
                  if last_idx.tzinfo is None
                  else last_idx.astimezone(timezone.utc))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            return age < max_age_hours * 3600
        return True

# ==============================================================================
# API HELPERS
# ==============================================================================

def get_alpaca_headers() -> Dict[str, str]:
    api_key = os.environ.get('ALPACA_API_KEY', '')
    secret_key = os.environ.get('ALPACA_SECRET_KEY', '')
    if not api_key or not secret_key:
        log.error("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY")
        return {}
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "Content-Type": "application/json",
    }

def alpaca_get(endpoint: str, headers: Dict[str, str], timeout: int = DEFAULT_TIMEOUT) -> Optional[Any]:
    try:
        resp = requests.get(f"{BASE_URL}{endpoint}", headers=headers, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        log.warning(f"GET {endpoint} returned {resp.status_code}")
        return None
    except Exception as e:
        log.error(f"GET {endpoint} failed: {e}")
        return None

def alpaca_post(endpoint: str, headers: Dict[str, str], data: Dict,
                timeout: int = DEFAULT_TIMEOUT) -> Tuple[Optional[Any], PostStatus]:
    try:
        resp = requests.post(f"{BASE_URL}{endpoint}", headers=headers, json=data, timeout=timeout)
        if resp.status_code in (200, 201):
            return resp.json(), PostStatus.SUCCESS
        log.warning(f"POST {endpoint} returned {resp.status_code}: {resp.text[:100]}")
        return None, PostStatus.REJECTED
    except requests.exceptions.Timeout:
        log.warning(f"POST {endpoint} timed out - order may have been accepted")
        return None, PostStatus.UNKNOWN
    except Exception as e:
        log.error(f"POST {endpoint} failed (unknown): {e}")
        return None, PostStatus.UNKNOWN

def alpaca_delete(endpoint: str, headers: Dict[str, str],
                  timeout: int = DEFAULT_TIMEOUT) -> Tuple[DeleteStatus, bool]:
    """Returns (status, timed_out). 204 = SUCCESS (cancel accepted, not terminal)."""
    try:
        resp = requests.delete(f"{BASE_URL}{endpoint}", headers=headers, timeout=timeout)
        if resp.status_code in (200, 204):
            return DeleteStatus.SUCCESS, False
        if resp.status_code == 422:
            return DeleteStatus.REJECTED, False
        log.warning(f"DELETE {endpoint} returned {resp.status_code}")
        return DeleteStatus.UNKNOWN, False
    except requests.exceptions.Timeout:
        log.warning(f"DELETE {endpoint} timed out")
        return DeleteStatus.UNKNOWN, True
    except Exception as e:
        log.error(f"DELETE {endpoint} failed: {e}")
        return DeleteStatus.UNKNOWN, False

def generate_client_order_id(ticker: str, direction: str, timestamp: datetime, tag: str = "") -> str:
    payload = f"{ticker}|{direction}|{tag}|{timestamp.isoformat()}"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"apex-{ticker}-{digest}"

def lookup_order_by_client_id(client_order_id: str,
                              headers: Dict[str, str]) -> Tuple[Optional[Dict], LookupStatus]:
    if not client_order_id:
        return None, LookupStatus.NOT_FOUND
    try:
        endpoint = f"/v2/orders:by_client_order_id?client_order_id={client_order_id}"
        resp = requests.get(f"{BASE_URL}{endpoint}", headers=headers, timeout=DEFAULT_TIMEOUT)
        if resp.status_code == 200:
            return resp.json(), LookupStatus.FOUND
        if resp.status_code == 404:
            return None, LookupStatus.NOT_FOUND
        log.warning(f"Client order ID lookup returned {resp.status_code}")
        return None, LookupStatus.UNKNOWN
    except Exception as e:
        log.error(f"Client order ID lookup failed: {e}")
        return None, LookupStatus.UNKNOWN

def _parse_iso(ts_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO timestamp to datetime with timezone."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _paginate_orders(status: str, headers, until: Optional[datetime] = None,
                     max_pages: int = MAX_ORDER_PAGES
                     ) -> Tuple[Optional[List[Dict]], bool]:
    """
    Paginate newest→oldest using 'until' (exclusive) as the backwards cursor.
    Returns (orders, complete). complete=True only when the final page is short.
    """
    all_orders: List[Dict] = []
    seen: set = set()
    cursor = until
    pages = 0

    while pages < max_pages:
        pages += 1
        q = (f"/v2/orders?status={status}&limit=500&direction=desc&nested=true")
        if cursor is not None:
            q += f"&until={cursor.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}"
        batch = alpaca_get(q, headers)
        if batch is None:
            return None, False
        if not batch:
            return all_orders, True

        fresh = [o for o in batch if o.get("id") and o["id"] not in seen]
        for o in fresh:
            seen.add(o["id"])
        all_orders.extend(fresh)

        if len(batch) < 500:
            return all_orders, True
        if not fresh:
            return all_orders, False

        oldest = None
        for o in batch:
            ts = _parse_iso(o.get("submitted_at") or o.get("created_at"))
            if ts and (oldest is None or ts < oldest):
                oldest = ts
        if oldest is None or (cursor is not None and oldest >= cursor):
            return all_orders, False
        cursor = oldest

    return all_orders, False

def _fetch_all_orders(headers, since: Optional[datetime] = None):
    return _paginate_orders("all", headers, until=since)

def _fetch_open_orders(headers):
    return _paginate_orders("open", headers)

# ==============================================================================
# MARKET STATE
# ==============================================================================

def compute_credit_z_score() -> Optional[float]:
    try:
        hy_url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLH0A0HYM2"
        ig_url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=BAMLC0A0CM"
        hy_resp = requests.get(hy_url, timeout=DEFAULT_TIMEOUT)
        ig_resp = requests.get(ig_url, timeout=DEFAULT_TIMEOUT)
        if hy_resp.status_code != 200 or ig_resp.status_code != 200:
            return None

        hy_df = pd.read_csv(io.StringIO(hy_resp.text), parse_dates=["DATE"]).set_index("DATE")
        ig_df = pd.read_csv(io.StringIO(ig_resp.text), parse_dates=["DATE"]).set_index("DATE")

        hy = pd.to_numeric(hy_df.iloc[:, 0], errors="coerce").dropna()
        ig = pd.to_numeric(ig_df.iloc[:, 0], errors="coerce").dropna()
        if len(hy) == 0 or len(ig) == 0:
            return None

        now = datetime.now(timezone.utc)
        def _utc(ts):
            return ts.tz_localize(timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)

        if (now - _utc(hy.index[-1])).days > FRED_MAX_AGE_DAYS:
            log.warning("FRED HY stale")
            return None
        if (now - _utc(ig.index[-1])).days > FRED_MAX_AGE_DAYS:
            log.warning("FRED IG stale")
            return None

        combined = pd.DataFrame({"HY": hy, "IG": ig}).dropna()
        if len(combined) < 60:
            return None

        last_common = combined.index[-1]
        if (now - _utc(last_common)).days > FRED_MAX_AGE_DAYS:
            log.warning(f"FRED aligned dataset stale ({last_common.date()})")
            return None

        z = lambda s: (s - s.rolling(60).mean()) / s.rolling(60).std()
        cs_z = float((0.6 * z(combined["HY"]) + 0.4 * z(combined["IG"])).iloc[-1])
        return cs_z if math.isfinite(cs_z) else None
    except Exception as e:
        log.warning(f"Credit Z failed: {e}")
        return None

def _fresh_index(cache: MarketDataCache, ticker: str, period: str = "1mo",
                 max_age_hours: int = 120) -> Optional[MarketData]:
    d = cache.get(ticker, period)
    if d is None or not d.is_bar_fresh(max_age_hours=max_age_hours):
        return None
    return d

def compute_market_state(cache: MarketDataCache) -> Dict[str, Optional[float]]:
    state = {
        "vix": None, "vix_5d": None, "term_ratio": None,
        "spy_sma": None, "spy_20d": None, "breadth": None,
        "cs_z": None, "dxy_5d": None, "tnx_5d": None, "gld_5d": None,
    }

    spy_data = _fresh_index(cache, "SPY", period="1y", max_age_hours=120)
    if spy_data and len(spy_data.close_series) >= 200:
        s = spy_data.close_series
        spy_current = float(s.iloc[-1])
        spy_sma200 = float(s.rolling(200).mean().iloc[-1])
        if math.isfinite(spy_sma200) and spy_sma200 > 0:
            state['spy_sma'] = (spy_current / spy_sma200) - 1
        if len(s) >= 20:
            state['spy_20d'] = (spy_current / float(s.iloc[-20])) - 1

    vix_data = _fresh_index(cache, "^VIX", period="1mo", max_age_hours=120)
    if vix_data and len(vix_data.close_series) >= 6:
        s = vix_data.close_series
        state['vix'] = float(s.iloc[-1])
        state['vix_5d'] = (float(s.iloc[-1]) / float(s.iloc[-6])) - 1

    vix3m_data = _fresh_index(cache, "^VIX3M", period="1mo", max_age_hours=120)
    if vix3m_data and len(vix3m_data.close_series) >= 1 and state['vix'] is not None:
        vix3m = float(vix3m_data.close_series.iloc[-1])
        if vix3m > 0:
            state['term_ratio'] = state['vix'] / vix3m

    sectors = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
    sector_data = [_fresh_index(cache, t, period="3mo", max_age_hours=120) for t in sectors]
    sector_data = [d for d in sector_data if d and d.is_valid_for_trading and len(d.close_series) >= 50]
    if len(sector_data) == len(sectors):
        above = 0
        for d in sector_data:
            s = d.close_series
            ma50 = float(s.rolling(50).mean().iloc[-1])
            if float(s.iloc[-1]) > ma50:
                above += 1
        state['breadth'] = above / len(sectors)

    state['cs_z'] = compute_credit_z_score()

    dxy_data = _fresh_index(cache, "DX-Y.NYB", period="1mo", max_age_hours=120)
    if dxy_data and len(dxy_data.close_series) >= 6:
        s = dxy_data.close_series
        state['dxy_5d'] = (float(s.iloc[-1]) / float(s.iloc[-6])) - 1

    tnx_data = _fresh_index(cache, "^TNX", period="1mo", max_age_hours=120)
    if tnx_data and len(tnx_data.close_series) >= 6:
        s = tnx_data.close_series
        state['tnx_5d'] = float(s.iloc[-1]) - float(s.iloc[-6])

    gld_data = _fresh_index(cache, "GLD", period="3mo", max_age_hours=120)
    if gld_data and gld_data.is_valid_for_trading and len(gld_data.close_series) >= 6:
        s = gld_data.close_series
        state['gld_5d'] = (float(s.iloc[-1]) / float(s.iloc[-6])) - 1

    return state

def verify_strategy(state: Dict[str, Optional[float]]) -> Tuple[str, str]:
    required = ['vix', 'breadth', 'cs_z', 'spy_sma']
    for key in required:
        val = state.get(key)
        if val is None:
            return "CRISIS_CASH", f"Missing required state: {key}"
        if not math.isfinite(val):
            return "CRISIS_CASH", f"Invalid state value: {key}={val}"

    vix = state['vix']
    breadth = state['breadth']
    cs_z = state['cs_z']
    spy_sma = state['spy_sma']
    term_ratio = state.get('term_ratio')

    if cs_z > 2.5:
        return "CRISIS_CASH", f"Credit Z {cs_z:.2f} > 2.5"
    if vix > 35:
        return "CRISIS_CASH", f"VIX {vix:.1f} > 35"
    if term_ratio is not None and math.isfinite(term_ratio) and term_ratio > 1.15:
        return "CRISIS_CASH", f"VIX backwardation {term_ratio:.2f} > 1.15"
    if spy_sma < -0.05 and breadth < 0.40 and vix < 25:
        return "CRISIS_CASH", "Bearish internals"

    return "AGGRESSIVE_MOMENTUM", f"Normal (VIX={vix:.1f}, Breadth={breadth:.0%})"

# ==============================================================================
# SAFETY CHECKS
# ==============================================================================

def check_trading_window(headers: Dict[str, str]) -> bool:
    clock = alpaca_get("/v2/clock", headers)
    if clock is None:
        log.warning("Clock API failed, assuming market closed")
        return False
    return bool(clock.get('is_open', False))

def check_daily_loss_limit(headers: Dict[str, str]) -> bool:
    account = alpaca_get("/v2/account", headers)
    if account is None:
        return False
    try:
        equity = float(account.get('equity', 0))
        last_equity = float(account.get('last_equity', equity))
        if not (math.isfinite(equity) and math.isfinite(last_equity)):
            return False
        if last_equity <= 0:
            return False
        dd = (equity - last_equity) / last_equity
        if dd <= -0.02:
            log.warning(f"Daily loss limit triggered: {dd:.1%}")
            return False
        return True
    except Exception as e:
        log.error(f"Daily loss check failed: {e}")
        return False

def check_stress_test_margin(order_value: float, headers: Dict[str, str]) -> bool:
    account = alpaca_get("/v2/account", headers)
    if account is None:
        return False
    try:
        current_equity = float(account.get('equity', 0))
        maintenance_margin = float(account.get('maintenance_margin', 0))
        long_market_value = float(account.get('long_market_value', 0))
        if not all(math.isfinite(v) for v in (current_equity, maintenance_margin, long_market_value)):
            return False
        simulated_equity = current_equity - long_market_value * 0.15 - order_value * 0.15
        safety_threshold = maintenance_margin * 1.10
        if simulated_equity < safety_threshold:
            log.warning(f"Stress test failed: ${simulated_equity:.0f} < ${safety_threshold:.0f}")
            return False
        return True
    except Exception as e:
        log.error(f"Stress test failed: {e}")
        return False

def _iter_stop_legs(order: Dict, parent_symbol: Optional[str] = None):
    sym = order.get("symbol") or parent_symbol
    status = order.get("status", "")
    if (order.get("type") in ("stop", "stop_limit")
            and order.get("side") == "sell"
            and status in ("accepted", "new", "held", "pending_new",
                           "partially_filled", "done_for_day", "pending_cancel")):
        leg = dict(order)
        leg["symbol"] = sym
        yield leg
    for child in order.get("legs") or []:
        yield from _iter_stop_legs(child, sym)

def _verified_stop_coverage(legs: List[Dict], position_qty: float
                            ) -> Tuple[float, Optional[float]]:
    """
    Return (covered_qty, worst_stop). covered_qty is capped at position_qty.
    If multiple legs overlap (total stop qty > position_qty), use worst stop on full position.
    """
    stops = []
    total_stop_qty = 0.0
    for leg in legs:
        try:
            sp = float(leg.get("stop_price", 0) or 0)
            sq = abs(float(leg.get("qty", 0) or 0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(sp) and sp > 0 and sq > 0:
            stops.append((sp, sq))
            total_stop_qty += sq

    if not stops:
        return 0.0, None

    if total_stop_qty > position_qty:
        worst = min(sp for sp, _ in stops)
        return position_qty, worst

    worst = min(sp for sp, _ in stops)
    return total_stop_qty, worst

def get_portfolio_heat_with_reserved(headers: Dict[str, str],
                                     reserved_heat: float,
                                     positions: List[Dict],
                                     open_orders: List[Dict]) -> Optional[float]:
    stops_by_symbol: Dict[str, List[Dict]] = {}
    seen_ids: set = set()
    for o in open_orders:
        for leg in _iter_stop_legs(o):
            lid = leg.get("id")
            if lid and lid in seen_ids:
                continue
            if lid:
                seen_ids.add(lid)
            sym = leg.get("symbol")
            if sym:
                stops_by_symbol.setdefault(sym, []).append(leg)

    try:
        heat = reserved_heat
        for pos in positions:
            sym = pos.get("symbol")
            try:
                entry = float(pos.get("avg_entry_price", 0) or 0)
                qty = abs(float(pos.get("qty", 0) or 0))
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(entry) and entry > 0 and math.isfinite(qty) and qty > 0):
                continue

            legs = stops_by_symbol.get(sym, [])
            covered, worst_stop = _verified_stop_coverage(legs, qty)

            if covered <= 0 or worst_stop is None:
                heat += entry * qty
                continue

            heat += max(0.0, entry - worst_stop) * covered
            uncovered = max(0.0, qty - covered)
            heat += entry * uncovered
        return heat
    except Exception as e:
        log.error(f"Heat calc failed: {e}")
        return None

def check_volume_participation(data: MarketData, proposed_qty: int) -> int:
    if not data.is_valid_for_trading:
        return 0
    cap = math.floor(data.avg_volume * 0.05)
    if proposed_qty > cap:
        log.info(f"Volume cap {data.ticker}: {proposed_qty} -> {cap}")
        return cap
    return proposed_qty

def portfolio_correlation_penalty(ticker: str, existing_tickers: List[str],
                                  cache: MarketDataCache) -> float:
    if not existing_tickers:
        return 1.0
    try:
        all_data = [cache.get(t) for t in [ticker] + existing_tickers]
        if any(d is None or not d.is_valid_for_trading for d in all_data):
            return 0.0
        returns_dict = {}
        for d in all_data:
            if len(d.close_series) >= 20:
                returns_dict[d.ticker] = d.close_series.pct_change().dropna()
        if ticker not in returns_dict:
            return 0.0
        df = pd.DataFrame(returns_dict).dropna()
        if len(df) < 20:
            return 0.0
        corrs = []
        for other in existing_tickers:
            if other in df.columns:
                c = df[ticker].corr(df[other])
                if math.isfinite(c):
                    corrs.append(c)
        if not corrs:
            return 0.0
        avg = sum(corrs) / len(corrs)
        if avg > 0.85:
            return 0.25
        if avg > 0.70:
            return 0.50
        if avg > 0.50:
            return 0.75
        return 1.0
    except Exception as e:
        log.warning(f"Correlation calc failed for {ticker}: {e}")
        return 0.0

def get_executable_price(ticker: str, headers: Dict[str, str],
                         max_age_seconds: int = 60,
                         max_forward_skew_seconds: int = 5,
                         max_spread_pct: float = 0.005) -> Optional[float]:
    try:
        url = f"{DATA_URL}/v2/stocks/{ticker}/snapshot"
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code != 200:
            return None
        snap = resp.json()
        now = datetime.now(timezone.utc)

        def _parse_ts(s):
            if not s:
                return None
            try:
                ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
                return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            except Exception:
                return None

        def _acceptable(ts):
            if ts is None:
                return False
            age = (now - ts).total_seconds()
            return -max_forward_skew_seconds <= age <= max_age_seconds

        def _finite_pos(x):
            try:
                f = float(x)
                return math.isfinite(f) and f > 0
            except (TypeError, ValueError):
                return False

        quote = snap.get("latestQuote") or {}
        bid, ask = quote.get("bp"), quote.get("ap")
        if _finite_pos(bid) and _finite_pos(ask) and _acceptable(_parse_ts(quote.get("t"))):
            b, a = float(bid), float(ask)
            if a > b:
                mid = (a + b) / 2
                if mid > 0 and (a - b) / mid <= max_spread_pct:
                    return mid

        trade = snap.get("latestTrade") or {}
        if _finite_pos(trade.get("p")) and _acceptable(_parse_ts(trade.get("t"))):
            return float(trade["p"])

        log.warning(f"{ticker}: no fresh executable quote")
        return None
    except Exception as e:
        log.warning(f"Snapshot failed {ticker}: {e}")
        return None

def validate_order_geometry(side: str, limit_p: float, sl_p: float,
                            tp_p: Optional[float]) -> bool:
    if side == "buy":
        if not (math.isfinite(sl_p) and sl_p > 0 and sl_p < limit_p):
            log.warning(f"Invalid geometry: stop {sl_p} not below entry {limit_p}")
            return False
        if tp_p is not None and not (math.isfinite(tp_p) and tp_p > limit_p):
            log.warning(f"Invalid geometry: target {tp_p} not above entry {limit_p}")
            return False
    else:
        if not (math.isfinite(sl_p) and sl_p > 0 and sl_p > limit_p):
            log.warning(f"Invalid geometry: stop {sl_p} not above entry {limit_p}")
            return False
        if tp_p is not None and not (math.isfinite(tp_p) and tp_p < limit_p):
            log.warning(f"Invalid geometry: target {tp_p} not below entry {limit_p}")
            return False

    if round(limit_p, 2) != limit_p or round(sl_p, 2) != sl_p:
        log.warning(f"Prices not on tick: limit={limit_p}, stop={sl_p}")
        return False
    if tp_p is not None and round(tp_p, 2) != tp_p:
        log.warning(f"Target not on tick: {tp_p}")
        return False
    return True

# ==============================================================================
# PREDICTION VALIDATION (with type coercion and BEARISH gate)
# ==============================================================================

def _coerce_float(val, default: float = 0.0) -> float:
    try:
        f = float(val) if val is not None else default
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default

def _coerce_int(val, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default

def _coerce_bool(val, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    return bool(val)

def _parse_fill_qty(raw_val, default: int, label: str, errors: List[str]) -> int:
    """Strict fill quantity parsing. No fractional fills."""
    if raw_val is None:
        return default
    try:
        f = float(raw_val)
    except (TypeError, ValueError):
        errors.append(f"{label} not numeric: {raw_val!r}")
        return default
    if not math.isfinite(f):
        errors.append(f"{label} non-finite")
        return default
    if f < 0:
        errors.append(f"{label} negative")
        return default
    if f != int(f):
        errors.append(f"{label} fractional: {f}")
        return default
    return int(f)

def _validate_and_parse_prediction(raw: Any) -> Prediction:
    if not isinstance(raw, dict):
        p = Prediction()
        p.validation_error = f"not a dict: {type(raw).__name__}"
        p.raw_snapshot = {"_unparseable": str(raw)[:500]}
        _refresh_status(p)
        return p

    try:
        p = Prediction.from_dict(raw)

        # Type coercion
        p.qty_filled = _coerce_int(p.qty_filled, 0)
        p.qty_submitted = _coerce_int(p.qty_submitted, 0)
        p.qty_exited = _coerce_int(p.qty_exited, 0)
        p.exit_request_qty = _coerce_int(p.exit_request_qty, 0)
        p.entry_price = _coerce_float(p.entry_price, 0.0)
        p.atr = _coerce_float(p.atr, 0.0)
        p.max_hold_days = _coerce_int(p.max_hold_days, 3)
        p.entry_terminal = _coerce_bool(p.entry_terminal, False)
        p.entry_cancel_pending = _coerce_bool(p.entry_cancel_pending, False)

        if not isinstance(p.qty_exited_by_order, dict):
            p.qty_exited_by_order = {}
        else:
            p.qty_exited_by_order = {
                str(k): _coerce_int(v, 0) for k, v in p.qty_exited_by_order.items()
            }

        if not isinstance(p.stop_order_ids, list):
            p.stop_order_ids = []

        errs = []
        if not p.ticker:
            errs.append("missing ticker")
        if not p.direction:
            errs.append("missing direction")
        if p.direction == "BEARISH":
            errs.append("BEARISH direction not supported in this version")
        if not p.order_id and not p.client_order_id and not p.entry_terminal:
            errs.append("active record has no order_id or client_order_id")

        if errs:
            p.validation_error = "; ".join(errs)
            p.raw_snapshot = raw

        _refresh_status(p)
        return p
    except Exception as e:
        p = Prediction()
        p.validation_error = f"parse failure: {e}"
        p.raw_snapshot = raw
        _refresh_status(p)
        return p

# ==============================================================================
# REPLACEMENT CHAIN RESOLUTION
# ==============================================================================

def _resolve_replacement(order: Dict, by_id: Dict[str, Dict],
                        headers, max_hops: int = 5
                        ) -> Tuple[Dict, bool]:
    """Follow replaced_by chain. Returns (terminal_order, resolved)."""
    seen = {order.get("id")}
    current = order
    for _ in range(max_hops):
        if current.get("status") != "replaced":
            return current, True
        next_id = current.get("replaced_by")
        if not next_id:
            return current, False
        if next_id in seen:
            return current, False
        seen.add(next_id)
        nxt = by_id.get(next_id)
        if nxt is None:
            nxt = alpaca_get(f"/v2/orders/{next_id}", headers)
            if nxt is None:
                return current, False
        current = nxt
    return current, False

# ==============================================================================
# RECONCILIATION
# ==============================================================================

def reconcile_predictions(predictions: List[Prediction],
                          headers: Dict[str, str]) -> Tuple[List[Prediction], ReconStatus]:
    orders, complete = _fetch_all_orders(headers)
    if orders is None:
        return predictions, ReconStatus.FAILED

    by_id = {o.get("id"): o for o in orders if o.get("id")}
    by_client = {o.get("client_order_id"): o for o in orders
                 if o.get("client_order_id")}

    unresolved_count = 0

    for pred in predictions:
        if pred.validation_error:
            unresolved_count += 1
            _refresh_status(pred)
            continue

        if pred.status == "CLOSED" and pred.qty_exited >= pred.qty_filled:
            _refresh_status(pred)
            continue
        if pred.status == "DEAD" and pred.entry_terminal and pred.qty_filled == 0:
            _refresh_status(pred)
            continue

        # Entry reconciliation
        if not pred.entry_terminal and (pred.order_id or pred.client_order_id):
            entry = by_id.get(pred.order_id) if pred.order_id else None
            if entry is None and pred.client_order_id:
                entry = by_client.get(pred.client_order_id)

            if entry is None and pred.client_order_id:
                o, st = lookup_order_by_client_id(pred.client_order_id, headers)
                if st == LookupStatus.FOUND and o:
                    entry = o
                elif st == LookupStatus.UNKNOWN:
                    unresolved_count += 1
                    pred.last_reconciliation_error = "entry lookup unknown"
                    _refresh_status(pred)
                    continue

            if entry is None:
                pred.reconciliation_attempts += 1
                pred.last_reconciliation_error = "entry not visible"
                
                # Quarantine after max attempts, but don't auto-DEAD
                if pred.reconciliation_attempts >= RECON_MAX_ATTEMPTS:
                    log.critical(
                        f"{pred.ticker}: entry unseen for {pred.reconciliation_attempts} "
                        f"cycles — QUARANTINE (still unresolved)"
                    )
                    # Try explicit cancel if we have order_id
                    if pred.order_id:
                        status, _ = alpaca_delete(f"/v2/orders/{pred.order_id}", headers)
                        if status == DeleteStatus.SUCCESS:
                            # Poll for terminal
                            for _ in range(5):
                                time.sleep(0.5)
                                o = alpaca_get(f"/v2/orders/{pred.order_id}", headers)
                                if o and o.get("status") in ENTRY_TERMINAL_STATUSES:
                                    pred.entry_terminal = True
                                    pred.reject_reason = f"confirmed terminal: {o['status']}"
                                    break
                
                unresolved_count += 1
                _refresh_status(pred)
                continue

            # Follow replacement chain
            if entry.get("status") == "replaced":
                resolved, ok = _resolve_replacement(entry, by_id, headers)
                if not ok:
                    unresolved_count += 1
                    pred.last_reconciliation_error = "replaced_by unresolved"
                    _refresh_status(pred)
                    continue
                entry = resolved

            pred.order_id = entry.get("id") or pred.order_id
            pred.client_order_id = entry.get("client_order_id") or pred.client_order_id

            entry_status = entry.get("status", "")
            
            errs: List[str] = []
            filled = _parse_fill_qty(entry.get("filled_qty"), pred.qty_filled,
                                     "entry filled_qty", errs)
            if errs:
                pred.last_reconciliation_error = "; ".join(errs)
                pred.validation_error = "; ".join(errs)
                unresolved_count += 1
                _refresh_status(pred)
                continue
            
            if filled > pred.qty_filled:
                pred.qty_filled = filled

            if entry_status in ENTRY_TERMINAL_STATUSES:
                pred.entry_terminal = True
                pred.entry_cancel_pending = False
                if entry_status != "filled":
                    pred.reject_reason = entry_status

        # Exit reconciliation
        if pred.exit_order_id or pred.exit_client_order_id:
            exit_o = by_id.get(pred.exit_order_id) if pred.exit_order_id else None
            if exit_o is None and pred.exit_client_order_id:
                exit_o = by_client.get(pred.exit_client_order_id)
            if exit_o is None and pred.exit_client_order_id:
                o, st = lookup_order_by_client_id(pred.exit_client_order_id, headers)
                if st == LookupStatus.FOUND:
                    exit_o = o
                elif st == LookupStatus.UNKNOWN:
                    unresolved_count += 1
                    pred.last_reconciliation_error = "exit lookup unknown"
                    _refresh_status(pred)
                    continue

            if exit_o is None:
                pred.last_reconciliation_error = "exit not visible"
                unresolved_count += 1
                _refresh_status(pred)
                continue

            # Follow replacement chain
            if exit_o.get("status") == "replaced":
                resolved, ok = _resolve_replacement(exit_o, by_id, headers)
                if not ok:
                    unresolved_count += 1
                    pred.last_reconciliation_error = "exit replaced_by unresolved"
                    _refresh_status(pred)
                    continue
                exit_o = resolved

            errs: List[str] = []
            this_filled = _parse_fill_qty(exit_o.get("filled_qty"), 0,
                                          "exit filled_qty", errs)
            if errs:
                pred.last_reconciliation_error = "; ".join(errs)
                pred.validation_error = "; ".join(errs)
                unresolved_count += 1
                _refresh_status(pred)
                continue

            key = exit_o.get("id") or pred.exit_order_id or pred.exit_client_order_id or ""
            if key:
                pred.qty_exited_by_order[key] = this_filled
                pred.qty_exited = sum(pred.qty_exited_by_order.values())

            if pred.qty_exited > pred.qty_filled:
                log.error(f"{pred.ticker}: over-exit {pred.qty_exited} > {pred.qty_filled}")
                pred.validation_error = "over-exit detected"
                pred.last_reconciliation_error = "over-exit detected"
                unresolved_count += 1
                _refresh_status(pred)
                continue

            exit_status = exit_o.get("status", "")
            if exit_status in EXIT_TERMINAL_STATUSES:
                pred.exit_request_qty = 0
                if exit_status != "filled":
                    pred.exit_order_id = None
                    pred.exit_client_order_id = None
                    pred.exit_reason = None

        _refresh_status(pred)

        if pred.status == "RECONCILIATION_REQUIRED":
            unresolved_count += 1

    if not complete:
        return predictions, ReconStatus.INCOMPLETE
    if unresolved_count > 0:
        log.warning(f"Reconciliation: {unresolved_count} prediction(s) unresolved")
        return predictions, ReconStatus.INCOMPLETE
    return predictions, ReconStatus.COMPLETE

# ==============================================================================
# PROTECTIVE STOPS
# ==============================================================================

def _cancel_and_verify_stops(headers, open_orders: List[Dict],
                             ticker: str,
                             max_polls: int = 5,
                             poll_interval_s: float = 0.5
                             ) -> Tuple[bool, List[str]]:
    """
    Cancel all stop legs for ticker and VERIFY terminal status via GET.
    Returns (all_confirmed_cancelled, unresolved_order_ids).
    """
    stop_ids = []
    for o in open_orders:
        for leg in _iter_stop_legs(o):
            if leg.get("symbol") == ticker and leg.get("id"):
                stop_ids.append(leg["id"])

    if not stop_ids:
        return True, []

    unresolved = []
    for oid in stop_ids:
        status, _ = alpaca_delete(f"/v2/orders/{oid}", headers)
        if status == DeleteStatus.REJECTED:
            unresolved.append(oid)
            continue
        
        # 204 OR UNKNOWN -> poll for terminal status
        confirmed = False
        for _ in range(max_polls):
            time.sleep(poll_interval_s)
            order = alpaca_get(f"/v2/orders/{oid}", headers)
            if order is None:
                unresolved.append(oid)
                break
            st = order.get("status", "")
            if st == "canceled":
                confirmed = True
                break
            if st in ("filled", "partially_filled"):
                log.critical(f"{ticker}: stop {oid} filled during cancel (status={st})")
                unresolved.append(oid)
                break
            if st == "pending_cancel":
                continue
            if st in ("canceled", "expired", "rejected"):
                confirmed = True
                break
        
        if not confirmed:
            unresolved.append(oid)

    return (len(unresolved) == 0), unresolved

def ensure_protective_stops(headers: Dict[str, str], predictions: List[Prediction],
                            open_orders: List[Dict]) -> None:
    """Ensure all filled entries have protective stops."""
    for pred in predictions:
        if pred.status != "FILLED":
            continue
        if pred.stop_order_ids:
            # Verify stops still exist
            existing_ids = {o.get("id") for o in open_orders}
            pred.stop_order_ids = [sid for sid in pred.stop_order_ids if sid in existing_ids]
            if pred.stop_order_ids:
                continue
        
        # Need to create stop
        if not pred.order_id or pred.qty_filled <= 0 or pred.atr <= 0:
            continue
        
        stop_distance = 3.0 * pred.atr
        sl_p = round(pred.entry_price - stop_distance, 2)
        
        if sl_p <= 0:
            log.warning(f"{pred.ticker}: invalid stop price {sl_p}")
            continue
        
        stop_data = {
            "symbol": pred.ticker,
            "qty": str(pred.qty_filled),
            "side": "sell",
            "type": "stop",
            "stop_price": f"{sl_p:.2f}",
            "time_in_force": "gtc",
        }
        
        result, status = alpaca_post("/v2/orders", headers, stop_data)
        if status == PostStatus.SUCCESS and result:
            stop_id = result.get("id")
            if stop_id:
                pred.stop_order_ids.append(stop_id)
                log.info(f"{pred.ticker}: protective stop created {stop_id}")

# ==============================================================================
# POSITION MANAGEMENT
# ==============================================================================

def _trading_days_held(entry_iso: str) -> int:
    """Count weekday sessions between entry and now (holidays not accounted)."""
    try:
        ed = datetime.fromisoformat(entry_iso.replace("Z", "+00:00"))
        ed = ed if ed.tzinfo else ed.replace(tzinfo=timezone.utc)
    except Exception:
        return 0
    now = datetime.now(timezone.utc)
    if ed > now:
        return 0
    d0, d1 = ed.date(), now.date()
    n = 0
    cur = d0
    while cur < d1:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n

def _sells_and_stops(open_orders: List[Dict]) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Extract open sells and stop legs, aggregated by symbol."""
    sells: Dict[str, float] = {}
    stop_legs: Dict[str, List[Dict]] = {}
    
    def _walk(o: Dict, parent_sym: Optional[str] = None):
        sym = o.get("symbol") or parent_sym
        st = o.get("status", "")
        if st in ("canceled", "expired", "rejected", "filled"):
            return
        if o.get("side") == "sell" and sym:
            try:
                q = float(o.get("qty", 0) or 0)
                f = float(o.get("filled_qty", 0) or 0)
                rem = q - f
            except (TypeError, ValueError):
                rem = 0.0
            if rem > 0:
                if o.get("type") in ("stop", "stop_limit"):
                    stop_legs.setdefault(sym, []).append(o)
                else:
                    sells[sym] = sells.get(sym, 0) + rem
        for leg in o.get("legs") or []:
            _walk(leg, sym)
    
    for o in open_orders:
        _walk(o)
    
    # Aggregate stop quantities
    stops: Dict[str, float] = {}
    for sym, legs in stop_legs.items():
        total = sum(abs(float(l.get("qty", 0) or 0)) for l in legs)
        stops[sym] = total
    
    return sells, stops

def manage_positions(headers: Dict[str, str], predictions: List[Prediction],
                     cache: MarketDataCache, memory: Dict[str, Any],
                     allow_exits: bool,
                     positions: Optional[List[Dict]] = None,
                     open_orders: Optional[List[Dict]] = None) -> None:
    """Manage positions. allow_exits controls whether exits can be submitted."""
    if positions is None:
        positions = alpaca_get("/v2/positions", headers)
        if positions is None:
            log.warning("Positions unavailable; skipping management")
            return

    if open_orders is None:
        open_orders_result = _fetch_open_orders(headers)
        if open_orders_result is None:
            log.warning("Open orders unavailable; skipping management")
            return
        open_orders, _ = open_orders_result

    broker_qty: Dict[str, float] = {}
    for p in positions:
        sym = p.get("symbol")
        if not sym:
            continue
        try:
            broker_qty[sym] = float(p.get("qty", 0) or 0)
        except (TypeError, ValueError):
            broker_qty[sym] = 0.0

    open_sells, protective_stops = _sells_and_stops(open_orders)
    reserved_exits = {p.ticker: p.exit_request_qty 
                      for p in predictions 
                      if p.exit_request_qty > 0 and not p.exit_order_id}
    allocated_this_cycle: Dict[str, float] = {}

    for pred in sorted(predictions, key=lambda p: p.date or ""):
        if not pred.ticker or pred.validation_error:
            continue

        # Entry cancels (always allowed)
        if not pred.entry_terminal:
            if pred.status == "DEAD":
                continue
            if (pred.order_id
                    and not pred.entry_cancel_pending
                    and pred.status in ("PENDING", "ENTRY_PARTIALLY_FILLED")):
                pred.entry_cancel_pending = True
                _refresh_status(pred)
                memory["predictions"] = predictions
                if not save_memory(memory):
                    pred.entry_cancel_pending = False
                    _refresh_status(pred)
                    log.error(f"{pred.ticker}: cannot persist cancel intent; aborting")
                    continue
                del_status, timed_out = alpaca_delete(f"/v2/orders/{pred.order_id}", headers)
                if del_status != DeleteStatus.SUCCESS and not timed_out:
                    pred.entry_cancel_pending = False
                    _refresh_status(pred)
                    memory["predictions"] = predictions
                    save_memory(memory)
                    log.warning(f"{pred.ticker}: cancel rejected")
                else:
                    log.info(f"{pred.ticker}: cancel submitted (pending={timed_out})")
            continue

        # Exits (require allow_exits)
        if not allow_exits:
            log.critical(f"{pred.ticker}: exit blocked — reconciliation FAILED")
            continue
        
        # BEARISH gate
        if pred.direction != "BULLISH":
            log.critical(f"{pred.ticker}: non-BULLISH prediction cannot be managed")
            continue
        
        if pred.qty_filled <= 0:
            continue
        if pred.qty_exited >= pred.qty_filled:
            continue
        if pred.exit_client_order_id or pred.exit_order_id:
            continue

        remaining = pred.remaining_qty
        if remaining <= 0:
            continue

        committed = (open_sells.get(pred.ticker, 0.0)
                     + reserved_exits.get(pred.ticker, 0.0)
                     + allocated_this_cycle.get(pred.ticker, 0.0))
        available = max(0.0, broker_qty.get(pred.ticker, 0.0) - committed)
        qty_to_exit = int(min(remaining, available))
        if qty_to_exit <= 0:
            log.info(f"{pred.ticker}: no sellable budget (remaining={remaining}, avail={available:.0f})")
            continue

        # Cap max_hold_days
        pred.max_hold_days = min(pred.max_hold_days, MAX_HOLD_DAYS_ABSOLUTE)

        exec_price = get_executable_price(pred.ticker, headers)
        if exec_price is None:
            continue
        entry_price = pred.entry_price
        if not (math.isfinite(entry_price) and entry_price > 0):
            continue
        atr = pred.atr
        if not (math.isfinite(atr) and atr > 0):
            continue

        days_held = _trading_days_held(pred.date)

        pnl_atr = ((exec_price - entry_price) * qty_to_exit) / (qty_to_exit * atr)

        decision = "HOLD"
        if days_held >= 1 and pnl_atr <= 0.2:
            decision = "EARLY_EJECT"
        elif days_held >= pred.max_hold_days:
            decision = "EXTEND_TIME" if pnl_atr >= 0.8 else "STANDARD_EXIT"

        if decision == "EXTEND_TIME":
            pred.max_hold_days = min(pred.max_hold_days + 2, MAX_HOLD_DAYS_ABSOLUTE)
            log.info(f"{pred.ticker}: EXTEND +2d (now {pred.max_hold_days}d)")
            continue
        if decision not in ("EARLY_EJECT", "STANDARD_EXIT"):
            continue

        # Cancel protective stops and verify
        has_stops = protective_stops.get(pred.ticker, 0) > 0
        if has_stops:
            ok, unresolved = _cancel_and_verify_stops(headers, open_orders, pred.ticker)
            if not ok:
                log.critical(f"{pred.ticker}: stop cancel not confirmed ({unresolved}); deferring exit")
                continue
            # Refresh snapshot after cancel
            open_orders_result = _fetch_open_orders(headers)
            if open_orders_result is None:
                log.critical(f"{pred.ticker}: cannot refresh open orders after cancel")
                continue
            open_orders, _ = open_orders_result
            # Recompute protective_stops
            _, protective_stops = _sells_and_stops(open_orders)

        exit_ts = datetime.now(timezone.utc)
        exit_cid = generate_client_order_id(pred.ticker, "EXIT", exit_ts, tag=decision)
        pred.exit_client_order_id = exit_cid
        pred.exit_request_qty = qty_to_exit
        pred.exit_reason = decision
        _refresh_status(pred)
        memory["predictions"] = predictions
        if not save_memory(memory):
            pred.exit_client_order_id = None
            pred.exit_request_qty = 0
            pred.exit_reason = None
            _refresh_status(pred)
            log.error(f"{pred.ticker}: cannot persist exit intent; aborting")
            continue

        order_data = {
            "symbol": pred.ticker,
            "qty": str(qty_to_exit),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": exit_cid,
        }
        result, post_status = alpaca_post("/v2/orders", headers, order_data)

        if post_status == PostStatus.SUCCESS and result:
            pred.exit_order_id = result.get("id")
            allocated_this_cycle[pred.ticker] = (
                allocated_this_cycle.get(pred.ticker, 0) + qty_to_exit
            )
            log.info(f"{pred.ticker}: exit submitted qty={qty_to_exit}")
        elif post_status == PostStatus.REJECTED:
            pred.exit_client_order_id = None
            pred.exit_request_qty = 0
            pred.exit_reason = None
            log.warning(f"{pred.ticker}: exit rejected")
        else:
            allocated_this_cycle[pred.ticker] = (
                allocated_this_cycle.get(pred.ticker, 0) + qty_to_exit
            )
            log.warning(f"{pred.ticker}: exit outcome unknown; reserved={qty_to_exit}")

        _refresh_status(pred)

# ==============================================================================
# CANDIDATE SCANNING
# ==============================================================================

def scan_candidates(strategy: str, cache: MarketDataCache) -> List[Dict[str, Any]]:
    if strategy == "AGGRESSIVE_MOMENTUM":
        universe = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD",
                    "DBC", "VNQ", "HYG", "LQD", "UUP"]
    else:
        return []

    candidates: List[Dict[str, Any]] = []
    for ticker in universe:
        data = cache.get(ticker)
        if data is None or not data.is_valid_for_trading:
            continue
        if not data.is_bar_fresh(max_age_hours=80):
            log.info(f"{ticker}: stale data, skipping")
            continue

        s = data.close_series
        current_price = data.current_price
        atr = data.atr

        if len(s) < 6:
            continue
        now = float(s.iloc[-1])
        then = float(s.iloc[-6])
        if not (then > 0 and math.isfinite(now) and math.isfinite(then)):
            continue
        mom = (now / then) - 1
        if mom <= 0 or not math.isfinite(mom):
            continue

        if len(s) >= 2:
            prev = float(s.iloc[-2])
            if prev > 0:
                gap = (current_price / prev) - 1
                if abs(gap) > 0.05:
                    continue

        candidates.append({
            "ticker": ticker,
            "direction": "BULLISH",
            "price": current_price,
            "atr": atr,
            "_abs_mom": abs(mom),
        })

    candidates.sort(key=lambda c: c["_abs_mom"], reverse=True)
    for c in candidates:
        c.pop("_abs_mom", None)
    result = candidates[:5]
    log.info(f"Scanner: {len(result)}/{len(universe)} candidates passed")
    return result

# ==============================================================================
# MEMORY
# ==============================================================================

def _default_memory() -> Dict[str, Any]:
    return {
        "predictions": [],
        "multipliers": {},
        "strategy_stats": {},
        "version": 0,
        "win_rate": 0.5,
        "memory_version": MEMORY_VERSION,
        "_valid": True,
    }

def _migrate_memory(raw: Any) -> Dict:
    if not isinstance(raw, dict):
        raise ValueError(f"memory top-level must be dict, got {type(raw).__name__}")

    version = raw.get("memory_version", 1)
    if not isinstance(version, int):
        raise ValueError(f"memory_version must be int, got {type(version).__name__}")
    if version > MEMORY_VERSION:
        raise ValueError(f"memory schema v{version} > supported v{MEMORY_VERSION}")

    preds = raw.get("predictions", [])
    if not isinstance(preds, list):
        raise ValueError(f"predictions must be list, got {type(preds).__name__}")

    if version < MEMORY_VERSION:
        log.info(f"Migrating memory v{version} -> v{MEMORY_VERSION}")
        for p in preds:
            if not isinstance(p, dict):
                log.warning(f"Non-dict prediction during migration: {type(p)}")
                continue
            if "client_order_id" not in p:
                p["client_order_id"] = ""
            p.setdefault("qty_exited", 0)
            if not isinstance(p.get("qty_exited_by_order"), dict):
                p["qty_exited_by_order"] = {}
            p.setdefault("exit_client_order_id", None)
            p.setdefault("exit_order_id", None)
            p.setdefault("exit_reason", None)
            p.setdefault("exit_request_qty", 0)
            p.setdefault("reconciliation_attempts", 0)
            p.setdefault("last_reconciliation_error", None)
            p.setdefault("validation_error", None)
            p.setdefault("raw_snapshot", None)
            p.setdefault("entry_terminal", False)
            p.setdefault("entry_cancel_pending", False)
            p.setdefault("reject_reason", None)
            p.setdefault("stop_order_ids", [])

            old_status = p.get("status", "")
            if old_status in ("FILLED", "EXIT_PENDING", "PARTIALLY_EXITED", "CLOSED", "DEAD"):
                p["entry_terminal"] = True

            if (p.get("status") == "PENDING"
                    and not p.get("order_id")
                    and not p.get("client_order_id")):
                p["status"] = "RECONCILIATION_REQUIRED"
                p["last_reconciliation_error"] = "migrated without identifiers"

    raw["predictions"] = preds
    raw["memory_version"] = MEMORY_VERSION
    return raw

def load_memory() -> Tuple[Dict[str, Any], bool]:
    gh_token = os.environ.get('GITHUB_TOKEN')
    repo = os.environ.get('GITHUB_REPOSITORY')

    mem = _default_memory()
    if not gh_token or not repo:
        log.warning("GitHub not configured; empty memory (valid)")
        return mem, True

    gh_headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
    }
    url = f"https://api.github.com/repos/{repo}/contents/memory/memory.json"

    try:
        resp = requests.get(url, headers=gh_headers, timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        log.error(f"Memory fetch failed: {e}")
        mem["_valid"] = False
        return mem, False

    if resp.status_code == 404:
        log.info("No memory file; starting fresh")
        return mem, True
    if resp.status_code != 200:
        log.error(f"Memory fetch {resp.status_code}; blocking")
        mem["_valid"] = False
        return mem, False

    try:
        payload = resp.json()
        sha = payload["sha"]
        raw = json.loads(base64.b64decode(payload["content"]).decode())
    except Exception as e:
        log.error(f"Memory decode failed: {e}; blocking")
        mem["_valid"] = False
        return mem, False

    try:
        migrated = _migrate_memory(raw)
    except Exception as e:
        log.error(f"Memory migration failed: {e}; blocking")
        mem["_valid"] = False
        return mem, False

    migrated["_sha"] = sha

    validated = []
    for raw_pred in migrated.get("predictions", []):
        validated.append(_validate_and_parse_prediction(raw_pred))
    migrated["predictions"] = validated

    for k, v in _default_memory().items():
        migrated.setdefault(k, v)

    migrated["_valid"] = True
    return migrated, True

def _must_preserve(p: Prediction) -> bool:
    if p.validation_error:
        return True
    if p.raw_snapshot is not None:
        return True
    if p.status == "RECONCILIATION_REQUIRED":
        return True
    if not p.entry_terminal:
        return True
    if p.entry_cancel_pending:
        return True
    if p.exit_client_order_id or p.exit_order_id:
        return True
    if p.exit_request_qty > 0:
        return True
    if p.remaining_qty > 0:
        return True
    return False

def _trim_predictions(preds: List[Any]) -> List[Any]:
    preds = [p if isinstance(p, Prediction) else _validate_and_parse_prediction(p) for p in preds]
    protected = [p for p in preds if _must_preserve(p)]
    trimmable = [p for p in preds if not _must_preserve(p)]

    if len(protected) > MAX_PREDICTIONS:
        log.error(f"Protected predictions ({len(protected)}) > MAX ({MAX_PREDICTIONS}). Keeping all.")
        return protected

    room = MAX_PREDICTIONS - len(protected)
    kept = protected + (trimmable[-room:] if room > 0 else [])
    kept.sort(key=lambda p: p.date or "")
    return kept

def save_memory(memory: Dict[str, Any]) -> bool:
    gh_token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not gh_token or not repo:
        log.warning("GitHub not configured; memory not persisted")
        return False

    try:
        memory["predictions"] = _trim_predictions(memory.get("predictions", []))

        gh_headers = {
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
        }
        clean = {k: v for k, v in memory.items() if not k.startswith("_")}
        clean["predictions"] = [
            p.to_dict() if isinstance(p, Prediction) else p
            for p in memory.get("predictions", [])
        ]
        body = {
            "message": f"apex-k: v{memory.get('version', 0)} [skip ci]",
            "branch": "main",
            "content": base64.b64encode(json.dumps(clean, indent=2).encode()).decode(),
        }
        if memory.get("_sha"):
            body["sha"] = memory["_sha"]

        url = f"https://api.github.com/repos/{repo}/contents/memory/memory.json"
        
        max_attempts = 3
        for attempt in range(max_attempts):
            resp = requests.put(url, headers=gh_headers, json=body, timeout=DEFAULT_TIMEOUT)
            if resp.status_code in (200, 201):
                try:
                    new_sha = resp.json().get("content", {}).get("sha")
                    if new_sha:
                        memory["_sha"] = new_sha
                except Exception as e:
                    log.warning(f"save_memory: SHA not propagated ({e})")
                return True
            
            if resp.status_code == 409 and attempt < max_attempts - 1:
                # Merge conflict - fetch remote and merge
                rg = requests.get(url, headers=gh_headers, timeout=DEFAULT_TIMEOUT)
                if rg.status_code == 200:
                    remote = json.loads(base64.b64decode(rg.json()["content"]).decode())
                    our_ids = {p.order_id for p in memory.get("predictions", []) if p.order_id}
                    for rp in remote.get("predictions", []):
                        if isinstance(rp, dict) and rp.get("order_id") not in our_ids:
                            memory["predictions"].append(_validate_and_parse_prediction(rp))
                    memory["_sha"] = rg.json()["sha"]
                    body["sha"] = memory["_sha"]
                    continue
            
            log.warning(f"Memory PUT {resp.status_code}: {resp.text[:120]}")
            return False
        
        return False
    except Exception as e:
        log.error(f"Memory save failed: {e}")
        return False

# ==============================================================================
# MAIN CYCLE
# ==============================================================================

def main() -> int:
    log.info("=" * 70)
    log.info("APEX-K V14.3.9 CYCLE START")
    log.info("=" * 70)

    headers = get_alpaca_headers()
    if not headers:
        return 1

    account = alpaca_get("/v2/account", headers)
    if account is None:
        return 1

    try:
        equity = float(account.get('equity', 0))
    except (TypeError, ValueError):
        log.error("Invalid equity value")
        return 1

    if not math.isfinite(equity) or equity < 1000:
        log.warning(f"Invalid or low equity ${equity:.0f}. Standing down.")
        return 0
    log.info(f"Account equity: ${equity:,.2f}")

    memory, valid = load_memory()
    if not valid:
        log.error("Memory load failed; blocking cycle")
        return 1

    predictions = memory.get("predictions", [])
    
    # BEARISH gate at startup
    if any(p.direction == "BEARISH" and p.is_open() for p in predictions):
        log.critical("BEARISH prediction present — unsupported. Standing down.")
        return 1
    
    predictions, recon_status = reconcile_predictions(predictions, headers)

    if not check_trading_window(headers):
        log.info("Market closed. Standing down.")
        memory["predictions"] = predictions
        save_memory(memory)
        return 0

    # Asymmetric gates
    allow_exits = (recon_status in (ReconStatus.COMPLETE, ReconStatus.INCOMPLETE))
    allow_entries = (recon_status == ReconStatus.COMPLETE)

    cache = MarketDataCache()
    
    # Fetch positions and orders once
    positions = alpaca_get("/v2/positions", headers)
    open_orders_result = _fetch_open_orders(headers)
    if open_orders_result is None:
        log.warning("Open orders unavailable; skipping management")
        open_orders = []
    else:
        open_orders, _ = open_orders_result
    
    manage_positions(headers, predictions, cache, memory, allow_exits,
                     positions=positions, open_orders=open_orders)
    
    # Ensure protective stops
    if positions is not None:
        ensure_protective_stops(headers, predictions, open_orders)

    if not allow_entries:
        log.warning(f"Reconciliation not complete ({recon_status}); blocking entries")
        memory["predictions"] = predictions
        save_memory(memory)
        return 0

    if not check_daily_loss_limit(headers):
        log.warning("Daily loss limit active. No new entries.")
        memory["predictions"] = predictions
        save_memory(memory)
        return 0

    state = compute_market_state(cache)
    log.info(
        f"Market state: VIX={state.get('vix')}, "
        f"Breadth={state.get('breadth')}, CS_Z={state.get('cs_z')}"
    )

    strategy, reason = verify_strategy(state)
    log.info(f"Strategy: {strategy} ({reason})")

    if strategy == "CRISIS_CASH":
        log.info("Crisis mode. No new entries.")
        memory["predictions"] = predictions
        save_memory(memory)
        return 0

    candidates = scan_candidates(strategy, cache)
    if not candidates:
        log.info("No candidates passed filters")
        memory["predictions"] = predictions
        save_memory(memory)
        return 0

    risk_frac = 0.005
    sl_mult = 3.0
    tp_mult = None
    time_stop = 60

    if positions is None:
        log.warning("Cannot fetch positions, skipping entries")
        memory["predictions"] = predictions
        save_memory(memory)
        return 0

    held_tickers: set = set()
    for pos in positions:
        sym = pos.get('symbol')
        if sym:
            held_tickers.add(sym)
    for order in open_orders:
        sym = order.get('symbol')
        if sym and order.get('side') in ('buy', 'sell'):
            held_tickers.add(sym)

    submitted_count = 0
    reserved_heat = 0.0

    for pred in predictions:
        if pred.status in ("PENDING", "ENTRY_PARTIALLY_FILLED", "INTENT_PENDING") \
                and not pred.entry_terminal:
            if pred.atr > 0 and pred.qty_submitted > 0:
                unfilled = max(0, pred.qty_submitted - pred.qty_filled)
                reserved_heat += unfilled * sl_mult * pred.atr

    for cand in candidates:
        ticker = cand['ticker']
        if ticker in held_tickers:
            log.info(f"Skipping {ticker}: already holding or pending")
            continue

        data = cache.get(ticker)
        if data is None or not data.is_valid_for_trading:
            log.warning(f"Invalid data for {ticker}, skipping")
            continue

        exec_price = get_executable_price(ticker, headers)
        if exec_price is None:
            log.warning(f"{ticker}: no fresh quote; skipping")
            continue

        price = exec_price
        atr = data.atr
        if not (math.isfinite(atr) and atr > 0):
            log.warning(f"Invalid ATR for {ticker}: {atr}")
            continue

        risk_budget = equity * risk_frac
        stop_distance = sl_mult * atr
        if not (math.isfinite(stop_distance) and stop_distance > 0):
            log.warning(f"Invalid stop distance for {ticker}")
            continue

        qty = math.floor(risk_budget / stop_distance)
        if qty <= 0:
            log.info(f"Skipping {ticker}: risk budget too small")
            continue

        penalty = portfolio_correlation_penalty(ticker, list(held_tickers), cache)
        if penalty <= 0:
            log.warning(f"Correlation check failed for {ticker}, skipping")
            continue
        qty = math.floor(qty * penalty)
        if qty <= 0:
            log.info(f"Skipping {ticker}: adjusted quantity too small")
            continue

        qty = check_volume_participation(data, qty)
        if qty <= 0:
            log.info(f"Skipping {ticker}: volume participation cap")
            continue

        order_value = qty * price
        if not check_stress_test_margin(order_value, headers):
            log.info(f"Skipping {ticker}: stress test failed")
            continue

        current_heat = get_portfolio_heat_with_reserved(headers, reserved_heat, positions, open_orders)
        if current_heat is None:
            log.warning(f"Heat calculation failed, skipping {ticker}")
            continue

        max_heat = 0.03 * equity
        proposed_heat = qty * stop_distance
        if current_heat + proposed_heat > max_heat:
            log.info(f"Skipping {ticker}: portfolio heat cap reached")
            continue

        direction = cand['direction']
        side = "buy" if direction == "BULLISH" else "sell"
        limit_p = round(price * 1.001, 2) if side == "buy" else round(price * 0.999, 2)
        sl_p = round(price - stop_distance, 2) if side == "buy" else round(price + stop_distance, 2)

        tp_p = None
        if tp_mult and tp_mult > 0:
            tp_distance = tp_mult * atr
            tp_p = round(price + tp_distance, 2) if side == "buy" else round(price - tp_distance, 2)

        if not validate_order_geometry(side, limit_p, sl_p, tp_p):
            log.warning(f"Skipping {ticker}: invalid order geometry")
            continue

        timestamp = datetime.now(timezone.utc)
        client_order_id = generate_client_order_id(ticker, direction, timestamp)

        order_data = {
            "symbol": ticker,
            "qty": str(qty),
            "side": side,
            "type": "limit",
            "limit_price": f"{limit_p:.2f}",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        if tp_p is not None:
            order_data["order_class"] = "bracket"
            order_data["take_profit"] = {"limit_price": f"{tp_p:.2f}"}
            order_data["stop_loss"] = {"stop_price": f"{sl_p:.2f}"}
        else:
            order_data["order_class"] = "oto"
            order_data["stop_loss"] = {"stop_price": f"{sl_p:.2f}"}

        pred = Prediction(
            order_id="",
            client_order_id=client_order_id,
            ticker=ticker,
            direction=direction,
            entry_price=limit_p,
            qty_submitted=qty,
            qty_filled=0,
            status="INTENT_PENDING",
            atr=atr,
            max_hold_days=time_stop,
            date=timestamp.isoformat(),
            strategy=strategy,
        )
        _refresh_status(pred)
        predictions.append(pred)
        memory["predictions"] = predictions

        if not save_memory(memory):
            log.error(f"{ticker}: cannot persist entry intent; aborting")
            predictions.pop()
            memory["predictions"] = predictions
            continue

        result, post_status = alpaca_post("/v2/orders", headers, order_data)

        if post_status == PostStatus.SUCCESS and result:
            pred.order_id = result.get('id')
            _refresh_status(pred)
            log.info(f"✅ {ticker} ({direction}) QUEUED | Qty: {qty}")
            submitted_count += 1
            held_tickers.add(ticker)
            reserved_heat += proposed_heat

        elif post_status == PostStatus.UNKNOWN:
            existing, st = lookup_order_by_client_id(client_order_id, headers)
            if st == LookupStatus.FOUND and existing:
                pred.order_id = existing.get('id')
                _refresh_status(pred)
                log.info(f"✅ {ticker} ({direction}) RECOVERED | ID: {pred.order_id}")
                submitted_count += 1
                held_tickers.add(ticker)
                reserved_heat += proposed_heat
            else:
                pred.last_reconciliation_error = f"post unknown, lookup={st.value}"
                held_tickers.add(ticker)
                reserved_heat += proposed_heat
                _refresh_status(pred)
                log.warning(f"❌ {ticker}: post unknown, lookup={st.value} — preserved for reconciliation")

        else:
            log.warning(f"❌ {ticker} order rejected")
            predictions.pop()
            memory["predictions"] = predictions

        save_memory(memory)

    memory["predictions"] = predictions
    memory["version"] = memory.get("version", 0) + 1
    memory["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_memory(memory)

    log.info(f"Cycle complete: {submitted_count} orders submitted")
    log.info("=" * 70)
    return 0

# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    with cycle_lock() as acquired:
        if acquired:
            sys.exit(main())
        else:
            sys.exit(1)