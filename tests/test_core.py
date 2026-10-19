"""
核心数据准确性回归测试（stdlib unittest，无网络依赖）
运行: python -m unittest discover -s tests -v
"""
import unittest
from types import SimpleNamespace

from exchanges.base_exchange import BaseExchange
from exchanges.gate_adapter import GateAdapter
from core.market_monitor import annualize_rate
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


class AlertPushTests(unittest.TestCase):
    def setUp(self):
        values = {
            'alerts.push_basis': True,
            'alerts.push_cross_spread': True,
            'alerts.push_funding_apr_threshold': 10.0,
        }
        config = SimpleNamespace(get=lambda category, key, default=None: values.get(f'{category}.{key}', default))
        self.manager = AlertManager(config, None)

    def test_basis_is_push_enabled(self):
        self.assertTrue(self.manager._should_push({'alert_type': 'basis_extreme'}))

    def test_cross_spread_is_panel_only_by_default(self):
        config = SimpleNamespace(get=lambda category, key, default=None: default)
        manager = AlertManager(config, None)
        self.assertFalse(manager._should_push({'alert_type': 'cross_spread'}))

    def test_funding_push_uses_absolute_apr_threshold(self):
        self.assertTrue(self.manager._should_push({
            'alert_type': 'funding_extreme', 'details': {'apr': -10.1}
        }))
        self.assertFalse(self.manager._should_push({
            'alert_type': 'funding_extreme', 'details': {'apr': 9.9}
        }))


class PegAssetTests(unittest.TestCase):
    def test_usdt_is_monitored_as_usd_stable(self):
        self.assertEqual(peg_assets.classify('USDT'), ('usd_stable', 'USD', 'tether'))

    def test_gold_token_issuer_and_reference(self):
        self.assertEqual(peg_assets.classify('XAUT'), ('commodity', 'GC=F', 'tether'))

    def test_fractional_gold_ratio(self):
        self.assertEqual(peg_assets.get_reference_ratio('RSGOL'), 0.01)
        self.assertEqual(peg_assets.get_reference_ratio('XAUT'), 1.0)

    def test_commodity_thresholds_allow_futures_basis(self):
        self.assertEqual(peg_assets.get_thresholds('commodity'), {'warning': 0.02, 'critical': 0.05})


if __name__ == '__main__':
    unittest.main()
