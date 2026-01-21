"""
Binance交易所适配器
"""
import ccxt
from .base_exchange import BaseExchange


class BinanceAdapter(BaseExchange):
    def _init_exchange(self):
        """初始化Binance交易所"""
        self.exchange = ccxt.binance({
            'apiKey': self.api_key,
            'secret': self.api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',  # 默认现货
            }
        })

    def _convert_to_futures_symbol(self, spot_symbol: str) -> str:
        """
        Binance永续合约格式: BTC/USDT:USDT
        """
        if ':' not in spot_symbol:
            return f"{spot_symbol}:USDT"
        return spot_symbol
