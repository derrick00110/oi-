"""Stop-loss and closed-watch buy-back alerts. Public prices only."""

import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oi_monitor.broadcast import (
    BUYBACK_PREFIX,
    MIN_DIRECTION_COOLDOWN_SEC,
    STOP_LOSS_PREFIX,
    BroadcastBook,
    post_webhook,
)
from oi_monitor.holdings import STOP_LOSS_FRACTION, HoldingsBook
from oi_monitor.service import Monitor
from server import BroadcastLoop, Handler


def _handler(method, path, body=b""):
    handler = Handler.__new__(Handler)
    handler.path = path
    handler.rfile = io.BytesIO(body)
    handler.headers = {"Content-Length": str(len(body))}
    handler.command = method
    captured = {}

    def send_json(status, payload):
        captured["status"] = status
        captured["payload"] = payload

    handler.send_json = send_json
    return handler, captured


def server_monitor():
    import server

    return server.MONITOR


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b""


def row(symbol="MANTRAUSDT", base="MANTRA", move=1.0, price_move=0.1, oi_value=1_000_000, price=1.0, value_delta=10_000):
    change = {
        "oiPct": move,
        "oiValuePct": move,
        "oiDelta": 1000,
        "oiValueDelta": value_delta,
        "pricePct": price_move,
    }
    return {
        "symbol": symbol,
        "base": base,
        "price": price,
        "priceChangePercent": -4.2,
        "oi": 95000,
        "oiValue": oi_value,
        "marketCap": 1_600_000_000_000,
        "oiToMarketCapPct": 0.47,
        "fundingRate": None,
        "changes": {"15m": change, "1h": change, "4h": change, "24h": change},
        "cycle": {
            "ready": True,
            "count": 5,
            "first": oi_value - value_delta,
            "fifth": oi_value,
            "oiValuePct": move,
            "pricePct": price_move,
            "oiValueDelta": value_delta,
            "ticks": [],
        },
    }


def green_row(symbol="MANTRAUSDT", base="MANTRA"):
    """Open interest and price both up, past the 9% tier for a 7.5M notional."""
    return row(symbol=symbol, base=base, move=12, price_move=6, oi_value=7_500_000, price=0.42, value_delta=800_000)


class ReferencePriceTest(unittest.TestCase):
    def test_legacy_file_backfills_once_and_keeps_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "holdings.json").write_text(json.dumps(["MANTRAUSDT", "牛来USDT"], ensure_ascii=False), encoding="utf-8")
            book = HoldingsBook(path)
            self.assertEqual(book.list(), ["MANTRAUSDT", "牛来USDT"])
            self.assertIsNone(book.public()["holdings"][0]["entry_price"])
            self.assertNotIn("stop_armed", book.public()["holdings"][0])

            events = book.observe_prices({"MANTRAUSDT": 1.5, "牛来USDT": 2.25, "ETHUSDT": 9})
            self.assertEqual(events, [])
            public = book.public()
            self.assertEqual(public["holdings"][0]["entry_price"], 1.5)
            self.assertEqual(public["holdings"][1]["entry_price"], 2.25)
            book.observe_prices({"MANTRAUSDT": 9, "牛来USDT": 1})
            self.assertEqual(book.public()["holdings"][0]["entry_price"], 1.5)
            raw = (path / "holdings.json").read_text(encoding="utf-8")
            self.assertIn("牛来USDT", raw)
            self.assertNotIn("\\u", raw)

    def test_scan_backfill_does_not_alert_on_the_fill_price(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "holdings.json").write_text('["MANTRAUSDT"]', encoding="utf-8")
            book = BroadcastBook(path)
            sample = row(price=4.2)
            self.assertEqual(book.evaluate([sample], 1, now=1_000), [])
            entry = book.holdings.public()["holdings"][0]["entry_price"]
            self.assertEqual(entry, 4.2)
            sample["price"] = entry * (1 - STOP_LOSS_FRACTION)
            hit = book.evaluate([sample], 2, now=2_000)
            self.assertEqual(len(hit), 1)
            self.assertTrue(hit[0]["text"].startswith(STOP_LOSS_PREFIX + " [MANTRAUSDT]"))

    def test_post_records_lookup_or_override_and_unicode_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            book = BroadcastBook(path)
            known = {"ETHUSDT", "牛来USDT"}
            looked = []

            def lookup(symbol):
                looked.append(symbol)
                return "1.75"

            body = json.dumps({"symbol": "eth"}).encode("utf-8")
            with patch("server.BOOK", book), patch.object(server_monitor(), "known_symbols", return_value=known), patch.object(
                server_monitor(), "current_price", side_effect=lookup
            ):
                post, posted = _handler("POST", "/api/holdings", body)
                post.do_POST()
            self.assertEqual(posted["status"], 200)
            self.assertEqual(looked, ["ETHUSDT"])
            self.assertEqual(posted["payload"]["entry_price"], 1.75)
            self.assertEqual(posted["payload"]["holdings"], ["MANTRAUSDT", "ETHUSDT"])

            called = {"n": 0}

            def forbidden(_symbol):
                called["n"] += 1
                raise AssertionError("entry_price 已给出，不应再查现价")

            override = json.dumps({"symbol": "牛来", "entry_price": 3.5}, ensure_ascii=False).encode("utf-8")
            with patch("server.BOOK", book), patch.object(server_monitor(), "known_symbols", return_value=known), patch.object(
                server_monitor(), "current_price", side_effect=forbidden
            ):
                post, posted = _handler("POST", "/api/holdings", override)
                post.do_POST()
            self.assertEqual(called["n"], 0)
            self.assertEqual(posted["status"], 200)
            self.assertEqual(posted["payload"]["symbol"], "牛来USDT")
            self.assertEqual(posted["payload"]["entry_price"], 3.5)
            raw = (path / "holdings.json").read_text(encoding="utf-8")
            self.assertIn("牛来USDT", raw)
            self.assertNotIn("\\u", raw)

            bad = json.dumps({"symbol": "ETH", "entry_price": -1}).encode("utf-8")
            with patch("server.BOOK", book), patch.object(server_monitor(), "known_symbols", return_value=known):
                rejected, rejection = _handler("POST", "/api/holdings", bad)
                rejected.do_POST()
            self.assertEqual(rejection["status"], 400)

            encoded = urllib.parse.quote("牛来USDT")
            with patch("server.BOOK", book):
                delete, deleted = _handler("DELETE", "/api/holdings/" + encoded)
                delete.do_DELETE()
                listing, listed = _handler("GET", "/api/holdings")
                listing.do_GET()
            self.assertEqual(deleted["status"], 200)
            self.assertEqual(deleted["payload"]["symbol"], "牛来USDT")
            self.assertTrue(deleted["payload"]["removed"])
            symbols = [item["symbol"] for item in listed["payload"]["holdings"]]
            self.assertNotIn("牛来USDT", symbols)
            self.assertEqual(listed["payload"]["closed_watch"][0]["symbol"], "牛来USDT")
            self.assertEqual(listed["payload"]["closed_watch"][0]["green_count"], 0)
            self.assertGreater(listed["payload"]["closed_watch"][0]["closed_at"], 0)

            again = json.dumps({"symbol": "牛来USDT", "entry_price": 4}).encode("utf-8")
            with patch("server.BOOK", book), patch.object(server_monitor(), "known_symbols", return_value=known), patch.object(
                server_monitor(), "current_price", side_effect=forbidden
            ):
                post, posted = _handler("POST", "/api/holdings", again)
                post.do_POST()
                listing, listed = _handler("GET", "/api/holdings")
                listing.do_GET()
            self.assertEqual(posted["payload"]["entry_price"], 4)
            self.assertEqual(listed["payload"]["closed_watch"], [])
            restored = next(item for item in listed["payload"]["holdings"] if item["symbol"] == "牛来USDT")
            self.assertEqual(restored["entry_price"], 4)


class StopLossTest(unittest.TestCase):
    def test_fires_once_per_breach_then_rearms_or_on_readd(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            book = BroadcastBook(path)
            book.holdings.add("MANTRA", entry_price=100)
            book.update_settings({"maxPerRound": 1})
            loud = row(symbol="BTCUSDT", base="BTC", move=12, price_move=6, oi_value=7_500_000, price=80_000, value_delta=800_000)

            self.assertEqual(book.evaluate([row(price=87.51)], 1, now=10_000), [])
            both = book.evaluate([loud, row(price=87.5)], 2, now=10_300)
            hit = [item for item in both if item["ruleId"] == "holding-stop"]
            self.assertEqual(len(both), 2)
            self.assertEqual(sum(item["symbol"] == "BTCUSDT" for item in both), 1)
            self.assertEqual(
                hit[0]["text"],
                "🚨【持仓止损提醒·跌破-12.5%】 [MANTRAUSDT] 参考价 100，现价 87.5，涨跌 -12.5%",
            )
            self.assertTrue(hit[0]["text"].startswith("🚨"))
            self.assertNotIn("🟢", hit[0]["text"])
            self.assertNotIn("🔁", hit[0]["text"])
            self.assertNotIn("今日第", hit[0]["text"])
            self.assertEqual(book.evaluate([row(price=80)], 3, now=10_600), [])

            restarted = BroadcastBook(path)
            sample = row(price=80)

            def quiet(target, price, snap, moment):
                sample["price"] = price
                return [item for item in target.evaluate([sample], snap, now=moment) if item["ruleId"] == "holding-stop"]

            self.assertEqual(quiet(restarted, 80, 4, 11_000), [])
            self.assertEqual(quiet(restarted, 87.51, 5, 11_300), [])
            again = quiet(restarted, 87.5, 6, 11_600)
            self.assertEqual(len(again), 1)
            self.assertTrue(again[0]["text"].startswith(STOP_LOSS_PREFIX))
            self.assertEqual(quiet(restarted, 70, 7, 11_900), [])

            restarted.holdings.remove("MANTRA")
            restarted.holdings.add("MANTRA", entry_price=100)
            readded = quiet(restarted, 70, 8, 12_200)
            self.assertEqual(len(readded), 1)
            self.assertIn("[MANTRAUSDT]", readded[0]["text"])

    def test_unicode_symbol_and_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            book = BroadcastBook(path)
            book.holdings.remove("MANTRA")
            book.holdings.add("牛来", entry_price=2)
            book.update_settings({"webhookUrl": "https://example.test/hook", "webhookToken": "tok"})
            sample = row(symbol="牛来USDT", base="牛来", price=1.5, move=1, oi_value=1_000_000)

            class Monitor:
                def scan(self, limit, fresh=True):
                    return {"rows": [sample], "snapshotAt": 9, "requested": 1, "returned": 1}

            loop = BroadcastLoop(Monitor(), book)
            with patch("server.MEMORY.record_scan", lambda *args, **kwargs: None), patch(
                "server.queue_webhooks"
            ) as hooks, self.assertLogs("oi-monitor", level="INFO") as caught:
                loop.tick()
            self.assertEqual(hooks.call_args[0][0], "https://example.test/hook")
            self.assertEqual(hooks.call_args[0][2], "tok")
            sent = hooks.call_args[0][1]
            self.assertEqual(len(sent), 1)
            self.assertTrue(sent[0]["text"].startswith("🚨【持仓止损提醒·跌破-12.5%】 [牛来USDT]"))
            self.assertIn("参考价 2", sent[0]["text"])
            self.assertIn("现价 1.5", sent[0]["text"])
            self.assertEqual(sent[0]["symbol"], "牛来USDT")
            self.assertIn("持仓止损提醒", "\n".join(caught.output))
            self.assertNotIn("tok", "\n".join(caught.output))

            with patch("server.BOOK", book):
                listing, listed = _handler("GET", "/api/broadcasts")
                listing.do_GET()
            self.assertTrue(any(item["text"].startswith(STOP_LOSS_PREFIX) for item in listed["payload"]["items"]))

            seen = {}

            def fake_open(request, timeout=0):
                seen["body"] = json.loads(request.data.decode("utf-8"))
                seen["auth"] = request.get_header("Authorization")
                return _Response()

            with patch("urllib.request.urlopen", fake_open):
                post_webhook("https://example.test/hook", sent[0], token="tok")
            self.assertEqual(set(seen["body"]), {"text", "symbol", "id"})
            self.assertEqual(seen["body"]["symbol"], "牛来USDT")
            self.assertTrue(seen["body"]["text"].startswith("🚨【持仓止损提醒·跌破-12.5%】 [牛来USDT]"))
            self.assertEqual(seen["auth"], "Bearer tok")


class ClosedWatchTest(unittest.TestCase):
    def test_two_green_broadcasts_then_drop_and_readd_clears(self):
        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            removed = book.holdings.remove("MANTRA", now=1_000)
            self.assertTrue(removed["removed"])
            self.assertEqual(removed["closed_watch"][0]["symbol"], "MANTRAUSDT")
            self.assertEqual(removed["closed_watch"][0]["green_count"], 0)

            green = green_row()
            # A green while it was still held does not exist here; the close is first.
            # A broadcast timestamp before the close must not count.
            self.assertIsNone(book.holdings.note_long_green("MANTRAUSDT", 999_000))
            self.assertEqual(book.holdings.public()["closed_watch"][0]["green_count"], 0)

            start = 2_000
            first = book.evaluate([green], 1, now=start)
            self.assertEqual(len(first), 1)
            self.assertIn("🟢", first[0]["text"])
            self.assertNotIn("买回", first[0]["text"])
            self.assertEqual(book.holdings.public()["closed_watch"][0]["green_count"], 1)
            self.assertEqual(book.evaluate([green], 2, now=start + 300), [])
            self.assertEqual(book.holdings.public()["closed_watch"][0]["green_count"], 1)

            white = row(move=-12, price_move=-3, oi_value=7_500_000, price=0.4, value_delta=-900_000)
            whites = book.evaluate([white], 3, now=start + 600)
            self.assertEqual(len(whites), 1)
            self.assertNotIn("🟢", whites[0]["text"])
            self.assertEqual(book.holdings.public()["closed_watch"][0]["green_count"], 1)

            second = book.evaluate([green], 4, now=start + MIN_DIRECTION_COOLDOWN_SEC)
            self.assertEqual(len(second), 2)
            self.assertIn("🟢", second[0]["text"])
            self.assertNotIn(BUYBACK_PREFIX, second[0]["text"])
            self.assertEqual(
                second[1]["text"],
                "🔔【平仓币买回提醒】 [MANTRAUSDT] 平仓后已出现 2 次绿色播报（OI 与价格共振上涨），"
                "未平仓金额：750 万美元，价格 0.42，未平仓金额 +12.0%，第 1 次到第 5 次刻度价格上涨 6.0%",
            )
            self.assertTrue(second[1]["text"].startswith("🔔"))
            self.assertNotIn("🟢", second[1]["text"])
            self.assertNotIn("🔁", second[1]["text"])
            self.assertEqual(book.holdings.public()["closed_watch"], [])
            texts = [item["text"] for item in book.snapshot()["items"]]
            self.assertTrue(any(text.startswith(BUYBACK_PREFIX) for text in texts))

            later = book.evaluate([green], 5, now=start + 2 * MIN_DIRECTION_COOLDOWN_SEC)
            self.assertEqual(len(later), 1)
            self.assertNotIn("买回", later[0]["text"])

    def test_readd_drops_the_watch_before_two_greens(self):
        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            book.holdings.add("ETH", entry_price=10)
            book.holdings.remove("ETH", now=5_000)
            green = green_row(symbol="ETHUSDT", base="ETH")
            made = book.evaluate([green], 1, now=6_000)
            self.assertEqual(len(made), 1)
            self.assertEqual(book.holdings.public()["closed_watch"][0]["green_count"], 1)
            book.holdings.add("ETH", entry_price=11)
            self.assertEqual(book.holdings.public()["closed_watch"], [])
            held = next(item for item in book.holdings.public()["holdings"] if item["symbol"] == "ETHUSDT")
            self.assertEqual(held["entry_price"], 11)
            again = book.evaluate([green], 2, now=6_000 + MIN_DIRECTION_COOLDOWN_SEC)
            self.assertTrue(all("买回" not in item["text"] for item in again))
            self.assertEqual(book.holdings.public()["closed_watch"], [])

    def test_doubled_green_without_rising_price_does_not_count(self):
        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            book.holdings.add("SOL", entry_price=1)
            book.holdings.remove("SOL", now=1_000)
            doubled = row(
                symbol="SOLUSDT",
                base="SOL",
                move=100,
                price_move=-3,
                oi_value=10_000_000,
                price=2,
                value_delta=5_000_000,
            )
            made = book.evaluate([doubled], 1, now=2_000)
            self.assertEqual(len(made), 1)
            self.assertIn("🟢", made[0]["text"])
            self.assertNotIn("优先推报，做多信号", made[0]["text"])
            self.assertEqual(book.holdings.public()["closed_watch"][0]["symbol"], "SOLUSDT")
            self.assertEqual(book.holdings.public()["closed_watch"][0]["green_count"], 0)

    def test_unicode_buyback(self):
        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            book.holdings.add("牛来", entry_price=2)
            book.holdings.remove("牛来", now=1_000)
            green = green_row(symbol="牛来USDT", base="牛来")
            book.evaluate([green], 1, now=2_000)
            second = book.evaluate([green], 2, now=2_000 + MIN_DIRECTION_COOLDOWN_SEC)
            self.assertTrue(second[-1]["text"].startswith("🔔【平仓币买回提醒】 [牛来USDT]"))
            self.assertIn("平仓后已出现 2 次绿色播报（OI 与价格共振上涨）", second[-1]["text"])
            self.assertEqual(book.holdings.public()["closed_watch"], [])
            raw = (Path(directory) / "holdings.json").read_text(encoding="utf-8")
            self.assertNotIn("牛来USDT", raw)


class PublicPriceTest(unittest.TestCase):
    def test_current_price_uses_the_public_ticker_only(self):
        seen = {}

        class Client:
            def get_json(self, path, params=None, timeout=20):
                seen["path"] = path
                seen["params"] = params
                return {"symbol": params["symbol"], "price": "1.25"}

        monitor = Monitor(client=Client())
        monitor._ticker_prices = {"ETHUSDT": 8}
        self.assertEqual(monitor.prices_for(["ETHUSDT", "NOPEUSDT"]), {"ETHUSDT": 8})
        self.assertEqual(monitor.current_price("ETHUSDT"), 8)
        self.assertNotIn("path", seen)
        self.assertEqual(monitor.current_price("牛来USDT"), 1.25)
        self.assertEqual(seen["path"], "/fapi/v1/ticker/price")
        self.assertEqual(seen["params"], {"symbol": "牛来USDT"})
        self.assertNotIn("order", seen["path"])
        self.assertNotIn("account", seen["path"])


if __name__ == "__main__":
    unittest.main()
