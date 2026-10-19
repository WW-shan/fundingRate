"""
Bitget 适配器（监控版）
"""
import ccxt
from .base_exchange import BaseExchange


class BitgetAdapter(BaseExchange):
    # Bitget fundingRate 是下期将收取的费率
    funding_rate_semantics = 'upcoming'

    def _init_exchange(self):
        cfg = self._ccxt_config()
        cfg['options'] = {'defaultType': 'swap'}
        self.exchange = ccxt.bitget(cfg)
