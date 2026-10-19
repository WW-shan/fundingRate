"""
交易所基类（监控版）
仅使用公开 API 获取行情、资金费率、订单簿数据，不需要 API 密钥
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from loguru import logger
import ccxt


class BaseExchange(ABC):
    # 资金费率语义：
    #   'upcoming'      = 下一期将收取/预测的费率（OKX/Bybit/Gate/Bitget）
    #   'last_settled'  = 上一期已结算的费率（Binance）
    funding_rate_semantics = 'upcoming'

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

    # ---------- 行情 ----------

    def get_spot_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取现货行情"""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return {
                'bid': ticker.get('bid'),
                'ask': ticker.get('ask'),
                'last': ticker.get('last'),
                'timestamp': ticker.get('timestamp')
            }
        except Exception as e:
            logger.debug(f"Error fetching spot ticker for {symbol}: {e}")
            return {}

    def get_futures_ticker(self, symbol: str) -> Dict[str, Any]:
        """获取永续合约行情"""
        try:
            futures_symbol = self._convert_to_futures_symbol(symbol)
            ticker = self.exchange.fetch_ticker(futures_symbol)
            return {
                'bid': ticker.get('bid'),
                'ask': ticker.get('ask'),
                'last': ticker.get('last'),
                'timestamp': ticker.get('timestamp')
            }
        except Exception as e:
            logger.debug(f"Error fetching futures ticker for {symbol}: {e}")
            return {}

    # ---------- 资金费率 ----------

    def get_funding_rate(self, symbol: str) -> Dict[str, Any]:
        """
        获取资金费率（含官方结算间隔）

        返回: {
            'funding_rate': 当前费率,
            'next_funding_time': 下次结算时间(ms),
            'funding_interval': 结算间隔(ms),
            'predicted_rate': 预测费率(若有),
            'last_settled_rate': 上期已结算费率(若有),
            'rate_semantics': 'upcoming' | 'last_settled'
        }
        """
        try:
            futures_symbol = self._convert_to_futures_symbol(symbol)
            funding = self.exchange.fetch_funding_rate(futures_symbol)

            funding_interval = self._parse_funding_interval(funding, allow_history=True)
            predicted_rate, last_settled_rate = self._parse_extra_rates(funding)

            return {
                'funding_rate': funding.get('fundingRate'),
                'next_funding_time': funding.get('fundingTimestamp'),
                'funding_interval': funding_interval,
                'predicted_rate': predicted_rate,
                'last_settled_rate': last_settled_rate,
                'rate_semantics': self.funding_rate_semantics,
            }
        except Exception as e:
            error_msg = str(e)
            if 'does not have market symbol' in error_msg or 'not found' in error_msg.lower():
                logger.debug(f"Symbol {symbol} not available for funding rate: {error_msg}")
            elif 'rate limit' in error_msg.lower():
                logger.warning(f"Rate limit hit for {symbol}: {error_msg}")
            else:
                logger.debug(f"Error fetching funding rate for {symbol}: {error_msg}")
            return {}

    def _parse_funding_interval(self, funding: Dict[str, Any], allow_history: bool = False) -> Optional[int]:
        """
        解析结算间隔（毫秒），优先级：
        1. CCXT 规范化 interval 字段（"8h"/"4h"）
        2. 交易所 info 原始字段（各家单位不同）
        3. 历史数据实测推算（兜底）
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
                    hours = float(s)
                    if hours > 0:
                        funding_interval = int(hours * 3600 * 1000)
            except (ValueError, AttributeError, TypeError):
                pass

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

        # 3. 历史数据实测推算（兜底，仅单币种查询时启用）
        if not funding_interval and allow_history:
            try:
                futures_symbol = funding.get('symbol')
                if futures_symbol:
                    history = self.exchange.fetch_funding_rate_history(futures_symbol, limit=2)
                    if history and len(history) >= 2:
                        interval_ms = abs(history[0]['timestamp'] - history[1]['timestamp'])
                        if 3600000 <= interval_ms <= 86400000:
                            funding_interval = interval_ms
            except Exception:
                pass

        return funding_interval

    def _parse_extra_rates(self, funding: Dict[str, Any]):
        """解析预测费率与上期已结算费率（子类可覆写）"""
        info = funding.get('info') or {}
        predicted_rate = None
        last_settled_rate = None

        if self.funding_rate_semantics == 'upcoming':
            predicted_rate = funding.get('fundingRate')
        else:
            last_settled_rate = funding.get('fundingRate')

        return predicted_rate, last_settled_rate

    # ---------- 订单簿 ----------

    def get_order_book(self, symbol: str, is_futures: bool = False, limit: int = 5) -> Dict[str, Any]:
        """获取订单簿（用于深度/流动性参考）"""
        try:
            if is_futures:
                symbol = self._convert_to_futures_symbol(symbol)

            orderbook = self.exchange.fetch_order_book(symbol, limit)
            bids = orderbook['bids'][:limit]
            asks = orderbook['asks'][:limit]

            return {
                'bids': bids,
                'asks': asks,
                'bid_depth': sum([bid[1] for bid in bids]),
                'ask_depth': sum([ask[1] for ask in asks])
            }
        except Exception as e:
            logger.debug(f"Error fetching order book for {symbol}: {e}")
            return {'bids': [], 'asks': [], 'bid_depth': 0, 'ask_depth': 0}

    # ---------- 交易手续费（公共费率，仅供参考） ----------

    def get_trading_fees(self, symbol: str) -> Dict[str, float]:
        """获取公共交易手续费率"""
        try:
            markets = self.exchange.load_markets()
            market = markets.get(symbol, {})
            return {
                'maker': market.get('maker', 0.001),
                'taker': market.get('taker', 0.001)
            }
        except Exception as e:
            logger.debug(f"Error fetching trading fees: {e}")
            return {'maker': 0.001, 'taker': 0.001}

    @abstractmethod
    def _convert_to_futures_symbol(self, spot_symbol: str) -> str:
        """现货 symbol 转永续 symbol，如 BTC/USDT -> BTC/USDT:USDT"""
        pass

    def test_connection(self) -> bool:
        """测试公共 API 连接"""
        try:
            self.exchange.fetch_time()
            logger.info(f"{self.__class__.__name__} connection OK")
            return True
        except Exception as e:
            logger.warning(f"{self.__class__.__name__} connection failed: {e}")
            return False
