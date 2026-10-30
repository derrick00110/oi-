"""Scan USD-M perpetual contracts and cache the open-interest snapshot."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from oi_monitor.client import BinanceClient, BinanceError
from oi_monitor.metrics import build_row, index_premium, index_tickers, normalize, parse_klines, usdt_perpetuals
from oi_monitor.trigger import KLINE_LIMIT

log = logging.getLogger("oi-monitor")

HIST_LIMIT = 300
HIST_PERIOD = "5m"


class Monitor:
    def __init__(self, client: BinanceClient | None = None):
        self.client = client or BinanceClient()
        self._lock = threading.Lock()
        self._cache: dict[int, tuple[float, dict, float]] = {}
        self._inflight: dict[int, threading.Event] = {}
        self._fail: dict[int, Exception] = {}

    def scan(self, limit: int | None, fresh: bool = False, cycle: dict | None = None) -> dict:
        limit_key = 0 if limit is None else limit
        key = (limit_key, _cycle_token(cycle))
        with self._lock:
            if not fresh:
                cached = self._fresh_hit(key)
                if cached is not None:
                    return cached
            event = self._inflight.get(key)
            if event is None:
                event = threading.Event()
                self._inflight[key] = event
                self._fail.pop(key, None)
                owner = True
            else:
                owner = False
        if not owner:
            if not event.wait(180):
                raise BinanceError("扫描超时，请重试")
            with self._lock:
                cached = self._fresh_hit(key)
                failure = self._fail.get(key)
            if cached is not None:
                return cached
            if failure is not None:
                raise BinanceError(str(failure))
            raise BinanceError("扫描失败，请重试")
        try:
            payload = self._scan_uncached(limit, cycle)
            if not payload["rows"]:
                ttl = 5
            elif limit_key == 0 or limit_key >= 300:
                # Longer than the 5-minute broadcast pull, so the page does not start a second scan.
                ttl = 420
            elif key > 200:
                ttl = 180
            else:
                ttl = 45
            stored = (time.monotonic(), payload, ttl)
            with self._lock:
                self._cache[key] = stored
            return self._decorate(stored, cached=False)
        except Exception as exc:
            with self._lock:
                self._fail[key] = exc
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            event.set()

    def _fresh_hit(self, key: int):
        hit = self._cache.get(key)
        if hit is None:
            return None
        stored_at, _payload, ttl = hit
        if time.monotonic() - stored_at >= ttl:
            return None
        return self._decorate(hit, cached=True)

    def _decorate(self, stored: tuple[float, dict, float], cached: bool) -> dict:
        stored_at, payload, _ttl = stored
        out = dict(payload)
        out["cached"] = cached
        out["cacheAgeMs"] = 0 if not cached else int((time.monotonic() - stored_at) * 1000)
        return out

    def _scan_uncached(self, limit: int | None, cycle: dict | None = None) -> dict:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=3) as pool:
            info_future = pool.submit(self.client.exchange_info)
            ticker_future = pool.submit(self.client.get_json, "/fapi/v1/ticker/24hr", None, 30)
            premium_future = pool.submit(self.client.get_json, "/fapi/v1/premiumIndex", None, 30)
            exchange_info = info_future.result()
            tickers = ticker_future.result()
            try:
                premium = index_premium(premium_future.result())
            except Exception as exc:  # noqa: BLE001 - funding is extra, the OI scan still stands
                log.warning("资金费率没取到：%s", exc)
                premium = {}
        bases = usdt_perpetuals(exchange_info)
        prices = index_tickers(tickers)
        universe = []
        for symbol, base in bases.items():
            ticker = prices.get(symbol)
            if not ticker:
                continue
            try:
                volume = float(ticker.get("quoteVolume") or 0)
                float(ticker["lastPrice"])
                float(ticker["priceChangePercent"])
            except (KeyError, TypeError, ValueError):
                continue
            universe.append((volume, symbol, base, ticker))
        universe.sort(key=lambda item: item[0], reverse=True)
        if not universe:
            raise BinanceError("没有找到交易中的 USDT 永续合约")
        chosen = universe if limit is None else universe[:limit]
        workers = 8 if len(chosen) > 200 else 10
        rows = []
        errors = []
        kline_errors = []
        trigger_mode = (cycle or {}).get("mode") == "trigger"
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(self._one, symbol, base, rank, ticker, premium.get(symbol), cycle if trigger_mode else None)
                for rank, (_volume, symbol, base, ticker) in enumerate(chosen, start=1)
            ]
            for future in as_completed(futures):
                symbol, row, error, kline_error = future.result()
                if row is not None:
                    rows.append(row)
                elif error:
                    errors.append({"symbol": symbol, "message": error[:180]})
                if kline_error:
                    kline_errors.append(symbol)
        rows.sort(key=lambda row: row["volumeRank"])
        snapshot_at = max((row["snapshotAt"] for row in rows), default=None)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "scanned %s/%s symbols in %sms via %s errors=%s klineErrors=%s mode=%s",
            len(rows),
            len(chosen),
            elapsed_ms,
            self.client.base,
            len(errors),
            len(kline_errors),
            "trigger" if trigger_mode else "clock",
        )
        return {
            "updatedAt": int(time.time() * 1000),
            "snapshotAt": snapshot_at,
            "source": self.client.base,
            "bar": HIST_PERIOD,
            "universe": len(universe),
            "requested": len(chosen),
            "returned": len(rows),
            "elapsedMs": elapsed_ms,
            "errorCount": len(errors),
            "errors": errors[:12],
            "rows": rows,
        }

    def find_row(self, symbol: str) -> dict | None:
        with self._lock:
            snapshots = list(self._cache.values())
        best = None
        for stored_at, payload, _ttl in snapshots:
            for row in payload.get("rows") or []:
                if row.get("symbol") == symbol and (best is None or stored_at > best[0]):
                    best = (stored_at, row)
        return None if best is None else best[1]

    def _one(self, symbol: str, base: str, rank: int, ticker: dict, premium: dict | None, cycle: dict | None = None):
        try:
            raw = self.client.get_json(
                "/futures/data/openInterestHist",
                {"symbol": symbol, "period": HIST_PERIOD, "limit": HIST_LIMIT},
                timeout=15,
            )
            if isinstance(raw, dict):
                message = str(raw.get("msg") or raw.get("code") or "未平仓合约历史返回异常")
                return symbol, None, message, None
            klines = None
            kline_error = None
            if cycle and cycle.get("mode") == "trigger":
                klines, kline_error = self._klines(symbol)
            row = build_row(symbol, base, rank, ticker, normalize(raw), premium, klines=klines, cycle_params=cycle)
            if row is None:
                return symbol, None, "没有未平仓合约历史", kline_error
            return symbol, row, None, kline_error
        except Exception as exc:  # noqa: BLE001 - one symbol must not fail the scan
            return symbol, None, str(exc), None

    def _klines(self, symbol: str) -> tuple[list[dict], str | None]:
        try:
            raw = self.client.get_json(
                "/fapi/v1/klines",
                {"symbol": symbol, "interval": HIST_PERIOD, "limit": KLINE_LIMIT},
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001 - price trigger is optional for this scan
            return [], str(exc)[:180]
        if not isinstance(raw, list):
            return [], "k线返回异常"
        try:
            return parse_klines(raw), None
        except ValueError as exc:
            return [], str(exc)[:180]


def _cycle_token(cycle: dict | None):
    if not cycle or cycle.get("mode") != "trigger":
        return "clock"
    return (
        "trigger",
        cycle.get("pricePct"),
        cycle.get("volumeMult"),
        cycle.get("oiPct"),
        cycle.get("oiPctLarge"),
        cycle.get("oiLargeUsd"),
        cycle.get("minQuoteVolume"),
        cycle.get("rearmMs"),
    )
