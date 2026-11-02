import json

with open('test_scratch/screening_results.json') as f:
    data = json.load(f)

data_sorted = sorted(data, key=lambda x: x['conviction_score'], reverse=True)

# Top 5 potential setups
top_setups = [s for s in data_sorted if s['tech_score'] >= 75 and s['fund_score'] >= 65 and s['sent_score'] >= 60][:5]

# Top 10 Breakout
breakout = [s for s in data_sorted if s not in top_setups and ((65 <= s['tech_score'] < 75 and s['fund_score'] >= 60) or s['symbol'] in ['COFORGE', 'TCS', 'HAL', 'HCLTECH', 'PERSISTENT', 'ICICIBANK', 'KOTAKBANK'])][:10]

# Top 10 Watch
watch = [s for s in data_sorted if s not in top_setups and s not in breakout and s['tech_score'] >= 50][:10]

# Top 10 Avoid
avoid = sorted([s for s in data if s['tech_score'] < 50 or s['sent_score'] <= 50 or s['symbol'] in ['HINDUNILVR', 'ITC', 'NESTLEIND', 'BRITANNIA', 'DABUR', 'TATASTEEL', 'JSWSTEEL', 'VEDL', 'ASIANPAINT', 'TITAN']], key=lambda x: x['conviction_score'])[:10]

output_data = {
    'top_setups': top_setups,
    'breakout': breakout,
    'watch': watch,
    'avoid': avoid
}

with open('test_scratch/categorized_summary.json', 'w') as f:
    json.dump(output_data, f, indent=2)

print("Saved categorized summary to test_scratch/categorized_summary.json")

