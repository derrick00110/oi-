import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oi_monitor.metrics import build_row, changes_for, judge, normalize, oi_move_threshold, parse_limit, pct_change


def linear_series(n=97, oi0=100.0, oi1=110.0, v0=1000.0, v1=1300.0, supply=2000.0):
    step = 15 * 60 * 1000
    start = 1_700_000_000_000
    rows = []
    for index in range(n):
        frac = index / (n - 1)
        rows.append(
            {
                "timestamp": start + index * step,
                "oi": oi0 + (oi1 - oi0) * frac,
                "oiValue": v0 + (v1 - v0) * frac,
                "supply": supply,
            }
        )
    return rows


class MetricsTest(unittest.TestCase):
    def test_pct_change(self):
        self.assertEqual(pct_change(110, 100), 10)
        self.assertIsNone(pct_change(10, 0))

    def test_windows_on_exact_grid(self):
        series = linear_series()
        changes = changes_for(series)
        self.assertAlmostEqual(changes["24h"]["oiPct"], 10.0, places=6)
        self.assertAlmostEqual(changes["24h"]["oiValuePct"], 30.0, places=6)
        self.assertAlmostEqual(changes["24h"]["oiDelta"], 10.0, places=6)
        self.assertAlmostEqual(changes["24h"]["oiValueDelta"], 300.0, places=6)
        self.assertAlmostEqual(changes["24h"]["pricePct"], (1300 / 110) / (1000 / 100) * 100 - 100, places=4)

        old_oi = 110 - (10 / 96)
        self.assertAlmostEqual(changes["15m"]["oiPct"], (110 - old_oi) / old_oi * 100, places=6)
        old_hour = 110 - 4 * (10 / 96)
        self.assertAlmostEqual(changes["1h"]["oiPct"], (110 - old_hour) / old_hour * 100, places=6)
        old_four = 110 - 16 * (10 / 96)
        self.assertAlmostEqual(changes["4h"]["oiPct"], (110 - old_four) / old_four * 100, places=6)

    def test_short_history_leaves_long_windows_empty(self):
        changes = changes_for(linear_series(n=8))
        self.assertIsNotNone(changes["15m"]["oiPct"])
        self.assertIsNotNone(changes["1h"]["oiPct"])
        self.assertIsNone(changes["4h"]["oiPct"])
        self.assertIsNone(changes["24h"]["oiPct"])

    def test_stale_point_is_not_used(self):
        step = 15 * 60 * 1000
        series = [
            {"timestamp": 0, "oi": 1, "oiValue": 1, "supply": 1},
            {"timestamp": 100 * step, "oi": 2, "oiValue": 2, "supply": 1},
        ]
        changes = changes_for(series)
        self.assertIsNone(changes["15m"]["oiPct"])
        self.assertIsNone(changes["24h"]["oiValuePct"])

    def test_normalize_sorts_and_keeps_latest_duplicate(self):
        raw = [
            {
                "timestamp": "2000",
                "sumOpenInterest": "2",
                "sumOpenInterestValue": "20",
                "CMCCirculatingSupply": "9",
            },
            {
                "timestamp": "1000",
                "sumOpenInterest": "1",
                "sumOpenInterestValue": "10",
                "CMCCirculatingSupply": "9",
            },
            {
                "timestamp": "2000",
                "sumOpenInterest": "3",
                "sumOpenInterestValue": "30",
                "CMCCirculatingSupply": "9",
            },
        ]
        series = normalize(raw)
        self.assertEqual([row["timestamp"] for row in series], [1000, 2000])
        self.assertEqual(series[-1]["oi"], 3)

    def test_market_cap_and_missing_supply(self):
        ticker = {"lastPrice": "50", "priceChangePercent": "-1.5", "quoteVolume": "12345.6"}
        row = build_row("BTCUSDT", "BTC", 1, ticker, linear_series())
        self.assertEqual(row["marketCap"], 100000)
        self.assertAlmostEqual(row["oiToMarketCapPct"], 1.3, places=6)
        self.assertEqual(row["priceChangePercent"], -1.5)
        self.assertEqual(len(row["spark"]), 97)

        empty = build_row("BTCDOMUSDT", "BTCDOM", 4, ticker, linear_series(supply=0))
        self.assertIsNone(empty["marketCap"])
        self.assertIsNone(empty["oiToMarketCapPct"])
        self.assertIsNone(empty["circulatingSupply"])
        funded = build_row("BTCUSDT", "BTC", 1, ticker, linear_series(), {"fundingRate": 0.0001, "nextFundingTime": 15})
        self.assertEqual(funded["fundingRate"], 0.0001)
        self.assertEqual(funded["nextFundingTime"], 15)

    def test_four_forms_and_tiered_threshold(self):
        self.assertEqual(judge(1, 1)["text"], "优先推报，做多信号")
        self.assertEqual(judge(1, -1)["text"], "可能为出货、洗盘或蓄力")
        self.assertEqual(judge(1, 0.2)["text"], "可能为出货、洗盘或蓄力")
        self.assertEqual(judge(-1, 1)["text"], "持仓减少、价格上涨，需观望")
        self.assertEqual(judge(-1, -1)["text"], "弃盘行情，需观望")
        self.assertEqual(judge(1, 0.5)["code"], "long")
        self.assertEqual(judge(1, 0.49)["code"], "wash")
        self.assertEqual(judge(0, 3)["code"], "oi-flat")
        self.assertEqual(judge(None, 3)["code"], "no-oi")
        self.assertEqual(judge(-2, None)["code"], "no-price")
        self.assertEqual(judge(-2, 0.1)["code"], "watch-flat")
        self.assertIsNone(oi_move_threshold(4_000_000))
        self.assertEqual(oi_move_threshold(5_000_000), 10)
        self.assertEqual(oi_move_threshold(10_000_000), 10)
        self.assertEqual(oi_move_threshold(10_000_001), 7)
        self.assertEqual(oi_move_threshold(15_000_001), 5)
        self.assertEqual(oi_move_threshold(30_000_000), 5)

    def test_parse_limit(self):
        self.assertEqual(parse_limit("60"), 60)
        self.assertIsNone(parse_limit("all"))
        self.assertIsNone(parse_limit("全市场"))
        with self.assertRaises(ValueError):
            parse_limit("0.5")
        with self.assertRaises(ValueError):
            parse_limit("601")


if __name__ == "__main__":
    unittest.main()
