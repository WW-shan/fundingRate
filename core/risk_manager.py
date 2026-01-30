"""
风险管理器
负责风险检查、多级预警、异常检测
"""
import time
import threading
from typing import Dict, List, Any, Optional
from datetime import datetime
from loguru import logger
from config import ConfigManager
from database import DatabaseManager


class RiskManager:
    """风险管理器"""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager):
        self.config = config_manager
        self.db = db_manager
        self.running = False
        self.risk_callbacks = []  # 风险事件回调

    def start(self):
        """启动风险监控"""
        logger.info("Starting risk manager...")
        self.running = True

        # 启动持仓监控线程
        threading.Thread(target=self._monitoring_loop, daemon=True).start()

        logger.info("Risk manager started")

    def stop(self):
        """停止风险监控"""
        logger.info("Stopping risk manager...")
        self.running = False

    def register_callback(self, callback):
        """注册风险事件回调"""
        self.risk_callbacks.append(callback)

    def _monitoring_loop(self):
        """监控循环"""
        while self.running:
            try:
                self._check_all_positions()
                time.sleep(30)  # 每30秒检查一次
            except Exception as e:
                logger.error(f"Error in risk monitoring loop: {e}")
                time.sleep(30)

    def _check_all_positions(self):
        """检查所有持仓的风险"""
        positions = self.db.execute_query(
            "SELECT * FROM positions WHERE status = 'open'"
        )

        for position in positions:
            try:
                self._check_position_risk(position)
            except Exception as e:
                logger.error(f"Error checking position {position['id']}: {e}")

    def _check_position_risk(self, position: Dict[str, Any]):
        """检查单个持仓的风险"""
        position_id = position['id']
        position_size = float(position['position_size'])
        current_pnl = float(position.get('current_pnl', 0))
        pnl_pct = current_pnl / position_size if position_size > 0 else 0

        # 获取风控配置
        warning_threshold = self.config.get('risk', 'warning_threshold', 0.005)
        critical_threshold = self.config.get('risk', 'critical_threshold', 0.010)
        emergency_threshold = self.config.get('risk', 'emergency_threshold', 0.015)

        # 检查预警级别
        if pnl_pct < -emergency_threshold:
            self._trigger_risk_event(
                level='emergency',
                event_type='position_loss',
                description=f"Position #{position_id} 紧急预警：浮亏 {pnl_pct*100:.2f}%，触发自动平仓",
                position_id=position_id
            )
            # 紧急情况下自动平仓
            try:
                logger.warning(f"🚨 触发紧急止损，自动平仓 Position #{position_id}")
                # 这里需要从strategy_executor获取close_position方法
                # 暂时只标记需要平仓，由外部处理
                self.db.execute_update(
                    "UPDATE positions SET status = 'emergency_close_pending' WHERE id = ?",
                    (position_id,)
                )
            except Exception as e:
                logger.error(f"自动平仓失败 Position #{position_id}: {e}")
        elif pnl_pct < -critical_threshold:
            self._trigger_risk_event(
                level='critical',
                event_type='position_loss',
                description=f"Position #{position_id} 严重预警：浮亏 {pnl_pct*100:.2f}%",
                position_id=position_id
            )
        elif pnl_pct < -warning_threshold:
            self._trigger_risk_event(
                level='warning',
                event_type='position_loss',
                description=f"Position #{position_id} 警告：浮亏 {pnl_pct*100:.2f}%",
                position_id=position_id
            )

    def _trigger_risk_event(self, level: str, event_type: str, description: str, position_id: Optional[int] = None):
        """触发风险事件"""
        # 记录到数据库
        self.db.execute_insert(
            """
            INSERT INTO risk_events (level, event_type, description, position_id)
            VALUES (?, ?, ?, ?)
            """,
            (level, event_type, description, position_id)
        )

        logger.warning(f"[{level.upper()}] {description}")

        # 触发回调
        for callback in self.risk_callbacks:
            try:
                callback({
                    'level': level,
                    'event_type': event_type,
                    'description': description,
                    'position_id': position_id,
                    'timestamp': datetime.now().isoformat()
                })
            except Exception as e:
                logger.error(f"Error in risk callback: {e}")

    def check_pre_trade_risk(self, opportunity: Dict[str, Any]) -> Dict[str, Any]:
        """
        交易前风险检查
        返回: {'passed': bool, 'reason': str, 'adjusted_position_size': float}
        """
        symbol = opportunity['symbol']
        position_size = opportunity['position_size']
        strategy_type = opportunity['type']
        # 0. 检查总账户亏损率（防止加仓亏损）
        max_drawdown = self.config.get('risk', 'max_drawdown', 0.1)
        total_capital = self.config.get('global', 'total_capital', 100)
        
        # 计算当前总盈亏
        total_pnl_result = self.db.execute_query(
            "SELECT SUM(current_pnl) as total_pnl FROM positions WHERE status = 'open'"
        )
        total_pnl = float(total_pnl_result[0]['total_pnl'] or 0)
        total_loss_pct = total_pnl / total_capital if total_capital > 0 else 0
        
        # 如果当前总亏损率超过最大回撤限制，禁止新开仓
        if total_loss_pct < -max_drawdown:
            return {
                'passed': False,
                'reason': f'当前总亏损率 {abs(total_loss_pct)*100:.2f}% 超过限制 {max_drawdown*100:.0f}%，禁止开仓',
                'adjusted_position_size': position_size
            }

        # 检查单笔交易最大仓位限制
        max_position_size = self.config.get('risk', 'max_position_size_per_trade', 1000)
        if position_size > max_position_size:
            return {
                'passed': True,
                'reason': f'单笔仓位过大，调整至 {max_position_size:.2f} USDT',
                'adjusted_position_size': max_position_size
            }
        # 1. 检查资金使用率
        total_capital = self.config.get('global', 'total_capital', 100)
        max_capital_usage = self.config.get('global', 'max_capital_usage', 0.8)

        # 计算当前已用资金
        open_positions = self.db.execute_query(
            "SELECT SUM(position_size) as total FROM positions WHERE status = 'open'"
        )
        used_capital = float(open_positions[0]['total'] or 0)

        available_capital = total_capital * max_capital_usage - used_capital

        if position_size > available_capital:
            if available_capital > 0:
                # 调整仓位大小
                return {
                    'passed': True,
                    'reason': f'资金不足，仓位调整至 {available_capital:.2f} USDT',
                    'adjusted_position_size': available_capital
                }
            else:
                return {
                    'passed': False,
                    'reason': '可用资金不足',
                    'adjusted_position_size': 0
                }

        # 2. 检查最大持仓数
        max_positions = self.config.get('global', 'max_positions', 10)
        current_positions_count = self.db.execute_query(
            "SELECT COUNT(*) as count FROM positions WHERE status = 'open'"
        )[0]['count']

        if current_positions_count >= max_positions:
            return {
                'passed': False,
                'reason': f'已达到最大持仓数 {max_positions}',
                'adjusted_position_size': position_size
            }

        # 3. 检查单交易对最大持仓数
        pair_config = self.config.get_pair_config(symbol)
        max_positions_per_pair = pair_config.get('max_positions', 3)

        pair_positions_count = self.db.execute_query(
            "SELECT COUNT(*) as count FROM positions WHERE status = 'open' AND symbol = ?",
            (symbol,)
        )[0]['count']

        if pair_positions_count >= max_positions_per_pair:
            return {
                'passed': False,
                'reason': f'{symbol} 已达到最大持仓数 {max_positions_per_pair}',
                'adjusted_position_size': position_size
            }

        # 4. 异常检测
        if opportunity['type'] == 'funding_rate_cross_exchange':
            # 检查价格偏离
            price_diff_pct = opportunity.get('price_diff_pct', 0)
            price_deviation_threshold = self.config.get('risk', 'price_deviation_threshold', 0.02)

            if price_diff_pct > price_deviation_threshold:
                return {
                    'passed': False,
                    'reason': f'价格偏离异常：{price_diff_pct*100:.2f}%',
                    'adjusted_position_size': position_size
                }

        # 5. 动态仓位调整
        if self.config.get('risk', 'dynamic_position_enabled', True):
            score = opportunity['score']
            high_score_multiplier = self.config.get('risk', 'high_score_multiplier', 1.5)
            medium_score_multiplier = self.config.get('risk', 'medium_score_multiplier', 1.0)
            low_score_multiplier = self.config.get('risk', 'low_score_multiplier', 0.5)

            if score > 85:
                adjusted_size = min(position_size * high_score_multiplier, available_capital)
            elif score > 60:
                adjusted_size = min(position_size * medium_score_multiplier, available_capital)
            else:
                adjusted_size = min(position_size * low_score_multiplier, available_capital)

            if adjusted_size != position_size:
                return {
                    'passed': True,
                    'reason': f'动态仓位调整（评分: {score:.1f}）',
                    'adjusted_position_size': adjusted_size
                }

        # 所有检查通过
        return {
            'passed': True,
            'reason': '风控检查通过',
            'adjusted_position_size': position_size
        }

    def check_abnormal_funding_rate(self, exchange: str, symbol: str, funding_rate: float) -> bool:
        """检查资金费率是否异常"""
        abnormal_threshold = self.config.get('risk', 'abnormal_funding_rate', 0.005)

        if abs(funding_rate) > abnormal_threshold:
            self._trigger_risk_event(
                level='warning',
                event_type='abnormal_funding_rate',
                description=f'{exchange} {symbol} 资金费率异常：{funding_rate*100:.4f}%'
            )
            return True

        return False

    def check_abnormal_price_deviation(self, symbol: str, prices: Dict[str, float]) -> bool:
        """检查价格偏离是否异常"""
        if len(prices) < 2:
            return False

        price_list = list(prices.values())
        avg_price = sum(price_list) / len(price_list)

        for exchange, price in prices.items():
            deviation = abs(price - avg_price) / avg_price

            price_deviation_threshold = self.config.get('risk', 'price_deviation_threshold', 0.02)

            if deviation > price_deviation_threshold:
                self._trigger_risk_event(
                    level='warning',
                    event_type='abnormal_price',
                    description=f'{exchange} {symbol} 价格异常偏离：{deviation*100:.2f}%'
                )
                return True

        return False

    def get_risk_statistics(self) -> Dict[str, Any]:
        """获取风险统计数据"""
        # 最近24小时的风险事件
        recent_events = self.db.execute_query(
            """
            SELECT level, COUNT(*) as count
            FROM risk_events
            WHERE timestamp > datetime('now', '-1 day')
            GROUP BY level
            """
        )

        event_stats = {event['level']: event['count'] for event in recent_events}

        # 当前持仓风险分布
        positions = self.db.execute_query(
            "SELECT * FROM positions WHERE status = 'open'"
        )

        risk_distribution = {
            'safe': 0,
            'warning': 0,
            'critical': 0,
            'emergency': 0
        }

        for pos in positions:
            position_size = float(pos['position_size'])
            current_pnl = float(pos.get('current_pnl', 0))
            pnl_pct = current_pnl / position_size if position_size > 0 else 0

            warning_threshold = self.config.get('risk', 'warning_threshold', 0.005)
            critical_threshold = self.config.get('risk', 'critical_threshold', 0.010)
            emergency_threshold = self.config.get('risk', 'emergency_threshold', 0.015)

            if pnl_pct < -emergency_threshold:
                risk_distribution['emergency'] += 1
            elif pnl_pct < -critical_threshold:
                risk_distribution['critical'] += 1
            elif pnl_pct < -warning_threshold:
                risk_distribution['warning'] += 1
            else:
                risk_distribution['safe'] += 1

        return {
            'recent_events': event_stats,
            'position_risk_distribution': risk_distribution,
            'total_positions': len(positions)
        }
