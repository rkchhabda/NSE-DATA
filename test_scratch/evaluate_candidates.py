import json

with open('test_scratch/screening_results.json') as f:
    data = json.load(f)

print(f"Total stocks: {len(data)}")
data_sorted = sorted(data, key=lambda x: x['conviction_score'], reverse=True)

print("\nTop 20 ranked candidates:")
print(f"{'Symbol':<12} {'Tech':<6} {'Fund':<6} {'Sent':<6} {'Backtest':<9} {'Conviction':<11} {'Sector'}")
print("-" * 75)
for s in data_sorted[:20]:
    print(f"{s['symbol']:<12} {s['tech_score']:<6} {s['fund_score']:<6} {s['sent_score']:<6} {s['backtest_score']:<9} {s['conviction_score']:<11} {s['sector']}")

qualifying = [s for s in data if s['tech_score'] >= 75 and s['fund_score'] >= 65 and s['sent_score'] >= 60 and s['backtest_score'] >= 60]
print(f"\nCandidates meeting upstream thresholds (Tech>=75, Fund>=65, Sent>=60, Backtest>=60): {len(qualifying)}")
for q in qualifying:
    print(f"-> {q['symbol']} (Tech: {q['tech_score']}, Fund: {q['fund_score']}, Sent: {q['sent_score']}, Backtest: {q['backtest_score']}, Conviction: {q['conviction_score']})")

# Categorize into Watchlist, Breakout, and Avoid
breakout_candidates = [s for s in data if 65 <= s['tech_score'] < 75 and s['fund_score'] >= 60]
watch_candidates = [s for s in data if s not in qualifying and s not in breakout_candidates and s['tech_score'] >= 50 and s['fund_score'] >= 50]
avoid_candidates = [s for s in data if s['tech_score'] < 45 or s['fund_score'] < 50 or s['sent_score'] < 45]

print(f"\nBreakout count: {len(breakout_candidates)}, Watch count: {len(watch_candidates)}, Avoid count: {len(avoid_candidates)}")

