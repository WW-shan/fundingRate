"""
订单管理器
负责订单的创建、跟踪、更新
"""
import os
from typing import Dict, List, Any, Optional
from datetime import datetime
from loguru import logger
from database import DatabaseManager
from exchanges import (
    BinanceAdapter, OKXAdapter, BybitAdapter,
    GateAdapter, BitgetAdapter
)


class OrderManager:
    """订单管理器"""

    def __init__(self, db_manager: DatabaseManager, exchanges: Dict[str, Any]):
        self.db = db_manager
        self.exchanges = exchanges
        self.enable_trading = os.getenv('ENABLE_TRADING', 'False').lower() == 'true'

        if not self.enable_trading:
            logger.warning("⚠️ Trading is DISABLED - Orders will be simulated only")

    def create_order(self, exchange: str, symbol: str, side: str, amount: float,
                    order_type: str = 'market', price: Optional[float] = None,
                    is_futures: bool = False, strategy_id: Optional[int] = None,
                    strategy_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        创建订单
        exchange: 交易所名称
        symbol: 交易对
        side: 'buy' or 'sell'
        amount: 数量
        order_type: 'market' or 'limit'
        price: 限价单价格
        is_futures: 是否是期货订单
        strategy_id: 策略ID
        strategy_type: 策略类型
        """
        try:
            if not self.enable_trading:
                # 模拟模式
                logger.info(f"[SIMULATED] {exchange} {side} {amount} {symbol} {'(futures)' if is_futures else '(spot)'}")
                order_id = f"SIM_{int(datetime.now().timestamp() * 1000)}"
                order_data = {
                    'id': order_id,
                    'symbol': symbol,
                    'side': side,
                    'type': order_type,
                    'price': price or 0,
                    'amount': amount,
                    'filled': amount,
                    'status': 'closed',
                    'timestamp': int(datetime.now().timestamp() * 1000)
                }
            else:
                # 实际交易
                exchange_adapter = self.exchanges.get(exchange.lower())
                if not exchange_adapter:
                    logger.error(f"Exchange {exchange} not found")
                    return None

                if order_type == 'market':
                    order_data = exchange_adapter.create_market_order(
                        symbol=symbol,
                        side=side,
                        amount=amount,
                        is_futures=is_futures
                    )
                elif order_type == 'limit':
                    order_data = exchange_adapter.create_limit_order(
                        symbol=symbol,
                        side=side,
                        amount=amount,
                        price=price,
                        is_futures=is_futures
                    )
                else:
                    logger.error(f"Unsupported order type: {order_type}")
                    return None

                if not order_data:
                    logger.error(f"Failed to create order on {exchange}")
                    return None

            # 记录订单到数据库
            self.db.execute_insert(
                """
                INSERT INTO orders (strategy_id, strategy_type, exchange, symbol, side,
                                  order_type, price, amount, filled, status, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    strategy_type,
                    exchange,
                    symbol,
                    side,
                    order_type,
                    order_data.get('price', 0),
                    order_data.get('amount', 0),
                    order_data.get('filled', 0),
                    order_data.get('status', 'open'),
                    order_data.get('id', '')
                )
            )

            logger.info(f"✅ Order created: {exchange} {side} {amount} {symbol}")
            return order_data

        except Exception as e:
            logger.error(f"Error creating order: {e}")
            return None

    def create_spot_futures_pair(self, exchange: str, symbol: str, amount: float,
                                strategy_id: int, strategy_type: str) -> Dict[str, Any]:
        """
        创建现货-期货对冲订单
        买入现货 + 开空单
        """
        results = {
            'spot_order': None,
            'futures_order': None,
            'success': False
        }

        try:
            # 1. 买入现货
            spot_order = self.create_order(
                exchange=exchange,
                symbol=symbol,
                side='buy',
                amount=amount,
                order_type='market',
                is_futures=False,
                strategy_id=strategy_id,
                strategy_type=strategy_type
            )

            if not spot_order:
                logger.error("Failed to create spot order")
                return results

            results['spot_order'] = spot_order

            # 2. 开期货空单
            futures_order = self.create_order(
                exchange=exchange,
                symbol=symbol,
                side='sell',
                amount=amount,
                order_type='market',
                is_futures=True,
                strategy_id=strategy_id,
                strategy_type=strategy_type
            )

            if not futures_order:
                logger.error("Failed to create futures order - spot order already executed!")
                # TODO: 应该平掉现货订单以避免风险
                return results

            results['futures_order'] = futures_order
            results['success'] = True

            logger.info(f"✅ Spot-Futures pair created successfully")
            return results

        except Exception as e:
            logger.error(f"Error creating spot-futures pair: {e}")
            return results

    def create_cross_exchange_pair(self, long_exchange: str, short_exchange: str,
                                  symbol: str, amount: float,
                                  strategy_id: int, strategy_type: str) -> Dict[str, Any]:
        """
        创建跨交易所对冲订单
        在long_exchange做多，在short_exchange做空
        """
        results = {
            'long_order': None,
            'short_order': None,
            'success': False
        }

        try:
            # 1. 在long_exchange做多（期货）
            long_order = self.create_order(
                exchange=long_exchange,
                symbol=symbol,
                side='buy',
                amount=amount,
                order_type='market',
                is_futures=True,
                strategy_id=strategy_id,
                strategy_type=strategy_type
            )

            if not long_order:
                logger.error(f"Failed to create long order on {long_exchange}")
                return results

            results['long_order'] = long_order

            # 2. 在short_exchange做空（期货）
            short_order = self.create_order(
                exchange=short_exchange,
                symbol=symbol,
                side='sell',
                amount=amount,
                order_type='market',
                is_futures=True,
                strategy_id=strategy_id,
                strategy_type=strategy_type
            )

            if not short_order:
                logger.error(f"Failed to create short order on {short_exchange}")
                # TODO: 应该平掉long订单
                return results

            results['short_order'] = short_order
            results['success'] = True

            logger.info(f"✅ Cross-exchange pair created: {long_exchange} (long) & {short_exchange} (short)")
            return results

        except Exception as e:
            logger.error(f"Error creating cross-exchange pair: {e}")
            return results

    def close_spot_futures_pair(self, exchange: str, symbol: str, amount: float,
                               strategy_id: int) -> Dict[str, Any]:
        """
        平仓现货-期货对冲
        卖出现货 + 平期货空单
        """
        results = {
            'spot_order': None,
            'futures_order': None,
            'success': False
        }

        try:
            # 1. 卖出现货
            spot_order = self.create_order(
                exchange=exchange,
                symbol=symbol,
                side='sell',
                amount=amount,
                order_type='market',
                is_futures=False,
                strategy_id=strategy_id,
                strategy_type='close_position'
            )

            if not spot_order:
                logger.error("Failed to close spot position")
                return results

            results['spot_order'] = spot_order

            # 2. 平期货空单（买入平仓）
            futures_order = self.create_order(
                exchange=exchange,
                symbol=symbol,
                side='buy',
                amount=amount,
                order_type='market',
                is_futures=True,
                strategy_id=strategy_id,
                strategy_type='close_position'
            )

            if not futures_order:
                logger.error("Failed to close futures position")
                return results

            results['futures_order'] = futures_order
            results['success'] = True

            logger.info(f"✅ Spot-Futures pair closed successfully")
            return results

        except Exception as e:
            logger.error(f"Error closing spot-futures pair: {e}")
            return results

    def close_cross_exchange_pair(self, long_exchange: str, short_exchange: str,
                                 symbol: str, amount: float, strategy_id: int) -> Dict[str, Any]:
        """
        平仓跨交易所对冲
        平掉long_exchange的多单，平掉short_exchange的空单
        """
        results = {
            'long_order': None,
            'short_order': None,
            'success': False
        }

        try:
            # 1. 平long_exchange的多单（卖出平仓）
            long_order = self.create_order(
                exchange=long_exchange,
                symbol=symbol,
                side='sell',
                amount=amount,
                order_type='market',
                is_futures=True,
                strategy_id=strategy_id,
                strategy_type='close_position'
            )

            if not long_order:
                logger.error(f"Failed to close long position on {long_exchange}")
                return results

            results['long_order'] = long_order

            # 2. 平short_exchange的空单（买入平仓）
            short_order = self.create_order(
                exchange=short_exchange,
                symbol=symbol,
                side='buy',
                amount=amount,
                order_type='market',
                is_futures=True,
                strategy_id=strategy_id,
                strategy_type='close_position'
            )

            if not short_order:
                logger.error(f"Failed to close short position on {short_exchange}")
                return results

            results['short_order'] = short_order
            results['success'] = True

            logger.info(f"✅ Cross-exchange pair closed successfully")
            return results

        except Exception as e:
            logger.error(f"Error closing cross-exchange pair: {e}")
            return results

    def get_order_history(self, strategy_id: Optional[int] = None,
                         limit: int = 100) -> List[Dict[str, Any]]:
        """获取订单历史"""
        if strategy_id:
            return self.db.execute_query(
                """
                SELECT * FROM orders
                WHERE strategy_id = ?
                ORDER BY create_time DESC
                LIMIT ?
                """,
                (strategy_id, limit)
            )
        else:
            return self.db.execute_query(
                """
                SELECT * FROM orders
                ORDER BY create_time DESC
                LIMIT ?
                """,
                (limit,)
            )
