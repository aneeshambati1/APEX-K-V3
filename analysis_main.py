import json
import numpy as np
from pathlib import Path

# Load your trade history
memory_path = Path('memory/memory.json')
if not memory_path.exists():
    print("❌ memory/memory.json not found")
    exit(1)

with open(memory_path, 'r') as f:
    memory = json.load(f)

# Extract evaluated trades
trades = [p for p in memory.get('predictions', []) if '_outcome_pct' in p]

if len(trades) < 10:
    print(f"⚠️  Only {len(trades)} evaluated trades. Need at least 20 for reliable stats.")
    print("Run the bot for 1-2 more months, then re-run this analysis.")
    exit(0)

returns = [p['_outcome_pct'] for p in trades]

# ============================================================================
# METRIC 1: SHARPE RATIO (annualized)
# ============================================================================
# Assuming ~3-day average hold = ~8 trades per month = ~100 trades per year
sharpe = (np.mean(returns) / np.std(returns)) * np.sqrt(100) if np.std(returns) > 0 else 0

# ============================================================================
# METRIC 2: MAX DRAWDOWN
# ============================================================================
equity_curve = [1.0]
for r in returns:
    equity_curve.append(equity_curve[-1] * (1 + r))

peak = equity_curve[0]
mdd = 0
for eq in equity_curve:
    peak = max(peak, eq)
    dd = (peak - eq) / peak
    mdd = max(mdd, dd)

# ============================================================================
# METRIC 3: WIN RATE
# ============================================================================
win_rate = sum(1 for r in returns if r > 0) / len(returns)

# ============================================================================
# METRIC 4: AVERAGE R-MULTIPLE
# ============================================================================
wins = [r for r in returns if r > 0]
losses = [abs(r) for r in returns if r < 0]
avg_r = np.mean(wins) / np.mean(losses) if losses else float('inf')

# ============================================================================
# METRIC 5: PER-STRATEGY SHARPE
# ============================================================================
strategy_returns = {}
for p in trades:
    strat = p.get('strategy', 'UNKNOWN')
    if strat not in strategy_returns:
        strategy_returns[strat] = []
    strategy_returns[strat].append(p['_outcome_pct'])

# ============================================================================
# PRINT RESULTS
# ============================================================================
print("=" * 70)
print(f"APEX-K PERFORMANCE ANALYSIS ({len(trades)} evaluated trades)")
print("=" * 70)
print(f"Sharpe Ratio:        {sharpe:.2f}")
print(f"Max Drawdown:        {mdd:.1%}")
print(f"Win Rate:            {win_rate:.1%}")
print(f"Average R-Multiple:  {avg_r:.2f}")
print()
print("Per-Strategy Performance:")
print("-" * 70)
for strat, rets in sorted(strategy_returns.items()):
    if len(rets) >= 5:
        s = (np.mean(rets) / np.std(rets)) * np.sqrt(100) if np.std(rets) > 0 else 0
        wr = sum(1 for r in rets if r > 0) / len(rets)
        print(f"  {strat:<30} Sharpe: {s:6.2f}  Win%: {wr:5.1%}  (n={len(rets)})")
    else:
        print(f"  {strat:<30} Insufficient data (n={len(rets)})")
print("=" * 70)

# ============================================================================
# INTERPRETATION
# ============================================================================
print()
if sharpe > 1.5 and mdd < 0.15 and avg_r > 1.5:
    print("✅ STRONG EDGE DETECTED")
    print("   → Scale to 100 trades/month")
    print("   → Double down on top 3 strategies")
    print("   → Kill bottom 40% of strategies")
elif 0.8 <= sharpe <= 1.5:
    print("⚠️  AMBIGUOUS EDGE")
    print("   → Some strategies work, some don't")
    print("   → Identify winners (Sharpe > 1.5) and losers (Sharpe < 0.5)")
    print("   → Kill losers, amplify winners")
    print("   → Run 3 more months before scaling")
elif 0.3 <= sharpe < 0.8:
    print("⚠️  WEAK EDGE")
    print("   → Not enough for short-term swing trading")
    print("   → Check if one strategy is carrying everything")
    print("   → Fix specific organs (adaptive_time_manager likely miscalibrated)")
    print("   → Do NOT scale")
elif 0 <= sharpe < 0.3:
    print("❌ NO EDGE")
    print("   → Strategy doesn't work in current form")
    print("   → Major redesign needed")
    print("   → Do NOT scale")
else:
    print("❌ NEGATIVE EDGE")
    print("   → Bot is losing money")
    print("   → Full stop. Redesign from scratch.")
