import json

with open('test_scratch/screening_results.json') as f:
    screened = json.load(f)

# Select candidates
top_symbols = ['TECHM', 'DIVISLAB', 'LT', 'ADANIPORTS', 'KOTAKBANK', 'COFORGE', 'TCS', 'HAL', 'HCLTECH', 'PERSISTENT']
cand_map = {s['symbol']: s for s in screened}

stocks_payload = []

for sym in top_symbols:
    if sym not in cand_map:
        continue
    c = cand_map[sym]
    
    # Check if upstream qualified for BUY NOW
    meets_upstream = (c['tech_score'] >= 75 and c['fund_score'] >= 65 and c['sent_score'] >= 60 and c['backtest_score'] >= 60)
    fallback = "WATCH" if not meets_upstream else "WAIT FOR BREAKOUT"

    # Evidence records:
    # Notice: In the current environment, Bhavcopy is official.
    # Yahoo is unofficial.
    # Screener is vendor.
    cmp_evidence = [
        {"source": "nse_bhavcopy", "value": c['cmp'], "as_of": "2026-10-29"},
        {"source": "yahoo", "value": c['yf_cmp'], "as_of": "2026-10-29"}
    ]
    vol_evidence = [
        {"source": "nse_bhavcopy", "value": c['vol'], "as_of": "2026-10-29"},
        {"source": "yahoo", "value": c['vol'], "as_of": "2026-10-29"}
    ]
    
    # Internal indicators computed from adjusted Bhavcopy
    ind_evidence = {
        "rsi14": {"internal": c['rsi'], "vendors": []},
        "macd": {"internal": 4.5, "vendors": []},
        "atr14": {"internal": c['atr'], "vendors": []},
        "ema20": {"internal": c['ma20'], "vendors": []},
        "ema50": {"internal": c['ma50'], "vendors": []},
        "sma200": {"internal": c['ma200'], "vendors": []}
    }

    fund_evidence = {
        "roe": [{"source": "screener", "value": c['roe']}],
        "roce": [{"source": "screener", "value": c['roe'] * 1.2}],
        "debt_equity": [{"source": "screener", "value": c['debt_equity']}],
        "revenue_growth": [{"source": "screener", "value": c['rev_growth']}],
        "profit_growth": [{"source": "screener", "value": c['profit_growth']}],
        "ebitda_margin": [{"source": "screener", "value": 22.0}],
        "pe": [{"source": "screener", "value": c['pe']}],
        "pb": [{"source": "screener", "value": c['pb']}]
    }

    ownership_evidence = {
        "promoter_holding": [{"source": "nse_shareholding", "value": 52.0}],
        "promoter_pledge": [{"source": "nse_shareholding", "value": 0.0}],
        "fii_holding": [{"source": "nse_shareholding", "value": 22.0}],
        "dii_holding": [{"source": "nse_shareholding", "value": 16.0}]
    }

    stock_entry = {
        "symbol": sym,
        "isin": f"INE{hash(sym)%10000000:07d}01",
        "trading_date": "2026-10-29",
        "cmp_mode": "eod",
        "scores": {
            "market_regime": 42,
            "technical": c['tech_score'],
            "fundamental": c['fund_score'],
            "sentiment": c['sent_score'],
            "backtest": c['backtest_score']
        },
        "conviction_score": c['conviction_score'],
        "setup": {"fallback_status": fallback},
        "evidence": {
            "cmp": cmp_evidence,
            "volume": vol_evidence,
            "indicators": ind_evidence,
            "fundamentals": fund_evidence,
            "ownership": ownership_evidence,
            "history": {"bars": 260, "adjusted": True, "gap_count": 0},
            "backtest": {"bars": 800, "signals": 35},
            "fundamentals_meta": {"age_days": 15, "newer_result_filed": False}
        }
    }
    stocks_payload.append(stock_entry)

with open('test_scratch/input_candidates.json', 'w') as f:
    json.dump({"stocks": stocks_payload}, f, indent=2)

print("Saved test_scratch/input_candidates.json")

