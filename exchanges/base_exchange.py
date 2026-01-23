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
            
            # 尝试获取资金费率间隔（毫秒）
            funding_interval = funding.get('fundingInterval')
            
            # 如果没有fundingInterval，尝试从info中获取
            if not funding_interval and 'info' in funding:
                info = funding['info']
                # 不同交易所的字段名可能不同
                funding_interval = info.get('fundingInterval') or info.get('funding_interval') or info.get('fundingIntervalHours')
            
            return {
                'funding_rate': funding.get('fundingRate'),
                'next_funding_time': funding.get('fundingTimestamp'),
                'predicted_rate': funding.get('indicativeRate') or funding.get('fundingRate'),
                'funding_interval': funding_interval
            }
        except Exception as e:
            logger.error(f"Error fetching funding rate for {symbol}: {e}")
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
                           is_futures: bool = False) -> Optional[Dict[str, Any]]:
        """
        创建市价单
        """
        try:
            if is_futures:
                symbol = self._convert_to_futures_symbol(symbol)

            order = self.exchange.create_order(
                symbol=symbol,
                type='market',
                side=side,  # 'buy' or 'sell'
                amount=amount
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

            order = self.exchange.create_order(
                symbol=symbol,
                type='limit',
                side=side,
                amount=amount,
                price=price
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
