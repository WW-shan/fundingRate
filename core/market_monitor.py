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
            interval = self.config.get_int('global', 'monitor_scan_interval', 60)
            # 采集线程尚未产出任何数据时先等待，避免启动瞬间写入空快照
            if not self.data_collector.market_data and not self.data_collector.stablecoin_data:
                time.sleep(2)
                continue
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

    def _funding_min_volume(self) -> float:
        """资金费率/基差机会的最小 24h 成交额（USDT）"""
        try:
            return max(0.0, float(self.config.get('global', 'funding_min_volume_usd', 500_000) or 0))
        except (TypeError, ValueError):
            return 500_000.0

    @staticmethod
    def _volume_ok(volume: Any, min_volume: float) -> bool:
        if min_volume <= 0:
            return True
        try:
            return float(volume or 0) >= min_volume
        except (TypeError, ValueError):
            return False

    def _futures_volume(self, exchange: str, symbol: str, data: Dict[str, Any]) -> float:
        """优先使用价格快照中的成交量，缺失时回退到采集器的全量永续成交量快照"""
        volume = data.get('futures_quote_volume')
        if volume is None and self.data_collector:
            getter = getattr(self.data_collector, 'get_futures_volume', None)
            if getter:
                try:
                    volume = getter(exchange, symbol)
                except Exception:
                    volume = None
        try:
            return float(volume or 0)
        except (TypeError, ValueError):
            return 0.0

    # ---------- APR 排行 ----------

    def build_apr_ranking(self, market_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """全市场 APR 排行（结算间隔归一化 + 流动性过滤）"""
        rows = []
        min_volume = self._funding_min_volume()
        now_ms = int(time.time() * 1000)
        funding_max_age_ms = max(
            600, self.config.get_int('global', 'funding_refresh_interval', 300) * 2
        ) * 1000
        for symbol, exchanges in market_data.items():
            for exchange, data in exchanges.items():
                rate = data.get('funding_rate')
                interval_ms = data.get('funding_interval')
                if rate is None:
                    continue
                # Ignore stale funding rates left over after a failed exchange fetch.
                funding_ts = data.get('funding_timestamp')
                if funding_ts and now_ms - funding_ts > funding_max_age_ms:
                    continue
                futures_volume = self._futures_volume(exchange, symbol, data)
                if not self._volume_ok(futures_volume, min_volume):
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
                    'futures_quote_volume': futures_volume,
                    'spot_quote_volume': data.get('spot_quote_volume'),
                })
        # 按 |APR| 降序：极端正负费率都排前面（如 -4380% 与 +4380% 同等重要）
        rows.sort(key=lambda r: (r['apr'] is None, -abs(r['apr'] or 0)))
        return rows

    # ---------- 跨所费率差 ----------

    def build_cross_exchange_spreads(self, market_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """跨所年化费率差（两腿各自年化后求差 + 双腿流动性过滤）"""
        rows = []
        min_volume = self._funding_min_volume()
        now_ms = int(time.time() * 1000)
        funding_max_age_ms = max(
            600, self.config.get_int('global', 'funding_refresh_interval', 300) * 2
        ) * 1000
        for symbol, exchanges in market_data.items():
            names = list(exchanges.keys())
            for i, ex_a in enumerate(names):
                for ex_b in names[i + 1:]:
                    a, b = exchanges[ex_a], exchanges[ex_b]
                    ts_a = a.get('funding_timestamp')
                    ts_b = b.get('funding_timestamp')
                    if ((ts_a and now_ms - ts_a > funding_max_age_ms)
                            or (ts_b and now_ms - ts_b > funding_max_age_ms)):
                        continue
                    apr_a = annualize_rate(a.get('funding_rate'), a.get('funding_interval'))
                    apr_b = annualize_rate(b.get('funding_rate'), b.get('funding_interval'))
                    if apr_a is None or apr_b is None:
                        continue
                    vol_a = self._futures_volume(ex_a, symbol, a)
                    vol_b = self._futures_volume(ex_b, symbol, b)
                    if not (self._volume_ok(vol_a, min_volume) and self._volume_ok(vol_b, min_volume)):
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
                        'volume_a': vol_a,
                        'volume_b': vol_b,
                    })
        rows.sort(key=lambda r: -r['abs_spread'])
        return rows

    # ---------- 基差 ----------

    def build_basis_list(self, market_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build spot/perp basis from the current in-memory price snapshot."""
        rows = []
        now_ms = int(time.time() * 1000)
        max_age_ms = max(60, self.config.get_int('global', 'price_stale_threshold', 600)) * 1000
        max_sane_basis = self.config.get_float('alerts', 'basis_sanity_limit', 0.5)
        min_volume = self._funding_min_volume()

        for symbol, exchanges in market_data.items():
            for exchange, data in exchanges.items():
                spot = data.get('spot_price')
                futures = data.get('futures_price')
                ts = data.get('timestamp')
                if not spot or not futures or spot <= 0 or futures <= 0:
                    continue
                if ts and now_ms - ts > max_age_ms:
                    continue
                spot_volume = data.get('spot_quote_volume')
                futures_volume = self._futures_volume(exchange, symbol, data)
                if not (self._volume_ok(spot_volume, min_volume)
                        and self._volume_ok(futures_volume, min_volume)):
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
                    'spot_quote_volume': spot_volume,
                    'futures_quote_volume': futures_volume,
                })

        rows.sort(key=lambda r: -abs(r['basis']))
        return rows

    def build_depeg_list(self, stablecoin_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        计算所有锚定资产相对参考价的偏离度。

        参考价来源：
          - usd_stable: 固定 1.0
          - eur_stable: Yahoo EURUSD=X
          - commodity:  Yahoo GC=F / 商品 ETF
          - stock:      不使用 Yahoo；始终用【其他发行商同标的产品】的中位价
                        （思路类似跨所价差，但比较的是不同产品/发行商）

        股票代币规则：
          - 有同类产品：reference_source='peer'，按跨产品中位价计算偏离；
            不产生 stablecoin_depeg 告警，比价信号由 issuer_spread 检测承担
          - 无同类产品：reference_source='no_peer'，不计算偏离、不参与排序/告警，
            仅展示链上价格
          - 休市与否不再影响股票代币的参考价选择

        其他类别保留原逻辑：
          - 休市时改用同类产品中位价；无同类则 stale_close，仅展示
        """
        from config import peg_assets

        refs = self.data_collector.get_reference_prices()
        registry = self.data_collector.peg_discovery.registry

        # 同类产品价格索引：underlying -> issuer -> [归一化价格]
        # 先按发行商取中位数，再跨发行商取中位数，避免单一发行商的多个交易所刷权重
        now_ms = int(time.time() * 1000)
        max_age_ms = max(120, self.config.get_int('global', 'stablecoin_refresh_interval', 60)) * 2 * 1000
        peer_index: Dict[str, Dict[str, List[float]]] = {}
        for sym, sym_exchanges in stablecoin_data.items():
            e = registry.get(sym) or {}
            ref_sym = e.get('reference_symbol')
            if not ref_sym:
                continue
            issuer = e.get('issuer') or sym.lower()
            ratio = float(e.get('reference_ratio', 1.0)) or 1.0
            for data in sym_exchanges.values():
                p = data.get('price')
                ts = data.get('timestamp')
                if p and p > 0 and (not ts or now_ms - ts <= max_age_ms):
                    peer_index.setdefault(ref_sym, {}).setdefault(issuer, []).append(float(p) / ratio)

        def _peer_median(ref_sym: str, own_issuer: str):
            """返回 (其他发行商中位价, 其他发行商数量)。"""
            issuer_medians = []
            for issuer, prices in peer_index.get(ref_sym, {}).items():
                if issuer == own_issuer or not prices:
                    continue
                prices = sorted(prices)
                mid = len(prices) // 2
                issuer_medians.append(
                    prices[mid] if len(prices) % 2 else (prices[mid - 1] + prices[mid]) / 2
                )
            if not issuer_medians:
                return None, 0
            issuer_medians.sort()
            mid = len(issuer_medians) // 2
            median = (issuer_medians[mid] if len(issuer_medians) % 2
                      else (issuer_medians[mid - 1] + issuer_medians[mid]) / 2)
            return median, len(issuer_medians)

        issuer_spread_threshold = self.config.get_float('alerts', 'issuer_spread_threshold', 0.01)

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
            reference_source = 'reference'
            depeg_signal = True
            ratio = float(entry.get('reference_ratio', 1.0)) if entry else 1.0
            reference = None
            peer_count = 0

            if category == 'usd_stable':
                reference = 1.0
            elif category == 'stock':
                # 股票代币：只和其他发行商的同标的产品比较，绝不使用 Yahoo 价格。
                own_issuer = (entry or {}).get('issuer') or symbol.lower()
                peer_ref, peer_count = _peer_median(ref_symbol, own_issuer)
                if peer_ref:
                    reference = peer_ref * ratio
                    reference_source = 'peer'
                else:
                    reference_source = 'no_peer'
                # 跨产品比价信号统一由 issuer_spread 检测，避免重复告警
                depeg_signal = False
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
                # 无 market_time 时按休市处理：优先同类中位价，否则仅展示
                if reference_time:
                    market_closed = (time.time() - reference_time) > 1800
                else:
                    market_closed = True

                if market_closed:
                    own_issuer = (entry or {}).get('issuer') or symbol.lower()
                    peer_ref, peer_count = _peer_median(ref_symbol, own_issuer)
                    if peer_ref:
                        reference = peer_ref * ratio
                        reference_source = 'peer'
                        reference_time = None
                    else:
                        reference_source = 'stale_close'
                    depeg_signal = False

            if reference_source == 'peer':
                # 单产品偏离中位价约为跨产品价差的一半，阈值同步折半，
                # 保证面板预警与 issuer_spread 告警口径一致
                thresholds = {'warning': issuer_spread_threshold / 2,
                              'critical': issuer_spread_threshold * 1.5}
            else:
                thresholds = peg_assets.get_thresholds(category)

            for exchange, data in exchanges.items():
                price = data.get('price')
                if not price or price <= 0:
                    continue
                # Do not compare quotes kept from an old snapshot after a failed fetch.
                ts = data.get('timestamp')
                if ts and now_ms - ts > max_age_ms:
                    continue

                # 无同类产品/仅有背景价时，不计算偏离、不参与排序和告警
                if reference_source in ('stale_close', 'no_peer'):
                    deviation = None
                else:
                    deviation = (price - reference) / reference

                    # 映射验证：偏离 > 50% 大概率是符号映射错误
                    # 此类数据不参与判定，避免误报
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
                    'reference_source': reference_source,
                    'comparable': reference_source not in ('stale_close', 'no_peer'),
                    'depeg_signal': depeg_signal,
                    'market_closed': market_closed,
                    'peer_count': peer_count,
                    'deviation': deviation,
                    'abs_deviation': abs(deviation) if deviation is not None else None,
                    'threshold_warning': thresholds['warning'],
                    'threshold_critical': thresholds['critical'],
                    'bid': data.get('bid'),
                    'ask': data.get('ask'),
                    'timestamp': data.get('timestamp'),
                })

        # 可比行按偏离排序；无同类/仅背景价的行排在最后
        rows.sort(key=lambda r: (r['abs_deviation'] is None, -(r['abs_deviation'] or 0)))
        return rows

    # ---------- 跨发行商价差（同一标的的不同 RWA 版本） ----------

    def build_cross_issuer_spreads(self, stablecoin_data: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        同一标的在多个发行商之间的价差（如 AAPL 的 bStock/Ondo/xStocks/Reality/gStocks 版本）。
        同一发行商在多个交易所的报价先取中位数，避免把跨所价差误当成跨发行商价差。
        """
        groups = self.data_collector.peg_discovery.get_cross_issuer_groups()
        registry = self.data_collector.peg_discovery.registry
        now_ms = int(time.time() * 1000)
        max_age_ms = max(120, self.config.get_int('global', 'stablecoin_refresh_interval', 60)) * 2 * 1000
        issuer_spread_threshold = self.config.get_float('alerts', 'issuer_spread_threshold', 0.01)
        rows = []

        for underlying, symbols in groups.items():
            quotes = []
            category = None
            for sym in symbols:
                entry = registry.get(sym) or {}
                issuer = entry.get('issuer') or sym.lower()
                category = category or entry.get('category')
                ratio = float(entry.get('reference_ratio', 1.0))
                for exchange, data in (stablecoin_data.get(sym) or {}).items():
                    price = data.get('price')
                    ts = data.get('timestamp')
                    if not price or price <= 0 or (ts and now_ms - ts > max_age_ms):
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
                'category': category,
                'quotes': sorted(quotes, key=lambda q: q['price']),
                'issuer_prices': issuer_prices,
                'low': lo,
                'high': hi,
                'spread': spread,
                'abs_spread': abs(spread),
                'issuer_count': len(issuer_prices),
                'exchange_count': len(set(q['exchange'] for q in quotes)),
                'threshold_warning': issuer_spread_threshold,
                'threshold_critical': issuer_spread_threshold * 3,
            })

        rows.sort(key=lambda r: -r['abs_spread'])
        return rows

    # ---------- 异常检测 ----------

    def _check_funding_anomalies(self, apr_ranking: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """异常资金费率告警：同一标的至少 N 家交易所同时异常才推送"""
        min_abs_apr = self.config.get_float('alerts', 'funding_apr_threshold', 10.0)  # 1000% APR
        min_exchanges = max(2, self.config.get_int('alerts', 'funding_min_exchanges', 2))

        grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in apr_ranking:
            apr = row.get('apr')
            if apr is None or abs(apr) < min_abs_apr:
                continue
            symbol = row.get('symbol')
            exchange = row.get('exchange')
            if not symbol or not exchange:
                continue
            grouped.setdefault(symbol, {})[exchange] = row

        alerts = []
        for symbol, by_exchange in grouped.items():
            rows = sorted(by_exchange.values(), key=lambda r: abs(r.get('apr') or 0), reverse=True)
            if len(rows) < min_exchanges:
                continue

            top = rows[0]
            top_apr = top.get('apr') or 0
            interval_h = top.get('funding_interval_hours')
            leg_parts = []
            leg_details = []
            for row in rows:
                rate = row.get('funding_rate')
                apr = row.get('apr')
                if rate is None or apr is None:
                    continue
                leg_parts.append(f"{row.get('exchange')} {apr * 100:+.1f}%")
                leg_details.append({
                    'exchange': row.get('exchange'),
                    'funding_rate': rate,
                    'apr': apr,
                    'funding_interval_hours': row.get('funding_interval_hours'),
                    'next_funding_time': row.get('next_funding_time'),
                    'rate_semantics': row.get('rate_semantics', 'upcoming'),
                })
            if len(leg_details) < min_exchanges:
                continue

            level = 'critical' if abs(top_apr) >= min_abs_apr * 2 else 'warning'
            message = (
                f"{symbol} 多所资金费率异常（{len(leg_details)}所）: "
                + " | ".join(leg_parts)
                + f" | 最高年化 APR {top_apr * 100:+.1f}%"
            )
            details = {
                'symbol': symbol,
                'exchange': 'multi',
                'exchange_count': len(leg_details),
                'exchanges': leg_details,
                'apr': top_apr,
                'funding_rate': top.get('funding_rate'),
                'funding_interval_hours': interval_h,
                'next_funding_time': top.get('next_funding_time'),
                'rate_semantics': top.get('rate_semantics', 'upcoming'),
                'threshold': min_abs_apr,
                'min_exchanges': min_exchanges,
            }
            alerts.append({
                'alert_type': 'funding_extreme',
                'level': level,
                'symbol': symbol,
                'exchange': 'multi',
                'message': message,
                'details': details,
            })

        alerts.sort(key=lambda a: (0 if a.get('level') == 'critical' else 1,
                                   -abs((a.get('details') or {}).get('apr') or 0)))
        return alerts

    def _check_cross_spread_anomalies(self, spreads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """跨所费率差告警"""
        alerts = []
        threshold = self.config.get_float('alerts', 'cross_spread_threshold', 0.50)  # 年化差 50%
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
        threshold = self.config.get_float('alerts', 'basis_threshold', 0.02)  # 2%
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
        threshold = self.config.get_float('alerts', 'issuer_spread_threshold', 0.01)  # 1%

        for row in spreads:
            # 稳定币/欧元稳定币已有 stablecoin_depeg 告警，避免同一事件重复推送
            if row.get('category') in ('usd_stable', 'eur_stable'):
                continue
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
            label = '跨产品价差' if row.get('category') == 'stock' else '跨发行商价差'
            alerts.append({
                'alert_type': 'issuer_spread',
                'level': 'critical' if row['abs_spread'] >= threshold * 3 else 'warning',
                'symbol': row['underlying'],
                'exchange': 'multi',
                'message': (
                    f"{row['underlying']} {label} {row['abs_spread']*100:.2f}%: {detail}"
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
            # 休市行（对比同类产品/无同类产品可比）不产生脱锚告警：
            # 盘后漂移属正常，比价信号由跨发行商价差检测承担
            if not row.get('depeg_signal', True):
                continue

            dev = row['abs_deviation']
            if dev is None:
                continue
            if dev >= row['threshold_critical']:
                level = 'critical'
            elif dev >= row['threshold_warning']:
                level = 'warning'
            else:
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
