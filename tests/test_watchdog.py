import io
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oi_monitor.client import BinanceClient, BinanceError, HTTP_ATTEMPTS, read_http
from server import LOOP, BroadcastLoop, Handler


class _BytesResponse:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.sent = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, _n=-1):
        if self.sent:
            return b""
        self.sent = True
        return self.payload


class HttpDeadlineTest(unittest.TestCase):
    def test_hung_connect_and_hung_read_both_give_up(self):
        def hang_connect(_request, timeout=0):
            time.sleep(30)
            return _BytesResponse(b"{}")

        class HungRead:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, _n=-1):
                time.sleep(30)
                return b""

        client = BinanceClient()
        for opener in (hang_connect, lambda _request, timeout=0: HungRead()):
            with self.subTest(opener=opener):
                started = time.monotonic()
                with patch("urllib.request.urlopen", opener):
                    with self.assertRaises(BinanceError) as caught:
                        client._request("https://example.test", "/fapi/v1/ping", None, 0.3, limited=False)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 2.0)
                self.assertIn("超过", str(caught.exception))

    def test_retries_stop_and_a_fast_body_still_parses(self):
        calls = {"n": 0}

        def boom(request, timeout=0):
            calls["n"] += 1
            self.assertGreater(timeout, 0)
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many", {}, io.BytesIO(b"busy"))

        client = BinanceClient()
        client.base = "https://example.test"
        client.candidates = ("https://example.test",)
        with patch("urllib.request.urlopen", boom), patch("oi_monitor.client.time.sleep"):
            with self.assertRaises(BinanceError):
                client.get_json("/fapi/v1/ping", timeout=1)
        self.assertEqual(calls["n"], HTTP_ATTEMPTS)

        def ok(_request, timeout=0):
            self.assertGreater(timeout, 0)
            return _BytesResponse(b'{"ok":true}')

        with patch("urllib.request.urlopen", ok):
            payload = read_http(urllib.request.Request("https://example.test/ping"), 1)
        self.assertEqual(json.loads(payload), {"ok": True})


class ScanLoopTest(unittest.TestCase):
    def test_tick_logs_traceback_and_the_loop_continues(self):
        class Boom:
            def scan(self, *_args, **_kwargs):
                raise RuntimeError("scan-broke")

        loop = BroadcastLoop(Boom(), LOOP.book, stale_sec=600, recover_wait=0)
        with patch("server.MEMORY.record_failure", lambda *_args, **_kwargs: None), self.assertLogs(
            "oi-monitor", level="ERROR"
        ) as caught:
            loop.tick()
        self.assertTrue(any(record.exc_info for record in caught.records))
        self.assertIn("scan-broke", "\n".join(caught.output))
        self.assertIsNone(loop.scan_status()["lastScanAt"])

        calls = {"n": 0}

        def tick():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("loop-broke")
            loop.stop.set()

        loop.tick = tick
        with patch("server.seconds_until_scan", return_value=0), self.assertLogs("oi-monitor", level="ERROR") as caught:
            loop._run()
        self.assertGreaterEqual(calls["n"], 2)
        self.assertTrue(any(record.exc_info and record.exc_info[1] and "loop-broke" in str(record.exc_info[1]) for record in caught.records))

    def test_success_resets_staleness(self):
        class Quiet:
            def scan(self, *_args, **_kwargs):
                return {"rows": [], "snapshotAt": 1, "requested": 0, "returned": 0}

        loop = BroadcastLoop(Quiet(), LOOP.book, stale_sec=600, recover_wait=0)
        with patch("server.MEMORY.record_scan", lambda *_args, **_kwargs: None), patch(
            "server.queue_webhooks", lambda *_args, **_kwargs: None
        ):
            loop.tick()
        status = loop.scan_status()
        self.assertIsNotNone(status["lastScanAt"])
        self.assertFalse(status["stale"])
        self.assertLess(status["scanAgeSec"], 5)

    def test_watchdog_restarts_a_dead_thread_and_exits_when_it_is_stuck(self):
        loop = BroadcastLoop(object(), LOOP.book, stale_sec=600, recover_wait=0)
        loop.started_monotonic = time.monotonic() - 50
        self.assertFalse(loop.scan_status()["stale"])

        loop.started_monotonic = time.monotonic() - 1000
        loop.last_success_monotonic = time.monotonic() - 1000
        loop.last_success_wall_ms = 1
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        loop.worker = dead
        started = []
        loop.start = lambda: started.append("started")
        exits = []
        with self.assertLogs("oi-monitor", level="WARNING"):
            self.assertEqual(loop.check_watchdog(exit_process=exits.append), "restart")
        self.assertEqual(started, ["started"])
        self.assertEqual(exits, [])

        stuck = threading.Thread(target=lambda: time.sleep(2), daemon=True)
        stuck.start()
        loop.worker = stuck
        with self.assertLogs("oi-monitor", level="ERROR"):
            self.assertEqual(loop.check_watchdog(exit_process=exits.append), "exit")
        self.assertEqual(exits, [1])
        stuck.join(timeout=3)

    def test_health_reports_last_scan_age(self):
        LOOP.note_success()
        handler = Handler.__new__(Handler)
        handler.path = "/api/health"
        captured = {}

        def send_json(status, payload):
            captured["status"] = status
            captured["payload"] = payload

        handler.send_json = send_json
        handler.do_GET()
        self.assertEqual(captured["status"], 200)
        body = captured["payload"]
        self.assertIn("lastScanAt", body)
        self.assertIn("scanAgeSec", body)
        self.assertFalse(body["stale"])
        self.assertTrue(body["ok"])

        LOOP.last_success_monotonic = time.monotonic() - 1000
        stale = LOOP.scan_status()
        self.assertTrue(stale["stale"])
        self.assertGreaterEqual(stale["scanAgeSec"], 1000)


if __name__ == "__main__":
    unittest.main()
