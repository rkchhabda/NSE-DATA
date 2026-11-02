# NSE-DATA: NSE Data Pipeline & Stock Momentum Engine

An automated data pipeline and screening engine for the National Stock Exchange of India (NSE).

## Features
- **Live Quotes & Detailed Scrip Data**: Fetches real-time market quotes and security depth using `nse` (v5.1.1+).
- **Historical Data & Indicators**: Retrieves historical OHLCV data and computes technical indicators:
  - Simple Moving Averages (`SMA-50`, `SMA-200`)
  - Relative Strength Index (`RSI-14`)
  - Moving Average Convergence Divergence (`MACD` & `Signal`)
  - 10-day Price Momentum
- **Momentum Scoring & Signal Generation**: Multi-factor scoring generating signals (`STRONG_BUY`, `BUY`, `HOLD`, `SELL`, `STRONG_SELL`).
- **Data Integrity & Gating**: Validation against Bhavcopy, delivery volumes, and historical patterns.
- **Automated Workflows**: GitHub Actions configurations for daily evening screening and morning entry checks.

## Project Structure
```
├── .github/workflows/         # Scheduled screening workflows
├── nse_fetcher.py             # Pipeline script for quotes, historical data & momentum signals
├── gate_check.py              # Data integrity engine
├── nse_bhavcopy.py            # NSE Bhavcopy processor
├── pipeline/                  # Screener tools, price basis checks, and analytics
├── nse_data/                  # Downloaded historical CSVs with calculated indicators
├── snapshots/                 # Generated live quote and signal JSON files
├── data/                      # Bhavcopy, reports, and history
└── requirements.txt           # Python dependencies
```

## Getting Started

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the Data Pipeline
```bash
python nse_fetcher.py
```

### 3. Pipeline Outputs
- `snapshots/live_quotes.json`: Live quote snapshot for watchlist stocks
- `snapshots/momentum_signals.json`: Momentum scores and recommendation signals
- `nse_data/<SYMBOL>_historical.csv`: 2-year OHLCV data with technical indicators

