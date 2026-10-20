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


class AlertPushTests(unittest.TestCase):
    def setUp(self):
        self.values = {
            "alerts.push_basis": True,
            "alerts.push_cross_spread": True,
            "alerts.push_funding_apr_threshold": 10.0,
            "alerts.push_basis_threshold": 0.03,
            "alerts.push_reset_gap": 1800,
            "alerts.push_reminder_cooldown": 43200,
            "alerts.persist_cooldown": 1800,
            "alerts.max_pushes_per_scan": 20,
        }
        config = SimpleNamespace(get=lambda category, key, default=None: self.values.get(f"{category}.{key}", default))
        self.manager = AlertManager(config, FakeDB())

    def test_basis_is_push_enabled(self):
        self.assertTrue(self.manager._should_push({
            "alert_type": "basis_extreme", "details": {"basis": 0.05}
        }))

    def test_basis_below_push_threshold_is_panel_only(self):
        self.assertFalse(self.manager._should_push({
            "alert_type": "basis_extreme", "details": {"basis": 0.025}
        }))

    def test_cross_spread_is_panel_only_by_default(self):
        config = SimpleNamespace(get=lambda category, key, default=None: default)
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
        self.assertEqual(peg_assets.classify('XAUT'), ('commodity', 'GC=F', 'tether'))

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


class MarketMonitorClosedMarketTests(unittest.TestCase):
    """休市期间：不拿收盘价当锚，改用同类产品中位价"""

    def _build_monitor(self, stablecoin_data, refs, registry):
        from core.market_monitor import MarketMonitor

        class FakeDiscovery:
            pass

        discovery = FakeDiscovery()
        discovery.registry = registry

        class FakeCollector:
            def get_reference_prices(self_inner):
                return refs

        collector = FakeCollector()
        collector.peg_discovery = discovery

        config = SimpleNamespace(get=lambda category, key, default=None: default)
        return MarketMonitor(config, None, collector)

    def test_closed_market_uses_peer_median(self):
        import time

        old_market_time = int(time.time()) - 7200  # 2h 前 => 休市
        refs = {"AAPL": {"price": 200.0, "timestamp": int(time.time() * 1000), "market_time": old_market_time}}
        registry = {
            "AAPLA": {"category": "stock", "reference_symbol": "AAPL", "issuer": "bstock", "reference_ratio": 1.0},
            "AAPLB": {"category": "stock", "reference_symbol": "AAPL", "issuer": "ondo", "reference_ratio": 1.0},
            "AAPLC": {"category": "stock", "reference_symbol": "AAPL", "issuer": "okx", "reference_ratio": 1.0},
        }
        now = int(time.time() * 1000)
        data = {
            "AAPLA": {"gate": {"price": 180.0, "timestamp": now}},
            "AAPLB": {"binance": {"price": 181.0, "timestamp": now}},
            "AAPLC": {"okx": {"price": 182.0, "timestamp": now}},
        }
        mm = self._build_monitor(data, refs, registry)
        rows = {r["symbol"]: r for r in mm.build_depeg_list(data)}

        self.assertEqual(rows["AAPLA"]["reference_source"], "peer")
        self.assertFalse(rows["AAPLA"]["depeg_signal"])
        self.assertTrue(rows["AAPLA"]["market_closed"])
        # 同类中位价 = median(181, 182) = 181.5（不含自身 180）
        self.assertAlmostEqual(rows["AAPLA"]["reference"], 181.5)
        # 休市行不产生脱锚告警
        self.assertEqual([a for a in mm._check_depeg_anomalies(list(rows.values()))
                          if a["symbol"] == "AAPLA"], [])

    def test_closed_market_without_peer_marks_stale_close(self):
        import time

        old_market_time = int(time.time()) - 7200
        refs = {"AAPL": {"price": 200.0, "timestamp": int(time.time() * 1000), "market_time": old_market_time}}
        registry = {
            "AAPLA": {"category": "stock", "reference_symbol": "AAPL", "issuer": "bstock", "reference_ratio": 1.0},
        }
        data = {"AAPLA": {"gate": {"price": 150.0, "timestamp": int(time.time() * 1000)}}}
        mm = self._build_monitor(data, refs, registry)
        rows = mm.build_depeg_list(data)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reference_source"], "stale_close")
        self.assertFalse(rows[0]["depeg_signal"])
        self.assertFalse(rows[0]["comparable"])
        self.assertIsNone(rows[0]["deviation"])
        self.assertIsNone(rows[0]["abs_deviation"])
        self.assertEqual(mm._check_depeg_anomalies(rows), [])

    def test_closed_market_without_peer_sorts_after_comparable_rows(self):
        import time

        now_s = int(time.time())
        old_market_time = now_s - 7200
        refs = {
            "AAPL": {"price": 200.0, "timestamp": now_s * 1000, "market_time": now_s - 60},
            "MSFT": {"price": 300.0, "timestamp": now_s * 1000, "market_time": old_market_time},
        }
        registry = {
            "AAPLA": {"category": "stock", "reference_symbol": "AAPL", "issuer": "bstock", "reference_ratio": 1.0},
            "MSFTR": {"category": "stock", "reference_symbol": "MSFT", "issuer": "reality", "reference_ratio": 1.0},
        }
        data = {
            "AAPLA": {"gate": {"price": 210.0, "timestamp": now_s * 1000}},
            "MSFTR": {"bitget": {"price": 250.0, "timestamp": now_s * 1000}},
        }
        mm = self._build_monitor(data, refs, registry)
        rows = mm.build_depeg_list(data)

        self.assertEqual([r["symbol"] for r in rows], ["AAPLA", "MSFTR"])
        self.assertEqual(rows[0]["reference_source"], "reference")
        self.assertIsNotNone(rows[0]["abs_deviation"])
        self.assertEqual(rows[1]["reference_source"], "stale_close")
        self.assertIsNone(rows[1]["abs_deviation"])

    def test_stock_without_market_time_is_not_compared_to_close(self):
        import time

        refs = {"AAPL": {"price": 200.0, "timestamp": int(time.time() * 1000)}}
        registry = {
            "AAPLA": {"category": "stock", "reference_symbol": "AAPL", "issuer": "bstock", "reference_ratio": 1.0},
        }
        data = {"AAPLA": {"gate": {"price": 150.0, "timestamp": int(time.time() * 1000)}}}
        mm = self._build_monitor(data, refs, registry)
        rows = mm.build_depeg_list(data)

        self.assertEqual(rows[0]["reference_source"], "stale_close")
        self.assertTrue(rows[0]["market_closed"])
        self.assertIsNone(rows[0]["deviation"])
        self.assertFalse(rows[0]["comparable"])

    def test_open_market_still_uses_reference(self):
        import time

        now_s = int(time.time())
        refs = {"AAPL": {"price": 200.0, "timestamp": now_s * 1000, "market_time": now_s - 60}}
        registry = {
            "AAPLA": {"category": "stock", "reference_symbol": "AAPL", "issuer": "bstock", "reference_ratio": 1.0},
        }
        data = {"AAPLA": {"gate": {"price": 190.0, "timestamp": now_s * 1000}}}
        mm = self._build_monitor(data, refs, registry)
        rows = mm.build_depeg_list(data)

        self.assertEqual(rows[0]["reference_source"], "reference")
        self.assertTrue(rows[0]["depeg_signal"])
        self.assertAlmostEqual(rows[0]["deviation"], -0.05)
        alerts = mm._check_depeg_anomalies(rows)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["level"], "critical")


class LiquidityFilterTests(unittest.TestCase):
    """资金费率/基差机会必须满足最小 24h 成交额"""

    def setUp(self):
        import time
        self.now = int(time.time() * 1000)
        values = {"global.funding_min_volume_usd": 500_000}
        config = SimpleNamespace(
            get=lambda category, key, default=None: values.get(f"{category}.{key}", default)
        )

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


if __name__ == '__main__':
    unittest.main()
