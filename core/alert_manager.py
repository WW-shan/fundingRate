"""
告警管理器
负责告警去重、冷却、持久化，并分发到推送通道（Telegram）
"""
import time
import threading
from typing import Dict, Any, List, Optional
from loguru import logger
from config import ConfigManager
from database import DatabaseManager


class AlertManager:
    """
    告警去重策略：
      - 同一 (alert_type, symbol, exchange) 在冷却期内只推一次
      - 冷却期默认 30 分钟，可按类型配置
      - 级别升级（warning -> critical）时立即重推
    """

    COOLDOWN_DEFAULTS = {
        'funding_extreme': 1800,
        'cross_spread': 3600,
        'basis_extreme': 1800,
        'stablecoin_depeg': 600,
    }

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager):
        self.config = config_manager
        self.db = db_manager
        self.running = False
        self.push_callbacks = []
        self._cooldown_cache: Dict[str, Dict[str, Any]] = {}  # key -> {ts, level}

    def register_push_callback(self, callback):
        """注册推送回调（Telegram 等）"""
        self.push_callbacks.append(callback)

    def start(self):
        self.running = True
        logger.info("Alert manager started")

    def stop(self):
        self.running = False

    # ---------- 处理告警 ----------

    def handle_alerts(self, alerts: List[Dict[str, Any]]) -> int:
        """
        批量处理一轮扫描的全部告警：
          1. 按严重度排序（critical 优先）
          2. 应用每轮推送上限（防止刷屏）
          3. 逐条走 handle_alert（去重/落库/推送）
        返回实际推送数量
        """
        if not alerts:
            return 0

        max_push = int(self.config.get('alerts', 'max_pushes_per_scan', 10))
        severity_order = {'critical': 0, 'warning': 1, 'emergency': 0}
        sorted_alerts = sorted(alerts, key=lambda a: severity_order.get(a.get('level'), 9))

        pushed = 0
        for alert in sorted_alerts:
            if not self._should_push(alert):
                self._persist(alert, pushed=False)
                continue
            if pushed >= max_push:
                self._persist(alert, pushed=False)
                continue
            if self.handle_alert(alert):
                pushed += 1
        return pushed

    def _should_push(self, alert: Dict[str, Any]) -> bool:
        """推送过滤：资金费率仅推 APR >= 阈值（默认 1000%），脱锚告警始终推送"""
        alert_type = alert.get('alert_type')

        # 稳定币脱锚：低频且重要，始终推送
        if alert_type == 'stablecoin_depeg':
            return bool(self.config.get('alerts', 'push_depeg', True))

        # 资金费率异常：APR 阈值过滤
        if alert_type == 'funding_extreme':
            threshold = float(self.config.get('alerts', 'push_funding_apr_threshold', 10.0))
            apr = (alert.get('details') or {}).get('apr')
            return apr is not None and abs(apr) >= threshold

        # 其他类型（跨所差/基差）：默认只落库不推送
        return False

    def _persist(self, alert: Dict[str, Any], pushed: bool):
        """仅落库"""
        try:
            self.db.execute_query(
                """
                INSERT INTO alerts (alert_type, level, symbol, exchange, message, details, pushed, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.get('alert_type'), alert.get('level'),
                    alert.get('symbol'), alert.get('exchange'),
                    alert.get('message'), str(alert.get('details', {})),
                    1 if pushed else 0, int(time.time() * 1000),
                )
            )
        except Exception as e:
            logger.error(f"Failed to persist alert: {e}")

    def handle_alert(self, alert: Dict[str, Any]) -> bool:
        """
        处理单条告警：去重 -> 落库 -> 推送
        返回是否实际推送
        """
        key = self._dedup_key(alert)
        level = alert.get('level', 'warning')
        cooldown = self._get_cooldown(alert.get('alert_type', ''))
        now = time.time()

        cached = self._cooldown_cache.get(key)
        should_push = True
        if cached:
            elapsed = now - cached['ts']
            level_upgraded = self._is_upgrade(cached.get('level'), level)
            if elapsed < cooldown and not level_upgraded:
                should_push = False

        # 落库（无论是否推送都记录，便于面板统计）
        try:
            self.db.execute_query(
                """
                INSERT INTO alerts (alert_type, level, symbol, exchange, message, details, pushed, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.get('alert_type'),
                    level,
                    alert.get('symbol'),
                    alert.get('exchange'),
                    alert.get('message'),
                    str(alert.get('details', {})),
                    1 if should_push else 0,
                    int(now * 1000),
                )
            )
        except Exception as e:
            logger.error(f"Failed to persist alert: {e}")

        if should_push:
            self._cooldown_cache[key] = {'ts': now, 'level': level}
            self._dispatch(alert)
            logger.info(f"[ALERT][{level}] {alert.get('message')}")

        return should_push

    def _dedup_key(self, alert: Dict[str, Any]) -> str:
        return f"{alert.get('alert_type')}|{alert.get('symbol')}|{alert.get('exchange')}"

    def _get_cooldown(self, alert_type: str) -> int:
        default = self.COOLDOWN_DEFAULTS.get(alert_type, 1800)
        return int(self.config.get('alerts', f'cooldown_{alert_type}', default))

    @staticmethod
    def _is_upgrade(old_level: Optional[str], new_level: str) -> bool:
        order = {'warning': 1, 'critical': 2, 'emergency': 3}
        return order.get(new_level, 0) > order.get(old_level or '', 0)

    def _dispatch(self, alert: Dict[str, Any]):
        for cb in self.push_callbacks:
            try:
                cb(alert)
            except Exception as e:
                logger.error(f"Push callback error: {e}")

    # ---------- 查询 ----------

    def get_recent_alerts(self, limit: int = 100, hours: int = 24) -> List[Dict[str, Any]]:
        """获取最近告警（面板用）"""
        try:
            since = int((time.time() - hours * 3600) * 1000)
            return self.db.execute_query(
                """
                SELECT * FROM alerts
                WHERE timestamp > ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (since, limit)
            )
        except Exception as e:
            logger.error(f"Failed to query alerts: {e}")
            return []

    def get_alert_stats(self, hours: int = 24) -> Dict[str, Any]:
        """告警统计（面板用）"""
        try:
            since = int((time.time() - hours * 3600) * 1000)
            rows = self.db.execute_query(
                """
                SELECT alert_type, level, COUNT(*) as count
                FROM alerts
                WHERE timestamp > ?
                GROUP BY alert_type, level
                """,
                (since,)
            )
            stats: Dict[str, Any] = {'total': 0, 'by_type': {}, 'by_level': {}}
            for row in rows:
                cnt = row['count']
                stats['total'] += cnt
                stats['by_type'][row['alert_type']] = stats['by_type'].get(row['alert_type'], 0) + cnt
                stats['by_level'][row['level']] = stats['by_level'].get(row['level'], 0) + cnt
            return stats
        except Exception as e:
            logger.error(f"Failed to get alert stats: {e}")
            return {'total': 0, 'by_type': {}, 'by_level': {}}
