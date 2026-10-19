"""
Bybit 适配器（监控版）
"""
import ccxt
from .base_exchange import BaseExchange


class BybitAdapter(BaseExchange):
    # Bybit fundingRate 是下期将收取的费率
    funding_rate_semantics = 'upcoming'

    def _init_exchange(self):
        self.exchange = ccxt.bybit(self._ccxt_config())
