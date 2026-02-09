"""
订单管理器
负责订单的创建、跟踪、更新
"""
import os
import time
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

    def _check_order_book_depth(self, exchange: str, symbol: str, side: str, amount: float, 
                                is_futures: bool = False) -> Dict[str, Any]:
        """
        检查订单簿深度，预估滑点
        返回: {'sufficient': bool, 'estimated_price': float, 'slippage_pct': float}
        """
        try:
            exchange_adapter = self.exchanges.get(exchange.lower())
            if not exchange_adapter:
                return {'sufficient': False, 'estimated_price': 0, 'slippage_pct': 0}
            
            # 获取订单簿深度
            orderbook = exchange_adapter.get_order_book(symbol, is_futures=is_futures, limit=20)
            
            if not orderbook or not orderbook.get('bids') or not orderbook.get('asks'):
                logger.warning(f"无法获取 {exchange} {symbol} 的订单簿")
                return {'sufficient': False, 'estimated_price': 0, 'slippage_pct': 0}
            
            # 根据买卖方向选择对应的盘口
            orders = orderbook['asks'] if side == 'buy' else orderbook['bids']
            best_price = orders[0][0] if orders else 0
            
            # 计算需要的深度
            cumulative_amount = 0
            total_cost = 0
            
            for price, order_amount in orders:
                if cumulative_amount >= amount:
                    break
                fill_amount = min(order_amount, amount - cumulative_amount)
                total_cost += fill_amount * price
                cumulative_amount += fill_amount
            
            if cumulative_amount < amount * 0.8:  # 如果连80%都填不满
                logger.warning(f"深度不足: {exchange} {symbol} 需要 {amount}，只有 {cumulative_amount}")
                return {'sufficient': False, 'estimated_price': 0, 'slippage_pct': 0}
            
            # 计算平均成交价和滑点
            avg_price = total_cost / cumulative_amount if cumulative_amount > 0 else 0
            slippage_pct = abs(avg_price - best_price) / best_price if best_price > 0 else 0
            
            return {
                'sufficient': True,
                'estimated_price': avg_price,
                'slippage_pct': slippage_pct,
                'available_amount': cumulative_amount
            }
            
        except Exception as e:
            logger.error(f"检查订单簿深度异常: {e}")
            return {'sufficient': False, 'estimated_price': 0, 'slippage_pct': 0}

    def _wait_for_order_filled(self, exchange: str, order_id: str, symbol: str, 
                               is_futures: bool = False, timeout: int = 30) -> Dict[str, Any]:
        """
        等待订单完全成交
        返回: {'filled': bool, 'filled_amount': float, 'status': str}
        """
        try:
            if not self.enable_trading:
                # 模拟模式直接返回成功
                return {'filled': True, 'filled_amount': 0, 'status': 'closed'}
            
            exchange_adapter = self.exchanges.get(exchange.lower())
            if not exchange_adapter:
                return {'filled': False, 'filled_amount': 0, 'status': 'unknown'}
            
            start_time = time.time()
            
            # 尝试多种symbol格式
            symbols_to_try = [symbol]
            if is_futures:
                # 期货合约可能需要不同的symbol格式
                if ':' not in symbol:
                    symbols_to_try.append(f"{symbol}:USDT")
            
            while time.time() - start_time < timeout:
                order = None
                last_error = None
                
                # 尝试不同的symbol格式查询订单
                for try_symbol in symbols_to_try:
                    try:
                        order = exchange_adapter.exchange.fetch_order(order_id, try_symbol)
                        break  # 成功，跳出循环
                    except Exception as e:
                        last_error = e
                        error_msg = str(e).lower()
                        # 如果是symbol不存在的错误，尝试下一个格式
                        if 'does not have market' in error_msg or 'symbol' in error_msg:
                            continue
                        # 其他错误也尝试下一个
                        continue
                
                # 如果所有symbol格式都失败，尝试直接用order_id查询（不传symbol）
                if not order:
                    try:
                        order = exchange_adapter.exchange.fetch_order(order_id)
                    except Exception as e:
                        last_error = e
                
                # 如果成功获取到订单
                if order:
                    try:
                        status = order.get('status')
                        filled = order.get('filled', 0)
                        
                        if status == 'closed' or status == 'filled':
                            return {'filled': True, 'filled_amount': filled, 'status': status}
                        elif status == 'canceled' or status == 'expired':
                            return {'filled': False, 'filled_amount': filled, 'status': status}
                        
                        # 订单还在执行中，等待1秒后重试
                        time.sleep(1)
                        continue
                    except Exception as e:
                        logger.debug(f"处理订单状态时出错: {e}")
                
                # 如果无法获取订单，尝试从成交历史查询
                if last_error:
                    error_msg = str(last_error).lower()
                    if 'could not find order' in error_msg or 'order not found' in error_msg or 'does not have market' in error_msg:
                        # 对于市价单，如果查询不到，大概率是已经快速成交了
                        logger.info(f"无法查询订单 {order_id}，假定市价单已快速成交")
                        return {'filled': True, 'filled_amount': 0, 'status': 'closed'}
                    else:
                        logger.warning(f"查询订单状态失败: {last_error}")
                
                time.sleep(1)
            
            # 超时
            logger.warning(f"订单等待超时: {order_id}")
            return {'filled': False, 'filled_amount': 0, 'status': 'timeout'}
            
        except Exception as e:
            logger.error(f"等待订单成交异常: {e}")
            return {'filled': False, 'filled_amount': 0, 'status': 'error'}

    def update_order_status(self, order_id: str, exchange: str, symbol: str, is_futures: bool = False) -> bool:
        """更新订单状态到数据库"""
        try:
            if not self.enable_trading:
                return True
            
            exchange_adapter = self.exchanges.get(exchange.lower())
            if not exchange_adapter:
                return False
            
            order = None
            # 尝试多种symbol格式
            symbols_to_try = [symbol]
            if is_futures and ':' not in symbol:
                symbols_to_try.append(f"{symbol}:USDT")
            
            for try_symbol in symbols_to_try:
                try:
                    order = exchange_adapter.exchange.fetch_order(order_id, try_symbol)
                    break
                except Exception as e:
                    error_msg = str(e).lower()
                    if 'does not have market' in error_msg or 'symbol' in error_msg:
                        continue
                    # 其他错误也尝试
                    continue
            
            # 尝试不传symbol查询
            if not order:
                try:
                    order = exchange_adapter.exchange.fetch_order(order_id)
                except Exception as e:
                    error_msg = str(e).lower()
                    # 如果找不到订单，假定已成交（市价单通常很快）
                    if 'could not find order' in error_msg or 'order not found' in error_msg or 'does not have market' in error_msg:
                        logger.info(f"无法查询订单 {order_id}，假定已成交")
                        return True
                    logger.error(f"更新订单状态失败: {e}")
                    return False
            
            if order:
                self.db.execute_update(
                    """
                    UPDATE orders 
                    SET status = ?, filled = ?, price = ?
                    WHERE order_id = ?
                    """,
                    (
                        order.get('status', 'unknown'),
                        order.get('filled', 0),
                        order.get('average', order.get('price', 0)),
                        order_id
                    )
                )
                
                return True
            
        except Exception as e:
            logger.error(f"更新订单状态失败: {e}")
            return False

    def _rollback_order(self, exchange: str, symbol: str, side: str, amount: float, is_futures: bool) -> bool:
        """回滚订单（平掉已开的仓位）"""
        try:
            reverse_side = 'sell' if side == 'buy' else 'buy'
            logger.warning(f"🔄 回滚订单: {exchange} {reverse_side} {amount} {symbol}")
            
            rollback_order = self.create_order(
                exchange=exchange,
                symbol=symbol,
                side=reverse_side,
                amount=amount,
                order_type='market',
                is_futures=is_futures,
                strategy_id=None,
                strategy_type='rollback',
                check_depth=False  # 回滚时不检查深度，直接执行
            )
            
            if rollback_order:
                logger.info(f"✅ 订单回滚成功")
                return True
            else:
                logger.error(f"❌ 订单回滚失败！请立即手动处理！")
                return False
        except Exception as e:
            logger.error(f"订单回滚异常: {e}")
            return False

    def create_order(self, exchange: str, symbol: str, side: str, amount: float,
                    order_type: str = 'market', price: Optional[float] = None,
                    is_futures: bool = False, strategy_id: Optional[int] = None,
                    strategy_type: Optional[str] = None, retry: int = 3,
                    check_depth: bool = True) -> Optional[Dict[str, Any]]:
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
        retry: 重试次数
        check_depth: 是否检查深度
        """
        # 在实际交易模式下检查深度
        if self.enable_trading and check_depth and strategy_type != 'rollback':
            depth_check = self._check_order_book_depth(exchange, symbol, side, amount, is_futures)
            
            if not depth_check['sufficient']:
                logger.warning(f"订单簿深度不足，取消订单: {exchange} {symbol}")
                return None
            
            slippage = depth_check['slippage_pct']
            if slippage > 0.01:  # 滑点超过1%
                logger.warning(f"预估滑点过大: {slippage*100:.2f}%")
        
        last_error = None
        for attempt in range(retry):
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
                        # 对于策略3，传递position_size作为cost参数（确保满足最小金额要求）
                        cost = None
                        if strategy_type == 'directional_funding' and strategy_id:
                            # 从数据库获取position_size
                            positions = self.db.execute_query(
                                "SELECT position_size FROM positions WHERE id = ?", (strategy_id,)
                            )
                            if positions and positions[0]['position_size']:
                                cost = float(positions[0]['position_size'])
                        
                        order_data = exchange_adapter.create_market_order(
                            symbol=symbol,
                            side=side,
                            amount=amount,
                            is_futures=is_futures,
                            cost=cost
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
                        if attempt < retry - 1:
                            logger.warning(f"订单创建失败，重试 {attempt + 1}/{retry}...")
                            time.sleep(0.5)  # 等待0.5秒后重试
                            continue
                        logger.error(f"Failed to create order on {exchange} after {retry} attempts")
                        return None

                # 提取手续费信息
                fee_info = order_data.get('fee', {})
                if fee_info:
                    order_data['fee_cost'] = float(fee_info.get('cost') or 0)
                    order_data['fee_currency'] = fee_info.get('currency', 'USDT')
                else:
                    # 如果没有fee信息，估算手续费（0.05% taker）
                    filled_amount = float(order_data.get('filled') or 0)
                    avg_price = float(order_data.get('average') or order_data.get('price') or 0)
                    if filled_amount > 0 and avg_price > 0:
                        order_data['fee_cost'] = filled_amount * avg_price * 0.0005
                        order_data['fee_currency'] = 'USDT'
                    else:
                        order_data['fee_cost'] = 0
                        order_data['fee_currency'] = 'USDT'
                
                # 记录订单到数据库（包含手续费信息）
                self.db.execute_insert(
                    """
                    INSERT INTO orders (strategy_id, strategy_type, exchange, symbol, side,
                                      order_type, price, amount, filled, status, order_id,
                                      fee_cost, fee_currency)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        order_data.get('id', ''),
                        order_data.get('fee_cost', 0),
                        order_data.get('fee_currency', 'USDT')
                    )
                )

                logger.info(f"✅ Order created: {exchange} {side} {amount} {symbol}, Fee: {order_data.get('fee_cost', 0):.4f} {order_data.get('fee_currency', 'USDT')}")
                
                # 实际交易模式下等待订单成交确认
                if self.enable_trading and order_type == 'market':
                    filled_status = self._wait_for_order_filled(
                        exchange=exchange,
                        order_id=order_data.get('id', ''),
                        symbol=symbol,
                        is_futures=is_futures,
                        timeout=30
                    )
                    
                    if not filled_status['filled']:
                        logger.warning(f"订单未完全成交: {filled_status['status']}")
                        # 更新数据库中的订单状态
                        self.update_order_status(order_data.get('id', ''), exchange, symbol, is_futures)
                    else:
                        logger.info(f"✅ 订单已完全成交: {filled_status['filled_amount']}")
                
                return order_data

            except Exception as e:
                last_error = e
                if attempt < retry - 1:
                    logger.warning(f"订单创建异常，重试 {attempt + 1}/{retry}: {e}")
                    time.sleep(0.5)
                    continue
                else:
                    logger.error(f"Error creating order after {retry} attempts: {e}")
                    return None
        
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
            'success': False,
            'total_fee': 0
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
                # 回滚现货订单
                logger.warning("🚨 尝试回滚现货订单...")
                self._rollback_order(
                    exchange=exchange,
                    symbol=symbol,
                    side='buy',  # 现货是买入的，回滚需要卖出
                    amount=amount,
                    is_futures=False
                )
                return results

            results['futures_order'] = futures_order
            results['success'] = True
            
            # 从数据库查询手续费
            total_fee = 0
            if spot_order and spot_order.get('id'):
                fee_data = self.db.execute_query(
                    "SELECT fee_cost FROM orders WHERE order_id = ? AND exchange = ?",
                    (spot_order['id'], exchange)
                )
                if fee_data and fee_data[0]['fee_cost']:
                    total_fee += float(fee_data[0]['fee_cost'])
            
            if futures_order and futures_order.get('id'):
                fee_data = self.db.execute_query(
                    "SELECT fee_cost FROM orders WHERE order_id = ? AND exchange = ?",
                    (futures_order['id'], exchange)
                )
                if fee_data and fee_data[0]['fee_cost']:
                    total_fee += float(fee_data[0]['fee_cost'])
            
            results['total_fee'] = total_fee

            logger.info(f"✅ Spot-Futures pair created successfully, Total Fee: ${total_fee:.4f}")
            return results

        except Exception as e:
            logger.error(f"Error creating spot-futures pair: {e}")
            # 如果现货订单已执行但期货订单失败，尝试回滚
            if results['spot_order'] and not results['futures_order']:
                logger.warning("🚨 异常后尝试回滚现货订单...")
                self._rollback_order(
                    exchange=exchange,
                    symbol=symbol,
                    side='buy',
                    amount=amount,
                    is_futures=False
                )
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
            'success': False,
            'total_fee': 0
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
                # 回滚多单
                logger.warning("🚨 尝试回滚多单...")
                self._rollback_order(
                    exchange=long_exchange,
                    symbol=symbol,
                    side='buy',  # 多单是买入的，回滚需要卖出
                    amount=amount,
                    is_futures=True
                )
                return results

            results['short_order'] = short_order
            results['success'] = True
            
            # 从数据库查询手续费
            total_fee = 0
            if long_order and long_order.get('id'):
                fee_data = self.db.execute_query(
                    "SELECT fee_cost FROM orders WHERE order_id = ? AND exchange = ?",
                    (long_order['id'], long_exchange)
                )
                if fee_data and fee_data[0]['fee_cost']:
                    total_fee += float(fee_data[0]['fee_cost'])
            
            if short_order and short_order.get('id'):
                fee_data = self.db.execute_query(
                    "SELECT fee_cost FROM orders WHERE order_id = ? AND exchange = ?",
                    (short_order['id'], short_exchange)
                )
                if fee_data and fee_data[0]['fee_cost']:
                    total_fee += float(fee_data[0]['fee_cost'])
            
            results['total_fee'] = total_fee

            logger.info(f"✅ Cross-exchange pair created: {long_exchange} (long) & {short_exchange} (short), Total Fee: ${total_fee:.4f}")
            return results

        except Exception as e:
            logger.error(f"Error creating cross-exchange pair: {e}")
            # 如果多单已执行但空单失败，尝试回滚
            if results['long_order'] and not results['short_order']:
                logger.warning("🚨 异常后尝试回滚多单...")
                self._rollback_order(
                    exchange=long_exchange,
                    symbol=symbol,
                    side='buy',
                    amount=amount,
                    is_futures=True
                )
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

    def sync_pending_orders(self) -> int:
        """
        同步所有未完成订单的状态
        返回: 更新的订单数量
        """
        try:
            # 查询所有未完成的订单
            pending_orders = self.db.execute_query(
                """
                SELECT * FROM orders 
                WHERE status IN ('open', 'pending', 'partially_filled')
                """
            )
            
            updated_count = 0
            
            for order in pending_orders:
                try:
                    success = self.update_order_status(
                        order_id=order['order_id'],
                        exchange=order['exchange'],
                        symbol=order['symbol']
                    )
                    if success:
                        updated_count += 1
                except Exception as e:
                    logger.error(f"同步订单 {order['order_id']} 失败: {e}")
                    continue
            
            if updated_count > 0:
                logger.info(f"✅ 同步了 {updated_count} 个订单状态")
            
            return updated_count
            
        except Exception as e:
            logger.error(f"批量同步订单状态失败: {e}")
            return 0
