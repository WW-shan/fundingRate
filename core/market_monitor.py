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
        while self.running:
            interval = int(self.config.get('global', 'monitor_scan_interval', 60))
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
        issuer_spreads = self.build_cross_issuer_spreads(stablecoin_data)

        self.last_scan = {
            'timestamp': int(time.time() * 1000),
            'apr_ranking': apr_ranking,
            'cross_spreads': cross_spreads,
            'basis': basis_list,
            'depeg': depeg_list,
            'issuer_spreads': issuer_spreads,
        }

        alerts = []
        alerts += self._check_funding_anomalies(apr_ranking)
        alerts += self._check_cross_spread_anomalies(cross_spreads)
        alerts += self._check_basis_anomalies(basis_list)
        alerts += self._check_depeg_anomalies(depeg_list)
        alerts += self._check_issuer_spread_anomalies(issuer_spreads)

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
        # 按 |APR| 降序：极端正负费率都排前面（如 -4380% 与 +4380% 同等重要）
        rows.sort(key=lambda r: (r['apr'] is None, -abs(r['apr'] or 0)))
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
                    })
        rows.sort(key=lambda r: -r['abs_spread'])
        return rows

    # ---------- 基差 ----------

    def build_basis_list(self, market_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build spot/perp basis from the current in-memory price snapshot."""
        rows = []
        now_ms = int(time.time() * 1000)
        max_age_ms = 15 * 60 * 1000
        max_sane_basis = float(self.config.get('alerts', 'basis_sanity_limit', 0.5))

        for symbol, exchanges in market_data.items():
            for exchange, data in exchanges.items():
                spot = data.get('spot_price')
                futures = data.get('futures_price')
                ts = data.get('timestamp')
                if not spot or not futures or spot <= 0:
                    continue
                if ts and now_ms - ts > max_age_ms:
                    continue
                basis = (futures - spot) / spot
                if abs(basis) > max_sane_basis:
                    logger.debug(
                        f"Basis sanity filter: {symbol}@{exchange} basis={basis:.2%} discarded"
                    )
                    continue
                rows.append({
                    'exchange': exchange,
                    'symbol': symbol,
                    'spot_price': spot,
                    'futures_price': futures,
                    'basis': basis,
                    'timestamp': ts,
                })

        rows.sort(key=lambda r: -abs(r['basis']))
        return rows

    def build_depeg_list(self, stablecoin_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        计算所有锚定资产相对参考价的偏离度
        参考价来源：
          - usd_stable: 固定 1.0
          - eur_stable: Yahoo EURUSD=X
          - gold:       Yahoo GC=F
          - stock:      Yahoo 对应股票代码
        """
        from config import peg_assets

        refs = self.data_collector.get_reference_prices()
        registry = self.data_collector.peg_discovery.registry
        rows = []

        for symbol, exchanges in stablecoin_data.items():
            entry = registry.get(symbol)
            if entry:
                category = entry['category']
                ref_symbol = entry.get('reference_symbol')
            else:
                category, ref_symbol, _ = peg_assets.classify(symbol)
            if not category:
                continue

            # 确定参考价
            reference_time = None
            market_closed = False
            ratio = float(entry.get('reference_ratio', 1.0)) if entry else 1.0
            if category == 'usd_stable':
                reference = 1.0
            else:
                ref_data = refs.get(ref_symbol) or {}
                reference = ref_data.get('price')
                if not reference:
                    continue  # 参考价未就绪，跳过（不猜测）
                # 参考价抓取时间过久（默认 15 分钟）则跳过，避免陈旧价格误报
                ref_fetched_at = ref_data.get('timestamp')
                if ref_fetched_at and (time.time() * 1000 - ref_fetched_at) > 15 * 60 * 1000:
                    continue
                reference = float(reference) * ratio
                reference_time = ref_data.get('market_time')
                # 标的行情超过 30 分钟未更新 => 休市（股票周末/盘后）
                if reference_time:
                    market_closed = (time.time() - reference_time) > 1800

            thresholds = peg_assets.get_thresholds(category)

            for exchange, data in exchanges.items():
                price = data.get('price')
                if not price:
                    continue
                deviation = (price - reference) / reference

                # 映射验证：偏离 > 50% 大概率是符号映射错误（如 RIRON 实际非 Disc Medicine）
                # 此类数据不参与脱锚判定，避免误报
                # Stablecoins must alert even on extreme moves. For stock/commodity
                # tokens a >50% gap is more likely a symbol mapping error.
                if abs(deviation) > 0.5 and category not in ('usd_stable', 'eur_stable'):
                    logger.debug(
                        f"Peg mapping check: {symbol}@{exchange} price={price} vs ref={reference} "
                        f"({deviation:.1%}) ? likely wrong mapping, skipped"
                    )
                    continue

                rows.append({
                    'symbol': symbol,
                    'exchange': exchange,
                    'category': category,
                    'price': price,
                    'reference': reference,
                    'reference_ratio': ratio,
                    'reference_time': reference_time,
                    'market_closed': market_closed,
                    'deviation': deviation,
                    'abs_deviation': abs(deviation),
                    'threshold_warning': thresholds['warning'],
                    'threshold_critical': thresholds['critical'],
                    'bid': data.get('bid'),
                    'ask': data.get('ask'),
                    'timestamp': data.get('timestamp'),
                })

        rows.sort(key=lambda r: -r['abs_deviation'])
        return rows

    # ---------- 跨发行商价差（同一标的的不同 RWA 版本） ----------

    def build_cross_issuer_spreads(self, stablecoin_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        同一标的在多个发行商之间的价差（如 AAPL 的 bStock/Ondo/xStocks/Reality/gStocks 版本）。
        同一发行商在多个交易所的报价先取中位数，避免把跨所价差误当成跨发行商价差。
        """
        groups = self.data_collector.peg_discovery.get_cross_issuer_groups()
        now_ms = int(time.time() * 1000)
        max_age_ms = max(120, int(self.config.get('global', 'stablecoin_refresh_interval', 60))) * 2 * 1000
        rows = []

        for underlying, symbols in groups.items():
            quotes = []
            for sym in symbols:
                entry = self.data_collector.peg_discovery.registry.get(sym) or {}
                issuer = entry.get('issuer') or sym.lower()
                ratio = float(entry.get('reference_ratio', 1.0))
                for exchange, data in (stablecoin_data.get(sym) or {}).items():
                    price = data.get('price')
                    ts = data.get('timestamp')
                    if not price or (ts and now_ms - ts > max_age_ms):
                        continue
                    quotes.append({
                        'symbol': sym,
                        'exchange': exchange,
                        'issuer': issuer,
                        'price': float(price),
                        'normalized_price': float(price) / ratio if ratio > 0 else float(price),
                    })

            if len(quotes) < 2:
                continue

            # 每个发行商取中位数，再比较不同发行商
            by_issuer: Dict[str, List[float]] = {}
            for q in quotes:
                by_issuer.setdefault(q['issuer'], []).append(q['normalized_price'])
            if len(by_issuer) < 2:
                continue

            issuer_prices = []
            for issuer, prices in by_issuer.items():
                prices.sort()
                mid = len(prices) // 2
                median = prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
                issuer_prices.append({'issuer': issuer, 'price': median})
            issuer_prices.sort(key=lambda x: x['price'])
            lo, hi = issuer_prices[0]['price'], issuer_prices[-1]['price']
            if lo <= 0:
                continue
            spread = (hi - lo) / lo
            # 超过 50% 视为份额比例/符号映射不一致，不作为价差信号
            if abs(spread) > 0.5:
                continue

            rows.append({
                'underlying': underlying,
                'quotes': sorted(quotes, key=lambda q: q['price']),
                'issuer_prices': issuer_prices,
                'low': lo,
                'high': hi,
                'spread': spread,
                'abs_spread': abs(spread),
                'issuer_count': len(issuer_prices),
                'exchange_count': len(set(q['exchange'] for q in quotes)),
            })

        rows.sort(key=lambda r: -r['abs_spread'])
        return rows

    # ---------- 异常检测 ----------

    def _check_funding_anomalies(self, apr_ranking: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """极端资金费率告警（APR 口径）"""
        alerts = []
        min_abs_apr = float(self.config.get('alerts', 'funding_apr_threshold', 10.0))  # 年化 1000%
        for row in apr_ranking:
            rate = row['funding_rate']
            apr = row['apr']
            if apr is None or abs(apr) < min_abs_apr:
                continue

            interval_h = row.get('funding_interval_hours')
            per_day = (24 / interval_h) if interval_h else None
            freq = f"每 {interval_h:g}h（每日 {per_day:.0f} 次）" if interval_h else "结算周期未知"
            semantics = '下期将收取值'

            alerts.append({
                'alert_type': 'funding_extreme',
                'level': 'critical' if abs(apr) >= min_abs_apr * 2 else 'warning',
                'symbol': row['symbol'],
                'exchange': row['exchange'],
                'message': (
                    f"{row['symbol']} @ {row['exchange']} | 当前费率 {rate*100:+.4f}% | "
                    f"频率 {freq} | 年化 APR {apr*100:+.1f}% | {semantics}"
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

    def _check_issuer_spread_anomalies(self, spreads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """跨发行商价差告警（同一标的的不同 RWA 版本偏离）"""
        alerts = []
        threshold = float(self.config.get('alerts', 'issuer_spread_threshold', 0.01))  # 1%

        for row in spreads:
            if row['abs_spread'] < threshold:
                continue
            quotes = row['quotes']
            def _quote_label(q):
                normalized = q.get('normalized_price', q['price'])
                label = f"{q['symbol']}({q['issuer'] or q['exchange']}) ${normalized:.4f}"
                if abs(normalized - q['price']) > 1e-9:
                    label += f" [raw ${q['price']:.4f}]"
                return label
            detail = ' | '.join(_quote_label(q) for q in quotes[:5])
            alerts.append({
                'alert_type': 'issuer_spread',
                'level': 'critical' if row['abs_spread'] >= threshold * 3 else 'warning',
                'symbol': row['underlying'],
                'exchange': 'multi',
                'message': (
                    f"{row['underlying']} 跨发行商价差 {row['abs_spread']*100:.2f}%: {detail}"
                ),
                'details': row,
            })
        return alerts

    def _check_depeg_anomalies(self, depeg_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """锚定资产脱锚告警（分级：稳定币/黄金/股票各有阈值）"""
        alerts = []
        category_label = {
            'usd_stable': '稳定币',
            'eur_stable': '欧元稳定币',
            'commodity': '大宗商品',
            'stock': '股票代币',
        }
        for row in depeg_list:
            dev = row['abs_deviation']
            if dev >= row['threshold_critical']:
                level = 'critical'
            elif dev >= row['threshold_warning']:
                level = 'warning'
            else:
                continue

            # 标的休市时参考价为收盘价，非 critical 偏离不推送，避免周末/盘后误报
            if row.get('market_closed') and level != 'critical':
                continue

            label = category_label.get(row['category'], '锚定资产')
            alerts.append({
                'alert_type': 'stablecoin_depeg',
                'level': level,
                'symbol': row['symbol'],
                'exchange': row['exchange'],
                'message': (
                    f"{label}脱锚: {row['symbol']} @ {row['exchange']} "
                    f"价格 {row['price']:.6f} vs 参考 {row['reference']:.6f} "
                    f"(偏离 {row['deviation']*100:+.3f}%)"
                ),
                'details': row,
            })
        return alerts
