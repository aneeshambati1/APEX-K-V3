import os
import json
import base64
import math
import re
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import warnings
warnings.filterwarnings('ignore')

print("="*70)
print("🌌 APEX-K V14.0: THE DEFINITIVE PRODUCTION MASTERPIECE 🌌")
print("="*70)

# ==============================================================================
# GLOBAL CONSTANTS
# ==============================================================================
DEFAULT_TIMEOUT = 15  # BUG #9 FIX: Global timeout for all requests

# ==============================================================================
# VAULT CONNECTION
# ==============================================================================
GEMINI_KEY = os.environ.get('GEMINI_API_KEY')
GROQ_KEY = os.environ.get('GROQ_API_KEY')
ALPACA_KEY = os.environ.get('ALPACA_API_KEY')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET_KEY')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
REPO = os.environ.get('GITHUB_REPOSITORY')
NTFY_TOPIC = os.environ.get('NTFY_TOPIC')

if not all([GEMINI_KEY, ALPACA_KEY, ALPACA_SECRET]):
    print("[FATAL] Missing Core API keys.")
    exit(1)

BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET, "Content-Type": "application/json"}
GH_HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"} if GITHUB_TOKEN else {}

memory = {"_sha": None, "predictions": [], "multipliers": {}, "version": 0, 
          "win_rate": 0.5, "strategy_stats": {}, "strategy_risk_overrides": {}, 
          "disabled_strategies": []}

# ==============================================================================
# V14.0 STRATEGY LIBRARY (Expanded with 5 new strategies)
# ==============================================================================
STRATEGY_LIBRARY = {
    "AGGRESSIVE_MOMENTUM": {"universe": ["NVDA", "AMD", "MSFT", "META", "TSLA", "AVGO"], "risk_frac": 0.005, "sl_mult": 3.0, "tp_mult": None, "time_stop": 60},
    "DEFENSIVE_MOMENTUM": {"universe": ["MSFT", "AAPL", "JNJ", "PG", "KO"], "risk_frac": 0.0025, "sl_mult": 2.0, "tp_mult": 2.0, "time_stop": 10},
    "MEAN_REVERSION_SWING": {"universe": ["SPY", "QQQ", "XLU", "XLP", "WMT"], "risk_frac": 0.0025, "sl_mult": 2.0, "tp_mult": 1.5, "time_stop": 3},
    "CRISIS_CASH": {"universe": [], "risk_frac": 0, "sl_mult": 0, "tp_mult": 0, "time_stop": 0},
    "VOLATILITY_HARVEST": {"universe": ["SVXY"], "risk_frac": 0.005, "sl_mult": 2.0, "tp_mult": 1.5, "time_stop": 10},
    "V_SHAPE_RECOVERY": {"universe": ["SPY", "QQQ", "XLF", "XLI", "XLY"], "risk_frac": 0.005, "sl_mult": 2.5, "tp_mult": None, "time_stop": 30},
    "GRINDING_BEAR_AVOIDANCE": {"universe": [], "risk_frac": 0, "sl_mult": 0, "tp_mult": 0, "time_stop": 0},
    "SECTOR_ROTATION": {"universe": ["XLK","XLF","XLE","XLV","XLI","XLY","XLP","XLU","XLB","XLRE","XLC"], "risk_frac": 0.004, "sl_mult": 2.5, "tp_mult": 3.0, "time_stop": 15},
    # NEW STRATEGIES
    "SAFE_HAVEN_FLIGHT": {"universe": ["GLD", "TLT", "IEF"], "risk_frac": 0.003, "sl_mult": 2.5, "tp_mult": 3.0, "time_stop": 15},
    "YIELD_CURVE_STEEPENER": {"universe": ["TLT", "XLU", "XLRE"], "risk_frac": 0.003, "sl_mult": 2.0, "tp_mult": 2.5, "time_stop": 12},
    "DOLLAR_WEAKNESS_ROTATION": {"universe": ["EEM", "EFA", "FXI"], "risk_frac": 0.003, "sl_mult": 2.5, "tp_mult": 2.5, "time_stop": 20},
    "GOLD_HEDGE": {"universe": ["GLD", "SLV", "GDX"], "risk_frac": 0.0025, "sl_mult": 2.5, "tp_mult": 2.0, "time_stop": 10},
    "SMALL_CAP_BREAKOUT": {"universe": ["IWM", "IWO", "IJR", "IJT", "IWN"], "risk_frac": 0.004, "sl_mult": 3.0, "tp_mult": 3.5, "time_stop": 20}
}

# ==============================================================================
# SAFE DATA FETCH WRAPPER (BUG #16 + BUG #1 FIX: single-ticker handling)
# ==============================================================================
def safe_yf_download(tickers, period, retries=2):
    for i in range(retries):
        try:
            df = yf.download(tickers, period=period, progress=False)
            if df.empty: 
                if i < retries - 1: time.sleep(2 ** i)
                continue
            
            # BUG #1 FIX: Handle single-ticker case
            if isinstance(tickers, str) or (isinstance(tickers, list) and len(tickers) == 1):
                # Single ticker returns flat DataFrame, not MultiIndex
                if 'Close' in df.columns and not isinstance(df.columns, pd.MultiIndex):
                    # Convert to expected format
                    ticker_name = tickers if isinstance(tickers, str) else tickers[0]
                    df = df[['Close']].copy()
                    df.columns = [ticker_name]
            return df
        except: 
            if i < retries - 1: time.sleep(2 ** i)
    return pd.DataFrame()

# ==============================================================================
# VERIFICATION GATE (with 5 new strategy gates)
# ==============================================================================
def verify_proposal(proposal, state):
    strat = proposal.get("selected_strategy", "CRISIS_CASH")
    conf = proposal.get("confidence", 0)
    if conf < 70: return "CRISIS_CASH", "OVERRIDDEN: Low confidence."
    
    cs_z = state.get('cs_z', 0) or 0
    if cs_z > 2.5 and strat not in ["CRISIS_CASH", "GRINDING_BEAR_AVOIDANCE"]: 
        return "CRISIS_CASH", f"OVERRIDDEN: Credit Z > 2.5."
    
    if state.get('vix', 0) > 35 and strat in ["AGGRESSIVE_MOMENTUM", "MEAN_REVERSION_SWING", "V_SHAPE_RECOVERY"]: 
        return "CRISIS_CASH", f"OVERRIDDEN: VIX > 35."
    
    if state.get('term_ratio', 1.0) > 1.15: 
        return "CRISIS_CASH", f"OVERRIDDEN: VIX backwardation {state.get('term_ratio', 0):.2f} > 1.15."
    
    # DXY + Yields spiking override
    if state.get('dxy_5d', 0) > 0.015 and state.get('tnx_5d', 0) > 0.10:
        if strat in ["AGGRESSIVE_MOMENTUM", "V_SHAPE_RECOVERY"]:
            return "DEFENSIVE_MOMENTUM", f"OVERRIDDEN: DXY + yields both spiking."
    
    # VOLATILITY_HARVEST only allowed in deep contango
    if strat == "VOLATILITY_HARVEST" and state.get('term_ratio', 1.0) >= 0.85:
        return "CRISIS_CASH", f"OVERRIDDEN: VIX not in deep contango."
    
    # NEW STRATEGY GATES
    if strat == "SAFE_HAVEN_FLIGHT":
        if not (state.get('cs_z', 0) >= 1.0 and state.get('vix_5d', 0) > 0.10):
            return "CRISIS_CASH", "OVERRIDDEN: Safe haven needs credit stress + VIX expansion."
    
    if strat == "YIELD_CURVE_STEEPENER":
        if state.get('tnx_5d', 0) > -0.10:
            return "CRISIS_CASH", "OVERRIDDEN: Yields not falling."
    
    if strat == "DOLLAR_WEAKNESS_ROTATION":
        if state.get('dxy_5d', 0) > -0.015:
            return "CRISIS_CASH", "OVERRIDDEN: Dollar not weakening."
        if state.get('vix', 0) > 25:
            return "CRISIS_CASH", "OVERRIDDEN: VIX too high for EM."
    
    if strat == "GOLD_HEDGE":
        if not (state.get('vix_5d', 0) > 0.08 and state.get('vix', 0) < 28):
            return "CRISIS_CASH", "OVERRIDDEN: Gold hedge needs VIX expanding (8%+) but not panicking (<28)."
    
    if strat == "SMALL_CAP_BREAKOUT":
        if state.get('breadth', 0) < 0.70:
            return "CRISIS_CASH", "OVERRIDDEN: Breadth too weak for small cap breakout."
        if state.get('vix', 0) > 20:
            return "CRISIS_CASH", "OVERRIDDEN: VIX too high for small cap risk."
    
    spy_sma = state.get('spy_sma', 0)
    if pd.isna(spy_sma): spy_sma = 0.0
    
    if spy_sma < -0.05 and state.get('breadth', 0) < 0.40 and state.get('vix', 0) < 25 \
       and strat not in ["GRINDING_BEAR_AVOIDANCE", "CRISIS_CASH"]: 
        return "GRINDING_BEAR_AVOIDANCE", f"OVERRIDDEN: Bearish internals."
    
    return strat, f"VERIFIED: {strat} ({conf}% conf)."

def extract_json(text):
    try:
        if "```json" in text: text = text.split("```json")[1].split("```")[0]
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match: return json.loads(match.group(0))
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match: return json.loads(match.group(0))
        return json.loads(text)
    except:
        return None

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================
def fetch_oas(series_id):
    try:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        df = pd.read_csv(url, parse_dates=["DATE"]).set_index("DATE")
        return pd.to_numeric(df[series_id], errors="coerce").dropna()
    except: return pd.Series()

def chronos_calendar(ts):
    dom = ts.day; dow = ts.weekday() 
    if dow == 4: return "LIQUIDATE_WEEKEND_RISK"
    return "NORMAL_SESSION"

def validate_swing_order(action, original_qty, proposed_qty, original_stop, proposed_stop):
    if action in {"ADD", "AVERAGE_DOWN"}: return False, "INCREASE_FORBIDDEN"
    if action == "NEW": return True, "ORDER_ALLOWED"
    return True, "ORDER_ALLOWED"

# BUG #12 FIX: Use signed PnL instead of abs()
def adaptive_time_manager(days_held, signed_pnl_atr, max_hold, current_price, entry_price, side):
    if days_held >= 1 and signed_pnl_atr <= 0.2: 
        return "EARLY_EJECT", "Velocity Stop."
    if days_held >= max_hold:
        if signed_pnl_atr >= 0.8:
            return "EXTEND_TIME", "Extend 2 days."
        else:
            return "STANDARD_EXIT", "Time limit reached."
    return "HOLD", "Normal."

# BUG #16 FIX: Cache JSON response
def validate_ticker(symbol):
    try:
        res = requests.get(f"{BASE_URL}/v2/assets/{symbol}", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT)
        if res.status_code == 200:
            data = res.json()
            return bool(data.get('tradable')) and data.get('status') == 'active'
        return False
    except: return False

def reconcile_partial_fills():
    try:
        res = requests.get(f"{BASE_URL}/v2/orders?status=all&limit=100", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT)
        orders = res.json() if res.status_code == 200 else []
        for order in orders:
            try:
                order_id = order.get('id')
                filled_qty = float(order.get('filled_qty') or 0)
                status = order.get('status')
                for p in memory.get("predictions", []):
                    if p.get("order_id") == order_id:
                        if status == 'canceled' and filled_qty > 0: p['qty'] = filled_qty
                        elif status == 'filled': p['qty'] = filled_qty
            except: continue
    except: pass

# BUG #5 FIX: Clean stale PENDING orders after 7 days
def garbage_collect_memory():
    now = datetime.now(timezone.utc)
    evaluated = []
    unevaluated = []
    for p in memory.get("predictions", []):
        if p.get("_evaluated"):
            evaluated.append(p)
        else:
            # Drop PENDING that never filled after 7 days
            if p.get("status") == "PENDING":
                try:
                    created = datetime.fromisoformat(p.get("date", "")).replace(tzinfo=timezone.utc)
                    if (now - created).days > 7:
                        continue  # drop stale
                except:
                    pass
            unevaluated.append(p)

    if len(evaluated) > 200:
        evaluated = evaluated[-200:]
    memory["predictions"] = evaluated + unevaluated

# ==============================================================================
# P2: PORTFOLIO HEAT CAP (BUG #10 FIX: None price handling)
# ==============================================================================
def get_portfolio_heat(ACCOUNT_EQUITY):
    try:
        pos = requests.get(f"{BASE_URL}/v2/positions", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT).json()
        orders = requests.get(f"{BASE_URL}/v2/orders?status=open&nested=true&limit=200", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT).json()
    except: return 0.0
    heat = 0.0
    for p in pos:
        try:
            entry = float(p.get('avg_entry_price') or 0)
            qty = abs(int(float(p.get('qty') or 0)))
            if entry <= 0: continue
            stop = entry * 0.97
            for o in orders:
                if o.get('symbol') == p['symbol'] and o.get('type') in ('stop','stop_limit'):
                    stop = float(o.get('stop_price', stop)); break
            heat += abs(entry - stop) * qty
        except: continue
    return heat

# ==============================================================================
# P2: DAILY DRAWDOWN CIRCUIT BREAKER
# ==============================================================================
def drawdown_breaker():
    try:
        acc = requests.get(f"{BASE_URL}/v2/account", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT).json()
        eq = float(acc.get('equity', 0)); last = float(acc.get('last_equity', eq))
        if last <= 0: return True
        dd = (eq - last) / last
        if dd <= -0.02:
            print(f"  🛑 DRAWDOWN BREAKER: {dd:.1%} today. No new entries.")
            return False
        return True
    except: return True

# BUG #9 FIX: DST-safe trading window using Alpaca clock
def in_trading_window():
    try:
        clock = requests.get(f"{BASE_URL}/v2/clock", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT).json()
        return clock.get('is_open', False)
    except:
        try:
            now = datetime.now(ZoneInfo("America/New_York"))
            if now.weekday() >= 5: return False
            mins = now.hour * 60 + now.minute
            return 575 <= mins <= 945
        except: return True

# BUG #17 FIX: Load macro calendar from repo JSON
def macro_blackout():
    now = datetime.now(timezone.utc).date()
    TIER1 = []
    if GITHUB_TOKEN and REPO:
        try:
            r = requests.get(f"https://api.github.com/repos/{REPO}/contents/config/macro_events.json", 
                             headers=GH_HEADERS, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                TIER1 = json.loads(base64.b64decode(r.json()["content"]).decode())
        except: pass
    
    if not TIER1:
        TIER1 = ["2026-12-10", "2026-12-16"]
    
    if all(d < now.strftime("%Y-%m-%d") for d in TIER1):
        print("  ⚠️ [MACRO] Calendar expired. Update config/macro_events.json.")
        return True
    
    for ev in TIER1:
        try:
            ev_date = datetime.fromisoformat(ev).date()
            if 0 <= (ev_date - now).days <= 2:
                print(f"  📅 [MACRO] Tier-1 event on {ev_date} < 48h. No new entries.")
                return True
        except: continue
    return False

# ==============================================================================
# P2: CORRELATION-AWARE POSITION SIZING
# ==============================================================================
def portfolio_beta_penalty(ticker, existing_tickers):
    if not existing_tickers: return 1.0
    try:
        px = safe_yf_download([ticker] + existing_tickers, period="3mo")
        if px.empty: return 1.0
        if isinstance(px.columns, pd.MultiIndex):
            px = px.droplevel(0, axis=1)
        px = px.dropna()
        if len(px.columns) < 2: return 1.0
        corr = px.pct_change().corr()
        if ticker not in corr.columns: return 1.0
        corr_series = corr[ticker].drop(ticker, errors='ignore')
        avg_corr = corr_series.mean() if len(corr_series) > 0 else 0.0
        if pd.isna(avg_corr): return 1.0
        if avg_corr > 0.85: return 0.25
        if avg_corr > 0.70: return 0.50
        if avg_corr > 0.50: return 0.75
        return 1.0
    except:
        return 1.0

# ==============================================================================
# P0 FIX: LEARN MULTIPLIERS (BUG #5: mark dead orders)
# ==============================================================================
def learn_multipliers():
    try:
        filled = requests.get(f"{BASE_URL}/v2/orders?status=filled&limit=200&direction=desc", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT).json()
    except: return
    mults = memory.get("multipliers", {})
    
    for p in memory.get("predictions", []):
        try:
            if p.get("_evaluated") or not p.get("order_id"): continue
            fill = next((f for f in filled if f.get('id') == p['order_id']), None)
            
            # BUG #5 FIX: Mark truly dead orders
            if not fill:
                try:
                    od = requests.get(f"{BASE_URL}/v2/orders/{p['order_id']}", headers=ALPACA_HEADERS, timeout=5)
                    if od.status_code == 200:
                        st = od.json().get('status')
                        if st in ('canceled', 'expired', 'rejected') and float(od.json().get('filled_qty') or 0) == 0:
                            p['_evaluated'] = True
                            p['_outcome_pct'] = 0.0
                            p['status'] = 'DEAD'
                except: pass
                continue
            
            fill_price = float(fill.get('filled_avg_price') or 0)
            if fill_price <= 0: continue
            
            close_side = "sell" if p['direction'] == "BULLISH" else "buy"
            fill_time = fill.get('filled_at', '')
            closes = [f for f in filled if f.get('symbol') == p['ticker'] 
                      and f.get('side') == close_side
                      and f.get('filled_at', '') > fill_time]
            if not closes: continue
            
            closes_sorted = sorted(closes, key=lambda x: x.get('filled_at', ''))
            exit_price = float(closes_sorted[0].get('filled_avg_price') or 0)
            if exit_price <= 0: continue
            
            pnl_pct = (exit_price - fill_price) / fill_price
            if p['direction'] == "BEARISH": pnl_pct = -pnl_pct
            
            cat = p.get('catalyst', 'UNKNOWN')
            current = mults.get(cat, 1.0)
            adj = 1.10 if pnl_pct > 0.005 else (0.90 if pnl_pct < -0.005 else 1.0)
            mults[cat] = round(max(0.5, min(2.0, current * adj)), 3)
            p['_evaluated'] = True
            p['_outcome_pct'] = round(pnl_pct, 4)
        except Exception as e:
            print(f"  [LEARN] Skipped pred {p.get('ticker')}: {e}")
            continue
    
    memory["multipliers"] = mults
    
    wins = sum(1 for p in memory["predictions"] if p.get("_outcome_pct", 0) > 0)
    total = sum(1 for p in memory["predictions"] if "_outcome_pct" in p)
    if total > 0:
        memory["win_rate"] = round(wins / total, 3)
    
    strat_stats = {}
    for p in memory["predictions"]:
        if "_outcome_pct" not in p: continue
        s = p.get("strategy", "UNKNOWN")
        if s not in strat_stats:
            strat_stats[s] = {"wins": 0, "total": 0}
        strat_stats[s]["total"] += 1
        if p["_outcome_pct"] > 0: strat_stats[s]["wins"] += 1
    for s, v in strat_stats.items():
        v["wr"] = round(v["wins"] / v["total"], 3) if v["total"] > 0 else 0.5
    memory["strategy_stats"] = strat_stats
    
    overrides = memory.get("strategy_risk_overrides", {})
    disabled = memory.get("disabled_strategies", [])
    
    for s, v in strat_stats.items():
        if v["total"] >= 20 and v["wr"] < 0.35:
            if s not in disabled:
                disabled.append(s)
                print(f"  🚫 [ADAPT] {s} DISABLED (WR {v['wr']:.1%}, {v['total']} trades).")
        elif v["total"] >= 10 and v["wr"] < 0.40 and s in STRATEGY_LIBRARY:
            current_risk = overrides.get(s, STRATEGY_LIBRARY[s]["risk_frac"])
            overrides[s] = max(0.0001, current_risk * 0.5)
            print(f"  ⚠️ [ADAPT] {s} underperforming ({v['wr']:.1%}). Risk halved to {overrides[s]:.4f}.")
    
    memory["strategy_risk_overrides"] = overrides
    memory["disabled_strategies"] = disabled
    
    print(f"  [LEARN] Win rate: {memory['win_rate']:.1%} | Catalysts: {len(mults)} | Strategies: {len(strat_stats)}")

# ==============================================================================
# BUG #2 FIX: Robust memory save (exclude message/branch from content)
# ==============================================================================
def save_memory(memory_dict, retries=3):
    CLEAN_EXCLUDE = {"_sha", "sha", "content", "message", "branch"}
    for attempt in range(retries):
        try:
            current_sha = None
            r = requests.get(f"https://api.github.com/repos/{REPO}/contents/memory/memory.json",
                             headers=GH_HEADERS, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                current_sha = r.json()["sha"]

            clean = {k: v for k, v in memory_dict.items() if k not in CLEAN_EXCLUDE}
            body = {
                "message": f"hippocampus: v{memory_dict.get('version', 0)} [skip ci]",
                "branch": "main",
                "content": base64.b64encode(json.dumps(clean, indent=2).encode()).decode(),
            }
            if current_sha:
                body["sha"] = current_sha

            resp = requests.put(
                f"https://api.github.com/repos/{REPO}/contents/memory/memory.json",
                headers=GH_HEADERS, json=body, timeout=DEFAULT_TIMEOUT
            )
            if resp.status_code in (200, 201):
                print("  [SUCCESS] Memory committed.")
                return True
        except Exception as e:
            print(f"  [SAVE] Attempt {attempt+1} failed: {e}")
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    print("  [WARN] Memory commit failed after retries.")
    return False

# ==============================================================================
# BUG #6 FIX: Adopt orphan positions
# ==============================================================================
def adopt_orphans(positions, memory):
    my_open = {p.get("ticker") for p in memory.get("predictions", []) 
               if p.get("status") not in ("CLOSED", "DEAD")}
    for pos in positions:
        try:
            sym = pos.get('symbol')
            if sym in my_open: continue
            print(f"  ⚠️ [ORPHAN] Adopting {sym} (no prediction record).")
            memory.setdefault("predictions", []).append({
                "order_id": None, "ticker": sym, "catalyst": "ORPHAN",
                "direction": "BULLISH" if float(pos.get('qty') or 0) > 0 else "BEARISH",
                "entry": float(pos.get('avg_entry_price') or 0),
                "atr": max(0.01, float(pos.get('avg_entry_price') or 1) * 0.02),
                "max_hold_days": 5, "status": "PENDING",
                "date": datetime.now(timezone.utc).isoformat(),
                "reasoning": "ORPHAN_ADOPTED", "strategy": "ORPHAN",
                "state_snapshot": {}
            })
        except: continue

# ==============================================================================
# NSE-PORTED RISK ORGANS
# ==============================================================================
def check_volume_participation(ticker, proposed_qty, max_participation_pct=0.05):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="2mo")
        avg_volume = float(hist['Volume'].tail(30).mean())
        max_allowed_qty = math.floor(avg_volume * max_participation_pct)
        if proposed_qty > max_allowed_qty:
            print(f"   🛑 VOLUME GOVERNOR: {ticker} order ({proposed_qty}) exceeds 5% of avg volume ({max_allowed_qty}). Capping size.")
            return max_allowed_qty
        return proposed_qty
    except:
        return proposed_qty

# BUG #11 FIX: Include shorts in stress test
def stress_test_margin(proposed_order_value):
    try:
        account_res = requests.get(f"{BASE_URL}/v2/account", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT)
        if account_res.status_code != 200: return True
        acc = account_res.json()
        current_equity = float(acc.get('equity') or 0)
        maintenance_margin = float(acc.get('maintenance_margin') or 0)
        long_mv = float(acc.get('long_market_value') or 0)
        short_mv = float(acc.get('short_market_value') or 0)
        # Longs crash -15%, shorts squeeze +15%
        simulated_equity = (
            current_equity 
            - (long_mv * 0.15)
            - (short_mv * 0.15)
            - (proposed_order_value * 0.15)
        )
        safety_threshold = maintenance_margin * 1.10 
        if simulated_equity < safety_threshold:
            print(f"   🛑 MARGIN STRESS TEST FAILED: Simulated crash breaches maintenance margin. Trade blocked.")
            return False
        return True
    except:
        return True

# ==============================================================================
# ORGAN 9: ORACLE & ORGAN 8: HIPPOCAMPUS
# ==============================================================================
print("\n🔮 [ORGAN 9: ORACLE] Fetching FRED OAS...")
hy = fetch_oas("BAMLH0A0HYM2")
ig = fetch_oas("BAMLC0A0CM")
credit_state = "NORMAL"
cs_z_score = 0.0
if not hy.empty and not ig.empty:
    df_oas = pd.concat([hy.rename("HY"), ig.rename("IG")], axis=1).dropna()
    z = lambda s: (s - s.rolling(60).mean()) / s.rolling(60).std()
    cs_z_score = float((0.6 * z(df_oas["HY"]) + 0.4 * z(df_oas["IG"])).iloc[-1])
    if cs_z_score >= 2.0: credit_state = "FREEZE_RISK"
    elif cs_z_score >= 1.0: credit_state = "EARLY_STRESS"
    print(f"  [ORACLE] Credit Z: {cs_z_score:.2f} | State: {credit_state}")

print("\n🧠 [ORGAN 8: HIPPOCAMPUS] Reading Memory...")
if GITHUB_TOKEN and REPO:
    try:
        r = requests.get(f"https://api.github.com/repos/{REPO}/contents/memory/memory.json", headers=GH_HEADERS, timeout=DEFAULT_TIMEOUT)
        if r.status_code == 200:
            memory = json.loads(base64.b64decode(r.json()["content"]).decode())
            memory["_sha"] = r.json()["sha"]
            # BUG #7 FIX: Set defaults for missing keys
            DEFAULTS = {
                "_sha": None, "predictions": [], "multipliers": {}, "version": 0,
                "win_rate": 0.5, "strategy_stats": {}, "strategy_risk_overrides": {},
                "disabled_strategies": []
            }
            for k, v in DEFAULTS.items():
                memory.setdefault(k, v)
            # Clean pollution
            memory.pop("message", None)
            memory.pop("branch", None)
            memory.pop("content", None)
    except: pass

# Apply persisted strategy risk overrides
for s, override in memory.get("strategy_risk_overrides", {}).items():
    if s in STRATEGY_LIBRARY:
        STRATEGY_LIBRARY[s]["risk_frac"] = override

reconcile_partial_fills()

# BUG #6 FIX: Adopt orphans after memory load
try:
    positions_res = requests.get(f"{BASE_URL}/v2/positions", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT)
    if positions_res.status_code == 200:
        adopt_orphans(positions_res.json(), memory)
except: pass

# BUG #6 + #8 FIX: Fetch real equity and check minimum
try:
    acct = requests.get(f"{BASE_URL}/v2/account", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT).json()
    ACCOUNT_EQUITY = float(acct.get('equity') or 100000)
except:
    ACCOUNT_EQUITY = 100000

if ACCOUNT_EQUITY < 1000:
    print(f"  🛑 [ACCOUNT] Equity ${ACCOUNT_EQUITY:.0f} too low. Standing down.")
    exit(0)

print(f"\n💰 [ACCOUNT] Equity: ${ACCOUNT_EQUITY:,.2f}")

# ==============================================================================
# ORGAN 1: JANITOR (Preserves Protective Stops)
# ==============================================================================
print("\n🧹 [ORGAN 1: JANITOR] Scrubbing Only Entry Orders...")
try:
    pos_res = requests.get(f"{BASE_URL}/v2/positions", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT)
    pos_symbols = {p['symbol'] for p in (pos_res.json() if pos_res.status_code == 200 else [])}
    orders_res = requests.get(f"{BASE_URL}/v2/orders?status=open&limit=200&nested=true", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT)
    open_orders = orders_res.json() if orders_res.status_code == 200 else []
    for o in open_orders:
        try:
            is_protective = (o.get('type') in ('stop', 'stop_limit', 'trailing_stop')
                             or (o.get('order_class') in ('bracket','oco','oto') and o.get('symbol') in pos_symbols))
            if is_protective: continue
            requests.delete(f"{BASE_URL}/v2/orders/{o['id']}", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT)
        except: pass
except Exception as e:
    print(f"  [WARN] Janitor error: {e}")

# ==============================================================================
# BUG #1 FIX: Friday liquidation blocks re-entry
# ==============================================================================
print("\n⏱️ [ORGAN 12/15: CHRONOS] Allocating Time & Profit States...")
now_ny = datetime.now(ZoneInfo("America/New_York"))
calendar_action = chronos_calendar(now_ny)
skip_new_entries = False

if calendar_action == "LIQUIDATE_WEEKEND_RISK":
    print("  🗑️ [CHRONOS] Friday Afternoon. Liquidating weekend risk.")
    try:
        pos = requests.get(f"{BASE_URL}/v2/positions", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT).json()
        for p in pos:
            sym = p['symbol']
            requests.delete(
                f"{BASE_URL}/v2/positions/{sym}?cancel_orders=true",
                headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT
            )
            print(f"  ✂️ Liquidated {sym} (legs canceled)")
    except Exception as e:
        print(f"  [WARN] Weekend liq failed: {e}")
    
    skip_new_entries = True
    print("  🚫 [CHRONOS] Friday liquidation active. Blocking re-entry.")

# ==============================================================================
# ORGAN 16: CHRONO-VELOCITY (BUG #10 + #12 FIXES)
# ==============================================================================
print("\n⏱️ [ORGAN 16: CHRONO-VELOCITY] Managing Open Positions...")
try:
    positions_res = requests.get(f"{BASE_URL}/v2/positions", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT)
    positions = positions_res.json() if positions_res.status_code == 200 else []
    
    my_tickers = {p.get("ticker") for p in memory.get("predictions", []) if p.get("status") not in ("CLOSED", "DEAD")}
    
    for pos in positions:
        try:
            ticker = pos.get('symbol')
            if ticker not in my_tickers: continue
            
            qty_raw = pos.get('qty')
            if qty_raw is None: continue
            qty = abs(int(float(qty_raw)))
            if qty <= 0: continue
            
            # BUG #10 FIX: Guard None prices
            entry_price = float(pos.get('avg_entry_price') or 0)
            current_price = float(pos.get('current_price') or 0)
            if entry_price <= 0 or current_price <= 0:
                print(f"  ⚠️ {ticker}: Invalid prices. Skip.")
                continue
            
            side = "long" if int(float(qty_raw)) > 0 else "short"
            
            open_preds = [p for p in memory.get("predictions", []) 
                          if p.get("ticker") == ticker and p.get("status") not in ("CLOSED", "DEAD")]
            trade_memory = open_preds[-1] if open_preds else None
            if not trade_memory: continue
            
            atr = trade_memory.get("atr", 1.0)
            max_hold = trade_memory.get("max_hold_days", 3)
            entry_date_str = trade_memory.get("date", "")
            if entry_date_str:
                entry_date = datetime.fromisoformat(entry_date_str).replace(tzinfo=timezone.utc)
                days_held = (datetime.now(timezone.utc) - entry_date).days
            else: 
                days_held = 0
            
            pnl_dollars = (current_price - entry_price) * qty if side == "long" else (entry_price - current_price) * qty
            # BUG #12 FIX: Use signed PnL
            signed_pnl_atr = pnl_dollars / (qty * atr) if atr > 0 else 0
            
            decision, reason = adaptive_time_manager(days_held, signed_pnl_atr, max_hold, current_price, entry_price, side)
            
            if decision in ["EARLY_EJECT", "STANDARD_EXIT"]:
                print(f"   ✂️ {ticker}: {decision}. Closing position + canceling bracket legs.")
                try:
                    requests.delete(
                        f"{BASE_URL}/v2/positions/{ticker}?cancel_orders=true",
                        headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT
                    )
                except Exception as e:
                    print(f"   [ERROR] Close failed for {ticker}: {e}")
                for p in memory["predictions"]:
                    if p.get("ticker") == ticker and p.get("status") not in ("CLOSED", "DEAD"):
                        p["status"] = "CLOSED"
                        p["closed_at"] = datetime.now(timezone.utc).isoformat()
            elif decision == "EXTEND_TIME":
                print(f"   ⏳ {ticker}: EXTENDING. (+2 Days)")
                for p in memory["predictions"]:
                    if p.get("ticker") == ticker and p.get("status") not in ("CLOSED", "DEAD"): 
                        p["max_hold_days"] = max_hold + 2
        except Exception as e:
            print(f"  [ERROR] Position {pos.get('symbol')}: {e}")
            continue
except Exception as e: 
    print(f"  [ERROR] Position management failed: {e}")

# ==============================================================================
# ORGAN 5/10: GROQ STRATEGY ROUTER & COMPASS
# ==============================================================================
print("\n🧭 [ORGAN 5/10: COMPASS & GROQ ROUTER] Assessing Regime...")
try:
    spy_data = safe_yf_download("SPY", period="1y")
    if spy_data.empty: raise ValueError("SPY data empty")
    spy_close_series = spy_data['Close']['SPY'] if isinstance(spy_data.columns, pd.MultiIndex) else spy_data['Close']
    spy_current = float(spy_close_series.iloc[-1])
    
    spy_sma200_series = spy_close_series.rolling(200).mean()
    spy_sma200 = float(spy_sma200_series.iloc[-1]) if not pd.isna(spy_sma200_series.iloc[-1]) else spy_current
    spy_ext = (spy_current / spy_sma200) - 1 if spy_sma200 > 0 else 0.0
    spy_20d_ret = (spy_current / float(spy_close_series.iloc[-20])) - 1 if len(spy_close_series) >= 20 else 0.0
    
    vix_data = safe_yf_download("^VIX", period="10d")
    vix_series = vix_data['Close']['^VIX'] if isinstance(vix_data.columns, pd.MultiIndex) else vix_data['Close']
    vix = float(vix_series.iloc[-1]); vix_5d = (vix / float(vix_series.iloc[-5])) - 1 if len(vix_series) >= 5 else 0.0
    
    try:
        vix3m_data = safe_yf_download("^VIX3M", period="10d")
        vix3m_series = vix3m_data['Close']['^VIX3M'] if isinstance(vix3m_data.columns, pd.MultiIndex) else vix3m_data['Close']
        vix3m = float(vix3m_series.iloc[-1])
        term_ratio = vix / vix3m if vix3m > 0 else 1.0
        term = "backwardation" if term_ratio > 1.0 else "contango"
    except:
        vix3m, term_ratio, term = vix * 1.1, 1.0, "unknown"
    
    SECTOR_ETFS = ["XLK","XLF","XLE","XLV","XLI","XLY","XLP","XLU","XLB","XLRE","XLC"]
    try:
        sect = safe_yf_download(SECTOR_ETFS, period="3mo")
        if sect.empty: raise ValueError("Sector data empty")
        if isinstance(sect.columns, pd.MultiIndex):
            sect = sect.droplevel(0, axis=1)
        above = sum(1 for t in SECTOR_ETFS if t in sect.columns and sect[t].iloc[-1] > sect[t].rolling(50).mean().iloc[-1])
        breadth = above / len(SECTOR_ETFS)
    except:
        breadth = 0.5
    
    try:
        dxy = safe_yf_download("DX-Y.NYB", period="1mo")
        dxy_series = dxy['Close']['DX-Y.NYB'] if isinstance(dxy.columns, pd.MultiIndex) else dxy['Close']
        dxy_ret_5d = float(dxy_series.iloc[-1] / dxy_series.iloc[-6] - 1)
        
        tnx = safe_yf_download("^TNX", period="1mo")
        tnx_series = tnx['Close']['^TNX'] if isinstance(tnx.columns, pd.MultiIndex) else tnx['Close']
        tnx_chg_5d = float(tnx_series.iloc[-1] - tnx_series.iloc[-6])
        
        gld = safe_yf_download("GLD", period="1mo")
        gld_series = gld['Close']['GLD'] if isinstance(gld.columns, pd.MultiIndex) else gld['Close']
        gld_ret_5d = float(gld_series.iloc[-1] / gld_series.iloc[-6] - 1)
    except:
        dxy_ret_5d = tnx_chg_5d = gld_ret_5d = 0.0
    
    state = {
        "vix": vix, "vix_5d": vix_5d, "term": term, "term_ratio": term_ratio,
        "spy_sma": spy_ext, "spy_20d": spy_20d_ret,
        "cs_z": cs_z_score, "breadth": breadth,
        "dxy_5d": dxy_ret_5d, "tnx_5d": tnx_chg_5d, "gld_5d": gld_ret_5d
    }
    
    print(f"  [STATE] VIX: {vix:.1f} ({term} {term_ratio:.2f}) | Breadth: {breadth:.1%} | Credit Z: {cs_z_score:.1f}")
    print(f"  [CROSS] DXY 5d: {dxy_ret_5d:+.2%} | 10Y Δ: {tnx_chg_5d:+.2f}bps | GLD 5d: {gld_ret_5d:+.2%}")

    groq_strategy = None
    if GROQ_KEY:
        print("  🧠 [GROQ] Querying Strategy Router...")
        models_res = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=DEFAULT_TIMEOUT)
        if models_res.status_code == 200:
            all_models = [m['id'] for m in models_res.json().get('data', [])]
            generative_models = [m for m in all_models if not any(x in m.lower() for x in ['guard', 'vision', 'whisper', 'tool'])]
            target_model = next((m for m in generative_models if '70b' in m.lower() or '8b' in m.lower() or 'qwen' in m.lower() or 'gpt-oss' in m.lower()), None)
            if not target_model and generative_models: target_model = generative_models[0]
            
            if target_model:
                prompt = f"""You are APEX-K Strategy Router. Select the optimal strategy.
MARKET STATE:
- VIX={state['vix']:.1f} (5d Chg: {state['vix_5d']:.1%}), Term={state['term']} (ratio {term_ratio:.2f})
- SPY vs SMA={state['spy_sma']:.1%}, SPY 20d={state['spy_20d']:.1%}
- Breadth={state['breadth']:.1%}, Credit Z={state['cs_z']:.2f}
- DXY 5d: {dxy_ret_5d:+.2%}, 10Y Δ: {tnx_chg_5d:+.2f}bps, Gold 5d: {gld_ret_5d:+.2%}
STRATEGIES: {', '.join(STRATEGY_LIBRARY.keys())}.
RESPOND IN EXACT JSON: {{"selected_strategy": "NAME", "confidence": 85, "reasoning": "1 sentence."}}"""
                
                payload = {"model": target_model, "messages": [{"role": "system", "content": "Output ONLY valid JSON."}, {"role": "user", "content": prompt}], "temperature": 0.1}
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}, json=payload, timeout=DEFAULT_TIMEOUT)
                if res.status_code == 200:
                    proposal = extract_json(res.json()['choices'][0]['message']['content'])
                    if proposal:
                        final_strat, verdict = verify_proposal(proposal, state)
                        print(f"  🧠 [GROQ] {final_strat} | {verdict}")
                        groq_strategy = final_strat
                    
except Exception as e:
    print(f"  [ERROR] Router failed: {e}. Defaulting to CRISIS.")
    state = {"vix": 40, "vix_5d": 0.0, "term": "backwardation", "term_ratio": 1.2, 
             "spy_sma": -0.10, "spy_20d": -0.05, "breadth": 0.20, "cs_z": 3.0,
             "dxy_5d": 0.0, "tnx_5d": 0.0, "gld_5d": 0.0}
    groq_strategy = "CRISIS_CASH"

# Apply strategy library selection
disabled_strategies = memory.get("disabled_strategies", [])
if groq_strategy in disabled_strategies:
    print(f"  🚫 [ROUTER] {groq_strategy} is disabled. Forcing CRISIS_CASH.")
    groq_strategy = "CRISIS_CASH"

# BUG #3 FIX: Handle missing GROQ_KEY
if groq_strategy and groq_strategy in STRATEGY_LIBRARY:
    active_strat = STRATEGY_LIBRARY[groq_strategy]
    UNIVERSE = active_strat["universe"]
    risk_frac = active_strat["risk_frac"]
    sl_mult = active_strat["sl_mult"]
    tp_mult = active_strat["tp_mult"]
    time_stop = active_strat["time_stop"]
else:
    if groq_strategy is None:
        print("  🛡️ [ROUTER] No router available (GROQ_KEY missing). Defaulting to CRISIS_CASH.")
        UNIVERSE = []; risk_frac = 0; sl_mult = 0; tp_mult = 0; time_stop = 0
        groq_strategy = "CRISIS_CASH"
    else:
        print(f"  ⚠️ [ROUTER] Unknown strategy '{groq_strategy}'. CRISIS_CASH.")
        UNIVERSE = []; risk_frac = 0; sl_mult = 0; tp_mult = 0; time_stop = 0
        groq_strategy = "CRISIS_CASH"

# ==============================================================================
# ORGAN 2/4: FUNNEL & SHIELD (BUG #14 FIX: cache hist)
# ==============================================================================
print("\n⚡ [ORGAN 2/4: FUNNEL & SHIELD] Scanning...")
shield_cache = {}  # BUG #14: Cache for HANDS

if not UNIVERSE:
    print("  🛡️ Shield blocks directional equity. Standing down.")
    clean_targets = []
else:
    if groq_strategy == "SECTOR_ROTATION":
        try:
            data = safe_yf_download(UNIVERSE, period="1mo")
            if data.empty: raise ValueError("Sector data empty")
            if isinstance(data.columns, pd.MultiIndex): data = data.droplevel(0, axis=1)
            close_data = data
            mom_20d = (data.iloc[-1] / data.iloc[-20] - 1).sort_values(ascending=False)
            sorted_tickers = mom_20d.head(3).index.tolist()
        except: 
            sorted_tickers = []
            close_data = pd.DataFrame()
    else:
        try:
            data = safe_yf_download(UNIVERSE, period="5d")
            if data.empty: raise ValueError("Universe data empty")
            close_data = data['Close'] if isinstance(data.columns, pd.MultiIndex) else data
            returns = close_data.pct_change().iloc[-1].dropna()
            sorted_tickers = returns.reindex(returns.abs().sort_values(ascending=False).index).index.tolist()
        except: 
            sorted_tickers = []
            close_data = pd.DataFrame()

    clean_targets = []
    for ticker in sorted_tickers[:5]:
        try:
            if not validate_ticker(ticker): continue
            if ticker not in close_data.columns: continue
            
            stock = yf.Ticker(ticker)
            current_price = float(close_data[ticker].iloc[-1])
            hist_3mo = stock.history(period="3mo")
            if hist_3mo.empty: continue
            
            # BUG #14: Cache hist for HANDS
            shield_cache[ticker] = hist_3mo
            
            if len(hist_3mo) >= 2:
                prev_close = float(hist_3mo['Close'].iloc[-2])
                if prev_close > 0:
                    today_gap = (current_price / prev_close) - 1
                    if abs(today_gap) > 0.05:
                        print(f"     ⚠️ {ticker}: {today_gap:+.1%} gap. Skipping (falling knife).")
                        continue
            
            rv_30 = np.log(hist_3mo['Close'] / hist_3mo['Close'].shift(1)).dropna().tail(30).std() * np.sqrt(252)
            iv = 0.0
            if stock.options:
                chain = stock.option_chain(stock.options[0])
                calls = chain.calls[chain.calls['ask'] > 0]
                if not calls.empty:
                    atm_c = calls.iloc[(calls['strike'] - current_price).abs().argsort()[:1]]
                    iv = float(atm_c['impliedVolatility'].values[0]) 
            if iv > 0 and rv_30 < (iv - 0.05): continue
            
            if groq_strategy == "SECTOR_ROTATION":
                move = float(mom_20d[ticker])
            else:
                move = float(returns[ticker]) if ticker in returns.index else 0.0
            clean_targets.append({"ticker": ticker, "move": move})
            print(f"     ✅ {ticker}: Passed Shield.")
        except: continue

# ==============================================================================
# ORGAN 3: BRAIN (Gemini)
# ==============================================================================
candidates = []
if clean_targets:
    print("\n🧠 [ORGAN 3: BRAIN] Querying Untrusted Analyst (Gemini)...")
    tickers = [t['ticker'] for t in clean_targets]
    prompt = f"Analyze news for: {', '.join(tickers)}. Output JSON array ONLY: [{{'ticker':'STR', 'direction':'BULLISH/BEARISH', 'conviction':0.0-1.0, 'est_move':0.0, 'catalyst':'STR'}}]"
    payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}}
    try:
        models_res = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_KEY}", timeout=DEFAULT_TIMEOUT).json()
        available_models = [m['name'].replace('models/', '') for m in models_res.get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]
        ai_success = False
        for model in available_models:
            if ai_success: break
            for ver in ['v1beta', 'v1']:
                try:
                    url = f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={GEMINI_KEY}"
                    res = requests.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
                    res.raise_for_status()
                    raw = res.json()['candidates'][0]['content']['parts'][0]['text']
                    parsed = extract_json(raw)
                    if isinstance(parsed, list):
                        candidates = parsed
                        print(f"  [SUCCESS] Brain connected via {ver}/{model}")
                        ai_success = True
                    break
                except: continue
    except: pass

# BUG #5 FIX: Filter hallucinated tickers
if candidates:
    approved_set = {t['ticker'] for t in clean_targets}
    original_count = len(candidates)
    candidates = [c for c in candidates if isinstance(c, dict) and c.get('ticker') in approved_set]
    if len(candidates) < original_count:
        print(f"  ⚠️ [SHIELD] Filtered out {original_count - len(candidates)} AI-hallucinated ticker(s).")

# ==============================================================================
# P1: GEMINI × GROQ CONSENSUS FILTER
# ==============================================================================
consensus_ok = False
if candidates and GROQ_KEY:
    print("\n🧠 [BRAIN-2] Groq consensus check...")
    try:
        tickers_list = [c['ticker'] for c in candidates]
        groq_consensus_prompt = f"""Analyze these tickers for directional bias using recent news only: {', '.join(tickers_list)}.
Return JSON array ONLY: [{{"ticker":"X","direction":"BULLISH/BEARISH","confidence":0-1}}]"""
        
        models_res = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=DEFAULT_TIMEOUT)
        if models_res.status_code == 200:
            all_models = [m['id'] for m in models_res.json().get('data', [])]
            generative_models = [m for m in all_models if not any(x in m.lower() for x in ['guard', 'vision', 'whisper', 'tool'])]
            target_model = next((m for m in generative_models if '70b' in m.lower() or '8b' in m.lower()), None)
            if not target_model and generative_models: target_model = generative_models[0]
            
            if target_model:
                payload = {"model": target_model, "messages": [{"role": "system", "content": "Output ONLY valid JSON."}, {"role": "user", "content": groq_consensus_prompt}], "temperature": 0.1}
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}, json=payload, timeout=DEFAULT_TIMEOUT)
                if res.status_code == 200:
                    raw = res.json()['choices'][0]['message']['content']
                    groq_opinions_list = extract_json(raw)
                    if isinstance(groq_opinions_list, list):
                        groq_opinions = {
                            o.get('ticker'): o 
                            for o in groq_opinions_list 
                            if isinstance(o, dict) and o.get('ticker')
                        }
                        verified = []
                        for c in candidates:
                            g = groq_opinions.get(c['ticker'])
                            if g and g.get('direction') == c['direction']:
                                c['conviction'] = min(c['conviction'], g.get('confidence', 1.0))
                                verified.append(c)
                                print(f"   ✅ {c['ticker']}: Consensus ({c['direction']})")
                            else:
                                print(f"   ⚠️ {c['ticker']}: Models disagree. Skipped.")
                        candidates = verified
                        consensus_ok = True
    except Exception as e:
        print(f"  [WARN] Consensus check failed: {e}")

CONSENSUS_REQUIRED = False
if candidates and not consensus_ok and CONSENSUS_REQUIRED:
    print("  🚫 [CONSENSUS] Groq unavailable. No trades this cycle.")
    candidates = []

# ==============================================================================
# EXECUTION HANDS (BUG #4 + #8 + #14 FIXES)
# ==============================================================================
print("\n⚔️ [HANDS] Executing with Guardrails...")
todays_predictions = []

if skip_new_entries:
    candidates = []

if not drawdown_breaker():
    candidates = []
if not in_trading_window():
    print("  ⏰ Outside trading window. Standing down.")
    candidates = []
if macro_blackout():
    candidates = []

# BUG #4 FIX: Robust held_tickers fetch
held_tickers = []
try:
    pos_res = requests.get(f"{BASE_URL}/v2/positions", headers=ALPACA_HEADERS, timeout=DEFAULT_TIMEOUT)
    if pos_res.status_code == 200:
        pos_data = pos_res.json()
        if isinstance(pos_data, list):
            held_tickers = [p.get('symbol') for p in pos_data if isinstance(p, dict) and p.get('symbol')]
        else:
            print("  ⚠️ [POSITIONS] Unexpected response format. Duplicate guard disabled.")
    else:
        print(f"  ⚠️ [POSITIONS] API returned {pos_res.status_code}. Duplicate guard disabled.")
except Exception as e:
    print(f"  ⚠️ [POSITIONS] Fetch failed: {e}. Duplicate guard disabled.")

for c in candidates:
    try:
        ticker = c.get('ticker')
        direction = str(c.get('direction', '')).upper().strip()
        if direction not in ("BULLISH", "BEARISH"):
            print(f"   ⚠️ {ticker}: Unknown direction '{c.get('direction')}'. Skip.")
            continue
        
        raw_conv = c.get('conviction', 0)
        catalyst = str(c.get('catalyst', 'UNKNOWN')).upper()
        mult = memory.get("multipliers", {}).get(catalyst, 1.0)
        adj_conv = raw_conv * mult
        if adj_conv < 0.6: continue
        
        # BUG #14: Skip duplicate validate_ticker (already done in Shield)
        
        if ticker in held_tickers:
            print(f"   ⏭️ {ticker}: Already holding. Skip pyramiding.")
            continue
        
        # BUG #14: Use cached hist if available
        if ticker in shield_cache:
            hist = shield_cache[ticker]
        else:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="3mo")
        
        if hist.empty: continue
        current_price = float(hist['Close'].iloc[-1]) 
        tr = pd.concat([hist.High - hist.Low, (hist.High - hist.Close.shift()).abs(), (hist.Low - hist.Close.shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1]) 
        
        if risk_frac == 0: continue 
        
        sl_pct = (sl_mult * atr) / current_price
        risk_budget = ACCOUNT_EQUITY * risk_frac
        stop_distance = sl_pct * current_price
        
        # BUG #8 FIX: Check minimum risk budget
        if risk_budget < 1.0:
            print(f"   ⏭️ {ticker}: Risk budget too small (${risk_budget:.2f}). Skip.")
            continue
        if stop_distance <= 0:
            print(f"   ⏭️ {ticker}: Invalid stop distance. Skip.")
            continue
        
        qty = max(1, math.floor(risk_budget / stop_distance))
        
        penalty = portfolio_beta_penalty(ticker, held_tickers)
        if penalty < 1.0:
            qty = max(1, int(qty * penalty))
            print(f"   📉 {ticker}: Correlation penalty {penalty:.2f}x → qty {qty}")
        
        qty = check_volume_participation(ticker, qty)
        if qty <= 0: continue
        
        order_value = qty * current_price
        if not stress_test_margin(order_value):
            print(f"   🚫 {ticker}: Blocked by Margin Stress Test.")
            continue

        MAX_HEAT = 0.03 * ACCOUNT_EQUITY
        current_heat = get_portfolio_heat(ACCOUNT_EQUITY)
        proposed_heat = qty * stop_distance
        if current_heat + proposed_heat > MAX_HEAT:
            print(f"   🛑 {ticker}: Portfolio heat cap reached (${current_heat+proposed_heat:,.0f} > ${MAX_HEAT:,.0f}).")
            continue

        side = "buy" if direction == "BULLISH" else "sell"
        limit_p = round(current_price * 1.001, 2) if side=="buy" else round(current_price * 0.999, 2)
        sl_p = round(current_price * (1 - sl_pct), 2) if side=="buy" else round(current_price * (1 + sl_pct), 2)
        
        order_data = {
            "symbol": ticker, "qty": str(qty), "side": side, 
            "type": "limit", "limit_price": f"{limit_p:.2f}", 
            "time_in_force": "day"
        }
        
        if tp_mult and tp_mult > 0:
            order_data["order_class"] = "bracket"
            tp_pct = (tp_mult * atr) / current_price
            tp_p = round(current_price * (1 + tp_pct), 2) if side=="buy" else round(current_price * (1 - tp_pct), 2)
            order_data["take_profit"] = {"limit_price": f"{tp_p:.2f}"}
            order_data["stop_loss"] = {"stop_price": f"{sl_p:.2f}"}
        elif sl_pct > 0:
            order_data["order_class"] = "oto"
            order_data["stop_loss"] = {"stop_price": f"{sl_p:.2f}"}
        else:
            order_data["order_class"] = "simple"
            
        allowed, reason = validate_swing_order("NEW", 0, qty, 0, sl_p)
        if not allowed: 
            print(f"   🚫 {ticker}: Guardrail Blocked ({reason})")
            continue

        res = requests.post(f"{BASE_URL}/v2/orders", headers=ALPACA_HEADERS, json=order_data, timeout=DEFAULT_TIMEOUT)
        if res.status_code in [200, 201]:
            order_id = res.json().get('id')
            if not order_id:
                print(f"  ❌ {ticker}: No order_id returned")
                continue
            print(f"  ✅ {ticker} ({direction}) QUEUED | Qty: {qty}")
            todays_predictions.append({
                "order_id": order_id, "ticker": ticker, "catalyst": catalyst, 
                "direction": direction, "entry": limit_p, "atr": atr, 
                "max_hold_days": time_stop, "status": "PENDING",
                "date": datetime.now(timezone.utc).isoformat(),
                "reasoning": c.get('catalyst', '') + " | conv=" + str(raw_conv) + " mult=" + str(mult),
                "state_snapshot": {k: round(float(v),3) if isinstance(v,(int,float)) else v for k,v in state.items()},
                "strategy": groq_strategy
            })
            held_tickers.append(ticker)
        else:
            print(f"  ❌ {ticker} REJECTED: {res.text[:50]}")
    except Exception as e:
        print(f"  [ERROR] Execution failed for {ticker}: {e}")
        continue

# Activate learning system (BEFORE garbage_collect_memory)
learn_multipliers()

# BUG #5 FIX: Now garbage collect AFTER learning (includes stale PENDING cleanup)
garbage_collect_memory()

# ==============================================================================
# ORGAN 8: HIPPOCAMPUS WRITE (BUG #2: clean save)
# ==============================================================================
if GITHUB_TOKEN and REPO and (todays_predictions or memory.get("multipliers") or memory.get("strategy_stats") or memory.get("strategy_risk_overrides")):
    print("\n🧠 [ORGAN 8: HIPPOCAMPUS] Committing Memory...")
    memory["predictions"].extend(todays_predictions)
    memory["updated_at"] = datetime.now(timezone.utc).isoformat()
    memory["version"] = memory.get("version", 0) + 1
    
    # BUG #2 FIX: Pass memory dict directly, save_memory handles cleaning
    save_memory(memory)

if NTFY_TOPIC:
    try: 
        stats = memory.get("strategy_stats", {})
        strat_summary = "\n".join([f"  {s}: {v['wr']:.0%} ({v['total']})" for s, v in list(stats.items())[:3]])
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", 
            data=f"APEX-K V14.0 Run\nStrategy: {groq_strategy or 'FALLBACK'}\nTrades: {len(todays_predictions)}\nEquity: ${ACCOUNT_EQUITY:,.0f}\nWin Rate: {memory.get('win_rate', 0):.1%}\n{strat_summary}".encode('utf-8'), 
            headers={"Title": "APEX-K Run"}, timeout=DEFAULT_TIMEOUT)
    except: pass

print("\n" + "="*70)
print("✅ APEX-K V14.0 CYCLE COMPLETE. ALL 16 BUGS FIXED. 5 NEW STRATEGIES ACTIVE.")
print("="*70)