#!/usr/bin/env python3
"""Local dashboard for Binance USDT-M open interest, notional value, and market cap."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from oi_monitor.broadcast import BroadcastBook, deliver_webhook
from oi_monitor.client import BinanceError
from oi_monitor.metrics import attach_reads, parse_limit, screen_change
from oi_monitor.service import Monitor
from oi_monitor.trading import TradingDesk

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
LOG = logging.getLogger("oi-monitor")

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}

MONITOR = Monitor()
DATA = ROOT / "data"
BOOK = BroadcastBook(DATA)
DESK = TradingDesk(DATA, MONITOR)
LOCALHOSTS = {"127.0.0.1", "::1"}


def sort_rows(rows: list[dict], key: str, descending: bool) -> list[dict]:
    def value(row: dict):
        if ":" in key:
            field, window = key.split(":", 1)
            item = row["changes"].get(window) or {}
            mapped = {"oi": "oiPct", "oiValue": "oiValuePct"}.get(field)
            if mapped is None:
                raise ValueError(f"未知排序字段 {key}")
            return item.get(mapped)
        return row.get(key)

    def ordering(row: dict):
        raw = value(row)
        if raw is None:
            return (1, 0)
        if isinstance(raw, str):
            return (0, raw.lower())
        return (0, -raw if descending else raw)

    return sorted(rows, key=ordering)


def display_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if ord(char) > 0x2E80 else 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    gap = width - display_width(text)
    if gap <= 0:
        return text
    return (" " * gap + text) if align == "right" else (text + " " * gap)


def compact_cn(value) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    if absolute >= 1e12:
        return f"{sign}{absolute / 1e12:.2f}万亿"
    if absolute >= 1e8:
        return f"{sign}{absolute / 1e8:.2f}亿"
    if absolute >= 1e4:
        return f"{sign}{absolute / 1e4:.2f}万"
    return f"{sign}{absolute:.2f}"


def pct_text(value) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def print_table(payload: dict, sort_key: str) -> None:
    rows = sort_rows(payload["rows"], sort_key, True)
    source = payload.get("source") or ""
    print(
        f"数据源 {source}  |  全市场 {payload['universe']}  |  "
        f"本次 {payload['returned']}/{payload['requested']}  |  "
        f"{payload['elapsedMs']} ms  |  排序 {sort_key}"
    )
    if payload.get("snapshotAt"):
        print("未平仓合约按 5 分钟统计，涨跌幅相对该快照计算。")
    headers = [
        ("币种", 14, "left"),
        ("价格", 12, "right"),
        ("价24h", 9, "right"),
        ("未平仓金额", 12, "right"),
        ("价值1h", 9, "right"),
        ("价值4h", 9, "right"),
        ("价值24h", 9, "right"),
        ("数量1h", 9, "right"),
        ("数量24h", 9, "right"),
        ("事实1h", 28, "left"),
        ("市值", 12, "right"),
        ("OI/市值", 9, "right"),
    ]
    print(" ".join(pad(name, width, align) for name, width, align in headers))
    shown = rows[:50]
    for row in shown:
        changes = row["changes"]
        cells = [
            row["base"],
            f"{row['price']:.6g}",
            pct_text(row["priceChangePercent"]),
            compact_cn(row["oiValue"]),
            pct_text(changes["1h"]["oiValuePct"]),
            pct_text(changes["4h"]["oiValuePct"]),
            pct_text(changes["24h"]["oiValuePct"]),
            pct_text(changes["1h"]["oiPct"]),
            pct_text(changes["24h"]["oiPct"]),
            screen_change(row["oiValue"], changes["1h"]["oiValuePct"], changes["1h"]["pricePct"], changes["1h"].get("oiValueDelta"), BOOK.settings.get("priceChopPct") or 0.5)["text"],
            compact_cn(row["marketCap"]),
            pct_text(row["oiToMarketCapPct"]),
        ]
        print(" ".join(pad(text, width, align) for text, (_n, width, align) in zip(cells, headers)))
    if len(rows) > len(shown):
        print(f"… 另有 {len(rows) - len(shown)} 个合约，网页里可以看全部。")
    if payload["errorCount"]:
        failed = ", ".join(item["symbol"] for item in payload["errors"][:8])
        print(f"有 {payload['errorCount']} 个合约拉取失败：{failed}")


class BroadcastLoop:
    def __init__(self, monitor: Monitor, book: BroadcastBook):
        self.monitor = monitor
        self.book = book
        self.stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="broadcast", daemon=True).start()

    def _run(self) -> None:
        while not self.stop.is_set():
            started = time.time()
            self.tick()
            interval = max(30, int(self.book.settings.get("intervalSec") or 60))
            self.stop.wait(max(1, interval - (time.time() - started)))

    def tick(self) -> None:
        limit = self.book.settings.get("limit")
        if limit is not None:
            limit = int(limit)
        try:
            payload = self.monitor.scan(limit)
        except Exception as exc:  # noqa: BLE001 - keep the next round alive
            self.book.mark_scan(str(exc)[:240])
            LOG.warning("播报扫描失败：%s", exc)
            return
        items = self.book.evaluate(payload.get("rows") or [], payload.get("snapshotAt"))
        self.book.mark_scan("")
        url = self.book.webhook_target()
        for item in items:
            LOG.info("播报 %s", item["text"])
            if deliver_webhook(url, item):
                LOG.warning("webhook 发送失败")


LOOP = BroadcastLoop(MONITOR, BOOK)


class Handler(BaseHTTPRequestHandler):
    server_version = "oi-monitor/1.0"

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/scan":
                self.handle_scan(parsed)
            elif parsed.path == "/api/broadcasts":
                self.send_json(200, BOOK.snapshot())
            elif parsed.path == "/api/trade/status":
                self.trade_status()
            elif parsed.path == "/api/health":
                self.send_json(200, {"ok": True, "broadcast": True, "autoTrade": False})
            elif parsed.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self.handle_static(parsed.path)
        except BrokenPipeError:
            return

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/rules":
                self.send_json(200, {"rules": BOOK.replace_rules(self.read_json().get("rules"))})
            elif parsed.path == "/api/settings":
                self.send_json(200, {"settings": BOOK.update_settings(self.read_json())})
            elif parsed.path == "/api/trade/preview":
                self.guard_local()
                self.send_json(200, DESK.preview(self.read_json()))
            elif parsed.path == "/api/trade/order":
                self.guard_local()
                self.require_confirm_header()
                self.send_json(200, DESK.submit(self.read_json()))
            elif parsed.path == "/api/trade/keys":
                self.guard_local()
                body = self.read_json()
                self.send_json(200, DESK.save_keys(str(body.get("apiKey") or ""), str(body.get("apiSecret") or "")))
            elif parsed.path == "/api/trade/test":
                self.guard_local()
                self.send_json(200, DESK.test_connection())
            else:
                self.send_json(404, {"error": "没有这个接口"})
        except BrokenPipeError:
            return
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except PermissionError as exc:
            self.send_json(403, {"error": str(exc)})
        except BinanceError as exc:
            self.send_json(403, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("请求失败 %s", parsed.path)
            self.send_json(500, {"error": "请求失败"})

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler name
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/trade/keys":
                self.guard_local()
                self.send_json(200, DESK.clear_keys())
            else:
                self.send_json(404, {"error": "没有这个接口"})
        except PermissionError as exc:
            self.send_json(403, {"error": str(exc)})
        except BrokenPipeError:
            return

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("请求体无效") from exc
        if length < 0 or length > 20_000:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 无法解析") from exc
        if not isinstance(payload, dict):
            raise ValueError("需要 JSON 对象")
        return payload

    def guard_local(self) -> None:
        if self.client_address[0] not in LOCALHOSTS:
            raise PermissionError("下单和密钥接口只接受本机访问")

    def require_confirm_header(self) -> None:
        if self.headers.get("X-OI-Confirm") != "1":
            raise ValueError("缺少确认头")

    def trade_status(self) -> None:
        if self.client_address[0] not in LOCALHOSTS:
            self.send_json(200, {"keysConfigured": False, "tradingEnabled": False, "hidden": True, "autoTrade": False})
            return
        try:
            self.send_json(200, DESK.status())
        except BinanceError as exc:
            self.send_json(200, {"keysConfigured": False, "tradingEnabled": False, "error": str(exc), "autoTrade": False})

    def handle_scan(self, parsed) -> None:
        query = urllib.parse.parse_qs(parsed.query)
        raw_limit = query.get("limit", ["60"])[0]
        fresh = query.get("fresh", ["0"])[0] in {"1", "true", "yes"}
        try:
            limit = parse_limit(raw_limit)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        try:
            payload = MONITOR.scan(limit, fresh=fresh)
        except BinanceError as exc:
            self.send_json(502, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - surface a useful API error
            LOG.exception("scan failed")
            self.send_json(502, {"error": f"扫描失败：{exc}"})
            return
        chop = float(BOOK.settings.get("priceChopPct") or 0.5)
        annotated = dict(payload)
        try:
            annotated["rows"] = attach_reads(payload.get("rows") or [], chop)
        except Exception:  # noqa: BLE001 - still return the scan if a label fails
            LOG.exception("事实判断失败")
            annotated["rows"] = payload.get("rows") or []
        annotated["priceChopPct"] = chop
        self.send_json(200, annotated)

    def handle_static(self, url_path: str) -> None:
        relative = "index.html" if url_path in {"", "/"} else url_path.lstrip("/")
        candidate = (STATIC / relative).resolve()
        try:
            candidate.relative_to(STATIC)
        except ValueError:
            self.send_error(404)
            return
        if not candidate.is_file():
            self.send_error(404)
            return
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(candidate.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(host: str, port: int) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    LOOP.start()
    shown = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print(f"oi事实监测播报已启动： http://{shown}:{port}")
    print("按未平仓金额涨跌和价格判断。不会自动下单。按 Ctrl+C 结束。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        LOOP.stop.set()
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="oi事实监测播报：用币安 U 本位未平仓金额涨跌和价格判断")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--once", action="store_true", help="扫描一次并打印，不启动网页")
    parser.add_argument("--json", action="store_true", help="和 --once 一起用，输出 JSON")
    parser.add_argument("--limit", default="60", help="按 24h 成交额取前 N 个，或 all")
    parser.add_argument("--sort", default="oiValue:1h", help="例如 oiValue:1h、oi:24h、oiValue、marketCap")
    parser.add_argument("--fresh", action="store_true", help="忽略缓存重新扫描")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        limit = parse_limit(args.limit)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.once:
        try:
            payload = MONITOR.scan(limit, fresh=args.fresh)
        except BinanceError as exc:
            print(exc, file=sys.stderr)
            return 1
        items = BOOK.evaluate(payload.get("rows") or [], payload.get("snapshotAt"))
        if args.json:
            payload = dict(payload)
            payload["broadcasts"] = items
            json.dump(payload, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
        else:
            print_table(payload, args.sort)
            if items:
                print("\n播报")
                for item in items:
                    print(item["text"])
            else:
                print("\n这一轮没有越过播报阈值的合约。")
        return 0
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
