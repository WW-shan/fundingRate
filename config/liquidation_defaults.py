"""Default per-symbol thresholds for 60-second liquidation alerts."""

DEFAULT_LIQUIDATION_SYMBOL_MIN_ALERT_USD = {
    # Largest caps need much more absolute liquidation volume before alerting.
    "BTC": 2_000_000,
    "ETH": 2_000_000,
    # High-liquidity mainstream alts use an intermediate threshold.
    "BNB": 1_000_000,
    "XRP": 1_000_000,
    "SOL": 1_000_000,
    "TRX": 1_000_000,
    "DOGE": 1_000_000,
    "ADA": 1_000_000,
    "BCH": 1_000_000,
    "LINK": 1_000_000,
    "HYPE": 1_000_000,
    "XLM": 1_000_000,
    "LTC": 1_000_000,
    "AVAX": 1_000_000,
    "SUI": 1_000_000,
    "TON": 1_000_000,
    "SHIB": 1_000_000,
    "DOT": 1_000_000,
    "UNI": 1_000_000,
    "PEPE": 1_000_000,
}
