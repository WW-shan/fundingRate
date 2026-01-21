"""
Bitget交易所适配器
"""
import ccxt
from .base_exchange import BaseExchange


class BitgetAdapter(BaseExchange):
    def _init_exchange(self):
        """初始化Bitget交易所"""
        self.exchange = ccxt.bitget({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'enableRateLimit': True,
        })

    def _convert_to_futures_symbol(self, spot_symbol: str) -> str:
        """
        Bitget永续合约格式: BTC/USDT:USDT
        """
        if ':' not in spot_symbol:
            return f"{spot_symbol}:USDT"
        return spot_symbol
