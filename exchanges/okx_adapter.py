"""
OKX 适配器（监控版）
"""
import ccxt
from typing import Dict, Any
from .base_exchange import BaseExchange


class OKXAdapter(BaseExchange):
    # OKX fundingRate 是下期预测费率
    funding_rate_semantics = 'upcoming'

    def _init_exchange(self):
        self.exchange = ccxt.okx(self._ccxt_config())

    def _convert_to_futures_symbol(self, spot_symbol: str) -> str:
        if ':' not in spot_symbol:
            return f"{spot_symbol}:USDT"
        return spot_symbol

    def _parse_extra_rates(self, funding: Dict[str, Any]):
        info = funding.get('info') or {}
        predicted = funding.get('fundingRate')          # 下期预测费率
        last_settled = None
        try:
            last_settled = float(info['settFundingRate']) if info.get('settFundingRate') else None
        except (ValueError, TypeError):
            pass
        return predicted, last_settled
