import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oi_monitor.broadcast import (
    BroadcastBook,
    authorization_value,
    clean_settings,
    default_settings,
    deliver_webhook,
    post_webhook,
    queue_webhooks,
)
from server import BroadcastLoop, Handler


TOKEN = "super-secret-token"
URL = "https://example.test/hook"


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b""


def _item(item_id=1, text="播报正文", symbol="BTCUSDT"):
    return {"id": item_id, "text": text, "symbol": symbol, "extra": "not-in-body"}


class WebhookDeliveryTest(unittest.TestCase):
    def test_payload_stays_the_same_without_a_token(self):
        seen = {}

        def fake_open(request, timeout=0):
            seen["timeout"] = timeout
            seen["method"] = request.get_method()
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data.decode("utf-8"))
            seen["type"] = request.get_header("Content-type")
            seen["agent"] = request.get_header("User-agent")
            return _Response()

        with patch("urllib.request.urlopen", fake_open):
            post_webhook(URL, _item(), timeout=5)

        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["timeout"], 5)
        self.assertIsNone(seen["auth"])
        self.assertEqual(seen["body"], {"text": "播报正文", "symbol": "BTCUSDT", "id": 1})
        self.assertEqual(seen["type"], "application/json; charset=utf-8")
        self.assertEqual(seen["agent"], "oi-monitor")
        self.assertIsNone(deliver_webhook("", _item(), token=TOKEN))

    def test_token_is_sent_as_bearer_and_not_doubled(self):
        cases = {
            TOKEN: f"Bearer {TOKEN}",
            f"Bearer {TOKEN}": f"Bearer {TOKEN}",
            "bearer lowercase": "Bearer bearer lowercase",
            "   spaced   ": "Bearer spaced",
        }
        for token, expected in cases.items():
            with self.subTest(token=token):
                seen = {}

                def fake_open(request, timeout=0, _seen=seen):
                    _seen["auth"] = request.get_header("Authorization")
                    _seen["body"] = json.loads(request.data.decode("utf-8"))
                    return _Response()

                with patch("urllib.request.urlopen", fake_open):
                    self.assertIsNone(deliver_webhook(URL, _item(), token=token))
                self.assertEqual(seen["auth"], expected)
                self.assertEqual(set(seen["body"]), {"text", "symbol", "id"})
                self.assertNotIn(token.strip(), json.dumps({k: v for k, v in seen.items() if k != "auth"}))

    def test_authorization_value_rejects_header_injection(self):
        self.assertEqual(authorization_value(f"Bearer {TOKEN}"), f"Bearer {TOKEN}")
        self.assertEqual(authorization_value(TOKEN), f"Bearer {TOKEN}")
        self.assertIsNone(authorization_value(""))
        self.assertIsNone(authorization_value("   "))
        self.assertIsNone(authorization_value(f"{TOKEN}\r\nX-Injected: 1"))

    def test_failure_is_returned_without_the_token(self):
        def fake_open(request, timeout=0):
            self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
            raise urllib.error.URLError(f"down {TOKEN}")

        with patch("urllib.request.urlopen", fake_open):
            error = deliver_webhook(URL, _item(), token=TOKEN)
        self.assertIsNotNone(error)
        self.assertNotIn(TOKEN, error)
        self.assertIn("down", error)

    def test_queue_does_not_block_and_logs_failures_without_the_token(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []
        auths = []

        def fake_open(request, timeout=0):
            body = json.loads(request.data.decode("utf-8"))
            calls.append(body["id"])
            auths.append(request.get_header("Authorization"))
            if body["id"] == 1:
                entered.set()
                release.wait(2)
                raise urllib.error.URLError(f"down {TOKEN}")
            return _Response()

        with patch("urllib.request.urlopen", fake_open):
            started = time.perf_counter()
            try:
                thread = queue_webhooks(URL, [_item(1), _item(2)], TOKEN)
                elapsed = time.perf_counter() - started
                self.assertIsNotNone(thread)
                self.assertLess(elapsed, 0.5)
                self.assertTrue(entered.wait(1))
                with self.assertLogs("oi-monitor", level="WARNING") as caught:
                    release.set()
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
            finally:
                release.set()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(auths, [f"Bearer {TOKEN}", f"Bearer {TOKEN}"])
        logged = "\n".join(caught.output)
        self.assertIn("webhook 发送失败", logged)
        self.assertNotIn(TOKEN, logged)
        self.assertIsNone(queue_webhooks("", [_item()], TOKEN))
        self.assertIsNone(queue_webhooks(URL, [], TOKEN))

    def test_unexpected_error_does_not_stop_the_rest_or_log_the_token(self):
        calls = []

        def fake_open(request, timeout=0):
            body = json.loads(request.data.decode("utf-8"))
            calls.append(body["id"])
            if body["id"] == 1:
                raise RuntimeError(TOKEN)
            return _Response()

        with patch("urllib.request.urlopen", fake_open):
            with self.assertLogs("oi-monitor", level="WARNING") as caught:
                thread = queue_webhooks(URL, [_item(1), _item(2)], TOKEN)
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [1, 2])
        self.assertNotIn(TOKEN, "\n".join(caught.output))
        self.assertIn("webhook 发送失败", "\n".join(caught.output))


class WebhookSettingsTest(unittest.TestCase):
    def test_settings_round_trip_masks_token_and_keeps_it_on_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            book = BroadcastBook(path)
            public = book.update_settings(
                {"webhookUrl": f"{URL}?key={TOKEN}", "webhookToken": f"Bearer {TOKEN}"}
            )
            raw = json.dumps(public)
            self.assertNotIn(TOKEN, raw)
            self.assertEqual(public["webhookToken"], "Bearer ***")
            self.assertTrue(public["webhookTokenSet"])
            self.assertEqual(public["webhookUrl"], f"{URL}?***")
            self.assertTrue(public["webhookSet"])
            self.assertNotIn(TOKEN, json.dumps(book.snapshot()))

            stored = json.loads((path / "settings.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["webhookToken"], f"Bearer {TOKEN}")
            self.assertEqual(book.webhook_delivery(), (f"{URL}?key={TOKEN}", f"Bearer {TOKEN}"))

            again = book.update_settings({"webhookToken": "Bearer ***", "priceChopPct": 0.8})
            self.assertEqual(again["priceChopPct"], 0.8)
            self.assertEqual(book.webhook_delivery()[1], f"Bearer {TOKEN}")
            self.assertNotIn(TOKEN, json.dumps(again))

            cleared = book.update_settings({"webhookToken": ""})
            self.assertEqual(cleared["webhookToken"], "")
            self.assertFalse(cleared["webhookTokenSet"])
            self.assertEqual(book.webhook_delivery()[1], "")

    def test_plain_token_mask_is_not_written_back(self):
        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            book.update_settings({"webhookToken": TOKEN, "webhookUrl": URL})
            kept = book.update_settings({"webhookToken": "***"})
            self.assertEqual(kept["webhookToken"], "***")
            self.assertEqual(book.webhook_delivery()[1], TOKEN)
            seen = {}

            def fake_open(request, timeout=0):
                seen["auth"] = request.get_header("Authorization")
                return _Response()

            with patch("urllib.request.urlopen", fake_open):
                url, token = book.webhook_delivery()
                self.assertIsNone(deliver_webhook(url, _item(), token=token))
            self.assertEqual(seen["auth"], f"Bearer {TOKEN}")

    def test_invalid_token_is_rejected_without_echoing_it(self):
        current = default_settings()
        with self.assertRaises(ValueError) as raised:
            clean_settings({"webhookToken": f"{TOKEN}\nX-Injected: 1"}, current)
        self.assertNotIn(TOKEN, str(raised.exception))
        with self.assertRaises(ValueError):
            clean_settings({"webhookToken": "x" * 4097}, current)
        with self.assertRaises(ValueError):
            clean_settings({"webhookToken": True}, current)
        untouched = clean_settings({"priceChopPct": 1}, {**current, "webhookToken": TOKEN})
        self.assertEqual(untouched["webhookToken"], TOKEN)

    def test_env_applies_at_startup_and_overrides_saved_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "settings.json").write_text(
                json.dumps({"webhookUrl": "https://saved.example/hook", "webhookToken": "saved-token-value"}),
                encoding="utf-8",
            )
            book = BroadcastBook(path)
            self.assertEqual(book.webhook_delivery(), ("https://saved.example/hook", "saved-token-value"))

            with patch.dict(
                os.environ,
                {"OI_WEBHOOK_URL": "https://env.example/hook", "OI_WEBHOOK_TOKEN": f"Bearer {TOKEN}"},
            ):
                self.assertEqual(book.webhook_delivery()[1], "saved-token-value")
                restarted = BroadcastBook(path)
            self.assertEqual(restarted.webhook_delivery(), ("https://env.example/hook", f"Bearer {TOKEN}"))
            self.assertNotIn(TOKEN, json.dumps(restarted.public_settings()))
            self.assertNotIn("saved-token-value", json.dumps(restarted.snapshot()))
            self.assertNotIn("env.example", (path / "settings.json").read_text(encoding="utf-8"))

    def test_invalid_env_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "settings.json").write_text(
                json.dumps({"webhookUrl": "https://saved.example/hook", "webhookToken": "saved-token-value"}),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"OI_WEBHOOK_URL": "ftp://nope", "OI_WEBHOOK_TOKEN": f"bad\n{TOKEN}"},
            ), self.assertLogs("oi-monitor", level="WARNING") as caught:
                book = BroadcastBook(path)
            self.assertEqual(book.webhook_delivery(), ("https://saved.example/hook", "saved-token-value"))
            logged = "\n".join(caught.output)
            self.assertIn("OI_WEBHOOK_URL 无效", logged)
            self.assertIn("OI_WEBHOOK_TOKEN 无效", logged)
            self.assertNotIn(TOKEN, logged)
            self.assertNotIn("ftp://nope", logged)

    def test_reloaded_book_keeps_a_token_saved_through_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            book = BroadcastBook(path)
            book.update_settings({"webhookUrl": URL, "webhookToken": TOKEN})
            reloaded = BroadcastBook(path)
            self.assertEqual(reloaded.webhook_delivery(), (URL, TOKEN))
            self.assertNotIn(TOKEN, json.dumps(reloaded.snapshot()))


class WebhookApiTest(unittest.TestCase):
    def _handler(self, method, path, body=b""):
        handler = Handler.__new__(Handler)
        handler.path = path
        handler.rfile = io.BytesIO(body)
        handler.headers = {"Content-Length": str(len(body))}
        handler.command = method
        captured = {}

        def send_json(status, payload):
            captured["status"] = status
            captured["raw"] = json.dumps(payload, ensure_ascii=False)

        handler.send_json = send_json
        return handler, captured

    def test_settings_and_broadcast_apis_never_return_the_token(self):
        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            body = json.dumps(
                {"webhookUrl": f"{URL}?key={TOKEN}", "webhookToken": TOKEN}
            ).encode("utf-8")
            with patch("server.BOOK", book):
                post, posted = self._handler("POST", "/api/settings", body)
                post.do_POST()
                get, listed = self._handler("GET", "/api/broadcasts")
                get.do_GET()
            self.assertEqual(posted["status"], 200)
            self.assertNotIn(TOKEN, posted["raw"])
            self.assertIn("***", posted["raw"])
            self.assertEqual(listed["status"], 200)
            self.assertNotIn(TOKEN, listed["raw"])
            self.assertIn(TOKEN, (Path(directory) / "settings.json").read_text(encoding="utf-8"))

    def test_invalid_token_error_does_not_echo_the_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            body = json.dumps({"webhookToken": f"{TOKEN}\nbad"}).encode("utf-8")
            with patch("server.BOOK", book):
                post, posted = self._handler("POST", "/api/settings", body)
                post.do_POST()
            self.assertEqual(posted["status"], 400)
            self.assertNotIn(TOKEN, posted["raw"])


class ScanLoopWebhookTest(unittest.TestCase):
    def test_tick_returns_while_webhook_is_still_sending(self):
        entered = threading.Event()
        release = threading.Event()
        done = threading.Event()
        seen = {}

        def slow(url, item, timeout=5, token=""):
            seen["token"] = token
            seen["url"] = url
            seen["body"] = {"text": item["text"], "symbol": item["symbol"], "id": item["id"]}
            entered.set()
            release.wait(2)
            done.set()

        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            book.update_settings({"webhookUrl": URL, "webhookToken": TOKEN})
            book.evaluate = lambda *args, **kwargs: [_item()]

            class Monitor:
                def scan(self, limit, fresh=True):
                    return {"rows": [], "snapshotAt": 1, "requested": 1, "returned": 0}

            loop = BroadcastLoop(Monitor(), book)
            with patch("server.MEMORY.record_scan", lambda *args, **kwargs: None), patch(
                "oi_monitor.broadcast.post_webhook", slow
            ):
                try:
                    with self.assertLogs("oi-monitor", level="INFO") as caught:
                        started = time.perf_counter()
                        loop.tick()
                        elapsed = time.perf_counter() - started
                    self.assertLess(elapsed, 0.5)
                    self.assertEqual(book.last_error, "")
                    self.assertTrue(entered.wait(1))
                finally:
                    release.set()
                self.assertTrue(done.wait(2))
            logged = "\n".join(caught.output)
            self.assertIn("播报正文", logged)
            self.assertNotIn(TOKEN, logged)
            self.assertEqual(seen["token"], TOKEN)
            self.assertNotIn(TOKEN, seen["url"])
            self.assertEqual(seen["body"], {"text": "播报正文", "symbol": "BTCUSDT", "id": 1})

    def test_tick_keeps_going_when_webhook_fails(self):
        auths = []

        def fake_open(request, timeout=0):
            auths.append(request.get_header("Authorization"))
            raise urllib.error.URLError(f"down {TOKEN}")

        with tempfile.TemporaryDirectory() as directory:
            book = BroadcastBook(Path(directory))
            book.update_settings({"webhookUrl": URL, "webhookToken": TOKEN})
            book.evaluate = lambda *args, **kwargs: [_item(1), _item(2)]

            class Monitor:
                def scan(self, limit, fresh=True):
                    return {"rows": [], "snapshotAt": 1, "requested": 1, "returned": 0}

            loop = BroadcastLoop(Monitor(), book)
            threads = []
            real_queue = queue_webhooks

            def spy(url, items, token=""):
                thread = real_queue(url, items, token)
                threads.append(thread)
                return thread

            with patch("server.MEMORY.record_scan", lambda *args, **kwargs: None), patch(
                "server.queue_webhooks", spy
            ), patch("urllib.request.urlopen", fake_open), self.assertLogs("oi-monitor", level="INFO") as caught:
                loop.tick()
                self.assertEqual(book.last_error, "")
                self.assertEqual(len(threads), 1)
                threads[0].join(2)
                self.assertFalse(threads[0].is_alive())
            logged = "\n".join(caught.output)
            self.assertEqual(auths, [f"Bearer {TOKEN}", f"Bearer {TOKEN}"])
            self.assertIn("webhook 发送失败", logged)
            self.assertNotIn(TOKEN, logged)


if __name__ == "__main__":
    unittest.main()
