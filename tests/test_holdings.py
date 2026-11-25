import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oi_monitor.broadcast import BroadcastBook, default_settings
from oi_monitor.holdings import HoldingsBook, main, normalize_symbol
from oi_monitor.service import Monitor
from server import Handler


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


class HoldingsStoreTest(unittest.TestCase):
    def test_normalize_base_or_full_symbol(self):
        self.assertEqual(normalize_symbol("MANTRA"), "MANTRAUSDT")
        self.assertEqual(normalize_symbol("mantra"), "MANTRAUSDT")
        self.assertEqual(normalize_symbol("  MANTRAUSDT  "), "MANTRAUSDT")
        self.assertEqual(normalize_symbol("ethusdt"), "ETHUSDT")
        self.assertEqual(normalize_symbol("1000PEPE"), "1000PEPEUSDT")
        for raw in ("", "   ", "USDT", "MAN TRA", None, 12, True):
            with self.assertRaises(ValueError):
                normalize_symbol(raw)

    def test_file_starts_with_mantra_and_survives_a_new_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            book = HoldingsBook(path)
            self.assertEqual(book.list(), ["MANTRAUSDT"])
            stored = json.loads((path / "holdings.json").read_text(encoding="utf-8"))
            self.assertEqual(stored, ["MANTRAUSDT"])
            self.assertNotIn("holdings", default_settings())

            added = book.add("eth")
            self.assertEqual(added["symbol"], "ETHUSDT")
            self.assertEqual(added["holdings"], ["MANTRAUSDT", "ETHUSDT"])
            self.assertEqual(book.add("ETHUSDT")["holdings"], ["MANTRAUSDT", "ETHUSDT"])
            with self.assertRaises(ValueError):
                book.add("NOPE", known={"MANTRAUSDT", "ETHUSDT"})
            self.assertEqual(book.list(), ["MANTRAUSDT", "ETHUSDT"])
            accepted = book.add("NOPE", known=None)
            self.assertEqual(accepted["symbol"], "NOPEUSDT")

            other = HoldingsBook(path)
            self.assertEqual(other.list(), ["MANTRAUSDT", "ETHUSDT", "NOPEUSDT"])
            removed = other.remove("eth")
            self.assertTrue(removed["removed"])
            self.assertEqual(removed["symbol"], "ETHUSDT")
            self.assertNotIn("ETHUSDT", removed["holdings"])
            again = other.remove("ETHUSDT")
            self.assertFalse(again["removed"])
            self.assertEqual(HoldingsBook(path).list(), ["MANTRAUSDT", "NOPEUSDT"])
            self.assertFalse((path / "settings.json").exists())

            (path / "holdings.json").write_text("not-json", encoding="utf-8")
            self.assertEqual(HoldingsBook(path).list(), ["MANTRAUSDT"])
            self.assertEqual(json.loads((path / "holdings.json").read_text(encoding="utf-8")), ["MANTRAUSDT"])

    def test_cli_add_remove_list_and_concurrent_writers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["list"], directory=path), 0)
            self.assertEqual(json.loads(out.getvalue()), ["MANTRAUSDT"])

            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["add", "btc"], directory=path, known={"BTCUSDT", "MANTRAUSDT"}), 0)
            self.assertEqual(json.loads(out.getvalue()), ["MANTRAUSDT", "BTCUSDT"])

            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(main(["add", "nope"], directory=path, known={"BTCUSDT"}), 1)
            self.assertIn("NOPEUSDT", err.getvalue())
            self.assertEqual(HoldingsBook(path).list(), ["MANTRAUSDT", "BTCUSDT"])

            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["remove", "BTC"], directory=path), 0)
            self.assertEqual(json.loads(out.getvalue()), ["MANTRAUSDT"])
            self.assertEqual(main(["list", "BTC"], directory=path), 2)
            self.assertEqual(main(["add"], directory=path), 2)

            errors = []

            def add(symbol):
                try:
                    HoldingsBook(path).add(symbol)
                except Exception as exc:  # noqa: BLE001 - the assertion reports it
                    errors.append(exc)

            threads = [threading.Thread(target=add, args=(symbol,)) for symbol in ("AAAUSDT", "BBBUSDT")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            self.assertEqual(errors, [])
            self.assertEqual(HoldingsBook(path).list(), ["MANTRAUSDT", "AAAUSDT", "BBBUSDT"])

    def test_module_list_command(self):
        root = Path(__file__).resolve().parents[1]
        target = root / "data" / "holdings.json"
        existed = target.is_file()
        before = target.read_bytes() if existed else None
        try:
            done = subprocess.run(
                [sys.executable, "-m", "oi_monitor.holdings", "list"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("MANTRAUSDT", done.stdout)
        finally:
            if existed:
                target.write_bytes(before)
            elif target.exists():
                target.unlink()


class HoldingsApiTest(unittest.TestCase):
    def test_endpoints_add_remove_and_leave_settings_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            book = BroadcastBook(path)
            known = {"MANTRAUSDT", "ETHUSDT"}
            body = json.dumps({"symbol": "eth"}).encode("utf-8")
            with patch("server.BOOK", book), patch.object(server_monitor(), "known_symbols", return_value=known):
                post, posted = _handler("POST", "/api/holdings", body)
                post.do_POST()
            self.assertEqual(posted["status"], 200)
            self.assertEqual(posted["payload"]["symbol"], "ETHUSDT")
            self.assertEqual(posted["payload"]["holdings"], ["MANTRAUSDT", "ETHUSDT"])

            with patch("server.BOOK", book), patch.object(server_monitor(), "known_symbols", return_value=known):
                rejected, rejection = _handler("POST", "/api/holdings", json.dumps({"symbol": "NOPE"}).encode())
                rejected.do_POST()
            self.assertEqual(rejection["status"], 400)
            self.assertIn("NOPEUSDT", rejection["payload"]["error"])

            with patch("server.BOOK", book), patch.object(server_monitor(), "known_symbols", return_value=None):
                early, early_body = _handler("POST", "/api/holdings", json.dumps({"symbol": "SOL"}).encode())
                early.do_POST()
            self.assertEqual(early_body["status"], 200)
            self.assertEqual(early_body["payload"]["symbol"], "SOLUSDT")

            with patch("server.BOOK", book):
                delete, deleted = _handler("DELETE", "/api/holdings/eth")
                delete.do_DELETE()
                listing, listed = _handler("GET", "/api/holdings")
                listing.do_GET()
            self.assertEqual(deleted["status"], 200)
            self.assertEqual(deleted["payload"]["symbol"], "ETHUSDT")
            self.assertTrue(deleted["payload"]["removed"])
            self.assertEqual(listed["status"], 200)
            self.assertEqual(listed["payload"]["holdings"], ["MANTRAUSDT", "SOLUSDT"])
            self.assertEqual(json.loads((path / "holdings.json").read_text(encoding="utf-8")), ["MANTRAUSDT", "SOLUSDT"])
            self.assertFalse((path / "settings.json").exists())
            self.assertFalse((path / "rules.json").exists())

            with patch("server.BOOK", book):
                missing, missed = _handler("DELETE", "/api/holdings/")
                missing.do_DELETE()
            self.assertEqual(missed["status"], 400)


def server_monitor():
    import server

    return server.MONITOR


if __name__ == "__main__":
    unittest.main()
