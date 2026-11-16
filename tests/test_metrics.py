import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oi_monitor.metrics import (
    build_row,
    changes_for,
    cycle_view,
    judge,
    normalize,
    oi_move_threshold,
    parse_limit,
    pct_change,
    screen_change,
)


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

    def test_five_minute_scale_is_latest_minus_previous(self):
        step = 5 * 60 * 1000
        start = 1_700_000_000_000
        series = []
        for index in range(40):
            series.append(
                {
                    "timestamp": start + index * step,
                    "oi": 1000 + index,
                    "oiValue": 5_000_000,
                    "supply": 1,
                }
            )
        series[-2]["oiValue"] = 5_200_000
        series[-1]["oiValue"] = 6_300_000
        series[-2]["oi"] = 500
        series[-1]["oi"] = 520
        change = changes_for(series)["5m"]
        self.assertAlmostEqual(change["oiValueDelta"], 1_100_000)
        self.assertAlmostEqual(change["oiDelta"], 20)
        self.assertEqual(series[-1]["oiValue"] - series[-2]["oiValue"], change["oiValueDelta"])

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
        self.assertEqual(judge(-1, 1)["text"], "未平仓减少、价格上涨，需观望")
        self.assertEqual(judge(-1, -1)["text"], "弃盘行情，需观望")
        self.assertEqual(judge(1, 0.5)["code"], "long")
        self.assertEqual(judge(1, 0.49)["code"], "wash")
        self.assertEqual(judge(0, 3)["code"], "oi-flat")
        self.assertEqual(judge(None, 3)["code"], "no-oi")
        self.assertEqual(judge(-2, None)["code"], "no-price")
        self.assertEqual(judge(-2, 0.1)["code"], "watch-flat")
        self.assertEqual(oi_move_threshold(4_000_000), 10)
        self.assertEqual(oi_move_threshold(4_999_999), 10)
        self.assertEqual(oi_move_threshold(5_000_000), 9)
        self.assertEqual(oi_move_threshold(10_000_000), 9)
        self.assertEqual(oi_move_threshold(10_000_001), 5)
        self.assertEqual(oi_move_threshold(15_000_000), 5)
        self.assertEqual(oi_move_threshold(15_000_001), 5)
        self.assertEqual(oi_move_threshold(30_000_000), 5)

    def test_broadcast_uses_first_and_fifth_tick_only(self):
        step = 5 * 60 * 1000
        start = 1_700_000_000_000
        start -= start % (30 * 60 * 1000)
        amounts = [5_000_000, 4_700_000, 5_700_000, 5_300_000, 6_000_000]
        prices = [10, 9, 12, 11, 11]
        series = []
        for index, (amount, price) in enumerate(zip(amounts, prices)):
            series.append({"timestamp": start + index * step, "oi": amount / price, "oiValue": amount, "supply": 0})
        ready = cycle_view(series, start + 20 * 60 * 1000)
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["count"], 5)
        self.assertAlmostEqual(ready["oiValuePct"], 20.0, places=4)
        self.assertAlmostEqual(ready["pricePct"], 10.0, places=4)
        still = cycle_view(series, start + 21 * 60 * 1000)
        self.assertTrue(still["ready"])
        self.assertAlmostEqual(still["oiValuePct"], 20.0, places=4)
        gap = [row for row in series if row["timestamp"] != start + 2 * step]
        self.assertFalse(cycle_view(gap, start + 21 * 60 * 1000)["ready"])

    def test_rolling_window_picks_latest_five_ticks_off_the_half_hour(self):
        step = 5 * 60 * 1000
        half = 30 * 60 * 1000
        base = 1_700_000_000_000
        base -= base % half
        # :25 is not :00 or :30. The old block would have used :00 through :20 and ignored :25.
        amounts = {
            -5: 1_000_000,
            0: 50_000_000,
            5: 8_000_000,
            10: 8_400_000,
            15: 7_900_000,
            20: 8_800_000,
            25: 9_200_000,
        }
        prices = {minute: 10 + minute / 100 for minute in amounts}
        series = []
        for minute, amount in amounts.items():
            price = prices[minute]
            series.append(
                {
                    "timestamp": base + minute * 60 * 1000,
                    "oi": amount / price,
                    "oiValue": amount,
                    "supply": 1,
                }
            )
        now = base + 27 * 60 * 1000
        view = cycle_view(series, now)
        self.assertNotEqual(now % half, 0)
        self.assertTrue(view["ready"])
        self.assertEqual(
            [tick["at"] for tick in view["ticks"]],
            [base + minute * 60 * 1000 for minute in (5, 10, 15, 20, 25)],
        )
        self.assertEqual(view["fifth"], 9_200_000)
        self.assertAlmostEqual(view["oiValuePct"], (9_200_000 - 8_000_000) / 8_000_000 * 100, places=4)
        self.assertAlmostEqual(view["pricePct"], (prices[25] - prices[5]) / prices[5] * 100, places=4)
        screened = screen_change(view["fifth"], view["oiValuePct"], view["pricePct"], view["oiValueDelta"])
        self.assertEqual(screened["threshold"], 9)
        self.assertTrue(screened["qualified"])

        series.append(
            {
                "timestamp": base + 30 * 60 * 1000,
                "oi": 10_500_000 / 12,
                "oiValue": 10_500_000,
                "supply": 1,
            }
        )
        rolled = cycle_view(series, base + 31 * 60 * 1000)
        self.assertEqual(
            [tick["at"] for tick in rolled["ticks"]],
            [base + minute * 60 * 1000 for minute in (10, 15, 20, 25, 30)],
        )
        self.assertTrue(rolled["ready"])
        self.assertEqual(rolled["fifth"], 10_500_000)
        rolled_screen = screen_change(rolled["fifth"], rolled["oiValuePct"], rolled["pricePct"], rolled["oiValueDelta"])
        self.assertEqual(rolled_screen["threshold"], 5)
        self.assertNotEqual([tick["at"] for tick in rolled["ticks"]][0] % half, 0)

        small = []
        for index, amount in enumerate((4_000_000, 4_100_000, 4_050_000, 4_200_000, 4_400_000)):
            small.append(
                {
                    "timestamp": base + 5 * 60 * 1000 + index * step,
                    "oi": amount / 10,
                    "oiValue": amount,
                    "supply": 1,
                }
            )
        small_view = cycle_view(small, small[-1]["timestamp"] + 60_000)
        small_screen = screen_change(
            small_view["fifth"], small_view["oiValuePct"], small_view["pricePct"], small_view["oiValueDelta"]
        )
        self.assertEqual(small_view["fifth"], 4_400_000)
        self.assertEqual(small_screen["threshold"], 10)
        self.assertTrue(small_screen["qualified"])

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
