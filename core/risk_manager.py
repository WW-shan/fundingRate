"""
风险管理器
全局持仓风险监控和止损
"""
import time
import threading
from typing import Dict, Any, Optional
from datetime import datetime
from loguru import logger
from config import ConfigManager
from database import DatabaseManager


class RiskManager:
    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager):
        self.config = config_manager
        self.db = db_manager
        self.running = False
        self.risk_callbacks = []

    def start(self):
        self.running = True
        threading.Thread(target=self._monitoring_loop, daemon=True).start()
        logger.info("Risk manager started")

    def stop(self):
        self.running = False

    def register_callback(self, callback):
        self.risk_callbacks.append(callback)

    def _monitoring_loop(self):
        while self.running:
            try:
                self._check_all_positions()
                time.sleep(30)
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
        """检查单个持仓风险 - 三级预警"""
        position_id = position['id']
        position_size = float(position['position_size'])
        current_pnl = float(position.get('current_pnl', 0))
        pnl_pct = current_pnl / position_size if position_size > 0 else 0

        warning_threshold = float(self.config.get('risk', 'warning_threshold', 0.05))
        critical_threshold = float(self.config.get('risk', 'critical_threshold', 0.10))
        emergency_threshold = float(self.config.get('risk', 'emergency_threshold', 0.15))

        if pnl_pct < -emergency_threshold:
            self._trigger_risk_event(
                level='emergency',
                event_type='position_loss',
                description=f"Position #{position_id} 紧急: 浮亏 {pnl_pct*100:.2f}%，触发自动平仓",
                position_id=position_id
            )
            try:
                logger.warning(f"🚨 触发紧急止损 Position #{position_id}")
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
                description=f"Position #{position_id} 严重: 浮亏 {pnl_pct*100:.2f}%",
                position_id=position_id
            )
        elif pnl_pct < -warning_threshold:
            self._trigger_risk_event(
                level='warning',
                event_type='position_loss',
                description=f"Position #{position_id} 警告: 浮亏 {pnl_pct*100:.2f}%",
                position_id=position_id
            )

    def _trigger_risk_event(self, level: str, event_type: str, description: str, position_id: Optional[int] = None):
        """触发风险事件"""
        self.db.execute_insert(
            "INSERT INTO risk_events (level, event_type, description, position_id) VALUES (?, ?, ?, ?)",
            (level, event_type, description, position_id)
        )
        logger.warning(f"[{level.upper()}] {description}")

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
        """交易前风险检查"""
        position_size = opportunity['position_size']

        # 检查总亏损率
        total_capital = float(self.config.get('global', 'total_capital', 100))
        max_drawdown = float(self.config.get('risk', 'max_drawdown', 0.1))

        total_pnl_result = self.db.execute_query(
            "SELECT SUM(current_pnl) as total_pnl FROM positions WHERE status = 'open'"
        )
        total_pnl = float(total_pnl_result[0]['total_pnl'] or 0)
        total_loss_pct = total_pnl / total_capital if total_capital > 0 else 0

        if total_loss_pct < -max_drawdown:
            return {
                'passed': False,
                'reason': f'总亏损率 {abs(total_loss_pct)*100:.2f}% 超过限制 {max_drawdown*100:.0f}%',
                'adjusted_position_size': position_size
            }

        # 检查单笔最大仓位
        max_position_size = float(self.config.get('risk', 'max_position_size_per_trade', 1000))
        if position_size > max_position_size:
            position_size = max_position_size

        # 检查可用资金
        max_capital_usage = float(self.config.get('global', 'max_capital_usage', 0.8))
        open_positions = self.db.execute_query(
            "SELECT SUM(position_size) as total FROM positions WHERE status = 'open'"
        )
        used_capital = float(open_positions[0]['total'] or 0)
        available_capital = total_capital * max_capital_usage - used_capital

        if position_size > available_capital:
            if available_capital > 0:
                return {
                    'passed': True,
                    'reason': f'资金不足，仓位调整至 {available_capital:.2f} USDT',
                    'adjusted_position_size': available_capital
                }
            return {'passed': False, 'reason': '可用资金不足', 'adjusted_position_size': 0}

        # 检查最大持仓数
        max_positions = int(self.config.get('global', 'max_positions', 10))
        current_count = self.db.execute_query(
            "SELECT COUNT(*) as count FROM positions WHERE status = 'open'"
        )[0]['count']

        if current_count >= max_positions:
            return {
                'passed': False,
                'reason': f'已达到最大持仓数 {max_positions}',
                'adjusted_position_size': position_size
            }

        return {
            'passed': True,
            'reason': '风控检查通过',
            'adjusted_position_size': position_size
        }
