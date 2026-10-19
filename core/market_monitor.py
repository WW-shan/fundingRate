"""
市场监控器（监控版）
基于采集的公开数据计算：
  - 各币种/交易所的资金费率 APR（结算间隔归一化）
  - 跨交易所费率差（年化口径）
  - 期现基差
  - 稳定币脱锚程度
并生成告警事件（交给 AlertManager 推送）
"""
import time
import threading
from typing import Dict, List, Any, Optional
from loguru import logger
from config import ConfigManager
from database import DatabaseManager
from core.data_collector import DataCollector


def annualize_rate(rate: float, interval_ms: Optional[int]) -> Optional[float]:
    """单期费率 -> 线性年化 APR。间隔未知时返回 None（不做猜测）"""
    if rate is None or not interval_ms or interval_ms <= 0:
        return None
    periods_per_year = (365 * 24 * 3600 * 1000) / interval_ms
    return rate * periods_per_year


class MarketMonitor:
    """市场监控 + 异常检测"""

    def __init__(self, config_manager: ConfigManager, db_manager: DatabaseManager,
                 data_collector: DataCollector):
        self.config = config_manager
        self.db = db_manager
        self.data_collector = data_collector
        self.running = False
        self.alert_callbacks = []
        self.last_scan: Dict[str, Any] = {}

    def register_alert_callback(self, callback):
        self.alert_callbacks.append(callback)

    def start(self):
        self.running = True
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        logger.info("Market monitor started")

    def stop(self):
        self.running = False

    def _monitor_loop(self):
        interval = int(self.config.get('global', 'monitor_scan_interval', 60))
        while self.running:
            try:
                self.scan_once()
            except Exception as e:
                logger.error(f"Monitor scan error: {e}")
            time.sleep(interval)

    # ---------- 主扫描 ----------

    def scan_once(self) -> Dict[str, Any]:
        """执行一次完整扫描，返回快照并触发告警（批量回调）"""
        market_data = self.data_collector.get_market_data()
        stablecoin_data = self.data_collector.get_stablecoin_data()

        apr_ranking = self.build_apr_ranking(market_data)
        cross_spreads = self.build_cross_exchange_spreads(market_data)
        basis_list = self.build_basis_list(market_data)
        depeg_list = self.build_depeg_list(stablecoin_data)

        self.last_scan = {
            'timestamp': int(time.time() * 1000),
            'apr_ranking': apr_ranking,
            'cross_spreads': cross_spreads,
            'basis': basis_list,
            'depeg': depeg_list,
        }

        alerts = []
        alerts += self._check_funding_anomalies(apr_ranking)
        alerts += self._check_cross_spread_anomalies(cross_spreads)
        alerts += self._check_basis_anomalies(basis_list)
        alerts += self._check_depeg_anomalies(depeg_list)

        for cb in self.alert_callbacks:
            try:
                cb(alerts)
            except Exception as e:
                logger.error(f"Alert callback error: {e}")

        return self.last_scan

    # ---------- APR 排行 ----------

    def build_apr_ranking(self, market_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """全市场 APR 排行（结算间隔归一化，线性年化）"""
        rows = []
        for symbol, exchanges in market_data.items():
            for exchange, data in exchanges.items():
                rate = data.get('funding_rate')
                interval_ms = data.get('funding_interval')
                if rate is None:
                    continue
                apr = annualize_rate(rate, interval_ms)
                rows.append({
                    'symbol': symbol,
                    'exchange': exchange,
                    'funding_rate': rate,
                    'funding_interval_ms': interval_ms,
                    'funding_interval_hours': (interval_ms / 3600000) if interval_ms else None,
                    'apr': apr,
                    'next_funding_time': data.get('next_funding_time'),
                    'rate_semantics': data.get('rate_semantics', 'upcoming'),
                })
        rows.sort(key=lambda r: (r['apr'] is None, -(r['apr'] or 0)))
        return rows

    # ---------- 跨所费率差 ----------

    def build_cross_exchange_spreads(self, market_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """跨所年化费率差（两腿各自年化后求差）"""
        rows = []
        for symbol, exchanges in market_data.items():
            names = list(exchanges.keys())
            for i, ex_a in enumerate(names):
                for ex_b in names[i + 1:]:
                    a, b = exchanges[ex_a], exchanges[ex_b]
                    apr_a = annualize_rate(a.get('funding_rate'), a.get('funding_interval'))
                    apr_b = annualize_rate(b.get('funding_rate'), b.get('funding_interval'))
                    if apr_a is None or apr_b is None:
                        continue
                    rows.append({
                        'symbol': symbol,
                        'exchange_a': ex_a,
                        'exchange_b': ex_b,
                        'rate_a': a.get('funding_rate'),
                        'rate_b': b.get('funding_rate'),
                        'apr_a': apr_a,
                        'apr_b': apr_b,
                        'apr_spread': apr_b - apr_a,
                        'abs_spread': abs(apr_b - apr_a),
                        'interval_mismatch': a.get('funding_interval') != b.get('funding_interval'),
                        'semantics_mixed': (
                            a.get('rate_semantics') == 'last_settled'
                            or b.get('rate_semantics') == 'last_settled'
                        ),
                    })
        rows.sort(key=lambda r: -r['abs_spread'])
        return rows

    # ---------- 基差 ----------

    def build_basis_list(self, market_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """期现基差（需要现货价格，来自 market_prices 表最新快照）"""
        rows = []
        try:
            latest = self.db.execute_query(
                """
                SELECT mp.exchange, mp.symbol, mp.spot_price, mp.futures_price, mp.timestamp
                FROM market_prices mp
                INNER JOIN (
                    SELECT exchange, symbol, MAX(timestamp) AS ts
                    FROM market_prices
                    WHERE timestamp > ?
                    GROUP BY exchange, symbol
                ) m ON mp.exchange = m.exchange AND mp.symbol = m.symbol AND mp.timestamp = m.ts
                """,
                (int(time.time() * 1000) - 3600 * 1000,)
            )
        except Exception as e:
            logger.debug(f"Basis query failed: {e}")
            return rows

        max_sane_basis = float(self.config.get('alerts', 'basis_sanity_limit', 0.5))
        for row in latest:
            spot = row.get('spot_price')
            futures = row.get('futures_price')
            if not spot or not futures or spot <= 0:
                continue
            basis = (futures - spot) / spot
            # 合理性防护：永续基差极少超过 50%，超出视为数据异常（如停牌陈旧价）
            if abs(basis) > max_sane_basis:
                logger.debug(f"Basis sanity filter: {row['symbol']}@{row['exchange']} basis={basis:.2%} discarded")
                continue
            rows.append({
                'exchange': row['exchange'],
                'symbol': row['symbol'],
                'spot_price': spot,
                'futures_price': futures,
                'basis': basis,
                'timestamp': row.get('timestamp'),
            })
        rows.sort(key=lambda r: -abs(r['basis']))
        return rows

    # ---------- 稳定币脱锚 ----------

    def build_depeg_list(self, stablecoin_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """稳定币偏离 1.0 的程度（绝对值排序）"""
        rows = []
        for symbol, exchanges in stablecoin_data.items():
            for exchange, data in exchanges.items():
                price = data.get('price')
                if not price:
                    continue
                deviation = price - 1.0
                rows.append({
                    'symbol': symbol,
                    'exchange': exchange,
                    'price': price,
                    'bid': data.get('bid'),
                    'ask': data.get('ask'),
                    'deviation': deviation,
                    'abs_deviation': abs(deviation),
                    'timestamp': data.get('timestamp'),
                })
        rows.sort(key=lambda r: -r['abs_deviation'])
        return rows

    # ---------- 异常检测 ----------

    def _check_funding_anomalies(self, apr_ranking: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """极端资金费率告警"""
        alerts = []
        min_abs_rate = float(self.config.get('alerts', 'funding_rate_threshold', 0.001))   # 单期 0.1%
        min_abs_apr = float(self.config.get('alerts', 'funding_apr_threshold', 0.30))      # 年化 30%
        for row in apr_ranking:
            rate = row['funding_rate']
            apr = row['apr']
            if abs(rate) >= min_abs_rate or (apr is not None and abs(apr) >= min_abs_apr):
                alerts.append({
                    'alert_type': 'funding_extreme',
                    'level': 'critical' if abs(rate) >= min_abs_rate * 3 else 'warning',
                    'symbol': row['symbol'],
                    'exchange': row['exchange'],
                    'message': (
                        f"{row['symbol']} @ {row['exchange']} 资金费率异常: "
                        f"单期 {rate*100:.4f}% / 年化 {apr*100:.1f}%"
                        if apr is not None else
                        f"{row['symbol']} @ {row['exchange']} 资金费率异常: 单期 {rate*100:.4f}%"
                    ),
                    'details': row,
                })
        return alerts

    def _check_cross_spread_anomalies(self, spreads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """跨所费率差告警"""
        alerts = []
        threshold = float(self.config.get('alerts', 'cross_spread_threshold', 0.50))  # 年化差 50%
        for row in spreads[:50]:
            if row['abs_spread'] >= threshold:
                alerts.append({
                    'alert_type': 'cross_spread',
                    'level': 'warning',
                    'symbol': row['symbol'],
                    'exchange': f"{row['exchange_a']}/{row['exchange_b']}",
                    'message': (
                        f"{row['symbol']} 跨所年化费率差 {row['abs_spread']*100:.1f}%: "
                        f"{row['exchange_a']} {row['apr_a']*100:.1f}% vs "
                        f"{row['exchange_b']} {row['apr_b']*100:.1f}%"
                        + ("（周期不一致）" if row['interval_mismatch'] else "")
                    ),
                    'details': row,
                })
        return alerts

    def _check_basis_anomalies(self, basis_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """基差异常告警"""
        alerts = []
        threshold = float(self.config.get('alerts', 'basis_threshold', 0.02))  # 2%
        for row in basis_list[:50]:
            if abs(row['basis']) >= threshold:
                alerts.append({
                    'alert_type': 'basis_extreme',
                    'level': 'warning',
                    'symbol': row['symbol'],
                    'exchange': row['exchange'],
                    'message': (
                        f"{row['symbol']} @ {row['exchange']} 基差异常: "
                        f"{row['basis']*100:.2f}% (现货 {row['spot_price']:.6g} / 期货 {row['futures_price']:.6g})"
                    ),
                    'details': row,
                })
        return alerts

    def _check_depeg_anomalies(self, depeg_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """稳定币脱锚告警（分级）"""
        alerts = []
        warn = float(self.config.get('alerts', 'depeg_warning', 0.003))     # 0.3%
        critical = float(self.config.get('alerts', 'depeg_critical', 0.01))  # 1%
        for row in depeg_list:
            dev = row['abs_deviation']
            if dev >= critical:
                level = 'critical'
            elif dev >= warn:
                level = 'warning'
            else:
                continue
            alerts.append({
                'alert_type': 'stablecoin_depeg',
                'level': level,
                'symbol': row['symbol'],
                'exchange': row['exchange'],
                'message': (
                    f"稳定币脱锚: {row['symbol']} @ {row['exchange']} "
                    f"价格 {row['price']:.6f} (偏离 {row['deviation']*100:+.3f}%)"
                ),
                'details': row,
            })
        return alerts
