"""
Binance 结算周期元数据不可用时的年化回归测试（stdlib unittest，无网络依赖）
运行: python -m unittest discover -s tests -v
"""
import unittest
from types import SimpleNamespace

from core.data_collector import DataCollector
from core.market_monitor import annualize_rate
from exchanges import EXCHANGE_CLASSES


def collect_one_rate(funding, metadata=None, metadata_error=None):
    """用真实的采集与解析逻辑，只把交易所 I/O 换成假的，不走网络。"""
    def fetch_intervals():
        if metadata_error is not None:
            raise metadata_error
        return metadata

    adapter = object.__new__(EXCHANGE_CLASSES["binance"])
    adapter.exchange = SimpleNamespace(
        has={"fetchFundingIntervals": True},
        fetch_funding_intervals=fetch_intervals,
        fetch_funding_rates=lambda: {"BTC/USDT:USDT": funding},
        market=lambda symbol: {"active": True, "id": "BTCUSDT", "quote": "USDT"},
    )
    db_rows = []
    collector = object.__new__(DataCollector)
    collector.exchanges = {"binance": adapter}
    collector.market_data = {}
    collector.db = SimpleNamespace(execute_many=lambda query, rows: db_rows.extend(rows))
    collector._collect_funding_rates()
    return collector.market_data["BTC/USDT"]["binance"], db_rows


class TestBinanceFundingInterval(unittest.TestCase):
    def test_metadata_failure_leaves_interval_unknown(self):
        # 周期接口失败时不知道真实周期，不能按 8h 年化
        entry, db_rows = collect_one_rate(
            {"fundingRate": 0.0001}, metadata_error=TimeoutError("simulated timeout"))
        self.assertEqual(entry["funding_rate"], 0.0001)
        self.assertIsNone(entry["funding_interval"])
        self.assertIsNone(db_rows[0][5])
        self.assertIsNone(annualize_rate(entry["funding_rate"], entry["funding_interval"]))

    def test_metadata_failure_keeps_interval_from_rate(self):
        # 费率对象自己带了周期时照常使用
        entry, _ = collect_one_rate(
            {"fundingRate": 0.0001, "interval": "4h"}, metadata_error=TimeoutError("simulated timeout"))
        self.assertEqual(entry["funding_interval"], 14_400_000)
        self.assertAlmostEqual(annualize_rate(entry["funding_rate"], entry["funding_interval"]), 0.219)

    def test_metadata_success_keeps_8h_default(self):
        # 周期接口成功、该合约没被列出时，仍按 8h（行为不变）
        entry, _ = collect_one_rate({"fundingRate": 0.0001}, metadata={})
        self.assertEqual(entry["funding_interval"], 28_800_000)
        self.assertAlmostEqual(annualize_rate(entry["funding_rate"], entry["funding_interval"]), 0.1095)


if __name__ == "__main__":
    unittest.main()
