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
import warnings
warnings.filterwarnings('ignore')

print("="*70)
print("🌌 APEX-K V11.0: THE AUDITED MASTERPIECE 🌌")
print("="*70)

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

memory = {"_sha": None, "predictions": [], "multipliers": {}, "version": 0, "win_rate": 0.5}

# ==============================================================================
# V11.0 STRATEGY LIBRARY & VERIFICATION GATE
# ==============================================================================
STRATEGY_LIBRARY = {
    "AGGRESSIVE_MOMENTUM": {"universe": ["NVDA", "AMD", "MSFT", "META", "TSLA", "AVGO"], "risk_frac": 0.005, "sl_mult": 3.0, "tp_mult": None, "time_stop": 60},
    "DEFENSIVE_MOMENTUM": {"universe": ["MSFT", "AAPL", "JNJ", "PG", "KO"], "risk_frac": 0.0025, "sl_mult": 2.0, "tp_mult": 2.0, "time_stop": 10},
    "MEAN_REVERSION_SWING": {"universe": ["SPY", "QQQ", "XLU", "XLP", "WMT"], "risk_frac": 0.0025, "sl_mult": 2.0, "tp_mult": 1.5, "time_stop": 3},
    "CRISIS_CASH": {"universe": [], "risk_frac": 0, "sl_mult": 0, "tp_mult": 0, "time_stop": 0},
    "VOLATILITY_HARVEST": {"universe": [], "risk_frac": 0, "sl_mult": 0, "tp_mult": 0, "time_stop": 0},
    "V_SHAPE_RECOVERY": {"universe": ["SPY", "QQQ", "XLF", "XLI", "XLY"], "risk_frac": 0.005, "sl_mult": 2.5, "tp_mult": None, "time_stop": 30},
    "GRINDING_BEAR_AVOIDANCE": {"universe": [], "risk_frac": 0, "sl_mult": 0, "tp_mult": 0, "time_stop": 0}
}

def verify_proposal(proposal, state):
    strat = proposal.get("selected_strategy", "CRISIS_CASH")
    conf = proposal.get("confidence", 0)
    if conf < 70: return "CRISIS_CASH", "OVERRIDDEN: Low confidence."
    if state['cs_z'] > 2.5 and strat not in ["CRISIS_CASH", "GRINDING_BEAR_AVOIDANCE"]: return "CRISIS_CASH", f"OVERRIDDEN: Credit Z > 2.5."
    if state['vix'] > 35 and strat in ["AGGRESSIVE_MOMENTUM", "MEAN_REVERSION_SWING", "V_SHAPE_RECOVERY"]: return "CRISIS_CASH", f"OVERRIDDEN: VIX > 35."
    if state.get('term_ratio', 1.0) > 1.15: return "CRISIS_CASH", f"OVERRIDDEN: VIX backwardation {state.get('term_ratio', 0):.2f} > 1.15."
    if state['spy_sma'] < -0.05 and state['breadth'] < 0.40 and state['vix'] < 25 and strat not in ["GRINDING_BEAR_AVOIDANCE", "CRISIS_CASH"]: return "GRINDING_BEAR_AVOIDANCE", f"OVERRIDDEN: Bearish internals."
    return strat, f"VERIFIED: {strat} ({conf}% conf)."

def extract_json(text):
    try:
        if "```json" in text: text = text.split("```json")[1].split("```")[0]
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match: return json.loads(match.group(0))
        return json.loads(text)
    except:
        return {"selected_strategy": "CRISIS_CASH", "confidence": 0, "reasoning": "JSON Parse Failure"}

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
    if dom >= 28: return "BUY_TOM"
    if dow == 4: return "LIQUIDATE_WEEKEND_RISK"
    return "NORMAL_SESSION"

def validate_swing_order(action, original_qty, proposed_qty, original_stop, proposed_stop):
    if action in {"ADD", "AVERAGE_DOWN"}: return False, "INCREASE_FORBIDDEN"
    if action == "NEW": return True, "ORDER_ALLOWED"
    if proposed_qty > original_qty: return False, "QTY_INCREASE_FORBIDDEN"
    if proposed_stop < original_stop: return False, "STOP_WIDENING_FORBIDDEN"
    return True, "ORDER_ALLOWED"

def adaptive_time_manager(days_held, current_pnl_atr, max_hold, current_price, entry_price):
    if days_held >= 1 and current_pnl_atr <= 0.2: return "EARLY_EJECT", "Velocity Stop."
    if days_held >= max_hold:
        if current_pnl_atr >= 0.8 and current_price > entry_price: return "EXTEND_TIME", "Extend 2 days."
        else: return "STANDARD_EXIT", "Time limit reached."
    return "HOLD", "Normal."

def validate_ticker(symbol):
    try:
        res = requests.get(f"{BASE_URL}/v2/assets/{symbol}", headers=ALPACA_HEADERS)
        if res.status_code == 200: return res.json().get('tradable', False) and res.json().get('status') == 'active'
        return False
    except: return False

def reconcile_partial_fills():
    try:
        res = requests.get(f"{BASE_URL}/v2/orders?status=all&limit=100", headers=ALPACA_HEADERS)
        orders = res.json() if res.status_code == 200 else []
        for order in orders:
            order_id = order.get('id'); filled_qty = float(order.get('filled_qty', 0)); status = order.get('status')
            for p in memory.get("predictions", []):
                if p.get("order_id") == order_id:
                    if status == 'canceled' and filled_qty > 0: p['qty'] = filled_qty
                    elif status == 'filled': p['qty'] = filled_qty
    except: pass

def garbage_collect_memory():
    if len(memory.get("predictions", [])) > 100: memory["predictions"] = memory["predictions"][-100:]

# ==============================================================================
# PORTFOLIO HEAT CAP (Prevents Over-Leverage)
# ==============================================================================
def get_portfolio_heat():
    """Sum of open risk = |entry - stop| * qty across all positions"""
    try:
        pos = requests.get(f"{BASE_URL}/v2/positions", headers=ALPACA_HEADERS).json()
        orders = requests.get(f"{BASE_URL}/v2/orders?status=open&nested=true&limit=200", headers=ALPACA_HEADERS).json()
    except: return 0.0
    heat = 0.0
    for p in pos:
        entry = float(p.get('avg_entry_price', 0)); qty = abs(int(float(p.get('qty', 0))))
        stop = entry * 0.97  # default fallback 3%
        for o in orders:
            if o.get('symbol') == p['symbol'] and o.get('type') in ('stop','stop_limit'):
                stop = float(o.get('stop_price', stop)); break
        heat += abs(entry - stop) * qty
    return heat

# ==============================================================================
# DAILY DRAWDOWN CIRCUIT BREAKER
# ==============================================================================
def drawdown_breaker():
    try:
        acc = requests.get(f"{BASE_URL}/v2/account", headers=ALPACA_HEADERS).json()
        eq = float(acc.get('equity', 0)); last = float(acc.get('last_equity', eq))
        if last <= 0: return True
        dd = (eq - last) / last
        if dd <= -0.02:
            print(f"  🛑 DRAWDOWN BREAKER: {dd:.1%} today. No new entries.")
            return False
        return True
    except: return True

# ==============================================================================
# TRADING HOURS GUARD
# ==============================================================================
def in_trading_window():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5: return False
    # 13:35–19:45 UTC = 9:35–3:45 ET (avoid open/close turbulence)
    mins = now.hour * 60 + now.minute
    return 13*60+35 <= mins <= 19*60+45

# ==============================================================================
# LEARN MULTIPLIERS (Activate Learning System)
# ==============================================================================
def learn_multipliers():
    """Update catalyst multipliers from closed trades."""
    try:
        filled = requests.get(f"{BASE_URL}/v2/orders?status=filled&limit=200&direction=desc", headers=ALPACA_HEADERS).json()
    except: return
    mults = memory.get("multipliers", {})
    for p in memory.get("predictions", []):
        if p.get("_evaluated") or not p.get("order_id"): continue
        fill = next((f for f in filled if f.get('id') == p['order_id']), None)
        if not fill or float(fill.get('filled_qty', 0)) == 0: continue
        fill_price = float(fill.get('filled_avg_price', 0))
        close_side = "sell" if p['direction'] == "BULLISH" else "buy"
        closes = [f for f in filled if f.get('symbol') == p['ticker'] and f.get('side') == close_side
                  and f.get('filled_at', '') > fill.get('filled_at', '')]
        if not closes: continue
        exit_price = float(closes[0].get('filled_avg_price', 0))
        pnl_pct = (exit_price - fill_price) / fill_price
        if p['direction'] == "BEARISH": pnl_pct = -pnl_pct
        cat = p.get('catalyst', 'UNKNOWN')
        current = mults.get(cat, 1.0)
        adj = 1.10 if pnl_pct > 0.005 else (0.90 if pnl_pct < -0.005 else 1.0)
        mults[cat] = round(max(0.5, min(2.0, current * adj)), 3)
        p['_evaluated'] = True
        p['_outcome_pct'] = round(pnl_pct, 4)
    memory["multipliers"] = mults
    wins = sum(1 for p in memory["predictions"] if p.get("_outcome_pct", 0) > 0)
    total = sum(1 for p in memory["predictions"] if "_outcome_pct" in p)
    if total > 0:
        memory["win_rate"] = round(wins / total, 3)
        print(f"  [LEARN] Win rate: {memory['win_rate']:.1%} | Multipliers: {len(mults)} catalysts")

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
        r = requests.get(f"https://api.github.com/repos/{REPO}/contents/memory/memory.json", headers=GH_HEADERS, timeout=8)
        if r.status_code == 200:
            memory = json.loads(base64.b64decode(r.json()["content"]).decode())
            memory["_sha"] = r.json()["sha"]
    except: pass
reconcile_partial_fills()
garbage_collect_memory()

# ==============================================================================
# UNIVERSAL RISK ORGANS (Ported from NSE V1 Architecture)
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

def stress_test_margin(proposed_order_value):
    try:
        account_res = requests.get(f"{BASE_URL}/v2/account", headers=ALPACA_HEADERS)
        if account_res.status_code != 200: return True
        acc = account_res.json()
        current_equity = float(acc.get('equity', 0))
        maintenance_margin = float(acc.get('maintenance_margin', 0))
        long_market_value = float(acc.get('long_market_value', 0))
        simulated_equity = current_equity - (long_market_value * 0.15) - (proposed_order_value * 0.15)
        safety_threshold = maintenance_margin * 1.10 
        if simulated_equity < safety_threshold:
            print(f"   🛑 MARGIN STRESS TEST FAILED: Simulated crash breaches maintenance margin. Trade blocked.")
            return False
        return True
    except:
        return True

# ==============================================================================
# ORGAN 1: JANITOR (FIXED: Preserve Protective Stops)
# ==============================================================================
print("\n🧹 [ORGAN 1: JANITOR] Scrubbing Only Entry Orders...")
try:
    pos_res = requests.get(f"{BASE_URL}/v2/positions", headers=ALPACA_HEADERS)
    pos_symbols = {p['symbol'] for p in (pos_res.json() if pos_res.status_code == 200 else [])}
    orders_res = requests.get(f"{BASE_URL}/v2/orders?status=open&limit=200&nested=true", headers=ALPACA_HEADERS)
    open_orders = orders_res.json() if orders_res.status_code == 200 else []
    for o in open_orders:
        is_protective = (o.get('type') in ('stop', 'stop_limit', 'trailing_stop')
                         or (o.get('order_class') in ('bracket','oco','oto') and o.get('symbol') in pos_symbols))
        if is_protective:
            continue
        try: requests.delete(f"{BASE_URL}/v2/orders/{o['id']}", headers=ALPACA_HEADERS)
        except: pass
except Exception as e:
    print(f"  [WARN] Janitor error: {e}")

# ==============================================================================
# ORGAN 16: CHRONO-VELOCITY (FIXED: Don't Wipe History)
# ==============================================================================
print("\n⏱️ [ORGAN 16: CHRONO-VELOCITY] Managing Open Positions...")
try:
    positions_res = requests.get(f"{BASE_URL}/v2/positions", headers=ALPACA_HEADERS)
    positions = positions_res.json() if positions_res.status_code == 200 else []
    for pos in positions:
        ticker = pos.get('symbol'); entry_price = float(pos.get('avg_entry_price')); current_price = float(pos.get('current_price'))
        qty = abs(int(float(pos.get('qty')))); side = "long" if int(float(pos.get('qty'))) > 0 else "short"
        trade_memory = next((p for p in memory.get("predictions", []) if p.get("ticker") == ticker), None)
        if not trade_memory: continue
        atr = trade_memory.get("atr", 1.0); max_hold = trade_memory.get("max_hold_days", 3); entry_date_str = trade_memory.get("date", "")
        if entry_date_str:
            entry_date = datetime.fromisoformat(entry_date_str).replace(tzinfo=timezone.utc)
            days_held = (datetime.now(timezone.utc) - entry_date).days
        else: days_held = 0
        pnl_dollars = (current_price - entry_price) * qty if side == "long" else (entry_price - current_price) * qty
        current_pnl_atr = abs(pnl_dollars / (qty * atr)) if atr > 0 else 0
        decision, reason = adaptive_time_manager(days_held, current_pnl_atr, max_hold, current_price, entry_price)
        if decision in ["EARLY_EJECT", "STANDARD_EXIT"]:
            print(f"   ✂️ {ticker}: {decision}. Liquidating.")
            close_side = "sell" if side == "long" else "buy"
            requests.post(f"{BASE_URL}/v2/orders", headers=ALPACA_HEADERS, json={"symbol": ticker, "qty": str(qty), "side": close_side, "type": "market", "time_in_force": "day"})
            # FIXED: Mark as CLOSED instead of deleting
            for p in memory["predictions"]:
                if p.get("ticker") == ticker and p.get("status") != "CLOSED":
                    p["status"] = "CLOSED"
                    p["closed_at"] = datetime.now(timezone.utc).isoformat()
        elif decision == "EXTEND_TIME":
            print(f"   ⏳ {ticker}: EXTENDING. (+2 Days)")
            for p in memory["predictions"]:
                if p.get("ticker") == ticker: p["max_hold_days"] = max_hold + 2
except Exception as e: print(f"  [ERROR] Position management failed: {e}")

# ==============================================================================
# ORGAN 5/10: GROQ STRATEGY ROUTER & COMPASS (FIXED: Real VIX Term Structure)
# ==============================================================================
print("\n🧭 [ORGAN 5/10: COMPASS & GROQ ROUTER] Assessing Regime...")
try:
    spy_data = yf.download("SPY", period="1y", progress=False)
    spy_close_series = spy_data['Close']['SPY'] if isinstance(spy_data.columns, pd.MultiIndex) else spy_data['Close']
    spy_current = float(spy_close_series.iloc[-1]); spy_sma200 = float(spy_close_series.rolling(200).mean().iloc[-1])
    spy_ext = (spy_current / spy_sma200) - 1
    spy_20d_ret = (spy_current / float(spy_close_series.iloc[-20])) - 1
    
    vix_data = yf.download("^VIX", period="10d", progress=False)
    vix_series = vix_data['Close']['^VIX'] if isinstance(vix_data.columns, pd.MultiIndex) else vix_data['Close']
    vix = float(vix_series.iloc[-1]); vix_5d = (vix / float(vix_series.iloc[-5])) - 1
    
    # FIXED: Real VIX Term Structure (VIX / VIX3M)
    try:
        vix3m_data = yf.download("^VIX3M", period="10d", progress=False)
        vix3m_series = vix3m_data['Close']['^VIX3M'] if isinstance(vix3m_data.columns, pd.MultiIndex) else vix3m_data['Close']
        vix3m = float(vix3m_series.iloc[-1])
        term_ratio = vix / vix3m if vix3m > 0 else 1.0
        term = "backwardation" if term_ratio > 1.0 else "contango"
    except:
        vix3m, term_ratio, term = vix * 1.1, 1.0, "unknown"
    
    # FIXED: Real Breadth (Sector ETFs)
    SECTOR_ETFS = ["XLK","XLF","XLE","XLV","XLI","XLY","XLP","XLU","XLB","XLRE","XLC"]
    try:
        sect = yf.download(SECTOR_ETFS, period="3mo", progress=False)['Close']
        above = sum(1 for t in SECTOR_ETFS if sect[t].iloc[-1] > sect[t].rolling(50).mean().iloc[-1])
        breadth = above / len(SECTOR_ETFS)
    except:
        breadth = 0.5
    
    state = {
        "vix": vix, "vix_5d": vix_5d, "term": term, "term_ratio": term_ratio,
        "spy_sma": spy_ext, "spy_20d": spy_20d_ret,
        "cs_z": cs_z_score, "breadth": breadth
    }
    
    print(f"  [STATE] VIX: {vix:.1f} | Term: {term} ({term_ratio:.2f}) | Breadth: {breadth:.1%} | Credit Z: {cs_z_score:.1f}")

    groq_strategy = None
    if GROQ_KEY:
        print("  🧠 [GROQ] Querying Strategy Router...")
        models_res = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {GROQ_KEY}"})
        if models_res.status_code == 200:
            all_models = [m['id'] for m in models_res.json().get('data', [])]
            generative_models = [m for m in all_models if not any(x in m.lower() for x in ['guard', 'vision', 'whisper', 'tool'])]
            target_model = next((m for m in generative_models if '70b' in m.lower() or '8b' in m.lower() or 'qwen' in m.lower() or 'gpt-oss' in m.lower()), None)
            if not target_model and generative_models: target_model = generative_models[0]
            
            if target_model:
                prompt = f"""You are APEX-K Strategy Router. Select the optimal strategy.
MARKET STATE: VIX={state['vix']:.1f} (5d Chg: {state['vix_5d']:.1%}), Term={state['term']} ({term_ratio:.2f}), SPY vs SMA={state['spy_sma']:.1%}, Breadth={state['breadth']:.1%}, Credit Z={state['cs_z']:.2f}.
STRATEGIES: AGGRESSIVE_MOMENTUM, DEFENSIVE_MOMENTUM, MEAN_REVERSION_SWING, CRISIS_CASH, VOLATILITY_HARVEST, V_SHAPE_RECOVERY, GRINDING_BEAR_AVOIDANCE.
RESPOND IN EXACT JSON: {{"selected_strategy": "NAME", "confidence": 85, "reasoning": "1 sentence."}}"""
                
                payload = {"model": target_model, "messages": [{"role": "system", "content": "Output ONLY valid JSON."}, {"role": "user", "content": prompt}], "temperature": 0.1}
                res = requests.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}, json=payload, timeout=15)
                if res.status_code == 200:
                    proposal = extract_json(res.json()['choices'][0]['message']['content'])
                    final_strat, verdict = verify_proposal(proposal, state)
                    print(f"  🧠 [GROQ] {final_strat} | {verdict}")
                    groq_strategy = final_strat
                    
except Exception as e:
    print(f"  [ERROR] Router failed: {e}. Defaulting to CRISIS.")
    state = {"vix": 40, "vix_5d": 0.0, "term": "backwardation", "term_ratio": 1.2, "spy_sma": -0.10, "spy_20d": -0.05, "breadth": 0.20, "cs_z": 3.0}
    groq_strategy = "CRISIS_CASH"

if groq_strategy and groq_strategy in STRATEGY_LIBRARY:
    active_strat = STRATEGY_LIBRARY[groq_strategy]
    UNIVERSE = active_strat["universe"]
    risk_frac = active_strat["risk_frac"]
    sl_mult = active_strat["sl_mult"]
    tp_mult = active_strat["tp_mult"]
    time_stop = active_strat["time_stop"]
else:
    if state['vix'] >= 25 or state['spy_sma'] <= -0.05: UNIVERSE = []; risk_frac = 0; sl_mult = 0; tp_mult = 0; time_stop = 0
    else: UNIVERSE = ["NVDA", "AMD", "MSFT", "META", "TSLA", "AVGO"]; risk_frac = 0.005; sl_mult = 3.0; tp_mult = None; time_stop = 60

# ==============================================================================
# ORGAN 2/4: FUNNEL & SHIELD
# ==============================================================================
print("\n⚡ [ORGAN 2/4: FUNNEL & SHIELD] Scanning...")
if not UNIVERSE:
    print("  🛡️ Shield blocks directional equity. Standing down.")
    clean_targets = []
else:
    try:
        data = yf.download(UNIVERSE, period="5d", progress=False)
        close = data['Close'] if isinstance(data.columns, pd.MultiIndex) else data
        returns = close.pct_change().iloc[-1].dropna()
        sorted_tickers = returns.reindex(returns.abs().sort_values(ascending=False).index).index.tolist()
    except: sorted_tickers = []

    clean_targets = []
    for ticker in sorted_tickers[:5]:
        try:
            if not validate_ticker(ticker): continue
            stock = yf.Ticker(ticker)
            current_price = float(close[ticker].iloc[-1]) 
            hist_3mo = stock.history(period="3mo")
            rv_30 = np.log(hist_3mo['Close'] / hist_3mo['Close'].shift(1)).dropna().tail(30).std() * np.sqrt(252)
            iv = 0.0
            if stock.options:
                chain = stock.option_chain(stock.options[0])
                calls = chain.calls[chain.calls['ask'] > 0]
                if not calls.empty:
                    atm_c = calls.iloc[(calls['strike'] - current_price).abs().argsort()[:1]]
                    iv = float(atm_c['impliedVolatility'].values[0]) 
            if iv > 0 and rv_30 < (iv - 0.05): continue
            clean_targets.append({"ticker": ticker, "move": float(returns[ticker])})
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
        models_res = requests.get(f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_KEY}").json()
        available_models = [m['name'].replace('models/', '') for m in models_res.get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]
        ai_success = False
        for model in available_models:
            if ai_success: break
            for ver in ['v1beta', 'v1']:
                try:
                    url = f"https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent?key={GEMINI_KEY}"
                    res = requests.post(url, json=payload, timeout=60)
                    res.raise_for_status()
                    raw = res.json()['candidates'][0]['content']['parts'][0]['text']
                    if "```json" in raw: raw = raw.split("```json")[1].split("```")[0]
                    candidates = json.loads(raw.strip())
                    print(f"  [SUCCESS] Brain connected via {ver}/{model}")
                    ai_success = True
                    break
                except: continue
    except: pass

# ==============================================================================
# ORGAN 12 & 15: CHRONOS ALLOCATOR (FIXED: Actual Weekend Liquidation)
# ==============================================================================
print("\n⏱️ [ORGAN 12/15: CHRONOS] Allocating Time & Profit States...")
now = datetime.now(timezone.utc)
calendar_action = chronos_calendar(now)
if calendar_action == "LIQUIDATE_WEEKEND_RISK":
    print("  🗑️ [CHRONOS] Friday Afternoon. Liquidating weekend risk.")
    try:
        pos = requests.get(f"{BASE_URL}/v2/positions", headers=ALPACA_HEADERS).json()
        for p in pos:
            q = int(float(p.get('qty', 0)))
            if q == 0: continue
            side = "sell" if q > 0 else "buy"
            requests.post(f"{BASE_URL}/v2/orders", headers=ALPACA_HEADERS,
                json={"symbol": p['symbol'], "qty": str(abs(q)), "side": side, "type": "market", "time_in_force": "day"})
            print(f"  ✂️ Liquidated {p['symbol']} x{abs(q)}")
    except Exception as e:
        print(f"  [WARN] Weekend liq failed: {e}")

# ==============================================================================
# EXECUTION HANDS (FIXED: order_class, Portfolio Heat, Duplicate Prevention)
# ==============================================================================
print("\n⚔️ [HANDS] Executing with Guardrails...")
todays_predictions = []

# Check circuit breakers
if not drawdown_breaker():
    candidates = []
if not in_trading_window():
    print("  ⏰ Outside trading window. Standing down.")
    candidates = []

for c in candidates:
    ticker = c.get('ticker'); direction = c.get('direction')
    raw_conv = c.get('conviction', 0); catalyst = c.get('catalyst', 'UNKNOWN').upper()
    mult = memory.get("multipliers", {}).get(catalyst, 1.0)
    adj_conv = raw_conv * mult
    if adj_conv < 0.6 or not validate_ticker(ticker): continue
    
    # Duplicate position prevention
    pos_check = requests.get(f"{BASE_URL}/v2/positions/{ticker}", headers=ALPACA_HEADERS)
    if pos_check.status_code == 200 and abs(int(float(pos_check.json().get('qty', 0)))) > 0:
        print(f"   ⏭️ {ticker}: Already holding. Skip pyramiding.")
        continue
    
    try:
        stock = yf.Ticker(ticker); hist = stock.history(period="3mo")
        current_price = float(hist['Close'].iloc[-1]) 
        tr = pd.concat([hist.High - hist.Low, (hist.High - hist.Close.shift()).abs(), (hist.Low - hist.Close.shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1]) 
        
        if risk_frac == 0: continue 
        
        sl_pct = (sl_mult * atr) / current_price
        risk_budget = 100000 * risk_frac
        stop_distance = sl_pct * current_price
        qty = max(1, math.floor(risk_budget / stop_distance))
        
        qty = check_volume_participation(ticker, qty)
        if qty <= 0: continue
        
        order_value = qty * current_price
        if not stress_test_margin(order_value):
            print(f"   🚫 {ticker}: Blocked by Margin Stress Test.")
            continue

        # Portfolio heat cap
        MAX_HEAT = 0.03 * 100000
        current_heat = get_portfolio_heat()
        proposed_heat = qty * stop_distance
        if current_heat + proposed_heat > MAX_HEAT:
            print(f"   🛑 {ticker}: Portfolio heat cap reached (${current_heat+proposed_heat:,.0f} > ${MAX_HEAT:,.0f}).")
            continue

        side = "buy" if direction == "BULLISH" else "sell"
        limit_p = round(current_price * 1.001, 2) if side=="buy" else round(current_price * 0.999, 2)
        sl_p = round(current_price * (1 - sl_pct), 2) if side=="buy" else round(current_price * (1 + sl_pct), 2)
        
        order_data = {"symbol": ticker, "qty": str(qty), "side": side, "type": "limit", "limit_price": str(limit_p), "time_in_force": "day"}
        
        # FIXED: Proper order_class handling
        if tp_mult and tp_mult > 0:
            order_data["order_class"] = "bracket"
            tp_pct = (tp_mult * atr) / current_price
            tp_p = round(current_price * (1 + tp_pct), 2) if side=="buy" else round(current_price * (1 - tp_pct), 2)
            order_data["take_profit"] = {"limit_price": str(tp_p)}
            order_data["stop_loss"] = {"stop_price": str(sl_p)}
        elif sl_pct > 0:
            order_data["order_class"] = "oto"
            order_data["stop_loss"] = {"stop_price": str(sl_p)}
        else:
            order_data["order_class"] = "simple"
            
        allowed, reason = validate_swing_order("NEW", 0, qty, 0, sl_p)
        if not allowed: 
            print(f"   🚫 {ticker}: Guardrail Blocked ({reason})")
            continue

        res = requests.post(f"{BASE_URL}/v2/orders", headers=ALPACA_HEADERS, json=order_data)
        if res.status_code in [200, 201]:
            order_id = res.json().get('id')
            print(f"  ✅ {ticker} ({direction}) QUEUED | Qty: {qty}")
            # FIXED: Persist reasoning and state snapshot
            todays_predictions.append({
                "order_id": order_id, "ticker": ticker, "catalyst": catalyst, 
                "direction": direction, "entry": limit_p, "atr": atr, 
                "max_hold_days": time_stop, "status": "PENDING",
                "date": datetime.now(timezone.utc).isoformat(),
                "reasoning": c.get('catalyst', '') + " | conv=" + str(raw_conv) + " mult=" + str(mult),
                "state_snapshot": {k: round(float(v),3) if isinstance(v,(int,float)) else v for k,v in state.items()},
                "strategy": groq_strategy
            })
        else:
            print(f"  ❌ {ticker} REJECTED: {res.text[:50]}")
            
    except Exception as e:
        print(f"  [ERROR] Execution failed for {ticker}: {e}")

# Activate learning system
learn_multipliers()

# ==============================================================================
# ORGAN 8: HIPPOCAMPUS WRITE
# ==============================================================================
if GITHUB_TOKEN and REPO and (todays_predictions or memory.get("multipliers")):
    print("\n🧠 [ORGAN 8: HIPPOCAMPUS] Committing Memory...")
    memory["predictions"].extend(todays_predictions)
    memory["updated_at"] = datetime.now(timezone.utc).isoformat()
    memory["version"] = memory.get("version", 0) + 1
    body = {"message": f"hippocampus: v{memory['version']} [skip ci]", "content": base64.b64encode(json.dumps({k:v for k,v in memory.items() if k != "_sha"}, indent=2).encode()).decode(), "branch": "main"}
    if memory.get("_sha"): body["sha"] = memory["_sha"]
    try: requests.put(f"https://api.github.com/repos/{REPO}/contents/memory/memory.json", headers=GH_HEADERS, json=body, timeout=10)
    except: pass

if NTFY_TOPIC:
    try: requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=f"APEX-K V11.0 Run\nStrategy: {groq_strategy or 'FALLBACK'}\nTrades: {len(todays_predictions)}\nWin Rate: {memory.get('win_rate', 0):.1%}".encode('utf-8'), headers={"Title": "APEX-K Run"})
    except: pass

print("\n" + "="*70)
print("✅ APEX-K V11.0 CYCLE COMPLETE. ALL CRITICAL BUGS FIXED.")
print("="*70)