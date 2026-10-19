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

    def canonical_symbol(self, symbol: str) -> str:
        """Gate ? CCXT ???????????? ID ???

        ???? RED_USDT ? CCXT ??? RedLang/USDT??????
        ?? ID ? BASE_QUOTE ????????????????????
        """
        try:
            market = self.exchange.market(symbol)
            market_id = market.get('id') or ''
            if market.get('quote') == 'USDT' and market_id.endswith('_USDT'):
                return f"{market_id[:-5]}/USDT"
        except Exception:
            pass
        return super().canonical_symbol(symbol)
