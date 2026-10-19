"""
Binance 适配器（监控版）
"""
import ccxt
from .base_exchange import BaseExchange


class BinanceAdapter(BaseExchange):
    # Binance premiumIndex 的 lastFundingRate 是上一期已结算费率
    funding_rate_semantics = 'last_settled'

    def _init_exchange(self):
        cfg = self._ccxt_config()
        cfg['options'] = {'defaultType': 'spot'}
        self.exchange = ccxt.binance(cfg)

    def _convert_to_futures_symbol(self, spot_symbol: str) -> str:
        if ':' not in spot_symbol:
            return f"{spot_symbol}:USDT"
        return spot_symbol
