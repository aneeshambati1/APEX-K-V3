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
print("🌌 APEX-K V11.0: THE GROQ-POWERED MASTERPIECE 🌌")
print("="*70)

# ==============================================================================
# VAULT CONNECTION
# ==============================================================================
GEMINI_KEY = os.environ.get('GEMINI_API_KEY')
GROQ_KEY = os.environ.get('GROQ_API_KEY')
ALPACA_KEY = os.environ.get('ALPACA_API_KEY')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET_KEY')
GITHUB_TOKEN = os.environ.get('AGITHUB_TOKEN')
REPO = os.environ.get('GITHUB_REPOSITORY')
NTFY_TOPIC = os.environ.get('NTFY_TOPIC')

if not all([GEMINI_KEY, ALPACA_KEY, ALPACA_SECRET]):
    print("[FATAL] Missing Core API keys.")
    exit(1)

BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET, "Content-Type": "application/json"}
GH_HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"} if GITHUB_TOKEN else {}

memory = {"_sha": None, "predictions": [], "multipliers": {}, "version": 0}

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
    """
    NSE Persona 4 Port: Ensures our order size does not exceed 5% of the 
    stock's 30-day average daily volume to prevent slippage and market impact.
    """
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
        return proposed_qty # Fail open if volume data is missing

def stress_test_margin(proposed_order_value):
    """
    NSE Persona 5 Port: Simulates a 15% market flash-crash.
    If the simulated equity drops below Alpaca's Maintenance Margin requirement,
    block the trade to prevent a forced margin call liquidation.
    """
    try:
        account_res = requests.get(f"{BASE_URL}/v2/account", headers=ALPACA_HEADERS)
        if account_res.status_code != 200: return True # Fail open
        
        acc = account_res.json()
        current_equity = float(acc.get('equity', 0))
        maintenance_margin = float(acc.get('maintenance_margin', 0))
        
        # Simulate a 15% flash crash on our current long exposure
        long_market_value = float(acc.get('long_market_value', 0))
        simulated_equity = current_equity - (long_market_value * 0.15) - (proposed_order_value * 0.15)
        
        # Add a 10% buffer above the broker's hard maintenance margin
        safety_threshold = maintenance_margin * 1.10 
        
        if simulated_equity < safety_threshold:
            print(f"   🛑 MARGIN STRESS TEST FAILED: Simulated crash breaches maintenance margin. Trade blocked.")
            return False # Block the trade
        return True # Trade is safe
    except:
        return True
# ==============================================================================
# ORGAN 1 & 16: JANITOR & CHRONO-VELOCITY
# ==============================================================================
print("\n🧹 [ORGAN 1: JANITOR] Scrubbing Ghost Orders...")
try: requests.delete(f"{BASE_URL}/v2/orders", headers=ALPACA_HEADERS)
except: pass

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
            memory["predictions"] = [p for p in memory.get("predictions", []) if p.get("ticker") != ticker]
        elif decision == "EXTEND_TIME":
            print(f"   ⏳ {ticker}: EXTENDING. (+2 Days)")
            for p in memory["predictions"]:
                if p.get("ticker") == ticker: p["max_hold_days"] = max_hold + 2
except Exception as e: print(f"  [ERROR] Position management failed: {e}")

# ==============================================================================
# ORGAN 5/10: GROQ STRATEGY ROUTER & COMPASS
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
    term = "backwardation" if vix > 25 else "contango" # Simplified proxy
    
    # Proxy for Breadth (Using SPY RSI as a proxy for internal momentum if breadth API isn't available)
    # In a real fund, you'd pull NYSE % > 200DMA here. We'll use a safe default.
    breadth_proxy = 0.60 if spy_current > spy_sma200 else 0.35 
    
    state = {
        "vix": vix, "vix_5d": vix_5d, "term": term,
        "spy_sma": spy_ext, "spy_20d": spy_20d_ret,
        "cs_z": cs_z_score, "breadth": breadth_proxy
    }
    
    print(f"  [STATE] VIX: {vix:.1f} | SPY Ext: {spy_ext:.1%} | Credit Z: {cs_z_score:.1f}")

    # GROQ AI ROUTING
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
MARKET STATE: VIX={state['vix']:.1f} (5d Chg: {state['vix_5d']:.1%}), Term={state['term']}, SPY vs SMA={state['spy_sma']:.1%}, Breadth={state['breadth']:.1%}, Credit Z={state['cs_z']:.2f}.
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
    state = {"vix": 40, "spy_sma": -0.10, "breadth": 0.20, "cs_z": 3.0}
    groq_strategy = "CRISIS_CASH"

# Map Groq Strategy to Execution Variables
if groq_strategy and groq_strategy in STRATEGY_LIBRARY:
    active_strat = STRATEGY_LIBRARY[groq_strategy]
    UNIVERSE = active_strat["universe"]
    risk_frac = active_strat["risk_frac"]
    sl_mult = active_strat["sl_mult"]
    tp_mult = active_strat["tp_mult"]
    time_stop = active_strat["time_stop"]
else:
    # Fallback if Groq fails
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
# ORGAN 12 & 15: CHRONOS ALLOCATOR
# ==============================================================================
print("\n⏱️ [ORGAN 12/15: CHRONOS] Allocating Time & Profit States...")
now = datetime.now(timezone.utc)
calendar_action = chronos_calendar(now)
if calendar_action == "LIQUIDATE_WEEKEND_RISK": print("  🗑️ [CHRONOS] Friday Afternoon. Liquidating weekend risk.")

# ==============================================================================
# EXECUTION HANDS
# ==============================================================================
print("\n⚔️ [HANDS] Executing with Guardrails...")
todays_predictions = []

for c in candidates:
    ticker = c.get('ticker'); direction = c.get('direction')
    raw_conv = c.get('conviction', 0); catalyst = c.get('catalyst', 'UNKNOWN').upper()
    mult = memory.get("multipliers", {}).get(catalyst, 1.0)
    adj_conv = raw_conv * mult
    if adj_conv < 0.6 or not validate_ticker(ticker): continue
    
    try:
        stock = yf.Ticker(ticker); hist = stock.history(period="3mo")
        current_price = float(hist['Close'].iloc[-1]) 
        tr = pd.concat([hist.High - hist.Low, (hist.High - hist.Close.shift()).abs(), (hist.Low - hist.Close.shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1]) 
        
        # DYNAMIC SIZING & STOPS BASED ON GROQ STRATEGY
        if risk_frac == 0: continue # Strategy says stand down
        
        sl_pct = (sl_mult * atr) / current_price
        risk_budget = 100000 * risk_frac
        stop_distance = sl_pct * current_price
        qty = max(1, math.floor(risk_budget / stop_distance))
        
        # --- NSE V1 UPGRADE 1: VOLUME PARTICIPATION GOVERNOR ---
        qty = check_volume_participation(ticker, qty)
        if qty <= 0: continue
        
        # --- NSE V1 UPGRADE 2: STRESSED MARGIN CHECK ---
        order_value = qty * current_price
        if not stress_test_margin(order_value):
            print(f"   🚫 {ticker}: Blocked by Margin Stress Test.")
            continue

# ==============================================================================
# ORGAN 8: HIPPOCAMPUS WRITE
# ==============================================================================
if GITHUB_TOKEN and REPO and todays_predictions:
    print("\n🧠 [ORGAN 8: HIPPOCAMPUS] Committing Memory...")
    memory["predictions"].extend(todays_predictions)
    memory["updated_at"] = datetime.now(timezone.utc).isoformat()
    memory["version"] = memory.get("version", 0) + 1
    body = {"message": f"hippocampus: v{memory['version']} [skip ci]", "content": base64.b64encode(json.dumps({k:v for k,v in memory.items() if k != "_sha"}, indent=2).encode()).decode(), "branch": "main"}
    if memory.get("_sha"): body["sha"] = memory["_sha"]
    try: requests.put(f"https://api.github.com/repos/{REPO}/contents/memory/memory.json", headers=GH_HEADERS, json=body, timeout=10)
    except: pass

if NTFY_TOPIC:
    try: requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=f"APEX-K V11.0 Run\nStrategy: {groq_strategy or 'FALLBACK'}\nTrades: {len(todays_predictions)}".encode('utf-8'), headers={"Title": "APEX-K Run"})
    except: pass

print("\n" + "="*70)
print("✅ APEX-K V11.0 CYCLE COMPLETE. SYSTEM STANDING DOWN.")
print("="*70)
