"""
交易所基类
定义统一的交易所接口
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
from loguru import logger
import ccxt


class BaseExchange(ABC):
    def __init__(self, api_key: str = "", api_secret: str = "", passphrase: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.exchange = None
        self._init_exchange()

    @abstractmethod
    def _init_exchange(self):
        """初始化交易所实例"""
        pass

    def get_spot_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        获取现货行情
        返回: {
            'bid': 买一价,
            'ask': 卖一价,
            'last': 最新价,
            'timestamp': 时间戳
        }
        """
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return {
                'bid': ticker.get('bid'),
                'ask': ticker.get('ask'),
                'last': ticker.get('last'),
                'timestamp': ticker.get('timestamp')
            }
        except Exception as e:
            logger.error(f"Error fetching spot ticker for {symbol}: {e}")
            return {}

    def get_futures_ticker(self, symbol: str) -> Dict[str, Any]:
        """
        获取永续合约行情
        """
        try:
            # 大多数交易所永续合约symbol格式为 BTC/USDT:USDT
            futures_symbol = self._convert_to_futures_symbol(symbol)
            ticker = self.exchange.fetch_ticker(futures_symbol)
            return {
                'bid': ticker.get('bid'),
                'ask': ticker.get('ask'),
                'last': ticker.get('last'),
                'timestamp': ticker.get('timestamp')
            }
        except Exception as e:
            logger.error(f"Error fetching futures ticker for {symbol}: {e}")
            return {}

    def get_funding_rate(self, symbol: str) -> Dict[str, Any]:
        """
        获取资金费率
        返回: {
            'funding_rate': 当前资金费率,
            'next_funding_time': 下次结算时间戳,
            'predicted_rate': 预测费率（如果有）,
            'funding_interval': 资金费率间隔（毫秒）
        }
        """
        try:
            futures_symbol = self._convert_to_futures_symbol(symbol)
            funding = self.exchange.fetch_funding_rate(futures_symbol)

            # 优先使用 CCXT 规范化的 interval 字段 (格式: "8h", "4h" 等)
            funding_interval = None
            interval_str = funding.get('interval')  # CCXT 规范化字段

            if interval_str:
                # 将 "8h", "4h" 等转换为毫秒
                try:
                    # 支持多种格式: "8h", "8H", "8"
                    interval_str_clean = str(interval_str).lower().replace('h', '').strip()
                    hours = int(interval_str_clean)
                    if hours > 0:
                        funding_interval = hours * 3600 * 1000  # 转换为毫秒
                except (ValueError, AttributeError, TypeError):
                    logger.warning(f"无法解析interval字段: {interval_str}")

            # 如果CCXT规范化字段没有,尝试从顶层获取 fundingInterval
            if not funding_interval:
                funding_interval = funding.get('fundingInterval')

            # 如果还是没有,尝试从info中获取(不同交易所字段名不同)
            if not funding_interval and 'info' in funding:
                info = funding['info']
                # Bybit: fundingIntervalHour (小时)
                # Gate: funding_interval (秒)
                # Bitget: fundingRateInterval (小时)
                # OKX: 没有,但CCXT已规范化到顶层interval

                # 尝试获取小时数
                interval_hours = info.get('fundingIntervalHour') or info.get('fundingIntervalHours') or info.get('fundingRateInterval')
                if interval_hours:
                    try:
                        hours = float(interval_hours)
                        if hours > 0:
                            funding_interval = int(hours * 3600 * 1000)
                    except (ValueError, TypeError):
                        logger.debug(f"无法解析interval_hours: {interval_hours}")

                # Gate的特殊情况: funding_interval是秒数
                if not funding_interval:
                    interval_seconds = info.get('funding_interval')
                    if interval_seconds:
                        try:
                            seconds = float(interval_seconds)
                            if seconds > 0:
                                funding_interval = int(seconds * 1000)
                        except (ValueError, TypeError):
                            logger.debug(f"无法解析interval_seconds: {interval_seconds}")

            # 如果仍然没有获取到,尝试通过历史数据计算
            # 这对于Binance等不返回interval的交易所很有用
            # 注意: 这是从交易所实际的历史结算时间计算的真实值,不是推算
            if not funding_interval:
                try:
                    history = self.exchange.fetch_funding_rate_history(futures_symbol, limit=2)
                    if len(history) >= 2:
                        # 计算最近两次资金费率的实际时间间隔
                        interval_ms = abs(history[0]['timestamp'] - history[1]['timestamp'])

                        # 验证间隔是否在合理范围内 (1小时到24小时)
                        # 主流交易所通常是4小时或8小时
                        if 3600000 <= interval_ms <= 86400000:  # 1h - 24h
                            funding_interval = interval_ms
                            interval_hours = interval_ms / 3600000
                            logger.debug(
                                f"从实际历史数据获取资金费率间隔: {funding_interval}ms ({interval_hours:.2f}小时) "
                                f"[时间戳: {history[1]['timestamp']} → {history[0]['timestamp']}]"
                            )
                        else:
                            logger.warning(
                                f"计算的间隔 {interval_ms}ms 超出合理范围,可能数据异常"
                            )
                except Exception as e:
                    logger.debug(f"无法通过历史数据计算间隔: {e}")

            return {
                'funding_rate': funding.get('fundingRate'),
                'next_funding_time': funding.get('fundingTimestamp'),
                'predicted_rate': funding.get('indicativeRate') or funding.get('fundingRate'),
                'funding_interval': funding_interval
            }
        except Exception as e:
            # 记录详细错误信息用于调试
            error_msg = str(e)
            # 常见的正常错误（币种不支持）使用DEBUG，其他错误使用WARNING
            if 'does not have market symbol' in error_msg or 'not found' in error_msg.lower():
                logger.debug(f"Symbol {symbol} not available for funding rate: {error_msg}")
            elif 'rate limit' in error_msg.lower():
                logger.warning(f"Rate limit hit for {symbol}: {error_msg}")
            else:
                # 其他未知错误，暂时用WARNING记录，方便调试
                logger.warning(f"Error fetching funding rate for {symbol}: {error_msg}")
            return {}

    def get_order_book(self, symbol: str, is_futures: bool = False, limit: int = 5) -> Dict[str, Any]:
        """
        获取订单簿
        返回: {
            'bids': [[price, amount], ...],
            'asks': [[price, amount], ...],
            'bid_depth': 买盘前N档总量,
            'ask_depth': 卖盘前N档总量
        }
        """
        try:
            if is_futures:
                symbol = self._convert_to_futures_symbol(symbol)

            orderbook = self.exchange.fetch_order_book(symbol, limit)
            bids = orderbook['bids'][:limit]
            asks = orderbook['asks'][:limit]

            bid_depth = sum([bid[1] for bid in bids])
            ask_depth = sum([ask[1] for ask in asks])

            return {
                'bids': bids,
                'asks': asks,
                'bid_depth': bid_depth,
                'ask_depth': ask_depth
            }
        except Exception as e:
            logger.error(f"Error fetching order book for {symbol}: {e}")
            return {'bids': [], 'asks': [], 'bid_depth': 0, 'ask_depth': 0}

    def get_balance(self) -> Dict[str, float]:
        """
        获取账户余额
        返回: {'USDT': 1000.0, 'BTC': 0.5, ...}
        """
        try:
            balance = self.exchange.fetch_balance()
            return balance.get('total', {})
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            return {}

    def get_account_info(self) -> Dict[str, Any]:
        """
        获取账户详细信息
        返回: {
            'balances': {'USDT': {'free': 1000, 'used': 100, 'total': 1100}, ...},
            'total_usdt': 总资产(USDT计价),
            'positions_count': 持仓数量,
            'timestamp': 查询时间戳
        }
        """
        try:
            from datetime import datetime
            
            # 获取余额
            balance = self.exchange.fetch_balance()
            
            # 整理余额信息
            balances = {}
            total_usdt = 0
            
            for currency, amount in balance.get('total', {}).items():
                if amount > 0:
                    free = balance.get('free', {}).get(currency, 0)
                    used = balance.get('used', {}).get(currency, 0)
                    
                    balances[currency] = {
                        'free': free,
                        'used': used,
                        'total': amount
                    }
                    
                    # 计算USDT价值
                    if currency == 'USDT':
                        total_usdt += amount
                    else:
                        # 尝试获取价格并计算价值
                        try:
                            symbol = f"{currency}/USDT"
                            ticker = self.exchange.fetch_ticker(symbol)
                            price = ticker.get('last', 0)
                            total_usdt += amount * price
                        except:
                            pass  # 如果无法获取价格，跳过
            
            # 获取持仓数量
            positions_count = 0
            try:
                positions = self.exchange.fetch_positions()
                positions_count = len([p for p in positions if float(p.get('contracts', 0)) != 0])
            except:
                pass
            
            return {
                'balances': balances,
                'total_usdt': round(total_usdt, 2),
                'positions_count': positions_count,
                'timestamp': int(datetime.now().timestamp() * 1000)
            }
        except Exception as e:
            logger.error(f"Error fetching account info: {e}")
            return {
                'balances': {},
                'total_usdt': 0,
                'positions_count': 0,
                'timestamp': 0
            }

    def get_positions(self) -> List[Dict[str, Any]]:
        """
        获取持仓（期货）
        """
        try:
            positions = self.exchange.fetch_positions()
            return [p for p in positions if float(p.get('contracts', 0)) != 0]
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return []

    def create_market_order(self, symbol: str, side: str, amount: float,
                           is_futures: bool = False, cost: Optional[float] = None, 
                           reduce_only: bool = False) -> Optional[Dict[str, Any]]:
        """
        创建市价单
        amount: 币的数量
        cost: 订单金额（USDT），仅用于现货买入订单
        reduce_only: 是否仅平仓（True=平仓，False=开仓）
        """
        try:
            if is_futures:
                symbol = self._convert_to_futures_symbol(symbol)

            # cost方式仅用于现货买入（期货不支持）
            if cost is not None and cost >= 5 and not is_futures and side == 'buy':
                # 现货市价买入可以按金额下单
                params = {'createMarketBuyOrderRequiresPrice': False}
                order = self.exchange.create_market_buy_order_with_cost(symbol, cost, params)
            else:
                # 期货订单或卖出订单使用amount
                params = {}
                if is_futures:
                    # Bitget期货单向持仓模式
                    # 在CCXT中，Bitget期货使用side来表示开平仓+方向
                    # 但在单向持仓模式下，需要明确指定是开仓还是平仓
                    # 开仓：使用'open'参数，或者默认就是开仓
                    params = {
                        'timeInForce': 'IOC',  # 立即成交或取消（市价单推荐）
                    }
                    if reduce_only:
                        params['reduceOnly'] = True  # 平仓模式
                
                order = self.exchange.create_order(
                    symbol=symbol,
                    type='market',
                    side=side,
                    amount=amount,
                    params=params
                )
            logger.info(f"Market order created: {order}")
            return order
        except Exception as e:
            logger.error(f"Error creating market order: {e}")
            return None

    def create_limit_order(self, symbol: str, side: str, amount: float, price: float,
                          is_futures: bool = False) -> Optional[Dict[str, Any]]:
        """
        创建限价单
        """
        try:
            if is_futures:
                symbol = self._convert_to_futures_symbol(symbol)

            params = {}
            if is_futures:
                # Bitget期货限价单参数
                params = {
                    'timeInForce': 'GTC',  # Good Till Cancel
                }

            order = self.exchange.create_order(
                symbol=symbol,
                type='limit',
                side=side,
                amount=amount,
                price=price,
                params=params
            )
            logger.info(f"Limit order created: {order}")
            return order
        except Exception as e:
            logger.error(f"Error creating limit order: {e}")
            return None

    def get_trading_fees(self, symbol: str) -> Dict[str, float]:
        """
        获取交易手续费
        返回: {'maker': 0.0001, 'taker': 0.0004}
        """
        try:
            markets = self.exchange.load_markets()
            market = markets.get(symbol, {})
            return {
                'maker': market.get('maker', 0.001),
                'taker': market.get('taker', 0.001)
            }
        except Exception as e:
            logger.error(f"Error fetching trading fees: {e}")
            return {'maker': 0.001, 'taker': 0.001}

    @abstractmethod
    def _convert_to_futures_symbol(self, spot_symbol: str) -> str:
        """
        将现货symbol转换为期货symbol
        例如: BTC/USDT -> BTC/USDT:USDT (Binance格式)
        """
        pass

    def test_connection(self) -> bool:
        """测试连接"""
        try:
            self.exchange.fetch_balance()
            logger.info(f"{self.__class__.__name__} connection test successful")
            return True
        except Exception as e:
            logger.error(f"{self.__class__.__name__} connection test failed: {e}")
            return False
