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
        self.last_position_sync = 0  # 上次持仓同步时间

    def start(self):
        """启动策略执行器"""
        logger.info("Starting strategy executor...")
        self.running = True

        # 启动执行线程
        threading.Thread(target=self._execution_loop, daemon=True).start()

        # 启动持仓监控线程
        threading.Thread(target=self._position_monitoring_loop, daemon=True).start()
        
        # 启动持仓同步线程
        threading.Thread(target=self._position_sync_loop, daemon=True).start()

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
                                     position_size, current_pnl, realized_pnl, funding_collected, fees_paid, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    'funding_rate_cross_exchange',
                    symbol,
                    json.dumps([long_exchange, short_exchange]),
                    json.dumps(entry_details),
                    position_size,
                    0,
                    0,
                    0,
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
                return {'success': False, 'error': 'Order execution failed'}
            
            # 保存开仓手续费
            total_fee = orders.get('total_fee', 0)
            if total_fee > 0:
                self.db.execute_update(
                    "UPDATE positions SET fees_paid = ? WHERE id = ?",
                    (total_fee, position_id)
                )
                logger.info(f"💰 开仓手续费已记录: ${total_fee:.4f}")
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
                                     position_size, current_pnl, realized_pnl, funding_collected, fees_paid, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    'funding_rate_spot_futures',
                    symbol,
                    json.dumps([exchange]),
                    json.dumps(entry_details),
                    position_size,
                    0,
                    0,
                    0,
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
            
            # 保存开仓手续费
            total_fee = orders.get('total_fee', 0)
            if total_fee > 0:
                self.db.execute_update(
                    "UPDATE positions SET fees_paid = ? WHERE id = ?",
                    (total_fee, position_id)
                )
                logger.info(f"💰 开仓手续费已记录: ${total_fee:.4f}")

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
                                     position_size, current_pnl, realized_pnl, funding_collected, fees_paid, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    'basis_arbitrage',
                    symbol,
                    exchange,
                    json.dumps(entry_details),
                    position_size,
                    0,
                    0,
                    0,
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
                                     position_size, current_pnl, realized_pnl, funding_collected, fees_paid, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    'directional_funding',
                    symbol,
                    exchange,
                    json.dumps(entry_details),
                    position_size,
                    0,
                    0,
                    0,
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
                    strategy_type='close_position',
                    reduce_only=True  # 平仓必须设为True，否则会开对冲单
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
                    
                    # 更新持仓的资金费和手续费（每次监控都更新）
                    self._update_position_fees(position)

                    if strategy_type == 'directional_funding':
                        self._check_directional_position(position)

                time.sleep(5)  # 每5秒检查一次持仓
            except Exception as e:
                logger.error(f"Error in position monitoring loop: {e}")
                time.sleep(5)
    
    def _update_position_fees(self, position: Dict[str, Any]):
        """更新持仓的资金费和手续费 - 从数据库直接计算"""
        try:
            position_id = position['id']
            symbol = position['symbol']
            entry_details = json.loads(position['entry_details'])
            position_size = float(position.get('position_size', 0))
            
            # 获取交易所信息
            exchanges_str = position.get('exchanges', '[]')
            try:
                exchanges_list = json.loads(exchanges_str) if isinstance(exchanges_str, str) else exchanges_str
                if isinstance(exchanges_list, list) and exchanges_list:
                    exchange = exchanges_list[0] if isinstance(exchanges_list[0], str) else entry_details.get('exchange')
                else:
                    exchange = exchanges_str if isinstance(exchanges_str, str) else entry_details.get('exchange')
            except:
                exchange = entry_details.get('exchange')
            
            if not exchange or position_size == 0:
                return
            
            # 获取开仓时间
            open_time_str = position.get('open_time')
            if not open_time_str:
                return
            
            from datetime import datetime, timezone
            
            # 解析开仓时间
            if open_time_str.endswith('Z'):
                open_time = datetime.fromisoformat(open_time_str.replace('Z', '+00:00'))
            else:
                open_time = datetime.fromisoformat(open_time_str)
                if open_time.tzinfo is None:
                    open_time = open_time.replace(tzinfo=timezone.utc)
            
            now = datetime.now(timezone.utc)
            hours_held = (now - open_time).total_seconds() / 3600
            
            funding_collected = 0
            
            # 只要持仓超过30分钟就尝试计算资金费（避免刚开仓就计算）
            if hours_held > 0.5:
                # 策略1需要查询两个交易所的费率
                if position['strategy_type'] == 'funding_rate_cross_exchange':
                    long_exchange = entry_details.get('long_exchange')
                    short_exchange = entry_details.get('short_exchange')
                    
                    if long_exchange and short_exchange:
                        funding_collected = self._calculate_cross_exchange_funding(
                            symbol, long_exchange, short_exchange, 
                            position_size, open_time, now
                        )
                else:
                    # 其他策略使用单交易所费率计算
                    funding_collected = self._calculate_single_exchange_funding(
                        position, exchange, symbol, position_size, 
                        open_time, now, entry_details
                    )
            
            # 获取当前手续费（开仓时已记录）
            current_fees = float(position.get('fees_paid', 0) or 0)
            
            # 只有当数据发生变化时才更新数据库
            if abs(funding_collected - float(position.get('funding_collected', 0) or 0)) > 0.0001 or abs(current_fees - float(position.get('fees_paid', 0) or 0)) > 0.0001:
                self.db.execute_query(
                    """
                    UPDATE positions
                    SET funding_collected = ?,
                        fees_paid = ?
                    WHERE id = ?
                    """,
                    (funding_collected, current_fees, position_id)
                )
                
        except Exception as e:
            logger.error(f"Error updating position fees for #{position.get('id')}: {e}")
    
    def _calculate_single_exchange_funding(self, position, exchange, symbol, position_size, 
                                           open_time, now, entry_details):
        """计算单交易所的资金费（策略2A/2B/3）"""
        try:
            position_id = position['id']
            open_time_ms = int(open_time.timestamp() * 1000)
            now_ms = int(now.timestamp() * 1000)
            
            # 先获取结算周期
            latest_funding = self.db.execute_query(
                """
                SELECT funding_interval
                FROM funding_rates
                WHERE exchange = ? AND symbol = ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (exchange, symbol)
            )
            
            if not latest_funding or len(latest_funding) == 0:
                return 0
            
            funding_interval_ms = latest_funding[0].get('funding_interval', 28800000)
            funding_interval_hours = funding_interval_ms / 3600000
            
            # 获取持仓期间已经结算过的所有资金费率记录
            funding_history = self.db.execute_query(
                """
                SELECT funding_rate, timestamp, next_funding_time
                FROM funding_rates
                WHERE exchange = ? AND symbol = ?
                AND next_funding_time > ?
                AND next_funding_time <= ?
                ORDER BY next_funding_time ASC
                """,
                (exchange, symbol, open_time_ms, now_ms)
            )
            
            if not funding_history or len(funding_history) == 0:
                return 0
            
            # 使用数据库中的 next_funding_time 来识别实际的结算时间点
            settlement_records = {}  # {next_funding_time: (rate, timestamp)}
            
            for row in funding_history:
                next_funding_time = row.get('next_funding_time')
                if next_funding_time:
                    timestamp = row.get('timestamp', 0)
                    if next_funding_time not in settlement_records or timestamp > settlement_records[next_funding_time][1]:
                        settlement_records[next_funding_time] = (float(row['funding_rate']), timestamp)
            
            funding_collected = 0
            
            # 按时间排序并累加资金费
            for settlement_time in sorted(settlement_records.keys()):
                rate, _ = settlement_records[settlement_time]
                
                # 累加这次结算的资金费
                if position['strategy_type'] in ['funding_rate_spot_futures', 'basis_arbitrage']:
                    # 策略2A/2B：期货做空，收取正资金费
                    funding_collected += position_size * rate
                elif position['strategy_type'] == 'directional_funding':
                    # 策略3：单边持仓
                    direction = entry_details.get('direction', 'short')
                    if direction == 'short':
                        funding_collected += position_size * rate
                    else:
                        funding_collected -= position_size * rate
            
            if len(settlement_records) > 0:
                logger.debug(f"📊 持仓 #{position_id} 资金费计算: {len(settlement_records)}次结算 ({funding_interval_hours}h周期), 累计${funding_collected:.4f}")
            
            return funding_collected
            
        except Exception as e:
            logger.error(f"Error calculating single exchange funding: {e}")
            return 0
    
    def _calculate_cross_exchange_funding(self, symbol, long_exchange, short_exchange, 
                                         position_size, open_time, now):
        """计算跨交易所套利的资金费（策略1）- 使用实际费率差"""
        try:
            open_time_ms = int(open_time.timestamp() * 1000)
            now_ms = int(now.timestamp() * 1000)
            
            # 获取做多交易所的费率历史
            long_history = self.db.execute_query(
                """
                SELECT funding_rate, timestamp, next_funding_time
                FROM funding_rates
                WHERE exchange = ? AND symbol = ?
                AND next_funding_time > ?
                AND next_funding_time <= ?
                ORDER BY next_funding_time ASC
                """,
                (long_exchange, symbol, open_time_ms, now_ms)
            )
            
            # 获取做空交易所的费率历史
            short_history = self.db.execute_query(
                """
                SELECT funding_rate, timestamp, next_funding_time
                FROM funding_rates
                WHERE exchange = ? AND symbol = ?
                AND next_funding_time > ?
                AND next_funding_time <= ?
                ORDER BY next_funding_time ASC
                """,
                (short_exchange, symbol, open_time_ms, now_ms)
            )
            
            if not long_history or not short_history:
                logger.warning(f"跨交易所套利 {symbol}: 缺少费率数据")
                return 0
            
            # 整理两个交易所的结算记录
            long_settlements = {}  # {next_funding_time: (rate, timestamp)}
            for row in long_history:
                next_funding_time = row.get('next_funding_time')
                if next_funding_time:
                    timestamp = row.get('timestamp', 0)
                    if next_funding_time not in long_settlements or timestamp > long_settlements[next_funding_time][1]:
                        long_settlements[next_funding_time] = (float(row['funding_rate']), timestamp)
            
            short_settlements = {}  # {next_funding_time: (rate, timestamp)}
            for row in short_history:
                next_funding_time = row.get('next_funding_time')
                if next_funding_time:
                    timestamp = row.get('timestamp', 0)
                    if next_funding_time not in short_settlements or timestamp > short_settlements[next_funding_time][1]:
                        short_settlements[next_funding_time] = (float(row['funding_rate']), timestamp)
            
            # 找出共同的结算时间点
            common_settlements = set(long_settlements.keys()) & set(short_settlements.keys())
            
            if not common_settlements:
                logger.warning(f"跨交易所套利 {symbol}: 两个交易所的结算时间点不匹配")
                return 0
            
            funding_collected = 0
            
            # 对每个共同的结算时间点，计算费率差收益
            for settlement_time in sorted(common_settlements):
                long_rate, _ = long_settlements[settlement_time]
                short_rate, _ = short_settlements[settlement_time]
                
                # 做多交易所支付费用（如果费率为正）或收取（如果为负）
                # 做空交易所收取费用（如果费率为正）或支付（如果为负）
                # 净收益 = 做空端收益 - 做多端成本 = position_size * (short_rate - long_rate)
                rate_diff = short_rate - long_rate
                funding_collected += position_size * rate_diff
            
            if len(common_settlements) > 0:
                logger.debug(f"📊 跨交易所套利 {symbol} ({long_exchange}/{short_exchange}) 资金费计算: {len(common_settlements)}次结算, 累计${funding_collected:.4f}")
            
            return funding_collected
            
        except Exception as e:
            logger.error(f"Error calculating cross exchange funding: {e}")
            return 0

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
            stop_loss_pct = float(pair_config.get('s3_stop_loss_pct', 0.05))
            short_exit_threshold = float(pair_config.get('s3_short_exit_threshold', 0.0))
            long_exit_threshold = float(pair_config.get('s3_long_exit_threshold', 0.0))
            trailing_stop_enabled = pair_config.get('s3_trailing_stop_enabled', True)
            if isinstance(trailing_stop_enabled, str):
                trailing_stop_enabled = trailing_stop_enabled.lower() in ('true', '1', 'yes')
            trailing_activation_pct = float(pair_config.get('s3_trailing_activation_pct', 0.04))
            trailing_callback_pct = float(pair_config.get('s3_trailing_callback_pct', 0.04))

            # 获取最新价格
            price_data = self.db.execute_query(
                """
                SELECT futures_price
                FROM market_prices
                WHERE exchange = ? AND symbol = ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (exchange, symbol)
            )
            
            # 获取最新资金费率
            funding_data = self.db.execute_query(
                """
                SELECT funding_rate
                FROM funding_rates
                WHERE exchange = ? AND symbol = ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (exchange, symbol)
            )

            if not price_data or not funding_data:
                return

            current_price = float(price_data[0]['futures_price'])
            current_funding_rate = float(funding_data[0]['funding_rate'])

            entry_price = float(entry_details['entry_price'])
            if entry_price <= 0:
                logger.error(f"Invalid entry_price {entry_price} for position #{position_id}")
                return

            trailing_activated = position.get('trailing_stop_activated', False)
            best_price = position.get('best_price')
            if best_price is not None:
                best_price = float(best_price)

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
                return

            # 4. Trailing Stop 逻辑
            if not trailing_stop_enabled:
                return

            if not trailing_activated:
                # 未启动：检查是否达到启动条件
                if pnl_pct >= trailing_activation_pct:
                    logger.info(f"Trailing stop activated for position #{position_id}: PnL {pnl_pct:.2%} >= {trailing_activation_pct:.2%}")
                    self.db.execute_update(
                        "UPDATE positions SET trailing_stop_activated = TRUE, best_price = ?, activation_price = ? WHERE id = ?",
                        (current_price, current_price, position_id)
                    )
                    self._trigger_callback('trailing_stop', {
                        'position_id': position_id,
                        'message': f"追踪止盈已启动: {symbol} 盈利 {pnl_pct:.2%}, 当前价 {current_price}"
                    })
            else:
                # 已启动：更新best_price并检查回撤
                should_update = False
                if direction == 'short':
                    # 做空：追踪最低价
                    if best_price is None or current_price < best_price:
                        best_price = current_price
                        should_update = True
                else:
                    # 做多：追踪最高价
                    if best_price is None or current_price > best_price:
                        best_price = current_price
                        should_update = True

                if should_update:
                    self.db.execute_update(
                        "UPDATE positions SET best_price = ? WHERE id = ?",
                        (best_price, position_id)
                    )

                # 检查回撤止盈
                should_take_profit = False
                retracement = 0.0
                if direction == 'short' and best_price is not None and best_price > 0:
                    # 做空：价格从最低点反弹超过阈值
                    retracement = (current_price - best_price) / best_price
                    if retracement >= trailing_callback_pct:
                        should_take_profit = True
                elif direction == 'long' and best_price is not None and best_price > 0:
                    # 做多：价格从最高点回落超过阈值
                    retracement = (best_price - current_price) / best_price
                    if retracement >= trailing_callback_pct:
                        should_take_profit = True

                if should_take_profit:
                    logger.info(f"Trailing stop take-profit for position #{position_id}: retracement {retracement:.2%}")
                    self.close_position(position_id)
                    self._trigger_callback('trailing_stop', {
                        'position_id': position_id,
                        'message': f"追踪止盈平仓: {symbol} 方向 {direction}, 入场价 {entry_price}, 最优价 {best_price}, 平仓价 {current_price}, 回撤 {retracement:.2%}"
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

    def _position_sync_loop(self):
        """持仓同步循环 - 启动时立即执行一次，然后每1分钟与交易所真实持仓对比"""
        # 启动时先同步一次
        logger.info("🔄 启动时执行持仓同步...")
        try:
            self._sync_positions_with_exchange()
        except Exception as e:
            logger.error(f"Error in initial position sync: {e}")
        
        while self.running:
            try:
                current_time = time.time()
                # 每30秒同步一次（监控循环已改为5秒，同步可以稍慢）
                if current_time - self.last_position_sync < 30:
                    time.sleep(5)
                    continue
                
                self.last_position_sync = current_time
                self._sync_positions_with_exchange()
                
            except Exception as e:
                logger.error(f"Error in position sync loop: {e}")
                time.sleep(30)

    def _sync_positions_with_exchange(self):
        """同步数据库持仓与交易所真实持仓（双向同步）"""
        try:
            # 获取数据库中的持仓
            db_positions = self.get_open_positions()

            # 构建数据库持仓索引 {exchange_symbol_direction: db_pos}
            db_positions_dict = {}
            for pos in db_positions:
                entry_details = json.loads(pos['entry_details'])
                exchange = entry_details.get('exchange', '').lower()
                symbol = pos['symbol']
                direction = entry_details.get('direction', '')
                key = f"{exchange}_{symbol}_{direction}"
                db_positions_dict[key] = pos

            # 遍历所有配置的交易所，获取真实持仓
            synced_keys = set()  # 记录已同步的持仓

            for exchange_name, exchange_adapter in self.order_manager.exchanges.items():
                try:
                    # 获取交易所所有持仓
                    real_positions = exchange_adapter.get_positions()

                    for rp in real_positions:
                        raw_symbol = rp.get('symbol', '')
                        # 统一symbol格式：去掉 :USDT 后缀
                        symbol = raw_symbol.replace(':USDT', '').replace('/USDT', '')
                        if '/' not in symbol:
                            symbol = f"{symbol}/USDT"

                        side = rp.get('side', '')  # long/short
                        contracts = float(rp.get('contracts', 0))
                        entry_price_real = float(rp.get('entryPrice', 0))
                        notional = float(rp.get('notional', 0)) or (contracts * entry_price_real)

                        if contracts <= 0:
                            continue

                        key = f"{exchange_name}_{symbol}_{side}"
                        synced_keys.add(key)

                        if key in db_positions_dict:
                            # 数据库已有此持仓，检查是否需要更新
                            db_pos = db_positions_dict[key]
                            db_entry_details = json.loads(db_pos['entry_details'])
                            db_entry_price = float(db_pos.get('entry_price') or db_entry_details.get('entry_price', 0) or 0)
                            db_position_size = float(db_pos.get('position_size', 0))

                            # 检查是否有变化（价格或数量）
                            price_changed = abs(entry_price_real - db_entry_price) > 0.0001 if db_entry_price > 0 else entry_price_real > 0
                            # notional 是 USDT 价值，与 position_size 比较
                            size_changed = abs(notional - db_position_size) > 0.01 if db_position_size > 0 else notional > 0

                            if price_changed or size_changed:
                                logger.info(
                                    f"🔄 更新持仓 #{db_pos['id']}: {exchange_name} {symbol} {side} "
                                    f"价格 {db_entry_price:.6f} → {entry_price_real:.6f}, "
                                    f"仓位 ${db_position_size:.2f} → ${notional:.2f}"
                                )

                                # 更新 entry_details
                                db_entry_details['entry_price'] = entry_price_real

                                self.db.execute_update(
                                    """
                                    UPDATE positions
                                    SET position_size = ?,
                                        entry_price = ?,
                                        entry_details = ?,
                                        updated_at = CURRENT_TIMESTAMP
                                    WHERE id = ?
                                    """,
                                    (notional, entry_price_real, json.dumps(db_entry_details), db_pos['id'])
                                )

                                self._trigger_callback('position_updated', {
                                    'position_id': db_pos['id'],
                                    'exchange': exchange_name,
                                    'symbol': symbol,
                                    'direction': side,
                                    'old_price': db_entry_price,
                                    'new_price': entry_price_real,
                                    'old_size': db_position_size,
                                    'new_size': notional
                                })
                        else:
                            # 数据库没有此持仓，自动添加
                            logger.info(
                                f"➕ 同步新持仓: {exchange_name} {symbol} {side} "
                                f"{contracts}张 @ ${entry_price_real:.6f} (价值 ${notional:.2f})"
                            )

                            entry_details = {
                                'exchange': exchange_name,
                                'direction': side,
                                'entry_price': entry_price_real,
                                'synced_from_exchange': True,
                                'sync_time': time.strftime('%Y-%m-%d %H:%M:%S')
                            }

                            position_id = self.db.execute_insert(
                                """
                                INSERT INTO positions (strategy_type, symbol, exchanges, entry_details,
                                                     entry_price, position_size, current_pnl, realized_pnl,
                                                     funding_collected, fees_paid, status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    'directional_funding',  # 默认策略类型
                                    symbol,
                                    exchange_name,
                                    json.dumps(entry_details),
                                    entry_price_real,
                                    notional,
                                    0,
                                    0,
                                    0,
                                    0,
                                    'open'
                                )
                            )

                            logger.info(f"✅ 已同步持仓到数据库: Position #{position_id}")

                            self._trigger_callback('position_synced', {
                                'position_id': position_id,
                                'exchange': exchange_name,
                                'symbol': symbol,
                                'direction': side,
                                'entry_price': entry_price_real,
                                'position_size': notional
                            })

                except Exception as e:
                    logger.error(f"Error syncing positions for {exchange_name}: {e}")

            # 检查数据库中是否有已不存在于交易所的持仓
            for key, db_pos in db_positions_dict.items():
                if key not in synced_keys:
                    entry_details = json.loads(db_pos['entry_details'])
                    exchange = entry_details.get('exchange', '')
                    symbol = db_pos['symbol']
                    direction = entry_details.get('direction', '')

                    logger.warning(
                        f"🔄 自动平仓: 持仓 #{db_pos['id']} {exchange} {symbol} {direction} "
                        f"在交易所不存在，标记为已平仓"
                    )

                    self.db.execute_update(
                        """
                        UPDATE positions
                        SET status = 'closed',
                            close_time = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (db_pos['id'],)
                    )

                    self._trigger_callback('position_auto_closed', {
                        'position_id': db_pos['id'],
                        'exchange': exchange,
                        'symbol': symbol,
                        'direction': direction,
                        'reason': 'not_found_on_exchange'
                    })

            total_synced = len(synced_keys)
            total_db = len(db_positions)
            logger.info(f"✅ 持仓同步完成: 交易所 {total_synced} 个, 数据库 {total_db} 个")

        except Exception as e:
            logger.error(f"Error syncing positions with exchange: {e}")
