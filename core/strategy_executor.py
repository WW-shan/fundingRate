"""
策略执行引擎
接收机会并决定是否执行，管理持仓生命周期
"""
import time
import threading
import json
from typing import Dict, List, Any, Optional
from datetime import datetime
from loguru import logger
from config import ConfigManager
from database import DatabaseManager
from core.risk_manager import RiskManager
from core.order_manager import OrderManager


class StrategyExecutor:
    """策略执行引擎"""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager,
                 risk_manager: RiskManager, order_manager: OrderManager):
        self.config = config_manager
        self.db = db_manager
        self.risk_manager = risk_manager
        self.order_manager = order_manager
        self.running = False
        self.paused = False  # 暂停状态
        self.pending_opportunities = []  # 待处理的机会队列
        self.execution_callbacks = []  # 执行回调

    def start(self):
        """启动策略执行器"""
        logger.info("Starting strategy executor...")
        self.running = True

        # 启动执行线程
        threading.Thread(target=self._execution_loop, daemon=True).start()

        # 启动持仓监控线程
        threading.Thread(target=self._position_monitoring_loop, daemon=True).start()

        logger.info("Strategy executor started")

    def stop(self):
        """停止策略执行器"""
        logger.info("Stopping strategy executor...")
        self.running = False

    def register_callback(self, callback):
        """注册执行事件回调"""
        self.execution_callbacks.append(callback)

    def set_paused(self, paused: bool):
        """设置暂停状态"""
        self.paused = paused
        status = "paused" if paused else "resumed"
        logger.info(f"Strategy executor {status}")

    def is_paused(self) -> bool:
        """检查是否暂停"""
        return self.paused

    def submit_opportunity(self, opportunity: Dict[str, Any]):
        """提交套利机会"""
        # 检查执行模式
        strategy_type = opportunity['type']
        risk_level = opportunity['risk_level']

        # 获取配置
        if strategy_type == 'funding_rate_cross_exchange':
            pair_config = self.config.get_pair_config(opportunity['symbol'])
            execution_mode = pair_config.get('s1_execution_mode', 'auto')
        elif strategy_type == 'funding_rate_spot_futures':
            pair_config = self.config.get_pair_config(opportunity['symbol'], opportunity['exchange'])
            execution_mode = pair_config.get('s2a_execution_mode', 'auto')
        elif strategy_type == 'basis_arbitrage':
            execution_mode = 'manual'  # 基差套利固定为手动模式
        elif strategy_type == 'directional_funding':
            execution_mode = 'auto'  # 策略3默认自动执行
        else:
            execution_mode = 'manual'

        # 如果是自动模式且风险等级低，直接执行
        if execution_mode == 'auto' and risk_level == 'low':
            self.pending_opportunities.append(opportunity)
            logger.info(f"Auto-executing opportunity: {opportunity['symbol']} - {strategy_type}")
        else:
            # 需要人工确认，触发回调通知
            logger.info(f"Opportunity requires manual confirmation: {opportunity['symbol']} - {strategy_type}")
            self._trigger_callback('opportunity_found', opportunity)

    def execute_opportunity(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """执行套利机会"""
        try:
            # 风控检查
            risk_check = self.risk_manager.check_pre_trade_risk(opportunity)

            if not risk_check['passed']:
                logger.warning(f"Risk check failed: {risk_check['reason']}")
                self._trigger_callback('execution_failed', {
                    'opportunity': opportunity,
                    'reason': risk_check['reason']
                })
                return {'success': False, 'error': risk_check['reason']}

            # 调整仓位（如果需要）
            adjusted_size = risk_check['adjusted_position_size']
            if adjusted_size != opportunity['position_size']:
                logger.info(f"Position size adjusted: {opportunity['position_size']} -> {adjusted_size}")
                opportunity['position_size'] = adjusted_size

            # 根据策略类型执行
            strategy_type = opportunity['type']

            if strategy_type == 'funding_rate_cross_exchange':
                result = self._execute_cross_exchange_funding(opportunity)
            elif strategy_type == 'funding_rate_spot_futures':
                result = self._execute_spot_futures_funding(opportunity)
            elif strategy_type == 'basis_arbitrage':
                result = self._execute_basis_arbitrage(opportunity)
            elif strategy_type == 'directional_funding':
                result = self._execute_directional_strategy(opportunity)
            else:
                logger.error(f"Unknown strategy type: {strategy_type}")
                return {'success': False, 'error': f'未知的策略类型: {strategy_type}'}
            
            return result

        except Exception as e:
            logger.error(f"Error executing opportunity: {e}")
            return {'success': False, 'error': str(e)}

    def _execute_cross_exchange_funding(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """执行跨交易所资金费率套利"""
        try:
            symbol = opportunity['symbol']
            long_exchange = opportunity['long_exchange']
            short_exchange = opportunity['short_exchange']
            position_size = opportunity['position_size']

            # 计算交易数量（BTC数量）
            long_price = opportunity['long_entry_price']
            amount = position_size / long_price

            # 创建持仓记录
            entry_details = {
                'long_exchange': long_exchange,
                'short_exchange': short_exchange,
                'long_price': long_price,
                'short_price': opportunity['short_entry_price'],
                'funding_diff': opportunity['funding_diff'],
                'expected_return': opportunity['expected_return']
            }

            position_id = self.db.execute_insert(
                """
                INSERT INTO positions (strategy_type, symbol, exchanges, entry_details,
                                     position_size, current_pnl, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    'funding_rate_cross_exchange',
                    symbol,
                    json.dumps([long_exchange, short_exchange]),
                    json.dumps(entry_details),
                    position_size,
                    0,
                    'open'
                )
            )

            # 执行订单
            orders = self.order_manager.create_cross_exchange_pair(
                long_exchange=long_exchange,
                short_exchange=short_exchange,
                symbol=symbol,
                amount=amount,
                strategy_id=position_id,
                strategy_type='funding_rate_cross_exchange'
            )

            if not orders['success']:
                # 订单失败，更新持仓状态
                self.db.execute_update(
                    "UPDATE positions SET status = 'failed' WHERE id = ?",
                    (position_id,)
                )
                logger.error("Failed to execute cross-exchange orders")
                return {'success': False, 'error': '订单执行失败'}

            logger.info(f"✅ Cross-exchange funding arbitrage executed: Position #{position_id}")

            # 触发回调
            self._trigger_callback('position_opened', {
                'position_id': position_id,
                'opportunity': opportunity,
                'orders': orders
            })

            return {'success': True, 'position_id': position_id}

        except Exception as e:
            logger.error(f"Error executing cross-exchange funding: {e}")
            return {'success': False, 'error': str(e)}

    def _execute_spot_futures_funding(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """执行现货-期货资金费率套利"""
        try:
            symbol = opportunity['symbol']
            exchange = opportunity['exchange']
            position_size = opportunity['position_size']

            # 计算交易数量
            spot_price = opportunity['spot_price']
            amount = position_size / spot_price

            # 创建持仓记录
            entry_details = {
                'exchange': exchange,
                'spot_price': spot_price,
                'futures_price': opportunity['futures_price'],
                'basis': opportunity['basis'],
                'funding_rate': opportunity['annual_funding_rate'],
                'expected_return': opportunity['expected_return']
            }

            position_id = self.db.execute_insert(
                """
                INSERT INTO positions (strategy_type, symbol, exchanges, entry_details,
                                     position_size, current_pnl, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    'funding_rate_spot_futures',
                    symbol,
                    json.dumps([exchange]),
                    json.dumps(entry_details),
                    position_size,
                    0,
                    'open'
                )
            )

            # 执行订单
            orders = self.order_manager.create_spot_futures_pair(
                exchange=exchange,
                symbol=symbol,
                amount=amount,
                strategy_id=position_id,
                strategy_type='funding_rate_spot_futures'
            )

            if not orders['success']:
                self.db.execute_update(
                    "UPDATE positions SET status = 'failed' WHERE id = ?",
                    (position_id,)
                )
                logger.error("Failed to execute spot-futures orders")
                return {'success': False, 'error': '订单执行失败'}

            logger.info(f"✅ Spot-futures funding arbitrage executed: Position #{position_id}")

            self._trigger_callback('position_opened', {
                'position_id': position_id,
                'opportunity': opportunity,
                'orders': orders
            })

            return {'success': True, 'position_id': position_id}

        except Exception as e:
            logger.error(f"Error executing spot-futures funding: {e}")
            return {'success': False, 'error': str(e)}

    def _execute_basis_arbitrage(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """执行基差套利"""
        try:
            symbol = opportunity['symbol']
            exchange = opportunity['exchange']
            position_size = opportunity['position_size']

            # 计算交易数量（使用实际开仓价）
            spot_price = opportunity.get('spot_entry_price', opportunity['spot_price'])  # 优先使用买入价
            futures_price = opportunity.get('futures_entry_price', opportunity['futures_price'])  # 优先使用做空价
            amount = position_size / spot_price

            # 创建持仓记录
            entry_details = {
                'exchange': exchange,
                'spot_price': spot_price,
                'futures_price': futures_price,
                'basis': opportunity['basis'],
                'expected_return': opportunity['expected_return'],
                'estimated_hold_days': opportunity.get('estimated_hold_days', 3)
            }

            position_id = self.db.execute_insert(
                """
                INSERT INTO positions (strategy_type, symbol, exchanges, entry_details,
                                     position_size, current_pnl, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    'basis_arbitrage',
                    symbol,
                    json.dumps([exchange]),
                    json.dumps(entry_details),
                    position_size,
                    0,
                    'open'
                )
            )

            # 执行订单
            orders = self.order_manager.create_spot_futures_pair(
                exchange=exchange,
                symbol=symbol,
                amount=amount,
                strategy_id=position_id,
                strategy_type='basis_arbitrage'
            )

            if not orders['success']:
                self.db.execute_update(
                    "UPDATE positions SET status = 'failed' WHERE id = ?",
                    (position_id,)
                )
                logger.error("Failed to execute basis arbitrage orders")
                return {'success': False, 'error': '订单执行失败'}

            logger.info(f"✅ Basis arbitrage executed: Position #{position_id}")

            self._trigger_callback('position_opened', {
                'position_id': position_id,
                'opportunity': opportunity,
                'orders': orders
            })

            return {'success': True, 'position_id': position_id}

        except Exception as e:
            logger.error(f"Error executing basis arbitrage: {e}")
            return {'success': False, 'error': str(e)}

    def _execute_directional_strategy(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """执行单边资金费率趋势策略"""
        try:
            symbol = opportunity['symbol']
            exchange = opportunity['exchange']
            position_size = opportunity['position_size']
            direction = opportunity['direction'] # 'long' or 'short'

            # 检查最小订单金额（交易所最小要求 5 USDT）
            if position_size < 5:
                logger.warning(f"订单金额 {position_size} USDT 小于最小要求 5 USDT，跳过执行")
                return {'success': False, 'error': f'订单金额小于最小要求 5 USDT'}

            # 计算数量（确保精度足够，避免订单价值低于5 USDT）
            entry_price = opportunity['entry_price']
            amount = position_size / entry_price
            
            # 验证计算出的amount对应的订单价值
            estimated_value = amount * entry_price
            if estimated_value < 5:
                # 如果因为精度问题导致价值不足，增加amount
                amount = 5.0 / entry_price
                logger.warning(f"调整amount以确保订单价值≥5 USDT: {amount} @ {entry_price} = {amount * entry_price:.2f} USDT")

            # 确定订单方向
            # 如果是short策略，我们要开空单 -> side='sell'
            # 如果是long策略，我们要开多单 -> side='buy'
            side = 'sell' if direction == 'short' else 'buy'

            # 创建持仓记录
            entry_details = {
                'exchange': exchange,
                'direction': direction,
                'entry_price': entry_price,
                'funding_rate': opportunity['funding_rate'],
                'expected_return': opportunity['expected_return']
            }

            position_id = self.db.execute_insert(
                """
                INSERT INTO positions (strategy_type, symbol, exchanges, entry_details,
                                     position_size, current_pnl, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    'directional_funding',
                    symbol,
                    json.dumps([exchange]),
                    json.dumps(entry_details),
                    position_size,
                    0,
                    'open'
                )
            )

            # 执行单边订单
            order = self.order_manager.create_order(
                exchange=exchange,
                symbol=symbol,
                side=side,
                amount=amount,
                order_type='market',
                is_futures=True,
                strategy_id=position_id,
                strategy_type='directional_funding'
            )

            if not order:
                self.db.execute_update(
                    "UPDATE positions SET status = 'failed' WHERE id = ?",
                    (position_id,)
                )
                logger.error("Failed to execute directional strategy order")
                return {'success': False, 'error': '订单执行失败'}

            logger.info(f"✅ Directional funding strategy executed: Position #{position_id} ({direction})")

            self._trigger_callback('position_opened', {
                'position_id': position_id,
                'opportunity': opportunity,
                'orders': {'main_order': order}
            })

            return {'success': True, 'position_id': position_id}

        except Exception as e:
            logger.error(f"Error executing directional strategy: {e}")
            return {'success': False, 'error': str(e)}

    def close_position(self, position_id: int) -> bool:
        """平仓"""
        try:
            # 获取持仓信息
            positions = self.db.execute_query(
                "SELECT * FROM positions WHERE id = ?",
                (position_id,)
            )

            if not positions:
                logger.error(f"Position #{position_id} not found")
                return False

            position = positions[0]
            strategy_type = position['strategy_type']
            symbol = position['symbol']
            entry_details = json.loads(position['entry_details'])

            logger.info(f"Closing position #{position_id} - {strategy_type}")

            # 根据策略类型平仓
            if strategy_type == 'funding_rate_cross_exchange':
                # 从entry_details获取交易所信息
                long_exchange = entry_details['long_exchange']
                short_exchange = entry_details['short_exchange']
                amount = float(position['position_size']) / entry_details['long_price']

                orders = self.order_manager.close_cross_exchange_pair(
                    long_exchange=long_exchange,
                    short_exchange=short_exchange,
                    symbol=symbol,
                    amount=amount,
                    strategy_id=position_id
                )

            elif strategy_type in ['funding_rate_spot_futures', 'basis_arbitrage']:
                exchange = entry_details['exchange']
                amount = float(position['position_size']) / entry_details['spot_price']

                orders = self.order_manager.close_spot_futures_pair(
                    exchange=exchange,
                    symbol=symbol,
                    amount=amount,
                    strategy_id=position_id
                )

            elif strategy_type == 'directional_funding':
                exchange = entry_details['exchange']
                direction = entry_details['direction']
                amount = float(position['position_size']) / entry_details['entry_price']

                # 平仓方向相反
                # 开空(short) -> 开空单(sell) -> 平仓买入(buy)
                # 开多(long)  -> 开多单(buy)  -> 平仓卖出(sell)
                side = 'buy' if direction == 'short' else 'sell'

                order = self.order_manager.create_order(
                    exchange=exchange,
                    symbol=symbol,
                    side=side,
                    amount=amount,
                    order_type='market',
                    is_futures=True,
                    strategy_id=position_id,
                    strategy_type='close_position'
                )

                orders = {'success': True if order else False}

            else:
                logger.error(f"Unknown strategy type: {strategy_type}")
                return False

            if not orders['success']:
                logger.error(f"Failed to close position #{position_id}")
                return False

            # 更新持仓状态
            self.db.execute_update(
                """
                UPDATE positions
                SET status = 'closed', close_time = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (position_id,)
            )

            logger.info(f"✅ Position #{position_id} closed successfully")

            self._trigger_callback('position_closed', {
                'position_id': position_id,
                'position': position
            })

            return True

        except Exception as e:
            logger.error(f"Error closing position: {e}")
            return False

    def _execution_loop(self):
        """执行循环"""
        while self.running:
            try:
                # 检查是否暂停
                if self.paused:
                    time.sleep(1)
                    continue

                if self.pending_opportunities:
                    opportunity = self.pending_opportunities.pop(0)
                    self.execute_opportunity(opportunity)
                else:
                    time.sleep(1)
            except Exception as e:
                logger.error(f"Error in execution loop: {e}")
                time.sleep(1)

    def _position_monitoring_loop(self):
        """持仓监控循环"""
        while self.running:
            try:
                positions = self.get_open_positions()

                for position in positions:
                    strategy_type = position['strategy_type']
                    
                    # 检查是否需要紧急平仓
                    if position['status'] == 'emergency_close_pending':
                        logger.warning(f"🚨 执行紧急平仓 Position #{position['id']}")
                        self.close_position(position['id'])
                        continue

                    if strategy_type == 'directional_funding':
                        self._check_directional_position(position)

                time.sleep(60)
            except Exception as e:
                logger.error(f"Error in position monitoring loop: {e}")
                time.sleep(60)

    def _check_directional_position(self, position: Dict[str, Any]):
        """检查单边策略持仓"""
        try:
            position_id = position['id']
            symbol = position['symbol']
            entry_details = json.loads(position['entry_details'])
            exchange = entry_details['exchange']
            direction = entry_details['direction']

            # 获取配置
            pair_config = self.config.get_pair_config(symbol, exchange, 's3')
            stop_loss_pct = pair_config.get('s3_stop_loss_pct', 0.05)
            short_exit_threshold = pair_config.get('s3_short_exit_threshold', 0.0)
            long_exit_threshold = pair_config.get('s3_long_exit_threshold', 0.0)

            # 获取最新价格和资金费率
            market_data = self.db.execute_query(
                """
                SELECT futures_price, funding_rate
                FROM market_prices
                WHERE exchange = ? AND symbol = ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (exchange, symbol)
            )

            if not market_data:
                return

            current_data = market_data[0]
            current_price = current_data['futures_price']
            current_funding_rate = current_data['funding_rate']

            entry_price = float(entry_details['entry_price'])

            # 1. 计算当前PnL (估算)
            if direction == 'short':
                # 做空收益 = (开仓价 - 当前价) / 开仓价
                pnl_pct = (entry_price - current_price) / entry_price
            else:
                # 做多收益 = (当前价 - 开仓价) / 开仓价
                pnl_pct = (current_price - entry_price) / entry_price

            # 更新数据库中的current_pnl (用于显示)
            current_pnl = float(position['position_size']) * pnl_pct
            self.db.execute_update(
                "UPDATE positions SET current_pnl = ? WHERE id = ?",
                (current_pnl, position_id)
            )

            # 2. 检查止损
            if pnl_pct <= -stop_loss_pct:
                logger.warning(f"Stop loss triggered for position #{position_id}: {pnl_pct:.2%}")
                self.close_position(position_id)
                self._trigger_callback('risk_alert', {
                    'type': 'stop_loss',
                    'position_id': position_id,
                    'message': f"止损触发: {symbol} 亏损 {pnl_pct:.2%}"
                })
                return

            # 3. 检查资金费率退出条件
            should_close = False
            if direction == 'short':
                # 做空时，如果费率跌破阈值（比如变成负数或0），平仓
                if current_funding_rate <= short_exit_threshold:
                    logger.info(f"Funding rate exit for position #{position_id} (Short): Rate {current_funding_rate} <= {short_exit_threshold}")
                    should_close = True
            else:
                # 做多时，如果费率涨破阈值（比如变成正数或0），平仓
                if current_funding_rate >= long_exit_threshold:
                    logger.info(f"Funding rate exit for position #{position_id} (Long): Rate {current_funding_rate} >= {long_exit_threshold}")
                    should_close = True

            if should_close:
                self.close_position(position_id)
                self._trigger_callback('strategy_exit', {
                    'position_id': position_id,
                    'message': f"费率条件触发平仓: {symbol} 费率 {current_funding_rate}"
                })

        except Exception as e:
            logger.error(f"Error checking position #{position['id']}: {e}")

    def _trigger_callback(self, event_type: str, data: Any):
        """触发回调"""
        for callback in self.execution_callbacks:
            try:
                callback(event_type, data)
            except Exception as e:
                logger.error(f"Error in execution callback: {e}")

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """获取所有开仓持仓"""
        return self.db.execute_query(
            "SELECT * FROM positions WHERE status = 'open' ORDER BY open_time DESC"
        )

    def get_position_summary(self) -> Dict[str, Any]:
        """获取持仓摘要"""
        positions = self.get_open_positions()

        total_pnl = sum(float(p.get('current_pnl', 0)) for p in positions)
        total_size = sum(float(p.get('position_size', 0)) for p in positions)

        by_strategy = {}
        for pos in positions:
            strategy = pos['strategy_type']
            if strategy not in by_strategy:
                by_strategy[strategy] = {'count': 0, 'pnl': 0}
            by_strategy[strategy]['count'] += 1
            by_strategy[strategy]['pnl'] += float(pos.get('current_pnl', 0))

        return {
            'total_positions': len(positions),
            'total_pnl': total_pnl,
            'total_size': total_size,
            'by_strategy': by_strategy
        }
