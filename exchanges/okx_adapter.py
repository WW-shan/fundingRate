"""
OKX交易所适配器
"""
import ccxt
from .base_exchange import BaseExchange


class OKXAdapter(BaseExchange):
    def _init_exchange(self):
        """初始化OKX交易所"""
        self.exchange = ccxt.okx({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'password': self.passphrase,  # OKX需要passphrase
            'enableRateLimit': True,
        })

    def _convert_to_futures_symbol(self, spot_symbol: str) -> str:
        """
        OKX永续合约格式: BTC/USDT:USDT
        """
        if ':' not in spot_symbol:
            return f"{spot_symbol}:USDT"
        return spot_symbol
