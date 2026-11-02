from nse import NSE
from pathlib import Path
import pandas as pd
import json
from datetime import date, timedelta, datetime
import logging

# Setup
Path("logs").mkdir(exist_ok=True)
Path("snapshots").mkdir(exist_ok=True)
Path("nse_data").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('logs/nse_fetch.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DATA_DIR = Path("./nse_data")

WATCHLIST = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN",
    "TATAMOTORS", "ITC", "LT", "AXISBANK", "BHARTIARTL"
]

def fetch_live_quotes(symbols: list) -> dict:
    """Fetch live quotes using get_detailed_scrip_data()."""
    results = {}
    with NSE(download_folder=DATA_DIR, use_http2=True) as nse:
        for i, symbol in enumerate(symbols):
            try:
                data = nse.get_detailed_scrip_data(symbol)
                eq_list = data.get("equityResponse", [])
                eq_resp = (eq_list[0] if isinstance(eq_list, list) and len(eq_list) > 0 else {}) or {}
                meta_data = eq_resp.get("metaData") or {}
                trade_info = eq_resp.get("tradeInfo") or {}
                price_info = data.get("priceInfo") or eq_resp.get("priceInfo") or {}
                sec_info = data.get("securityWiseDP") or eq_resp.get("secInfo") or {}

                last_price = price_info.get("lastPrice") or trade_info.get("lastPrice") or meta_data.get("closePrice")
                change = price_info.get("change") or meta_data.get("change")
                pct_change = price_info.get("pChange") or meta_data.get("pChange")
                day_high = price_info.get("intradayHigh") or meta_data.get("dayHigh")
                day_low = price_info.get("intradayLow") or meta_data.get("dayLow")
                vwap = price_info.get("vwap") or meta_data.get("averagePrice")
                volume = sec_info.get("quantityTraded") or trade_info.get("totalTradedVolume") or trade_info.get("quantitytraded")

                results[symbol] = {
                    "last_price": last_price,
                    "change": change,
                    "pct_change": pct_change,
                    "day_high": day_high,
                    "day_low": day_low,
                    "vwap": vwap,
                    "volume": volume,
                    "timestamp": datetime.now().isoformat()
                }
                logger.info(f"[{i+1}/{len(symbols)}] {symbol}: ₹{results[symbol]['last_price']}")
            except Exception as e:
                logger.error(f"Failed to fetch {symbol}: {e}")
                results[symbol] = {"error": str(e)}
    return results

def fetch_historical(symbol: str, years: int = 2) -> pd.DataFrame:
    """Fetch historical OHLCV data."""
    end_date = date.today()
    start_date = end_date - timedelta(days=years * 365)
    with NSE(download_folder=DATA_DIR, use_http2=True) as nse:
        data = nse.fetch_equity_historical_data(
            symbol=symbol,
            from_date=start_date,
            to_date=end_date,
            series="eq"
        )
    df = pd.DataFrame(data)
    df = df.rename(columns={
        "mTIMESTAMP": "date",
        "mtimestamp": "date",
        "CH_OPEN": "open",
        "chOpeningPrice": "open",
        "CH_HIGH": "high",
        "chTradeHighPrice": "high",
        "CH_LOW": "low",
        "chTradeLowPrice": "low",
        "CH_CLOSE": "close",
        "chClosingPrice": "close",
        "CH_TOT_TRADED_QTY": "volume",
        "chTotTradedQty": "volume"
    })
    df = df.drop_duplicates(subset=['date'])
    if 'date' in df.columns:
        date_series = pd.to_datetime(df['date'], format='%d-%b-%Y', errors='coerce')
        if not date_series.isna().all():
            df['_sort_date'] = date_series
            df = df.sort_values('_sort_date').drop(columns=['_sort_date']).reset_index(drop=True)
        else:
            df = df.sort_values('date').reset_index(drop=True)
    logger.info(f"✓ {symbol}: {len(df)} trading days collected")
    return df

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add technical indicators."""
    df['sma_50'] = df['close'].rolling(window=50).mean()
    df['sma_200'] = df['close'].rolling(window=200).mean()
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    df['rsi_14'] = 100 - (100 / (1 + rs))
    ema12 = df['close'].ewm(span=12).mean()
    ema26 = df['close'].ewm(span=26).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9).mean()
    df['momentum_10d'] = ((df['close'] - df['close'].shift(10)) / df['close'].shift(10) * 100)
    return df

def generate_momentum_signal(row) -> dict:
    """Generate momentum signal."""
    score = 0
    if row['rsi_14'] > 70: score -= 15
    elif row['rsi_14'] > 60: score += 10
    elif row['rsi_14'] < 30: score += 15
    elif row['rsi_14'] < 40: score -= 10
    if row['macd'] > row['macd_signal']: score += 20
    else: score -= 20
    if row['sma_50'] > row['sma_200']: score += 25
    else: score -= 25
    if row['momentum_10d'] > 3: score += 15
    elif row['momentum_10d'] > 0: score += 8
    elif row['momentum_10d'] < -3: score -= 15
    elif row['momentum_10d'] < 0: score -= 8
    if score >= 60: signal = "STRONG_BUY"
    elif score >= 30: signal = "BUY"
    elif score <= -60: signal = "STRONG_SELL"
    elif score <= -30: signal = "SELL"
    else: signal = "HOLD"
    return {"score": int(score), "signal": signal}

if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("NSE Momentum Data Pipeline - Starting")
    logger.info("=" * 50)

    logger.info("\n[1/4] Fetching live quotes...")
    quotes = fetch_live_quotes(WATCHLIST)
    with open("snapshots/live_quotes.json", "w") as f:
        json.dump(quotes, f, indent=2, default=str)
    logger.info(f"✓ Saved {len(quotes)} quotes")

    logger.info("\n[2/4] Fetching historical data...")
    for symbol in WATCHLIST[:5]:
        logger.info(f"\n  → {symbol}")
        df = fetch_historical(symbol, years=2)
        logger.info(f"  Computing indicators for {symbol}...")
        df = compute_indicators(df)
        df.to_csv(f"nse_data/{symbol}_historical.csv", index=False)
        logger.info(f"  ✓ Saved {len(df)} rows")

    logger.info("\n[3/4] Generating momentum signals...")
    signals = {}
    for symbol in WATCHLIST[:5]:
        df = pd.read_csv(f"nse_data/{symbol}_historical.csv")
        latest = df.iloc[-1]
        signals[symbol] = generate_momentum_signal(latest)
        logger.info(f"  {symbol}: {signals[symbol]['signal']} (score: {signals[symbol]['score']})")
    with open("snapshots/momentum_signals.json", "w") as f:
        json.dump(signals, f, indent=2)

    logger.info("\n[4/4] Pipeline complete!")
    logger.info("=" * 50)
