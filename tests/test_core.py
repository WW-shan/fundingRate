"""
核心数据准确性回归测试（stdlib unittest，无网络依赖）
运行: python -m unittest discover -s tests -v
"""
import unittest
from types import SimpleNamespace

from exchanges.base_exchange import BaseExchange
from exchanges.gate_adapter import GateAdapter
from core.market_monitor import annualize_rate, MarketMonitor
from core.alert_manager import AlertManager
from config import peg_assets


class FakeConfig:
    """测试用配置对象，提供与 ConfigManager 相同的数值/布尔读取接口"""

    def __init__(self, values=None):
        self.values = values or {}

    def get(self, category, key, default=None):
        return self.values.get(f"{category}.{key}", default)

    def get_int(self, category, key, default=0):
        try:
            return int(self.get(category, key, default))
        except (TypeError, ValueError):
            return int(default)

    def get_float(self, category, key, default=0.0):
        try:
            return float(self.get(category, key, default))
        except (TypeError, ValueError):
            return float(default)

    def get_bool(self, category, key, default=False):
        value = self.get(category, key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return bool(value)


class DummyExchange:
    _parse_funding_interval = BaseExchange._parse_funding_interval


class FundingIntervalTests(unittest.TestCase):
    def setUp(self):
        self.ex = DummyExchange()

    def test_okx_millisecond_interval_is_not_treated_as_hours(self):
        # OKX 对 2h 特殊周期返回毫秒数字串 '7200000'
        self.assertEqual(self.ex._parse_funding_interval({'interval': '7200000'}), 7_200_000)

    def test_standard_interval_strings(self):
        self.assertEqual(self.ex._parse_funding_interval({'interval': '4h'}), 14_400_000)
        self.assertEqual(self.ex._parse_funding_interval({'interval': '8h'}), 28_800_000)

    def test_gate_seconds_and_bitget_hours(self):
        self.assertEqual(self.ex._parse_funding_interval({'info': {'funding_interval': '28800'}}), 28_800_000)
        self.assertEqual(self.ex._parse_funding_interval({'info': {'fundingRateInterval': '4'}}), 14_400_000)

    def test_invalid_interval_is_rejected(self):
        self.assertIsNone(self.ex._parse_funding_interval({'interval': '999999999999'}))

    def test_apr_uses_actual_interval(self):
        # 1% / 2h = 1% * 4380 = 43.8
        self.assertAlmostEqual(annualize_rate(0.01, 7_200_000), 43.8, places=8)
        # 0.01% / 8h = 0.1095
        self.assertAlmostEqual(annualize_rate(0.0001, 28_800_000), 0.1095, places=8)

    def test_base_canonical_symbol_strips_settle_suffix(self):
        self.assertEqual(BaseExchange.canonical_symbol(None, 'BTC/USDT:USDT'), 'BTC/USDT')

    def test_gate_canonical_symbol_uses_official_market_id(self):
        class FakeGate:
            def market(self, symbol):
                return {'id': 'RED_USDT', 'quote': 'USDT'}

        adapter = GateAdapter.__new__(GateAdapter)
        adapter.exchange = FakeGate()
        self.assertEqual(adapter.canonical_symbol('RedLang/USDT:USDT'), 'RED/USDT')


class FakeDB:
    def __init__(self):
        self.rows = []

    def execute_query(self, sql, params=None):
        self.rows.append((sql, params))
        return []

    def set_config(self, category, key, value, is_hot_reload=True, description=""):
        self.rows.append(("SET_CONFIG", category, key, value, is_hot_reload, description))


class BasisAlertThresholdTests(unittest.TestCase):
    def setUp(self):
        self.monitor = MarketMonitor(FakeConfig(), None, None)

    def test_default_five_percent_threshold_applies_to_all_symbols(self):
        rows = [
            {"symbol": "AAPL/USDT", "exchange": "binance", "basis": 0.049,
             "spot_price": 105.0, "futures_price": 100.0},
            {"symbol": "TSLA/USDT", "exchange": "okx", "basis": 0.05,
             "spot_price": 105.0, "futures_price": 100.0},
            {"symbol": "BTC/USDT", "exchange": "bybit", "basis": -0.05,
             "spot_price": 95.0, "futures_price": 100.0},
        ]

        alerts = self.monitor._check_basis_anomalies(rows)

        self.assertEqual([alert["symbol"] for alert in alerts], ["TSLA/USDT", "BTC/USDT"])

    def test_default_config_uses_five_percent_for_detection_and_push(self):
        from config.config_manager import ConfigManager

        config = ConfigManager(FakeDB())
        config.init_default_configs()

        self.assertEqual(config.get_float("alerts", "basis_threshold"), 0.05)
        self.assertEqual(config.get_float("alerts", "push_basis_threshold"), 0.05)
        self.assertEqual(config.get_float("alerts", "issuer_spread_threshold"), 0.05)

    def test_stock_cross_issuer_alert_uses_five_percent_default(self):
        rows = [
            {"category": "stock", "underlying": "AAPL", "abs_spread": 0.049, "quotes": []},
            {"category": "stock", "underlying": "TSLA", "abs_spread": 0.05, "quotes": []},
        ]

        alerts = self.monitor._check_issuer_spread_anomalies(rows)

        self.assertEqual([alert["symbol"] for alert in alerts], ["TSLA"])


class AlertPushTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            "alerts.push_basis": True,
            "alerts.push_cross_spread": True,
            "alerts.push_funding_apr_threshold": 10.0,
            "alerts.push_basis_threshold": 0.05,
            "alerts.push_reset_gap": 1800,
            "alerts.push_reminder_cooldown": 43200,
            "alerts.persist_cooldown": 1800,
            "alerts.max_pushes_per_scan": 20,
        }
        config = FakeConfig(self.values)
        self.manager = AlertManager(config, FakeDB())

    def test_basis_is_push_enabled(self):
        self.assertTrue(self.manager._should_push({
            "alert_type": "basis_extreme", "details": {"basis": 0.05}
        }))

    def test_basis_below_push_threshold_is_panel_only(self):
        self.assertFalse(self.manager._should_push({
            "alert_type": "basis_extreme", "details": {"basis": 0.049}
        }))

    def test_basis_default_push_threshold_is_five_percent(self):
        manager = AlertManager(FakeConfig(), FakeDB())
        self.assertFalse(manager._should_push({
            "alert_type": "basis_extreme", "details": {"basis": 0.049}
        }))
        self.assertTrue(manager._should_push({
            "alert_type": "basis_extreme", "details": {"basis": -0.05}
        }))

    def test_cross_spread_is_panel_only_by_default(self):
        config = FakeConfig()
        manager = AlertManager(config, None)
        self.assertFalse(manager._should_push({"alert_type": "cross_spread"}))

    def test_funding_push_uses_absolute_apr_threshold(self):
        self.assertTrue(self.manager._should_push({
            "alert_type": "funding_extreme", "details": {"apr": -10.1}
        }))
        self.assertFalse(self.manager._should_push({
            "alert_type": "funding_extreme", "details": {"apr": 9.9}
        }))

    def _alert(self, apr, level="warning"):
        return {
            "alert_type": "funding_extreme", "level": level, "symbol": "TEST/USDT",
            "exchange": "binance", "message": "test", "details": {"apr": apr},
        }

    def test_same_band_is_not_repushed_in_one_batch(self):
        self.assertEqual(self.manager.handle_alerts([self._alert(10.5), self._alert(11.0)]), 1)

    def test_band_upgrade_is_pushed(self):
        self.manager.handle_alerts([self._alert(10.5)])
        pushed = self.manager.handle_alerts([self._alert(25.0)])
        self.assertEqual(pushed, 1)

    def test_band_downgrade_is_not_pushed(self):
        self.manager.handle_alerts([self._alert(60.0)])
        pushed = self.manager.handle_alerts([self._alert(12.0)])
        self.assertEqual(pushed, 0)

    def test_recovery_then_reappear_pushes_again(self):
        a = self._alert(10.5)
        self.manager.handle_alerts([a])
        # 模拟 31 分钟未出现（超过 push_reset_gap=1800）
        key = self.manager._dedup_key(a)
        self.manager._push_state[key]["last_seen"] -= 1900
        self.assertEqual(self.manager.handle_alerts([a]), 1)

    def test_digest_callback_receives_batch(self):
        batches = []
        self.manager.register_push_callback(lambda payload: batches.append(payload))
        self.manager.handle_alerts([self._alert(10.5)])
        self.assertEqual(len(batches), 1)
        self.assertIn("alerts", batches[0])
        self.assertEqual(batches[0]["count"], 1)

    def test_alert_query_filters_in_sql(self):
        self.manager.get_recent_alerts(
            limit=5, hours=24, level="critical", alert_type="funding_extreme"
        )
        sql, params = self.manager.db.rows[-1]
        self.assertIn("level = ?", sql)
        self.assertIn("alert_type = ?", sql)
        self.assertEqual(params[-3:], ("critical", "funding_extreme", 5))

    def test_depeg_market_closed_non_critical_not_pushed(self):
        alert = {
            "alert_type": "stablecoin_depeg", "level": "warning", "symbol": "KORU",
            "exchange": "gate", "details": {"market_closed": True},
        }
        self.assertFalse(self.manager._should_push(alert))


class PegAssetTests(unittest.TestCase):
    def test_usdt_is_monitored_as_usd_stable(self):
        self.assertEqual(peg_assets.classify('USDT'), ('usd_stable', 'USD', 'tether'))

    def test_gold_token_issuer_and_reference(self):
        self.assertEqual(peg_assets.classify('XAUT'), ('commodity', 'XAUUSD', 'tether'))

    def test_reality_gold_tokens_use_public_references(self):
        # rSGOL is a tokenized SGOL ETF share; rGOLD is Gold.com equity.
        # Neither is a 0.01 oz bullion token, so the old GC=F * 0.01 mapping was wrong.
        self.assertEqual(peg_assets.classify("RSGOL"), ("stock", "SGOL", "reality"))
        self.assertEqual(peg_assets.classify("RGOLD"), ("stock", "GOLD", "reality"))
        self.assertEqual(peg_assets.get_reference_ratio("RSGOL"), 1.0)
        self.assertEqual(peg_assets.get_reference_ratio("RGOLD"), 1.0)

    def test_unlisted_reality_company_is_not_given_a_fake_reference(self):
        self.assertEqual(peg_assets.classify("RFOIL"), (None, None, None))

    def test_commodity_thresholds_allow_futures_basis(self):
        self.assertEqual(peg_assets.get_thresholds('commodity'), {'warning': 0.02, 'critical': 0.05})


class MarketMonitorStockComparisonTests(unittest.TestCase):
    """股票代币：不再和 Yahoo 比，而是和其他发行商同标的产品中位价比较"""

    def _build_monitor(self, stablecoin_data, refs, registry):
        from core.market_monitor import MarketMonitor

        class FakeDiscovery:
            def __init__(self):
                self.registry = registry

            def get_cross_issuer_groups(self):
                groups = {}
                for sym, entry in registry.items():
                    ref = entry.get('reference_symbol')
                    if ref:
                        groups.setdefault(ref, []).append(sym)
                return {k: v for k, v in groups.items() if len(v) > 1}

        discovery = FakeDiscovery()

        class FakeCollector:
            def get_reference_prices(self_inner):
                return refs

        collector = FakeCollector()
        collector.peg_discovery = discovery

        config = FakeConfig()
        return MarketMonitor(config, None, collector)

    def _registry(self):
        return {
            "AAPLA": {"category": "stock", "reference_symbol": "AAPL", "issuer": "bstock", "reference_ratio": 1.0},
            "AAPLB": {"category": "stock", "reference_symbol": "AAPL", "issuer": "ondo", "reference_ratio": 1.0},
            "AAPLC": {"category": "stock", "reference_symbol": "AAPL", "issuer": "okx", "reference_ratio": 1.0},
        }

    def test_stock_uses_peer_median_when_market_closed(self):
        import time

        now_ms = int(time.time() * 1000)
        old_market_time = int(time.time()) - 7200
        refs = {"AAPL": {"price": 200.0, "timestamp": now_ms, "market_time": old_market_time}}
        data = {
            "AAPLA": {"gate": {"price": 180.0, "timestamp": now_ms}},
            "AAPLB": {"binance": {"price": 181.0, "timestamp": now_ms}},
            "AAPLC": {"okx": {"price": 182.0, "timestamp": now_ms}},
        }
        mm = self._build_monitor(data, refs, self._registry())
        rows = {r["symbol"]: r for r in mm.build_depeg_list(data)}

        self.assertEqual(rows["AAPLA"]["reference_source"], "peer")
        self.assertFalse(rows["AAPLA"]["depeg_signal"])
        # 同类中位价 = median(181, 182) = 181.5（不含自身 180）
        self.assertAlmostEqual(rows["AAPLA"]["reference"], 181.5)
        self.assertEqual([a for a in mm._check_depeg_anomalies(list(rows.values()))
                          if a["symbol"] == "AAPLA"], [])

    def test_stock_uses_peer_median_when_market_open(self):
        import time

        now_s = int(time.time())
        refs = {"AAPL": {"price": 200.0, "timestamp": now_s * 1000, "market_time": now_s - 60}}
        data = {
            "AAPLA": {"gate": {"price": 180.0, "timestamp": now_s * 1000}},
            "AAPLB": {"binance": {"price": 181.0, "timestamp": now_s * 1000}},
            "AAPLC": {"okx": {"price": 182.0, "timestamp": now_s * 1000}},
        }
        mm = self._build_monitor(data, refs, self._registry())
        rows = {r["symbol"]: r for r in mm.build_depeg_list(data)}

        # 即使美股开盘，也不使用 Yahoo 的 200，而是用同类中位价 181.5
        self.assertEqual(rows["AAPLA"]["reference_source"], "peer")
        self.assertAlmostEqual(rows["AAPLA"]["reference"], 181.5)
        self.assertFalse(rows["AAPLA"]["depeg_signal"])

    def test_stock_without_peer_marks_no_peer(self):
        import time

        now_s = int(time.time())
        refs = {"AAPL": {"price": 200.0, "timestamp": now_s * 1000, "market_time": now_s - 60}}
        registry = {
            "AAPLA": {"category": "stock", "reference_symbol": "AAPL", "issuer": "bstock", "reference_ratio": 1.0},
        }
        data = {"AAPLA": {"gate": {"price": 150.0, "timestamp": now_s * 1000}}}
        mm = self._build_monitor(data, refs, registry)
        rows = mm.build_depeg_list(data)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reference_source"], "no_peer")
        self.assertIsNone(rows[0]["reference"])
        self.assertFalse(rows[0]["depeg_signal"])
        self.assertFalse(rows[0]["comparable"])
        self.assertIsNone(rows[0]["deviation"])
        self.assertIsNone(rows[0]["abs_deviation"])
        self.assertEqual(mm._check_depeg_anomalies(rows), [])

    def test_no_peer_rows_sort_after_comparable_rows(self):
        import time

        now_s = int(time.time())
        refs = {
            "AAPL": {"price": 200.0, "timestamp": now_s * 1000, "market_time": now_s - 60},
            "MSFT": {"price": 300.0, "timestamp": now_s * 1000, "market_time": now_s - 60},
        }
        registry = {
            "AAPLA": {"category": "stock", "reference_symbol": "AAPL", "issuer": "bstock", "reference_ratio": 1.0},
            "AAPLB": {"category": "stock", "reference_symbol": "AAPL", "issuer": "ondo", "reference_ratio": 1.0},
            "MSFTR": {"category": "stock", "reference_symbol": "MSFT", "issuer": "reality", "reference_ratio": 1.0},
        }
        data = {
            "AAPLA": {"gate": {"price": 210.0, "timestamp": now_s * 1000}},
            "AAPLB": {"binance": {"price": 200.0, "timestamp": now_s * 1000}},
            "MSFTR": {"bitget": {"price": 250.0, "timestamp": now_s * 1000}},
        }
        mm = self._build_monitor(data, refs, registry)
        rows = mm.build_depeg_list(data)

        self.assertEqual(rows[0]["symbol"], "AAPLA")
        self.assertEqual(rows[1]["symbol"], "AAPLB")
        self.assertEqual(rows[-1]["symbol"], "MSFTR")
        self.assertEqual(rows[-1]["reference_source"], "no_peer")
        self.assertIsNone(rows[-1]["abs_deviation"])

    def test_stock_peer_deviation_does_not_create_depeg_alert(self):
        import time

        now_s = int(time.time())
        refs = {"AAPL": {"price": 200.0, "timestamp": now_s * 1000, "market_time": now_s - 60}}
        data = {
            "AAPLA": {"gate": {"price": 190.0, "timestamp": now_s * 1000}},
            "AAPLB": {"binance": {"price": 180.0, "timestamp": now_s * 1000}},
        }
        registry = {
            "AAPLA": {"category": "stock", "reference_symbol": "AAPL", "issuer": "bstock", "reference_ratio": 1.0},
            "AAPLB": {"category": "stock", "reference_symbol": "AAPL", "issuer": "ondo", "reference_ratio": 1.0},
        }
        mm = self._build_monitor(data, refs, registry)
        rows = mm.build_depeg_list(data)

        # 偏离约 5.6%，但股票代币不产生脱锚告警，交给 issuer_spread
        self.assertGreater(rows[0]["abs_deviation"], 0.05)
        self.assertEqual(mm._check_depeg_anomalies(rows), [])

    def test_cross_issuer_spread_detects_stock_divergence(self):
        import time

        now_s = int(time.time())
        registry = {
            "RSOXL": {"category": "stock", "reference_symbol": "SOXL", "issuer": "reality", "reference_ratio": 1.0},
            "SOXLG": {"category": "stock", "reference_symbol": "SOXL", "issuer": "gstocks", "reference_ratio": 1.0},
            "XSOXL": {"category": "stock", "reference_symbol": "SOXL", "issuer": "okx", "reference_ratio": 1.0},
        }
        data = {
            "RSOXL": {"bitget": {"price": 92.07, "timestamp": now_s * 1000}},
            "SOXLG": {"gate": {"price": 92.11, "timestamp": now_s * 1000}},
            "XSOXL": {"okx": {"price": 98.0, "timestamp": now_s * 1000}},
        }
        mm = self._build_monitor(data, {}, registry)
        spreads = mm.build_cross_issuer_spreads(data)
        soxl = [s for s in spreads if s["underlying"] == "SOXL"][0]

        self.assertEqual(soxl["category"], "stock")
        self.assertAlmostEqual(soxl["spread"], (98.0 - 92.07) / 92.07, places=6)
        self.assertEqual(soxl["issuer_count"], 3)
        alerts = mm._check_issuer_spread_anomalies(spreads)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["symbol"], "SOXL")

    def test_commodity_uses_spot_gold_reference(self):
        import time

        now_s = int(time.time())
        refs = {"XAUUSD": {"price": 2000.0, "timestamp": now_s * 1000, "market_time": now_s - 60,
                                 "source": "gold_api", "instrument": "XAU/USD spot"}}
        registry = {
            "XAUT": {"category": "commodity", "reference_symbol": "XAUUSD", "issuer": "tether", "reference_ratio": 1.0},
        }
        data = {"XAUT": {"gate": {"price": 2010.0, "timestamp": now_s * 1000}}}
        mm = self._build_monitor(data, refs, registry)
        rows = mm.build_depeg_list(data)

        self.assertEqual(rows[0]["reference_source"], "reference")
        self.assertTrue(rows[0]["depeg_signal"])
        self.assertAlmostEqual(rows[0]["deviation"], 0.005)


class LiquidityFilterTests(unittest.TestCase):
    """资金费率/基差机会必须满足最小 24h 成交额"""

    def setUp(self):
        import time
        self.now = int(time.time() * 1000)
        values = {"global.funding_min_volume_usd": 500_000}
        config = FakeConfig(values)

        class FakeCollector:
            def __init__(self):
                self.volumes = {("gate", "FALLBACK/USDT"): 600_000}

            def get_futures_volume(self, exchange, symbol):
                return self.volumes.get((exchange, symbol), 0)

        self.mm = MarketMonitor(config, None, FakeCollector())

    def test_apr_filters_low_volume_and_uses_fallback(self):
        data = {
            "LIQ/USDT": {"gate": {"funding_rate": 0.01, "funding_interval": 3_600_000,
                                  "futures_quote_volume": 600_000}},
            "ILLIQ/USDT": {"gate": {"funding_rate": 0.01, "funding_interval": 3_600_000,
                                    "futures_quote_volume": 100_000}},
            "MISSING/USDT": {"gate": {"funding_rate": 0.01, "funding_interval": 3_600_000}},
            "FALLBACK/USDT": {"gate": {"funding_rate": 0.01, "funding_interval": 3_600_000}},
        }
        symbols = {r["symbol"] for r in self.mm.build_apr_ranking(data)}
        self.assertIn("LIQ/USDT", symbols)
        self.assertIn("FALLBACK/USDT", symbols)
        self.assertNotIn("ILLIQ/USDT", symbols)
        self.assertNotIn("MISSING/USDT", symbols)

    def test_cross_spread_requires_both_legs_liquid(self):
        data = {"BTC/USDT": {
            "binance": {"funding_rate": 0.001, "funding_interval": 3_600_000,
                        "futures_quote_volume": 2_000_000},
            "gate": {"funding_rate": 0.01, "funding_interval": 3_600_000,
                     "futures_quote_volume": 100_000},
        }}
        self.assertEqual(self.mm.build_cross_exchange_spreads(data), [])
        data["BTC/USDT"]["gate"]["futures_quote_volume"] = 600_000
        rows = self.mm.build_cross_exchange_spreads(data)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["volume_a"], 2_000_000)
        self.assertEqual(rows[0]["volume_b"], 600_000)

    def test_basis_requires_spot_and_futures_liquidity(self):
        def entry(spot_vol, fut_vol):
            return {"spot_price": 100, "futures_price": 102,
                    "spot_quote_volume": spot_vol, "futures_quote_volume": fut_vol,
                    "timestamp": self.now}

        data = {
            "LIQ/USDT": {"gate": entry(600_000, 600_000)},
            "LOWSPOT/USDT": {"gate": entry(100_000, 600_000)},
            "LOWFUT/USDT": {"gate": entry(600_000, 100_000)},
        }
        rows = self.mm.build_basis_list(data)
        self.assertEqual([r["symbol"] for r in rows], ["LIQ/USDT"])
        self.assertEqual(rows[0]["spot_quote_volume"], 600_000)
        self.assertEqual(rows[0]["futures_quote_volume"], 600_000)


class TelegramDigestTests(unittest.TestCase):
    def test_digest_truncates_before_telegram_limit(self):
        from bot.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        alerts = []
        for i in range(60):
            alerts.append({
                "alert_type": "issuer_spread", "level": "warning",
                "symbol": f"SYM{i}", "exchange": "multi", "message": "x",
                "details": {
                    "underlying": f"SYM{i}", "abs_spread": 0.05,
                    "issuer_count": 3, "exchange_count": 3,
                    "quotes": [
                        {"symbol": f"A{i}", "issuer": "reality", "price": 100.0, "normalized_price": 100.0},
                        {"symbol": f"B{i}", "issuer": "tether", "price": 105.0, "normalized_price": 105.0},
                        {"symbol": f"C{i}", "issuer": "paxos", "price": 106.0, "normalized_price": 106.0},
                    ],
                },
            })
        msg = bot._format_digest(alerts)
        self.assertLessEqual(len(msg), 4000)
        self.assertIn("另有", msg)


class StalenessRegressionTests(unittest.TestCase):
    """Stale snapshots must not keep producing APR/spread/depeg signals."""

    def test_stale_funding_rows_are_ignored(self):
        import time
        from core.market_monitor import MarketMonitor

        mm = MarketMonitor(FakeConfig(), None, None)
        old = int(time.time() * 1000) - 3600 * 1000
        data = {
            "BTC/USDT": {
                "binance": {"funding_rate": 0.01, "funding_interval": 8 * 3600 * 1000,
                            "funding_timestamp": old, "futures_quote_volume": 10_000_000},
                "gate": {"funding_rate": -0.01, "funding_interval": 8 * 3600 * 1000,
                         "funding_timestamp": old, "futures_quote_volume": 10_000_000},
            }
        }
        self.assertEqual(mm.build_apr_ranking(data), [])
        self.assertEqual(mm.build_cross_exchange_spreads(data), [])

    def test_stale_depeg_quotes_are_ignored(self):
        import time
        from core.market_monitor import MarketMonitor

        class FakeDiscovery:
            registry = {
                "USDCA": {"category": "usd_stable", "reference_symbol": "USD",
                          "issuer": "circle", "reference_ratio": 1.0},
            }

            def get_cross_issuer_groups(self):
                return {}

        class FakeCollector:
            def get_reference_prices(self):
                return {}

            peg_discovery = FakeDiscovery()

        mm = MarketMonitor(FakeConfig(), None, FakeCollector())
        old = int(time.time() * 1000) - 3600 * 1000
        data = {"USDCA": {"binance": {"price": 0.5, "timestamp": old}}}
        self.assertEqual(mm.build_depeg_list(data), [])


if __name__ == '__main__':
    unittest.main()

class RegressionAuditTests(unittest.TestCase):
    """本轮端到端审计发现的回归点"""

    def test_cross_spread_dedup_key_is_order_independent(self):
        am = AlertManager(FakeConfig(), None)
        a = {"alert_type": "cross_spread", "symbol": "X/USDT", "exchange": "binance/gate"}
        b = {"alert_type": "cross_spread", "symbol": "X/USDT", "exchange": "gate/binance"}
        self.assertEqual(am._dedup_key(a), am._dedup_key(b))

    def test_issuer_spread_alert_skips_stablecoins(self):
        from core.market_monitor import MarketMonitor

        mm = MarketMonitor(FakeConfig(), None, None)
        rows = [
            {"category": "usd_stable", "underlying": "USD", "abs_spread": 0.02, "quotes": []},
            {"category": "stock", "underlying": "AAPL", "abs_spread": 0.06, "quotes": []},
        ]
        alerts = mm._check_issuer_spread_anomalies(rows)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["symbol"], "AAPL")

    def test_peg_quote_volume_falls_back_to_base_volume(self):
        self.assertAlmostEqual(
            peg_assets.PegAssetDiscovery._ticker_quote_volume(
                {"quoteVolume": None, "baseVolume": 10, "last": 2.5}
            ),
            25.0,
        )
        self.assertAlmostEqual(
            peg_assets.PegAssetDiscovery._ticker_quote_volume({"quoteVolume": 7.5}),
            7.5,
        )

    def test_market_data_snapshot_is_isolated(self):
        from core.data_collector import DataCollector

        obj = SimpleNamespace(market_data={"A/USDT": {"binance": {"x": 1}}})
        snap = DataCollector.get_market_data(obj)
        snap["A/USDT"]["binance"]["x"] = 2
        snap["B/USDT"] = {}
        self.assertEqual(obj.market_data["A/USDT"]["binance"]["x"], 1)
        self.assertNotIn("B/USDT", obj.market_data)
    def test_telegram_queues_message_when_loop_not_ready(self):
        import threading
        from bot.telegram_bot import TelegramBot

        bot = object.__new__(TelegramBot)
        bot.app = object()
        bot.chat_id = "test"
        bot._loop = None
        bot._pending_messages = []
        bot._pending_lock = threading.Lock()
        bot._send_async("hello")
        self.assertEqual(bot._pending_messages, ["hello"])

    def test_config_get_accepts_capitalized_boolean_strings(self):
        from config.config_manager import ConfigManager

        cm = object.__new__(ConfigManager)
        cm._config_cache = {"alerts.push_depeg": "False", "alerts.push_basis": "True"}
        self.assertFalse(cm.get("alerts", "push_depeg", True))
        self.assertTrue(cm.get("alerts", "push_basis", False))

class FundingMultiExchangeAlertTests(unittest.TestCase):
    """Funding alerts require the same symbol to be extreme on multiple exchanges."""

    @staticmethod
    def _row(exchange, apr, rate=0.01):
        return {
            "symbol": "BTC/USDT",
            "exchange": exchange,
            "funding_rate": rate,
            "apr": apr,
            "funding_interval_hours": 8,
            "next_funding_time": 123,
            "rate_semantics": "upcoming",
        }

    @staticmethod
    def _monitor(min_exchanges=2):
        config = FakeConfig({
            "alerts.funding_apr_threshold": 10.0,
            "alerts.funding_min_exchanges": min_exchanges,
        })
        return MarketMonitor(config, None, None)

    def test_single_exchange_is_not_alerted(self):
        alerts = self._monitor()._check_funding_anomalies([self._row("binance", 12.0)])
        self.assertEqual(alerts, [])

    def test_two_exchanges_produce_one_combined_alert(self):
        alerts = self._monitor()._check_funding_anomalies([
            self._row("binance", 12.0),
            self._row("gate", -25.0, -0.02),
        ])
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["symbol"], "BTC/USDT")
        self.assertEqual(alert["exchange"], "multi")
        self.assertEqual(alert["level"], "critical")
        self.assertEqual(alert["details"]["exchange_count"], 2)
        self.assertEqual(
            {x["exchange"] for x in alert["details"]["exchanges"]},
            {"binance", "gate"},
        )
        self.assertIn("binance", alert["message"])
        self.assertIn("gate", alert["message"])

    def test_below_threshold_leg_does_not_count(self):
        alerts = self._monitor()._check_funding_anomalies([
            self._row("binance", 12.0),
            self._row("gate", 5.0),
        ])
        self.assertEqual(alerts, [])

    def test_min_exchanges_is_configurable(self):
        rows = [
            self._row("binance", 12.0),
            self._row("gate", 13.0),
            self._row("okx", 14.0),
        ]
        self.assertEqual(self._monitor(3)._check_funding_anomalies(rows[:2]), [])
        self.assertEqual(len(self._monitor(3)._check_funding_anomalies(rows)), 1)

    def test_telegram_formats_combined_funding_alert(self):
        from bot.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        alert = {
            "alert_type": "funding_extreme",
            "level": "warning",
            "symbol": "BTC/USDT",
            "exchange": "multi",
            "message": "x",
            "details": {
                "symbol": "BTC/USDT",
                "exchange": "multi",
                "exchange_count": 2,
                "apr": 12.0,
                "funding_rate": 0.01,
                "funding_interval_hours": 8,
                "exchanges": [
                    {"exchange": "binance", "apr": 12.0, "funding_rate": 0.01, "funding_interval_hours": 8},
                    {"exchange": "gate", "apr": -11.0, "funding_rate": -0.01, "funding_interval_hours": 8},
                ],
            },
        }
        line = bot._format_alert_line(alert)
        single = bot._format_single_alert(alert)
        for text in (line, single):
            self.assertIn("BINANCE", text.upper())
            self.assertIn("GATE", text.upper())
            self.assertIn("2", text)

    def test_okx_safe_loader_drops_malformed_markets(self):
        import threading
        from exchanges.okx_adapter import OKXAdapter

        class FakeExchange:
            def __init__(self):
                self.markets = {}
                self.loaded = []

            def fetch_markets(self, params=None):
                return [
                    {"id": "BTC-USDT", "symbol": "BTC/USDT"},
                    {"id": None, "symbol": None},
                ]

            def set_markets(self, markets):
                self.loaded = markets
                self.markets = {m["symbol"]: m for m in markets}

        adapter = OKXAdapter.__new__(OKXAdapter)
        adapter.exchange = FakeExchange()
        adapter._markets_lock = threading.Lock()
        adapter._install_safe_load_markets()
        adapter.exchange.load_markets()
        self.assertEqual(len(adapter.exchange.loaded), 1)
        self.assertEqual(adapter.exchange.loaded[0]["id"], "BTC-USDT")

    def test_push_alert_sends_one_message_per_event(self):
        from bot.telegram_bot import TelegramBot

        bot = TelegramBot.__new__(TelegramBot)
        sent = []
        bot._send_async = lambda message: sent.append(message)
        alerts = [
            {
                "alert_type": "basis_extreme", "level": "warning",
                "symbol": "A/USDT", "exchange": "gate", "message": "A/USDT @ gate basis",
                "details": {"symbol": "A/USDT", "exchange": "gate", "basis": 0.03,
                            "spot_price": 1.0, "futures_price": 1.03},
            },
            {
                "alert_type": "basis_extreme", "level": "warning",
                "symbol": "B/USDT", "exchange": "okx", "message": "B/USDT @ okx basis",
                "details": {"symbol": "B/USDT", "exchange": "okx", "basis": -0.04,
                            "spot_price": 2.0, "futures_price": 1.92},
            },
        ]
        bot.push_alert({"alerts": alerts})
        self.assertEqual(len(sent), 2)
        self.assertIn("A/USDT", sent[0])
        self.assertIn("B/USDT", sent[1])

class ReferencePriceParserTests(unittest.TestCase):
    def test_gold_api_payload(self):
        from core.data_collector import DataCollector

        data = DataCollector._parse_gold_api_payload(
            {"price": 4119.3, "updatedAt": "2026-10-21T03:58:10Z"}, 123456
        )
        self.assertEqual(data["price"], 4119.3)
        self.assertEqual(data["source"], "gold_api")
        self.assertEqual(data["instrument"], "XAU/USD spot")
        self.assertIsNotNone(data["market_time"])

    def test_swissquote_payload_uses_bid_ask_mid(self):
        from core.data_collector import DataCollector

        payload = [{
            "ts": 1792555249393,
            "spreadProfilePrices": [
                {"spreadProfile": "premium", "bid": 4118.0, "ask": 4119.0},
            ],
        }]
        data = DataCollector._parse_swissquote_payload(payload, 123456)
        self.assertEqual(data["price"], 4118.5)
        self.assertEqual(data["source"], "swissquote")
        self.assertEqual(data["market_time"], 1792555249)

    def test_gold_api_invalid_price_is_rejected(self):
        from core.data_collector import DataCollector

        self.assertIsNone(DataCollector._parse_gold_api_payload({"price": None}, 1))
        self.assertIsNone(DataCollector._parse_gold_api_payload({"price": 0}, 1))
