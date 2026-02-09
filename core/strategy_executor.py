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

                time.sleep(60)
            except Exception as e:
                logger.error(f"Error in position monitoring loop: {e}")
                time.sleep(60)
    
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
                # 每1分钟同步一次
                if current_time - self.last_position_sync < 60:
                    time.sleep(10)
                    continue
                
                self.last_position_sync = current_time
                self._sync_positions_with_exchange()
                
            except Exception as e:
                logger.error(f"Error in position sync loop: {e}")
                time.sleep(30)
    
    def _sync_positions_with_exchange(self):
        """同步数据库持仓与交易所真实持仓（自动修正）"""
        try:
            # 获取数据库中的持仓
            db_positions = self.get_open_positions()
            
            if not db_positions:
                return
            
            # 按交易所分组
            positions_by_exchange = {}
            for pos in db_positions:
                entry_details = json.loads(pos['entry_details'])
                exchange = entry_details.get('exchange', '')
                symbol = pos['symbol']
                
                if exchange not in positions_by_exchange:
                    positions_by_exchange[exchange] = {}
                if symbol not in positions_by_exchange[exchange]:
                    positions_by_exchange[exchange][symbol] = []
                positions_by_exchange[exchange][symbol].append(pos)
            
            # 从交易所获取真实持仓
            for exchange_name, symbols in positions_by_exchange.items():
                exchange_adapter = self.order_manager.exchanges.get(exchange_name.lower())
                if not exchange_adapter:
                    continue
                
                try:
                    # 获取交易所所有持仓
                    real_positions = exchange_adapter.get_positions()
                    
                    # 构建真实持仓字典 {symbol: position_data}
                    real_positions_dict = {}
                    for rp in real_positions:
                        symbol = rp.get('symbol', '').replace(':USDT', '')
                        side = rp.get('side', '')  # long/short
                        contracts = float(rp.get('contracts', 0))
                        
                        if contracts > 0:
                            key = f"{symbol}_{side}"
                            real_positions_dict[key] = rp
                    
                    # 检查数据库持仓是否在交易所存在
                    for symbol, db_pos_list in symbols.items():
                        for db_pos in db_pos_list:
                            entry_details = json.loads(db_pos['entry_details'])
                            direction = entry_details.get('direction', '')  # long/short
                            position_size = float(db_pos.get('position_size', 0))
                            entry_price = float(db_pos.get('entry_price', 0))
                            
                            key = f"{symbol}_{direction}"
                            
                            if key not in real_positions_dict:
                                # 数据库有持仓但交易所没有 - 自动标记为已平仓
                                logger.warning(
                                    f"🔄 自动同步: 持仓 #{db_pos['id']} {exchange_name} {symbol} {direction} "
                                    f"在交易所不存在，自动标记为已平仓"
                                )
                                
                                # 更新为已平仓状态
                                self.db.execute_query(
                                    """
                                    UPDATE positions 
                                    SET status = 'closed',
                                        exit_details = ?,
                                        updated_at = CURRENT_TIMESTAMP
                                    WHERE id = ?
                                    """,
                                    (json.dumps({
                                        'reason': 'auto_sync',
                                        'note': '交易所持仓不存在，自动同步为已平仓',
                                        'sync_time': time.strftime('%Y-%m-%d %H:%M:%S')
                                    }), db_pos['id'])
                                )
                                
                                self._trigger_callback('position_auto_closed', {
                                    'position_id': db_pos['id'],
                                    'exchange': exchange_name,
                                    'symbol': symbol,
                                    'direction': direction
                                })
                                
                            else:
                                # 检查数量和价格是否一致
                                real_contracts = float(real_positions_dict[key].get('contracts', 0))
                                real_entry_price = float(real_positions_dict[key].get('entryPrice', 0))
                                
                                # 数量或价格不一致 - 自动更新
                                if abs(real_contracts - position_size) > 0.0001 or abs(real_entry_price - entry_price) > 0.0001:
                                    logger.warning(
                                        f"🔄 自动同步: 持仓 #{db_pos['id']} {exchange_name} {symbol} {direction} "
                                        f"数据不一致 - 数据库: {position_size}张@{entry_price}, "
                                        f"交易所: {real_contracts}张@{real_entry_price}"
                                    )
                                    
                                    # 更新entry_details
                                    entry_details['entry_price'] = real_entry_price
                                    
                                    # 更新数据库
                                    self.db.execute_query(
                                        """
                                        UPDATE positions 
                                        SET position_size = ?,
                                            entry_price = ?,
                                            entry_details = ?,
                                            updated_at = CURRENT_TIMESTAMP
                                        WHERE id = ?
                                        """,
                                        (real_contracts, real_entry_price, json.dumps(entry_details), db_pos['id'])
                                    )
                                    
                                    logger.info(f"✅ 已自动更新持仓 #{db_pos['id']} 的数量和价格")
                                    
                                    self._trigger_callback('position_updated', {
                                        'position_id': db_pos['id'],
                                        'old_size': position_size,
                                        'new_size': real_contracts,
                                        'old_price': entry_price,
                                        'new_price': real_entry_price
                                    })
                    
                    # 检查交易所是否有未记录的持仓
                    for key, real_pos in real_positions_dict.items():
                        symbol_side = key.split('_')
                        if len(symbol_side) != 2:
                            continue
                        symbol, side = symbol_side
                        
                        # 检查数据库是否有这个持仓
                        found = False
                        if symbol in symbols:
                            for db_pos in symbols[symbol]:
                                entry_details = json.loads(db_pos['entry_details'])
                                if entry_details.get('direction') == side:
                                    found = True
                                    break
                        
                        if not found:
                            contracts = float(real_pos.get('contracts', 0))
                            entry_price_real = float(real_pos.get('entryPrice', 0))
                            logger.warning(
                                f"⚠️ 发现未记录持仓: {exchange_name} {symbol} {side} "
                                f"{contracts}张@{entry_price_real} (可能是手动开仓，暂不自动添加到数据库)"
                            )
                            self._trigger_callback('position_mismatch', {
                                'type': 'not_in_database',
                                'exchange': exchange_name,
                                'symbol': symbol,
                                'side': side,
                                'contracts': contracts,
                                'entry_price': entry_price_real,
                                'real_position': real_pos
                            })
                
                except Exception as e:
                    logger.error(f"Error syncing positions for {exchange_name}: {e}")
            
            logger.info(f"✅ 持仓自动同步完成，检查了 {len(db_positions)} 个持仓")
                
        except Exception as e:
            logger.error(f"Error syncing positions with exchange: {e}")
