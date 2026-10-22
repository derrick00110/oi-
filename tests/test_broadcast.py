import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oi_monitor.broadcast import BroadcastBook, compose, default_rules
from oi_monitor.trading import TradingDesk, sign_query
from oi_monitor.client import BinanceError


def row(symbol="BTCUSDT", base="BTC", move=6.5, oi_move=None, price_move=5.2, oi_value=7_600_000_000, funding=None):
    change = {
        "oiPct": move if oi_move is None else oi_move,
        "oiValuePct": move,
        "oiDelta": 1000,
        "oiValueDelta": 460_000_000,
        "pricePct": price_move,
    }
    return {
        "symbol": symbol,
        "base": base,
        "price": 80000,
        "priceChangePercent": -4.2,
        "oi": 95000,
        "oiValue": oi_value,
        "marketCap": 1_600_000_000_000,
        "oiToMarketCapPct": 0.47,
        "fundingRate": funding,
        "changes": {"15m": change, "1h": change, "4h": change, "24h": change},
    }


def loose_rule():
    rule = dict(default_rules()[0])
    rule["minAbsPct"] = 0.1
    rule["minOiValue"] = 0
    rule["maxOiValue"] = None
    rule["minPricePct"] = None
    return rule


def band_row(**kwargs):
    values = {"move": 12, "price_move": 6, "oi_value": 7_500_000}
    values.update(kwargs)
    return row(**values)


class BroadcastTest(unittest.TestCase):
    def test_message_matches_chinese_alert_form(self):
        text = compose(band_row(), default_rules()[0])
        self.assertIn("🟢 [BTCUSDT] 币安未平仓合约量 +12.0%", text)
        self.assertIn("过去 3600 秒价格上涨 6.0%", text)
        self.assertIn("未平仓合约量：750 万美元", text)
        self.assertIn("未平仓合约量/市值比率：0.5%", text)
        self.assertIn("24 小时价格变动：-4.2%", text)
        self.assertIn("多头主导，看涨", text)
        body = text.split("] ", 1)[1]
        self.assertNotRegex(body, r"[A-Za-z]")

        small_rule = loose_rule()
        small = compose(row(oi_value=4_500_000), small_rule)
        self.assertIn("450 万美元", small)

    def test_band_requires_oi_range_quantity_and_price_rise(self):
        rule = default_rules()[0]
        self.assertEqual(rule["minAbsPct"], 10)
        self.assertEqual(rule["minOiValue"], 5_000_000)
        self.assertEqual(rule["maxOiValue"], 10_000_000)
        self.assertEqual(rule["minPricePct"], 5)
        self.assertIsNotNone(compose(band_row(), rule))
        self.assertIsNotNone(compose(band_row(oi_move=-12), rule))
        self.assertIsNotNone(compose(band_row(oi_value=5_000_000), rule))
        self.assertIsNotNone(compose(band_row(oi_value=10_000_000, price_move=5), rule))
        self.assertIsNone(compose(band_row(move=9.9), rule))
        self.assertIsNone(compose(band_row(price_move=4.9), rule))
        self.assertIsNone(compose(band_row(price_move=-6), rule))
        self.assertIsNone(compose(band_row(oi_value=4_999_999), rule))
        self.assertIsNone(compose(band_row(oi_value=10_000_001), rule))

    def test_five_facts_follow_quantity_not_notional(self):
        rule = loose_rule()
        cases = [
            (2, 1, None, "🟢", "多头主导，看涨"),
            (2, -1, None, "🔴", "空头主导，看跌"),
            (2, 0.1, 0.0001, "⚪", "看不清，配合费率再判，资金费率 +0.0100%，多头付给空头"),
            (-2, 1, None, "🟢", "空头止损拉盘，大概率出向上的针"),
            (-2, -1, None, "🔴", "多头派发第一阶段，可跟空"),
            (-2, 0.1, -0.00025, "⚪", "持仓减少、价格震荡，先不判方向，资金费率 -0.0250%，空头付给多头"),
        ]
        for oi_move, price_move, funding, mark, fact in cases:
            text = compose(row(oi_move=oi_move, price_move=price_move, funding=funding), rule, 0.5)
            self.assertTrue(text.startswith(mark), text)
            self.assertIn(fact, text)

        disguised = row(move=8, oi_move=-3, price_move=4)
        value_rule = dict(rule)
        value_rule["metric"] = "oiValuePct"
        text = compose(disguised, value_rule, 0.5)
        self.assertIn("空头止损拉盘，大概率出向上的针", text)
        self.assertNotIn("多头主导", text)

    def test_threshold_cooldown_and_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            quiet = book.evaluate([band_row(move=1.0)], 1000, now=1_000)
            self.assertEqual(quiet, [])
            first = book.evaluate([band_row()], 1000, now=1_000)
            self.assertEqual(len(first), 1)
            self.assertEqual(book.evaluate([band_row()], 1000, now=1_100), [])
            self.assertEqual(book.evaluate([band_row()], 2000, now=1_100), [])
            again = book.evaluate([band_row()], 3000, now=1_000 + 901)
            self.assertEqual(len(again), 1)

            book.settings["maxPerRound"] = 5
            rows = [band_row(symbol=f"C{i}USDT", base=f"C{i}", move=20 - i) for i in range(6)]
            burst = book.evaluate(rows, 9000, now=5_000)
            self.assertEqual(len(burst), 5)
            self.assertEqual(burst[0]["symbol"], "C0USDT")

    def test_disabled_order_never_signs_a_request(self):
        class Finder:
            def find_row(self, _symbol):
                return {"price": 100}

        with tempfile.TemporaryDirectory() as directory:
            desk = TradingDesk(Path(directory), Finder())
            preview = desk.preview({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"})
            self.assertTrue(preview["ok"])
            self.assertAlmostEqual(preview["notional"], 1.0)
            self.assertFalse(preview["willSend"])
            too_big = desk.preview({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "100"})
            self.assertFalse(too_big["ok"])
            with self.assertRaises(BinanceError):
                desk.submit({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01", "confirm": "下单"})

    def test_signature_is_hmac_sha256_of_the_query(self):
        query = "symbol=BTCUSDT&side=BUY&type=MARKET&quantity=1&recvWindow=5000&timestamp=1600000000000"
        self.assertEqual(
            sign_query("test-secret", query),
            "80dccb3f2d1f3a61ad360f3ed1619bd675b02434e58f0f7c72b83f1ecb19f2a8",
        )


if __name__ == "__main__":
    unittest.main()
