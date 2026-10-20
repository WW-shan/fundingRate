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
        """Gate 的 symbol 与 CCXT 标准符号可能不一致，需要用 market ID 反推。

        例如 RED_USDT 在 CCXT 中是 RedLang/USDT，直接取 base 会得到 RedLang，
        导致无法与 Binance/OKX 的 RED 对齐。这里用 market ID 的 BASE_QUOTE 结构还原。
        """
        try:
            market = self.exchange.market(symbol)
            market_id = market.get('id') or ''
            if market.get('quote') == 'USDT' and market_id.endswith('_USDT'):
                return f"{market_id[:-5]}/USDT"
        except Exception:
            pass
        return super().canonical_symbol(symbol)
