"""
告警管理器
负责告警去重、冷却、持久化，并分发到推送通道（Telegram）

推送策略（v2，降噪）：
  - 批量汇总：每轮扫描的推送合并为一条汇总消息（digest），不再逐条刷屏
  - 新事件推送：某个 (类型, 标的, 交易所) 首次出现时推送
  - 档位升级推送：严重度跨档（如 APR 1000% -> 2000% -> 5000%）时立即推送
  - 持续提醒：同一档位最长每 push_reminder_cooldown（默认 24h）提醒一次
  - 恢复重置：连续 push_reset_gap（默认 1h）未出现，视为新事件重新推送
  - 落库冷却：同标的同类型 persist_cooldown 内不重复落库（面板不受影响）
"""
import time
import json
from typing import Dict, Any, List
from loguru import logger
from config import ConfigManager
from database import DatabaseManager


class AlertManager:
    """告警管理器（去重 / 分档冷却 / 批量推送 / 落库）"""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager):
        self.config = config_manager
        self.db = db_manager
        self.running = False
        self.push_callbacks = []
        # key -> {ts: 上次推送时间, band: 上次推送档位, level: 上次等级, last_seen: 最近出现时间}
        self._push_state: Dict[str, Dict[str, Any]] = {}
        self._persist_cache: Dict[str, float] = {}  # key -> ts（落库冷却，防止面板噪音）
        self._load_push_state()

    def register_push_callback(self, callback):
        """注册推送回调（Telegram 等）；回调接收 digest 字典 {'alerts': [...], ...}"""
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
          2. 应用推送过滤（_should_push）与每轮推送上限
          3. 逐条判定是否推送（新事件 / 档位升级 / 提醒到期）
          4. 所有待推送合并为一条 digest 消息
        返回实际推送条数
        """
        if not alerts:
            return 0

        max_push = self.config.get_int("alerts", "max_pushes_per_scan", 20)
        # 清理 24h 前的缓存，避免长期运行后无限增长
        cutoff = time.time() - 86400
        self._push_state = {
            k: v for k, v in self._push_state.items()
            if max(v.get("ts", 0), v.get("last_seen", 0)) >= cutoff
        }
        self._persist_cache = {k: v for k, v in self._persist_cache.items() if v >= cutoff}

        severity_order = {"critical": 0, "emergency": 0, "warning": 1}
        sorted_alerts = sorted(alerts, key=lambda a: severity_order.get(a.get("level"), 9))

        to_push: List[Dict[str, Any]] = []
        for alert in sorted_alerts:
            allow_push = self._should_push(alert) and len(to_push) < max_push
            if self._handle_alert(alert, allow_push=allow_push):
                to_push.append(alert)

        if to_push:
            self._dispatch_digest(to_push)
        return len(to_push)

    def handle_alert(self, alert: Dict[str, Any], allow_push: bool = True) -> bool:
        """处理单条告警（兼容接口）；推送时同样走 digest 通道"""
        pushed = self._handle_alert(alert, allow_push=allow_push and self._should_push(alert))
        if pushed:
            self._dispatch_digest([alert])
        return pushed

    def _handle_alert(self, alert: Dict[str, Any], allow_push: bool) -> bool:
        """
        单条告警内部处理：落库冷却 + 分档推送判定。
        返回是否实际推送。
        """
        key = self._dedup_key(alert)
        level = alert.get("level", "warning")
        now = time.time()

        # 落库判定
        persist_cooldown = self.config.get_int("alerts", "persist_cooldown", 1800)
        persist_ok = (now - self._persist_cache.get(key, 0)) >= persist_cooldown

        # 推送判定（分档 + 提醒 + 恢复重置）
        state = self._push_state.get(key) or {}
        band = self._alert_band(alert)
        reset_gap = self.config.get_int("alerts", "push_reset_gap", 3600)
        reminder = self.config.get_int("alerts", "push_reminder_cooldown", 86400)
        is_new = (not state) or (now - state.get("last_seen", 0)) > reset_gap

        push_ok = False
        if allow_push:
            if is_new:
                push_ok = True
            elif band > int(state.get("band", -1)):
                push_ok = True  # 严重度跨档，立即重推
            elif now - state.get("ts", 0) >= reminder:
                push_ok = True  # 同档位持续存在，按提醒间隔重推

        state["last_seen"] = now
        if push_ok:
            state["ts"] = now
            state["band"] = band
            state["level"] = level
        self._push_state[key] = state
        # 推送时立即保存；持续存在的异常每 5 分钟保存一次心跳（供重启恢复 last_seen）
        if push_ok or (now - float(state.get("saved") or 0)) >= 300:
            self._save_push_state(key, state, now)

        # 推送成功时必须落库（即使落库冷却期内），保证推送统计准确
        if persist_ok or push_ok:
            self._persist(alert, pushed=push_ok)
            self._persist_cache[key] = now

        if push_ok:
            logger.info(f"[ALERT][{level}] {alert.get('message')}")

        return push_ok

    def _should_push(self, alert: Dict[str, Any]) -> bool:
        """推送过滤：各类型独立阈值（低于阈值仅落库上面板）"""
        alert_type = alert.get("alert_type")

        # 锚定资产脱锚：低频且重要；休市期间的 warning 不推送，仅 critical 推送
        if alert_type == "stablecoin_depeg":
            if not self.config.get_bool("alerts", "push_depeg", True):
                return False
            details = alert.get("details") or {}
            if details.get("market_closed") and alert.get("level") != "critical":
                return False
            return True

        # 跨发行商价差：可配置（默认推送，这是稀缺套利信号）
        if alert_type == "issuer_spread":
            return self.config.get_bool("alerts", "push_issuer_spread", True)

        # 跨所费率差：默认仅面板
        if alert_type == "cross_spread":
            return self.config.get_bool("alerts", "push_cross_spread", False)

        # 期现基差：所有现货/永续配对共用 5% 默认推送阈值。
        if alert_type == "basis_extreme":
            if not self.config.get_bool("alerts", "push_basis", True):
                return False
            threshold = self.config.get_float("alerts", "push_basis_threshold", 0.05)
            basis = (alert.get("details") or {}).get("basis")
            return basis is not None and abs(basis) >= threshold

        # 资金费率：仅推送 |APR| >= 阈值（默认 1000%）
        if alert_type == "funding_extreme":
            threshold = self.config.get_float("alerts", "push_funding_apr_threshold", 10.0)
            apr = (alert.get("details") or {}).get("apr")
            return apr is not None and abs(apr) >= threshold

        return False

    def _alert_band(self, alert: Dict[str, Any]) -> int:
        """把告警指标映射为严重度档位（0=最低）；档位上升时立即重推"""
        alert_type = alert.get("alert_type")
        d = alert.get("details") or {}

        if alert_type == "funding_extreme":
            base = self.config.get_float("alerts", "push_funding_apr_threshold", 10.0) or 1.0
            steps = [base, base * 2, base * 5, base * 10]
            metric = abs(d.get("apr") or 0)
        elif alert_type == "basis_extreme":
            base = self.config.get_float("alerts", "push_basis_threshold", 0.05) or 0.01
            steps = [base, max(0.05, base * 1.5), 0.10, 0.20]
            metric = abs(d.get("basis") or 0)
        elif alert_type == "stablecoin_depeg":
            warn = float(d.get("threshold_warning") or 0.003)
            crit = float(d.get("threshold_critical") or 0.01)
            steps = [warn, crit, crit * 2, crit * 4]
            metric = abs(d.get("deviation") or 0)
        elif alert_type == "issuer_spread":
            base = self.config.get_float("alerts", "issuer_spread_threshold", 0.05) or 0.05
            steps = [base, base * 3, base * 5, base * 10]
            metric = abs(d.get("abs_spread") or 0)
        elif alert_type == "cross_spread":
            base = self.config.get_float("alerts", "cross_spread_threshold", 1.0) or 1.0
            steps = [base, base * 2, base * 5]
            metric = abs(d.get("apr_spread") or 0)
        else:
            return 0

        band = 0
        for step in steps:
            if metric >= step:
                band += 1
        return band

    def _load_push_state(self):
        """从数据库加载推送状态（跨重启去重）"""
        if not self.db:
            return
        try:
            cutoff = time.time() - 86400
            rows = self.db.execute_query(
                "SELECT key, ts, band, level, last_seen FROM alert_push_state WHERE last_seen > ?",
                (cutoff,)
            )
            for r in rows:
                last_seen = float(r.get("last_seen") or 0)
                self._push_state[r["key"]] = {
                    "ts": float(r.get("ts") or 0),
                    "band": int(r.get("band") or -1),
                    "level": r.get("level") or "",
                    "last_seen": last_seen,
                    "saved": last_seen,
                }
            if rows:
                logger.info(f"Loaded {len(rows)} alert push states (restart dedup)")
        except Exception as e:
            logger.warning(f"Failed to load alert push state: {e}")

    def _save_push_state(self, key: str, state: Dict[str, Any], now: float):
        """持久化单条推送状态（跨重启去重）"""
        if not self.db:
            return
        try:
            self.db.execute_query(
                """
                INSERT INTO alert_push_state (key, ts, band, level, last_seen, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    ts = excluded.ts,
                    band = excluded.band,
                    level = excluded.level,
                    last_seen = excluded.last_seen,
                    timestamp = excluded.timestamp
                """,
                (
                    key, float(state.get("ts") or 0), int(state.get("band") or 0),
                    state.get("level") or "", float(state.get("last_seen") or now),
                    int(now * 1000),
                )
            )
            state["saved"] = now
        except Exception as e:
            logger.warning(f"Failed to save alert push state: {e}")

    def _dedup_key(self, alert: Dict[str, Any]) -> str:
        alert_type = alert.get('alert_type')
        exchange = alert.get('exchange')
        # 跨所价差的 A/B 顺序可能随字典顺序变化，排序后再作为 key，避免重复推送
        if alert_type == 'cross_spread' and isinstance(exchange, str) and '/' in exchange:
            parts = [p.strip() for p in exchange.split('/', 1)]
            exchange = '/'.join(sorted(parts))
        return f"{alert_type}|{alert.get('symbol')}|{exchange}"

    def _dispatch_digest(self, alerts: List[Dict[str, Any]]):
        """把一批待推送告警合并为一条 digest 分发"""
        digest = {
            "alerts": alerts,
            "count": len(alerts),
            "timestamp": int(time.time() * 1000),
        }
        for cb in self.push_callbacks:
            try:
                cb(digest)
            except Exception as e:
                logger.error(f"Push callback error: {e}")

    def _persist(self, alert: Dict[str, Any], pushed: bool):
        """落库"""
        try:
            self.db.execute_query(
                """
                INSERT INTO alerts (alert_type, level, symbol, exchange, message, details, pushed, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.get("alert_type"), alert.get("level"),
                    alert.get("symbol"), alert.get("exchange"),
                    alert.get("message"),
                    json.dumps(alert.get("details", {}), ensure_ascii=False, default=str),
                    1 if pushed else 0, int(time.time() * 1000),
                )
            )
        except Exception as e:
            logger.error(f"Failed to persist alert: {e}")

    # ---------- 查询 ----------

    def get_recent_alerts(self, limit: int = 100, hours: int = 24,
                          level: str = "", alert_type: str = "") -> List[Dict[str, Any]]:
        """获取最近告警（面板用）；level/alert_type 在 SQL 层过滤，避免 limit 截断后漏数据"""
        try:
            since = int((time.time() - hours * 3600) * 1000)
            sql = "SELECT * FROM alerts WHERE timestamp > ?"
            params: List[Any] = [since]
            if level:
                sql += " AND level = ?"
                params.append(level)
            if alert_type:
                sql += " AND alert_type = ?"
                params.append(alert_type)
            sql += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            rows = self.db.execute_query(sql, tuple(params))
            for row in rows:
                details = row.get("details")
                if isinstance(details, str):
                    try:
                        row["details"] = json.loads(details)
                    except (json.JSONDecodeError, TypeError):
                        pass
            return rows
        except Exception as e:
            logger.error(f"Failed to query alerts: {e}")
            return []

    def count_alerts(self, hours: int = 24, level: str = "", alert_type: str = "") -> int:
        """统计满足条件的告警总数（不受 limit 截断）"""
        try:
            since = int((time.time() - hours * 3600) * 1000)
            sql = "SELECT COUNT(*) AS c FROM alerts WHERE timestamp > ?"
            params: List[Any] = [since]
            if level:
                sql += " AND level = ?"
                params.append(level)
            if alert_type:
                sql += " AND alert_type = ?"
                params.append(alert_type)
            rows = self.db.execute_query(sql, tuple(params))
            return int(rows[0]["c"]) if rows else 0
        except Exception as e:
            logger.error(f"Failed to count alerts: {e}")
            return 0

    def get_alert_stats(self, hours: int = 24) -> Dict[str, Any]:
        """告警统计（面板用）"""
        try:
            since = int((time.time() - hours * 3600) * 1000)
            rows = self.db.execute_query(
                """
                SELECT alert_type, level, pushed, COUNT(*) as count
                FROM alerts
                WHERE timestamp > ?
                GROUP BY alert_type, level, pushed
                """,
                (since,)
            )
            stats: Dict[str, Any] = {"total": 0, "pushed_total": 0, "by_type": {}, "by_level": {}}
            for row in rows:
                cnt = row["count"]
                stats["total"] += cnt
                if row.get("pushed"):
                    stats["pushed_total"] += cnt
                stats["by_type"][row["alert_type"]] = stats["by_type"].get(row["alert_type"], 0) + cnt
                stats["by_level"][row["level"]] = stats["by_level"].get(row["level"], 0) + cnt
            return stats
        except Exception as e:
            logger.error(f"Failed to get alert stats: {e}")
            return {"total": 0, "pushed_total": 0, "by_type": {}, "by_level": {}}
