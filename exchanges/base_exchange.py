"""
交易所基类（监控版）
仅使用公开 API 获取行情与资金费率，不需要 API 密钥
"""
import time
from abc import ABC, abstractmethod
from typing import Dict, Optional, Any
from loguru import logger


class BaseExchange(ABC):
    # 资金费率语义：'upcoming' = 当前预测/下期将收取的费率（五家统一）
    funding_rate_semantics = 'upcoming'
    # 官方缺省结算间隔（毫秒）。Binance fundingInfo 只返回调整过的币种，其余默认 8h
    default_funding_interval_ms = None

    def __init__(self, proxy: str = ''):
        self.proxy = proxy or None
        self.exchange = None
        self._init_exchange()

    def _ccxt_config(self) -> Dict[str, Any]:
        """公共 ccxt 配置（含代理）

        注意：ccxt 4.5+ 不允许同时设置 httpProxy/httpsProxy，
        必须使用 proxies dict 形式。
        """
        cfg: Dict[str, Any] = {'enableRateLimit': True}
        if self.proxy:
            cfg['proxies'] = {'http': self.proxy, 'https': self.proxy}
        return cfg

    @abstractmethod
    def _init_exchange(self):
        """初始化交易所实例"""
        pass

    def canonical_symbol(self, symbol: str) -> str:
        """规范化交易所符号：去掉 :USDT 等结算后缀，统一为 BASE/QUOTE 格式"""
        return symbol.split(':')[0]

    def _parse_funding_interval(self, funding: Dict[str, Any]) -> Optional[int]:
        """
        解析结算间隔（毫秒），优先级：
        1. CCXT 规范化 interval 字段（"8h"/"4h"；OKX 特殊币种会返回毫秒数字串）
        2. 交易所 info 原始字段（各家单位不同）
        """
        funding_interval = None

        # 1. CCXT 规范化字段
        interval_str = funding.get('interval')
        if interval_str:
            try:
                s = str(interval_str).lower().strip()
                if s.endswith('h'):
                    hours = float(s[:-1])
                    if hours > 0:
                        funding_interval = int(hours * 3600 * 1000)
                elif s.endswith('m'):
                    minutes = float(s[:-1])
                    if minutes > 0:
                        funding_interval = int(minutes * 60 * 1000)
                else:
                    value = float(s)
                    if value > 0:
                        # OKX 对 1h/2h 等特殊周期直接返回毫秒数字串（如 '7200000'），
                        # 而非 '8h' 这类规范格式；按数值大小判定单位，避免把 2h 当成 7200000h
                        if value >= 60000:
                            funding_interval = int(value)
                        else:
                            funding_interval = int(value * 3600 * 1000)
            except (ValueError, AttributeError, TypeError):
                pass

        # 1b. 合法性校验：间隔必须在 1h ~ 24h 之间，越界视为解析错误
        if funding_interval is not None and not (3600000 <= funding_interval <= 86400000):
            funding_interval = None

        info = funding.get('info') or {}

        # 2. 交易所原始字段
        if not funding_interval and info:
            # Bybit: fundingIntervalHour (小时)
            # Bitget: fundingRateInterval (小时字符串)
            interval_hours = (
                info.get('fundingIntervalHour')
                or info.get('fundingIntervalHours')
                or info.get('fundingRateInterval')
            )
            if interval_hours:
                try:
                    hours = float(interval_hours)
                    if hours > 0:
                        funding_interval = int(hours * 3600 * 1000)
                except (ValueError, TypeError):
                    pass

            # Gate: funding_interval (秒)
            if not funding_interval:
                interval_seconds = info.get('funding_interval')
                if interval_seconds:
                    try:
                        seconds = float(interval_seconds)
                        if seconds > 0:
                            funding_interval = int(seconds * 1000)
                    except (ValueError, TypeError):
                        pass

            # Bybit instruments: fundingInterval (分钟)
            if not funding_interval:
                interval_minutes = info.get('fundingInterval')
                if interval_minutes:
                    try:
                        minutes = float(interval_minutes)
                        if minutes > 0:
                            funding_interval = int(minutes * 60 * 1000)
                    except (ValueError, TypeError):
                        pass

        return funding_interval




    def test_connection(self, retries: int = 2) -> bool:
        """Test public API connectivity with short retries for transient errors."""
        last_error = None
        for attempt in range(retries + 1):
            try:
                self.exchange.fetch_time()
                logger.info(f"{self.__class__.__name__} connection OK")
                return True
            except Exception as e:
                last_error = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        logger.warning(f"{self.__class__.__name__} connection failed after {retries + 1} attempts: {last_error}")
        return False
