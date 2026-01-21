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

    def execute_opportunity(self, opportunity: Dict[str, Any]) -> bool:
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
                return False

            # 调整仓位（如果需要）
            adjusted_size = risk_check['adjusted_position_size']
            if adjusted_size != opportunity['position_size']:
                logger.info(f"Position size adjusted: {opportunity['position_size']} -> {adjusted_size}")
                opportunity['position_size'] = adjusted_size

            # 根据策略类型执行
            strategy_type = opportunity['type']

            if strategy_type == 'funding_rate_cross_exchange':
                return self._execute_cross_exchange_funding(opportunity)
            elif strategy_type == 'funding_rate_spot_futures':
                return self._execute_spot_futures_funding(opportunity)
            elif strategy_type == 'basis_arbitrage':
                return self._execute_basis_arbitrage(opportunity)
            else:
                logger.error(f"Unknown strategy type: {strategy_type}")
                return False

        except Exception as e:
            logger.error(f"Error executing opportunity: {e}")
            return False

    def _execute_cross_exchange_funding(self, opportunity: Dict[str, Any]) -> bool:
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
                return False

            logger.info(f"✅ Cross-exchange funding arbitrage executed: Position #{position_id}")

            # 触发回调
            self._trigger_callback('position_opened', {
                'position_id': position_id,
                'opportunity': opportunity,
                'orders': orders
            })

            return True

        except Exception as e:
            logger.error(f"Error executing cross-exchange funding: {e}")
            return False

    def _execute_spot_futures_funding(self, opportunity: Dict[str, Any]) -> bool:
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
                return False

            logger.info(f"✅ Spot-futures funding arbitrage executed: Position #{position_id}")

            self._trigger_callback('position_opened', {
                'position_id': position_id,
                'opportunity': opportunity,
                'orders': orders
            })

            return True

        except Exception as e:
            logger.error(f"Error executing spot-futures funding: {e}")
            return False

    def _execute_basis_arbitrage(self, opportunity: Dict[str, Any]) -> bool:
        """执行基差套利"""
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
                return False

            logger.info(f"✅ Basis arbitrage executed: Position #{position_id}")

            self._trigger_callback('position_opened', {
                'position_id': position_id,
                'opportunity': opportunity,
                'orders': orders
            })

            return True

        except Exception as e:
            logger.error(f"Error executing basis arbitrage: {e}")
            return False

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
                # TODO: 监控持仓，检查平仓条件
                # - 资金费率变化
                # - 基差变化
                # - 止损触发
                # - 达到目标收益
                time.sleep(60)
            except Exception as e:
                logger.error(f"Error in position monitoring loop: {e}")
                time.sleep(60)

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
