import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.telegram_bot import TelegramBot
from config import ConfigManager
from core import LiquidationMonitor
from core.alert_manager import AlertManager
from database import DatabaseManager
from web.app import create_app


class FakeConfig:
    def __init__(self, values=None):
        self.values = values or {}

    def get_bool(self, category, key, default=False):
        return self.values.get(key, default)

    def get_int(self, category, key, default=0):
        return int(self.values.get(key, default))

    def get_float(self, category, key, default=0.0):
        return float(self.values.get(key, default))

    def get(self, category, key, default=None):
        return self.values.get(key, default)


class FakeDB:
    def execute_many(self, query, rows):
        return None

    def execute_query(self, query, params=()):
        return []


DETECTION_CONFIG = {
    "liquidation_enabled": True,
    "liquidation_window_seconds": 60,
    "liquidation_baseline_minutes": 30,
    "liquidation_warmup_minutes": 5,
    "liquidation_scan_interval": 10,
    "liquidation_min_alert_usd": 500_000,
    "liquidation_ratio_threshold": 3.0,
    "liquidation_critical_ratio": 6.0,
    "liquidation_critical_usd": 2_000_000,
    "liquidation_large_order_usd": 1_000_000,
    "liquidation_min_sources": 2,
    "liquidation_alert_sides": ["short", "long"],
}


class LiquidationParserTests(unittest.TestCase):
    def setUp(self):
        self.monitor = LiquidationMonitor(FakeConfig(), FakeDB())

    def events(self):
        return [event for events in self.monitor._events.values() for event in events]

    def test_all_exchange_payloads_normalize_to_usd_and_side(self):
        self.monitor._parse_binance(
            {
                "e": "forceOrder",
                "E": 1_000,
                "o": {"s": "BTCUSDT", "S": "SELL", "z": "2", "ap": "100"},
            }
        )
        self.monitor._parse_binance(
            {
                "e": "forceOrder",
                "E": 1_001,
                "o": {"s": "ETHUSDC", "S": "BUY", "z": "2", "ap": "100"},
            }
        )
        self.monitor._okx_instruments = {
            "BTC-USDT-SWAP": {"ctVal": "0.01", "ctType": "linear"}
        }
        self.monitor._parse_okx(
            {
                "data": [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "details": [
                            {
                                "sz": "10",
                                "bkPx": "100",
                                "posSide": "short",
                                "ts": "1002",
                            }
                        ],
                    }
                ]
            }
        )
        self.monitor._parse_bybit(
            {
                "topic": "allLiquidation.BTCUSDT",
                "ts": 1_003,
                "data": [{"s": "BTCUSDT", "S": "Sell", "v": "2", "p": "100"}],
            }
        )
        self.monitor._parse_bitget(
            {
                "arg": {"topic": "liquidation"},
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "buy",
                        "price": "100",
                        "amount": "250",
                        "ts": "1004",
                    }
                ],
            }
        )
        self.monitor._gate_multipliers = {"BTC_USDT": 0.0001}
        self.assertTrue(
            self.monitor._parse_gate_row(
                {
                    "contract": "BTC_USDT",
                    "size": "-2",
                    "fill_price": "100000",
                    "time": 2,
                }
            )
        )

        rows = {(event["exchange"], event["side"]): event["usd"] for event in self.events()}
        self.assertEqual(rows[("binance", "long")], 200.0)
        self.assertEqual(rows[("okx", "short")], 10.0)
        self.assertEqual(rows[("bybit", "short")], 200.0)
        self.assertEqual(rows[("bitget", "long")], 250.0)
        self.assertEqual(rows[("gate", "short")], 20.0)
        self.assertNotIn(("binance", "short"), rows)

    def test_duplicate_event_fingerprint_is_ignored(self):
        monitor = LiquidationMonitor(FakeConfig(), FakeDB())
        monitor._record_event("bitget", "BTC", "short", 100, 80_000, 1_000)
        monitor._record_event("bitget", "BTC", "short", 100, 80_000, 1_000)
        events = [
            event for values in monitor._events.values() for event in values
        ]
        self.assertEqual(len(events), 1)

    def test_contract_multiplier_prefixes_are_normalized(self):
        self.assertEqual(LiquidationMonitor._base_symbol("1000SHIBUSDT"), "SHIB")
        self.assertEqual(LiquidationMonitor._base_symbol("10000SATSUSDT"), "SATS")
        self.assertEqual(LiquidationMonitor._base_symbol("1000000MOGUSDT"), "MOG")
        self.assertEqual(LiquidationMonitor._base_symbol("1INCHUSDT"), "1INCH")

    def test_malformed_rows_are_skipped(self):
        self.monitor._parse_binance({"e": "forceOrder", "o": "not-an-object"})
        self.monitor._parse_okx(
            {"data": [{"instId": 123, "details": [{"sz": "1", "bkPx": "1"}]}]}
        )
        self.monitor._parse_bybit({"topic": "allLiquidation.BTCUSDT", "data": ["bad"]})
        self.monitor._parse_bitget({"arg": {"topic": "liquidation"}, "data": ["bad"]})

        self.assertEqual(self.events(), [])

    def test_unknown_sides_are_skipped(self):
        self.monitor._parse_binance(
            {
                "e": "forceOrder",
                "E": 1_000,
                "o": {"s": "BTCUSDT", "S": "UNKNOWN", "z": "2", "ap": "100"},
            }
        )
        self.monitor._okx_instruments = {
            "BTC-USDT-SWAP": {"ctVal": "0.01", "ctType": "linear"}
        }
        self.monitor._parse_okx(
            {
                "data": [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "details": [
                            {"sz": "10", "bkPx": "100", "posSide": "net", "ts": "1"}
                        ],
                    }
                ]
            }
        )
        self.monitor._parse_bybit(
            {
                "topic": "allLiquidation.BTCUSDT",
                "data": [{"s": "BTCUSDT", "S": "UNKNOWN", "v": "2", "p": "100"}],
            }
        )
        self.monitor._parse_bitget(
            {
                "arg": {"topic": "liquidation"},
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "side": "unknown",
                        "price": "100",
                        "amount": "250",
                        "ts": "1",
                    }
                ],
            }
        )
        self.assertEqual(self.events(), [])

    def test_subscription_failures_are_not_silenced(self):
        monitor = LiquidationMonitor(FakeConfig(), FakeDB())
        monitor._mark_connected("bybit", True)

        with self.assertRaises(RuntimeError):
            monitor._handle_raw_message(
                "bybit",
                json.dumps(
                    {
                        "success": False,
                        "ret_msg": "subscribe failed",
                        "op": "subscribe",
                    }
                ),
            )
        with self.assertRaises(RuntimeError):
            monitor._handle_raw_message(
                "binance",
                json.dumps({"id": 1, "error": {"code": -1, "msg": "failed"}}),
            )

    def test_binance_health_probe_is_not_satisfied_by_book_ticker(self):
        self.assertEqual(
            LiquidationMonitor.BINANCE_LIQUIDATION_STREAM,
            "!forceOrder@arr",
        )
        self.assertEqual(
            LiquidationMonitor.BINANCE_HEALTH_STREAM,
            "btcusdt@markPrice@1s",
        )
        self.assertNotIn("bookTicker", LiquidationMonitor.BINANCE_HEALTH_STREAM)
        self.assertEqual(
            LiquidationMonitor.WS_ENDPOINTS["binance"],
            "wss://fstream.binance.com/market/ws",
        )

    def test_subscription_ack_does_not_refresh_data_freshness(self):
        monitor = LiquidationMonitor(FakeConfig(), FakeDB())
        monitor._mark_connected("binance", True)

        monitor._handle_raw_message("binance", '{"result":null,"id":1}')
        self.assertIsNone(monitor._connections["binance"]["last_message"])

        monitor._handle_raw_message(
            "binance",
            '{"e":"markPriceUpdate","s":"BTCUSDT","p":"80000"}',
        )
        self.assertIsNotNone(monitor._connections["binance"]["last_message"])

    def test_websocket_proxy_candidates_prefer_direct_before_generic(self):
        monitor = LiquidationMonitor(FakeConfig(), FakeDB())
        with patch.dict(
            os.environ,
            {
                "BINANCE_PROXY": "http://binance-proxy:1080",
                "PROXY_URL": "http://generic-proxy:1080",
            },
        ):
            self.assertEqual(
                monitor._websocket_proxy_candidates("binance"),
                ["http://binance-proxy:1080", None, "http://generic-proxy:1080"],
            )
            self.assertEqual(
                monitor._websocket_proxy_candidates("gate"),
                ["http://generic-proxy:1080"],
            )

    def test_stale_connected_source_is_not_active(self):
        monitor = LiquidationMonitor(FakeConfig(), FakeDB())
        now = time.time()
        monitor._connections["binance"].update(
            connected=True, last_message=now - 121
        )
        monitor._connections["okx"].update(
            connected=True, last_message=now
        )

        status = monitor.get_status()

        self.assertEqual(status["active_exchanges"], ["okx"])
        self.assertFalse(status["connections"]["binance"]["active"])
        self.assertTrue(status["connections"]["okx"]["active"])

    def test_required_sources_respect_config_and_enabled_exchanges(self):
        monitor = LiquidationMonitor(
            FakeConfig(
                {
                    "liquidation_min_sources": 4,
                    "enabled_exchanges": ["binance", "okx"],
                }
            ),
            FakeDB(),
        )

        self.assertEqual(monitor.required_active_sources(), 2)
        status = monitor.get_status()
        self.assertEqual(status["enabled_exchanges"], ["binance", "okx"])
        self.assertEqual(status["required_active_sources"], 2)

    def test_gate_missing_multiplier_is_skipped(self):
        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return [
                    {"name": "BTC_USDT", "quanto_multiplier": "0"},
                    {"name": "ETH_USDT", "quanto_multiplier": "0.01"},
                ]

        with patch("core.liquidation_monitor.requests.get", return_value=Response()):
            self.monitor._load_gate_contracts()

        self.assertEqual(self.monitor._gate_multipliers, {"ETH_USDT": 0.01})

    def test_stop_disconnects_sources_and_rejects_late_events(self):
        for state in self.monitor._connections.values():
            state["connected"] = True
        self.monitor.running = True

        self.monitor.stop()
        self.monitor._mark_connected("binance", True)
        self.monitor._record_event("binance", "BTC", "short", 100, 80_000, 1_000)

        self.assertFalse(self.monitor.running)
        self.assertFalse(
            any(state["connected"] for state in self.monitor._connections.values())
        )
        self.assertEqual(self.events(), [])

    def test_missing_contract_metadata_is_skipped(self):
        self.monitor._parse_okx(
            {
                "data": [
                    {
                        "instId": "NEW-USDT-SWAP",
                        "details": [
                            {"sz": "10", "bkPx": "1", "posSide": "long", "ts": "1"}
                        ],
                    }
                ]
            }
        )
        self.assertFalse(self.monitor._parse_gate_row(
            {"contract": "NEW_USDT", "size": "1", "fill_price": "1", "time": 1}
        ))
        self.assertEqual(self.events(), [])


class LiquidationDetectionTests(unittest.TestCase):
    def monitor(self):
        monitor = LiquidationMonitor(FakeConfig(DETECTION_CONFIG), FakeDB())
        monitor._started_at = time.time() - 600
        now = time.time()
        for name in monitor._connections:
            monitor._connections[name]["connected"] = True
            monitor._connections[name]["last_message"] = now
        return monitor

    def test_surge_uses_observed_minute_mean_and_emits_alert(self):
        monitor = self.monitor()
        monitor._started_at = time.time() - 600
        now_ms = int(time.time() * 1000)
        current_start = now_ms - 60_000
        minute_floor = current_start // 60_000 * 60_000
        for offset in range(1, 9):
            monitor._record_event(
                "binance",
                "BTC",
                "short",
                50_000,
                80_000,
                minute_floor - offset * 60_000 + 30_000,
            )
        monitor._record_event("binance", "BTC", "short", 300_000, 81_000, now_ms - 20_000)
        monitor._record_event("bybit", "BTC", "short", 200_000, 81_000, now_ms - 10_000)
        alerts = []
        monitor.register_alert_callback(alerts.extend)

        scan = monitor.scan_once()

        self.assertEqual(scan["ranking"][0]["ratio"], 10.0)
        self.assertEqual(scan["summary"]["short_usd"], 500_000.0)
        self.assertEqual(alerts[0]["level"], "emergency")
        self.assertEqual(alerts[0]["details"]["side"], "short")
        retained = [event for values in monitor._events.values() for event in values]
        self.assertLess(len(retained), 10)
        self.assertTrue(all(event["timestamp"] >= now_ms - 180_000 for event in retained))

    def test_missing_threshold_config_uses_500k_default(self):
        values = dict(DETECTION_CONFIG)
        values.pop("liquidation_min_alert_usd")
        monitor = LiquidationMonitor(FakeConfig(values), FakeDB())
        monitor._started_at = time.time() - 600
        now = time.time()
        for name in monitor._connections:
            monitor._connections[name]["connected"] = True
            monitor._connections[name]["last_message"] = now
        now_ms = int(now * 1000)
        monitor._record_event(
            "binance", "BTC", "short", 300_000, 81_000, now_ms - 20_000
        )
        monitor._record_event(
            "bybit", "BTC", "short", 200_000, 81_000, now_ms - 10_000
        )
        alerts = []
        monitor.register_alert_callback(alerts.extend)

        scan = monitor.scan_once()

        self.assertEqual(scan["alert_candidates"], 1)
        self.assertEqual(alerts[0]["details"]["current_usd"], 500_000)

    def test_cold_start_does_not_report_fake_ratio(self):
        monitor = self.monitor()
        now_ms = int(time.time() * 1000)
        monitor._record_event("binance", "BTC", "short", 300_000, 81_000, now_ms - 20_000)
        monitor._record_event("bybit", "BTC", "short", 200_000, 81_000, now_ms - 10_000)
        alerts = []
        monitor.register_alert_callback(alerts.extend)

        scan = monitor.scan_once()

        self.assertIsNone(scan["ranking"][0]["ratio"])
        self.assertEqual(scan["ranking"][0]["trigger_ratio"], 3.0)
        self.assertEqual(alerts[0]["level"], "warning")


class LiquidationPersistenceTests(unittest.TestCase):
    def test_legacy_100k_threshold_migrates_once_to_500k(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(str(Path(temp_dir) / "test.db"))
            db.init_database()
            db.set_config(
                "alerts",
                "liquidation_min_alert_usd",
                "100000",
                True,
                "legacy threshold",
            )
            config = ConfigManager(db)
            config.init_default_configs()

            self.assertEqual(
                config.get_float("alerts", "liquidation_min_alert_usd"), 500_000
            )

            config.set("alerts", "liquidation_min_alert_usd", 250_000)
            config.init_default_configs()
            self.assertEqual(
                config.get_float("alerts", "liquidation_min_alert_usd"), 250_000
            )

    def test_minute_aggregates_can_be_filtered_in_sql(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(str(Path(temp_dir) / "test.db"))
            db.init_database()
            bucket = int(time.time() * 1000) // 60_000 * 60_000
            db.execute_many(
                """
                INSERT INTO liquidation_minutes
                    (bucket, exchange, symbol, side, usd_value, event_count, max_order_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (bucket, "binance", "BTC", "short", 100.0, 1, 100.0),
                    (bucket, "bybit", "ETH", "long", 200.0, 2, 150.0),
                ],
            )
            monitor = LiquidationMonitor(FakeConfig(), db)

            rows = monitor.get_recent_aggregates(
                hours=24, limit=10, symbol="BTC", side="short"
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbol"], "BTC")
            self.assertEqual(rows[0]["side"], "short")

    def test_each_flush_upserts_current_minute(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(str(Path(temp_dir) / "test.db"))
            db.init_database()
            monitor = LiquidationMonitor(FakeConfig(), db)
            now_ms = int(time.time() * 1000)

            monitor._record_event("binance", "BTC", "short", 100, 1, now_ms)
            monitor._flush_buckets()
            monitor._record_event("binance", "BTC", "short", 50, 1, now_ms)
            monitor._flush_buckets()

            rows = db.execute_query(
                "SELECT usd_value, event_count, max_order_usd FROM liquidation_minutes"
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["usd_value"], 150.0)
            self.assertEqual(rows[0]["event_count"], 2)
            self.assertEqual(rows[0]["max_order_usd"], 100.0)

    def test_retention_cleanup_uses_bucket_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = DatabaseManager(str(Path(temp_dir) / "test.db"))
            db.init_database()
            now_ms = int(time.time() * 1000)
            db.execute_many(
                """
                INSERT INTO liquidation_minutes
                    (bucket, exchange, symbol, side, usd_value, event_count, max_order_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (now_ms - 9 * 86400_000, "binance", "BTC", "short", 1, 1, 1),
                    (now_ms, "binance", "BTC", "short", 2, 1, 2),
                ],
            )

            db.cleanup_old_data()

            rows = db.execute_query("SELECT bucket FROM liquidation_minutes")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["bucket"], now_ms)


class LiquidationWebTests(unittest.TestCase):
    def test_page_and_apis_expose_monitor_data(self):
        class Monitor:
            def __init__(self):
                self.history_args = None

            def get_status(self):
                return {
                    "running": True,
                    "active_exchanges": ["binance", "bybit"],
                    "connections": {"binance": {"connected": True}},
                    "last_scan": {
                        "timestamp": 1,
                        "window_seconds": 60,
                        "summary": {
                            "short_usd": 100_000,
                            "long_usd": 20_000,
                            "max_order_usd": 60_000,
                        },
                        "ranking": [
                            {
                                "symbol": "BTC",
                                "side": "short",
                                "current_usd": 100_000,
                            }
                        ],
                    },
                }

            def get_recent_aggregates(self, **kwargs):
                self.history_args = kwargs
                return []

        monitor = Monitor()
        app = create_app(FakeConfig(), FakeDB(), None, None, None, monitor)
        client = app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True

        page = client.get("/liquidations")
        current = client.get("/api/liquidations")
        history = client.get("/api/liquidations/history?symbol=btc&side=short")

        self.assertEqual(page.status_code, 200)
        self.assertIn("爆仓量监控", page.get_data(as_text=True))
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.get_json()["summary"]["short_usd"], 100_000)
        self.assertEqual(history.status_code, 200)
        self.assertEqual(monitor.history_args["symbol"], "BTC")
        self.assertEqual(monitor.history_args["side"], "short")


class LiquidationAlertTests(unittest.TestCase):
    def test_telegram_shows_missing_baseline(self):
        bot = object.__new__(TelegramBot)
        alert = {
            "alert_type": "liquidation_surge",
            "level": "warning",
            "symbol": "BTC",
            "exchange": "multi",
            "message": "test",
            "details": {
                "symbol": "BTC",
                "side": "short",
                "side_label": "空单",
                "window_seconds": 60,
                "current_usd": 100_000,
                "baseline_usd": 0,
                "ratio": None,
                "max_order_usd": 60_000,
                "exchange_breakdown": {"binance": 100_000},
            },
        }
        text = bot._format_single_alert(alert)
        self.assertIn("基准不足", text)

    def test_dedup_key_separates_long_and_short(self):
        manager = object.__new__(AlertManager)
        short = {
            "alert_type": "liquidation_surge",
            "symbol": "BTC",
            "exchange": "multi",
            "details": {"side": "short"},
        }
        long = {**short, "details": {"side": "long"}}
        self.assertNotEqual(manager._dedup_key(short), manager._dedup_key(long))


if __name__ == "__main__":
    unittest.main()
