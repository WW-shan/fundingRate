"""
交易所适配器（监控版，仅公开 API）
"""
from .binance_adapter import BinanceAdapter
from .okx_adapter import OKXAdapter
from .bybit_adapter import BybitAdapter
from .gate_adapter import GateAdapter
from .bitget_adapter import BitgetAdapter

EXCHANGE_CLASSES = {
    'binance': BinanceAdapter,
    'okx': OKXAdapter,
    'bybit': BybitAdapter,
    'gate': GateAdapter,
    'bitget': BitgetAdapter,
}

__all__ = [
    'BinanceAdapter', 'OKXAdapter', 'BybitAdapter', 'GateAdapter', 'BitgetAdapter',
    'EXCHANGE_CLASSES',
]
