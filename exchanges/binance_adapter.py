"""
Binance 适配器（监控版）
"""
import ccxt
from .base_exchange import BaseExchange


class BinanceAdapter(BaseExchange):
    # Binance premiumIndex 的 lastFundingRate 是上一期已结算费率
    funding_rate_semantics = 'last_settled'
    # 官方文档：fundingInfo 只返回调整过间隔的币种，未返回的默认 8h
    default_funding_interval_ms = 8 * 3600 * 1000

    def _init_exchange(self):
        cfg = self._ccxt_config()
        cfg['options'] = {'defaultType': 'spot'}
        self.exchange = ccxt.binance(cfg)
