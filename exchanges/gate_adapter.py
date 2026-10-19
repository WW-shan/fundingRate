"""
Gate.io 适配器（监控版）
"""
import ccxt
from .base_exchange import BaseExchange


class GateAdapter(BaseExchange):
    # Gate funding_rate 是下期将收取的费率
    funding_rate_semantics = 'upcoming'

    def _init_exchange(self):
        self.exchange = ccxt.gate(self._ccxt_config())

    def _convert_to_futures_symbol(self, spot_symbol: str) -> str:
        if ':' not in spot_symbol:
            return f"{spot_symbol}:USDT"
        return spot_symbol
