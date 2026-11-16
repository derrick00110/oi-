import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oi_monitor.broadcast import (
    MIN_DIRECTION_COOLDOWN_SEC,
    BroadcastBook,
    clean_rules,
    compose,
    default_rules,
    seconds_until_scan,
)
from oi_monitor.metrics import cycle_view, screen_change
from oi_monitor.trading import TradingDesk, sign_query
from oi_monitor.client import BinanceError


def row(symbol="BTCUSDT", base="BTC", move=6.5, oi_move=None, value_move=None, price_move=5.2, oi_value=7_600_000_000, funding=None, value_delta=460_000_000):
    change = {
        "oiPct": move if oi_move is None else oi_move,
        "oiValuePct": move if value_move is None else value_move,
        "oiDelta": 1000,
        "oiValueDelta": value_delta,
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
        "cycle": {
            "ready": True,
            "count": 5,
            "first": oi_value - value_delta,
            "fifth": oi_value,
            "oiValuePct": change["oiValuePct"],
            "pricePct": price_move,
            "oiValueDelta": value_delta,
            "ticks": [],
        },
    }


def loose_rule():
    rule = dict(default_rules()[0])
    rule["dynamic"] = False
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
        self.assertEqual(default_rules()[0]["metric"], "oiValuePct")
        self.assertIn("🟢 [BTCUSDT] 币安未平仓金额 +12.0%", text)
        self.assertIn("第 1 次到第 5 次刻度价格上涨 6.0%", text)
        self.assertIn("第 1 次刻度", text)
        self.assertIn("第 5 次刻度 750 万美元", text)
        self.assertNotIn("3600", text)
        self.assertIn("未平仓金额/市值比率：0.5%", text)
        self.assertIn("24 小时价格变动：-4.2%", text)
        self.assertIn("优先推报，做多信号", text)
        body = text.split("] ", 1)[1]
        self.assertNotRegex(body, r"[A-Za-z]")

        small_rule = loose_rule()
        small = compose(row(oi_value=4_500_000), small_rule)
        self.assertIn("450 万美元", small)

    def test_tiered_gate_four_forms_double_and_watershed(self):
        rule = default_rules()[0]
        self.assertTrue(rule["dynamic"])
        self.assertIn("优先推报，做多信号", compose(band_row(), rule))
        self.assertIn("可能为出货、洗盘或蓄力", compose(band_row(price_move=-3), rule))
        self.assertIn("可能为出货、洗盘或蓄力", compose(band_row(price_move=0.1), rule))
        self.assertIn("弃盘行情，需观望", compose(band_row(value_move=-12, price_move=-3), rule))
        self.assertIn("未平仓减少、价格上涨，需观望", compose(band_row(value_move=-12, price_move=3), rule))
        self.assertIsNone(compose(band_row(move=8.9), rule))
        self.assertIn("优先推报，做多信号", compose(band_row(move=9), rule))
        self.assertIsNone(compose(band_row(move=2, oi_move=20), rule))
        self.assertIsNone(compose(band_row(oi_value=4_000_000, move=9.9), rule))
        self.assertIn("优先推报，做多信号", compose(band_row(oi_value=4_000_000, move=10), rule))
        self.assertIn("弃盘行情，需观望", compose(band_row(oi_value=4_000_000, value_move=-10, price_move=-3), rule))
        self.assertIn("优先推报，做多信号", compose(band_row(oi_value=12_000_000, move=5), rule))
        self.assertIsNone(compose(band_row(oi_value=12_000_000, move=4.9), rule))
        self.assertIn("优先推报，做多信号", compose(band_row(oi_value=18_000_000, move=5), rule))
        self.assertIsNone(compose(band_row(oi_value=18_000_000, move=4.9), rule))

        doubled = compose(band_row(oi_value=10_000_000, value_delta=5_000_000, move=100, price_move=8), rule)
        self.assertIn("最高级别", doubled)
        self.assertIn("短时间翻倍", doubled)
        self.assertIn("优先推报，做多信号", doubled)

        crossed = compose(band_row(oi_value=20_100_000, value_delta=500_000, move=2.5, price_move=1), rule)
        self.assertIn("2000万关键分水岭", crossed)

        waiting = band_row(oi_value=6_000_000, value_delta=1_000_000, move=20, price_move=10)
        waiting["cycle"]["ready"] = False
        self.assertIsNone(compose(waiting, rule))

    def test_forms_follow_notional_not_quantity(self):
        rule = loose_rule()
        cases = [
            (2, 1, None, "🟢", "优先推报，做多信号"),
            (2, -1, None, "⚪", "可能为出货、洗盘或蓄力"),
            (2, 0.1, 0.0001, "⚪", "可能为出货、洗盘或蓄力"),
            (-2, 1, None, "⚪", "未平仓减少、价格上涨，需观望"),
            (-2, -1, None, "⚪", "弃盘行情，需观望"),
            (-2, 0.1, -0.00025, "⚪", "未平仓减少、价格持平，需观望"),
        ]
        for value_move, price_move, funding, mark, fact in cases:
            text = compose(row(value_move=value_move, oi_move=-value_move, price_move=price_move, funding=funding), rule, 0.5)
            self.assertTrue(text.startswith(mark), text)
            self.assertIn(fact, text)

        disguised = row(move=8, oi_move=-3, price_move=4)
        text = compose(disguised, rule, 0.5)
        self.assertIn("币安未平仓金额 +8.0%", text)
        self.assertIn("优先推报，做多信号", text)
        self.assertNotIn("未平仓减少", text)

    def test_fresh_scan_lands_just_after_each_five_minute_mark(self):
        base = 1_700_000_100
        base -= base % 300
        self.assertEqual(seconds_until_scan(base), 90)
        self.assertEqual(seconds_until_scan(base + 89), 1)
        self.assertEqual(seconds_until_scan(base + 90), 300)
        self.assertEqual(seconds_until_scan(base + 120), 270)
        self.assertLessEqual(seconds_until_scan(base + 1), 120)

    def test_threshold_cooldown_and_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            quiet = book.evaluate([band_row(move=1.0)], 1000, now=1_000)
            self.assertEqual(quiet, [])
            first = book.evaluate([band_row()], 1000, now=1_000)
            self.assertEqual(len(first), 1)
            self.assertEqual(book.evaluate([band_row()], 1000, now=1_100), [])
            self.assertEqual(book.evaluate([band_row()], 2000, now=1_100), [])
            self.assertEqual(book.evaluate([band_row()], 3000, now=1_000 + 901), [])
            again = book.evaluate([band_row()], 4000, now=1_000 + MIN_DIRECTION_COOLDOWN_SEC)
            self.assertEqual(len(again), 1)

            book.settings["maxPerRound"] = 1
            plain = band_row(symbol="AAAUSDT", base="AAA", move=40)
            doubled = band_row(symbol="BBBUSDT", base="BBB", oi_value=10_000_000, value_delta=5_000_000, move=12)
            first_only = book.evaluate([plain, doubled], 8000, now=4_000)
            self.assertEqual(first_only[0]["symbol"], "BBBUSDT")
            self.assertIn("最高级别", first_only[0]["text"])

            book.settings["maxPerRound"] = 5
            rows = [band_row(symbol=f"C{i}USDT", base=f"C{i}", move=20 - i) for i in range(6)]
            burst = book.evaluate(rows, 9000, now=5_000)
            self.assertEqual(len(burst), 5)
            self.assertEqual(burst[0]["symbol"], "C0USDT")

    def test_each_five_minute_scan_checks_the_rolling_gate(self):
        step = 5 * 60 * 1000
        half = 30 * 60 * 1000
        base = 1_700_000_000_000
        base -= base % half

        def points(spec):
            rows = []
            for minute, amount in spec:
                rows.append({"timestamp": base + minute * 60 * 1000, "oi": amount / 10, "oiValue": amount, "supply": 0})
            return rows

        def watched(symbol, series, now_ms):
            view = cycle_view(series, now_ms)
            return {
                "symbol": symbol,
                "base": symbol.removesuffix("USDT"),
                "price": 10,
                "priceChangePercent": 0,
                "oi": 1,
                "oiValue": view["fifth"],
                "marketCap": None,
                "oiToMarketCapPct": None,
                "changes": {},
                "cycle": view,
                "snapshotAt": now_ms,
            }

        # :11:30 is inside the old 30-minute block, before the old verify minute (:25).
        first_now = base + 10 * 60 * 1000 + 90 * 1000
        second_now = first_now + step
        self.assertNotEqual(first_now % half, 0)
        self.assertLess(first_now, base + 25 * 60 * 1000)
        early_spec = [(-10, 10_000_000), (-5, 10_100_000), (0, 10_200_000), (5, 10_400_000), (10, 11_000_000)]
        early = points(early_spec)
        early_view = cycle_view(early, first_now)
        self.assertTrue(early_view["ready"])
        self.assertEqual([tick["at"] for tick in early_view["ticks"]], [base + minute * 60 * 1000 for minute, _amount in early_spec])
        early_gate = screen_change(early_view["fifth"], early_view["oiValuePct"], early_view["pricePct"], early_view["oiValueDelta"])
        self.assertEqual(early_gate["threshold"], 5)
        self.assertTrue(early_gate["qualified"])

        flat = points([(-10, 10_000_000), (-5, 10_000_000), (0, 10_000_000), (5, 10_000_000), (10, 10_000_000)])
        flat_view = cycle_view(flat, first_now)
        flat_gate = screen_change(flat_view["fifth"], flat_view["oiValuePct"], flat_view["pricePct"], flat_view["oiValueDelta"])
        self.assertTrue(flat_view["ready"])
        self.assertFalse(flat_gate["qualified"])
        jumped = points(
            [(-10, 10_000_000), (-5, 10_000_000), (0, 10_000_000), (5, 10_000_000), (10, 10_000_000), (15, 11_500_000)]
        )
        jumped_view = cycle_view(jumped, second_now)
        jumped_gate = screen_change(
            jumped_view["fifth"], jumped_view["oiValuePct"], jumped_view["pricePct"], jumped_view["oiValueDelta"]
        )
        self.assertTrue(jumped_view["ready"])
        self.assertEqual(jumped_view["ticks"][0]["at"], base - 5 * 60 * 1000)
        self.assertEqual(jumped_view["ticks"][-1]["at"], base + 15 * 60 * 1000)
        self.assertTrue(jumped_gate["qualified"])

        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            first = book.evaluate(
                [watched("AAAUSDT", early, first_now), watched("BBBUSDT", flat, first_now)],
                1000,
                now=1_000,
            )
            self.assertEqual([item["symbol"] for item in first], ["AAAUSDT"])
            second = book.evaluate(
                [
                    watched("AAAUSDT", points(early_spec + [(15, 11_200_000)]), second_now),
                    watched("BBBUSDT", jumped, second_now),
                ],
                2000,
                now=1_000 + 300,
            )
            self.assertEqual([item["symbol"] for item in second], ["BBBUSDT"])

    def test_same_direction_cooldown_is_thirty_minutes_and_configurable(self):
        self.assertGreaterEqual(default_rules()[0]["cooldownSec"], 30 * 60)
        longer = default_rules()
        longer[0]["cooldownSec"] = 45 * 60
        self.assertEqual(clean_rules(longer)[0]["cooldownSec"], 45 * 60)
        shorter = default_rules()
        shorter[0]["cooldownSec"] = 900
        with self.assertRaises(ValueError):
            clean_rules(shorter)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            saved = default_rules()
            saved[0]["cooldownSec"] = 900
            saved[0]["minAbsPct"] = 12
            (path / "rules.json").write_text(json.dumps(saved), encoding="utf-8")
            book = BroadcastBook(path)
            self.assertEqual(book.rules[0]["cooldownSec"], MIN_DIRECTION_COOLDOWN_SEC)
            self.assertEqual(book.rules[0]["minAbsPct"], 12)

            first = book.evaluate([band_row()], 1000, now=10_000)
            self.assertEqual(len(first), 1)
            self.assertEqual(book.evaluate([band_row()], 2000, now=10_000 + 300), [])
            self.assertEqual(book.evaluate([band_row()], 3000, now=10_000 + 900), [])
            self.assertEqual(book.evaluate([band_row()], 4000, now=10_000 + MIN_DIRECTION_COOLDOWN_SEC - 1), [])
            again = book.evaluate([band_row()], 5000, now=10_000 + MIN_DIRECTION_COOLDOWN_SEC)
            self.assertEqual(len(again), 1)
            opposite = book.evaluate(
                [band_row(value_move=-12, price_move=-3)],
                6000,
                now=10_000 + MIN_DIRECTION_COOLDOWN_SEC,
            )
            self.assertEqual(len(opposite), 1)
            self.assertIn("弃盘行情，需观望", opposite[0]["text"])

            book.replace_rules([{**default_rules()[0], "cooldownSec": 45 * 60}])
            held = book.evaluate([band_row(symbol="ZZZUSDT", base="ZZZ")], 7000, now=20_000)
            self.assertEqual(len(held), 1)
            self.assertEqual(book.evaluate([band_row(symbol="ZZZUSDT", base="ZZZ")], 8000, now=20_000 + 30 * 60), [])
            released = book.evaluate([band_row(symbol="ZZZUSDT", base="ZZZ")], 9000, now=20_000 + 45 * 60)
            self.assertEqual(len(released), 1)

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
