"""Minimal Binance USD-M public client with a restricted-region fallback."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

log = logging.getLogger("oi-monitor")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


class BinanceError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class Restricted(BinanceError):
    pass


class RateLimited(BinanceError):
    pass


HTTP_ATTEMPTS = 4
MAX_HTTP_BYTES = 8_000_000
DEFAULT_HTTP_TIMEOUT = 20.0


def _positive_timeout(timeout) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        value = DEFAULT_HTTP_TIMEOUT
    if value <= 0:
        return DEFAULT_HTTP_TIMEOUT
    return min(value, 120.0)


def _read_limited(response, limit: int) -> bytes:
    chunks = []
    size = 0
    while size <= limit:
        part = response.read(min(65536, limit - size + 1))
        if not part:
            break
        size += len(part)
        if size > limit:
            raise BinanceError("币安响应过大")
        chunks.append(part)
    return b"".join(chunks)


def _run_bounded(fn, timeout: float):
    """Run fn on a daemon thread so DNS, connect, and read all share one deadline."""
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - handed back to the caller
            box["error"] = exc

    thread = threading.Thread(target=run, name="binance-http", daemon=True)
    thread.start()
    thread.join(timeout)
    if "error" in box:
        raise box["error"]
    if "value" in box:
        return box["value"]
    raise BinanceError(f"请求超过 {timeout:g} 秒")


def read_http(request: urllib.request.Request, timeout: float, limit: int = MAX_HTTP_BYTES) -> bytes:
    """GET/POST with a socket timeout and a hard deadline around the whole call."""
    timeout = _positive_timeout(timeout)

    def attempt() -> bytes:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _read_limited(response, limit)
        except urllib.error.HTTPError as exc:
            try:
                exc.body_bytes = _read_limited(exc, limit)
            except Exception:  # noqa: BLE001 - the status still has to surface
                exc.body_bytes = b""
            raise

    return _run_bounded(attempt, timeout)


class Limiter:
    """Local guard for the open-interest history route (1000 requests / 5 min)."""

    def __init__(self, max_calls: int = 800, per_seconds: float = 300):
        self.max_calls = max_calls
        self.per_seconds = per_seconds
        self.times: deque[float] = deque()
        self.cv = threading.Condition()

    def acquire(self) -> None:
        with self.cv:
            while True:
                now = time.monotonic()
                while self.times and now - self.times[0] >= self.per_seconds:
                    self.times.popleft()
                if len(self.times) < self.max_calls:
                    self.times.append(now)
                    return
                wait = self.per_seconds - (now - self.times[0])
                self.cv.wait(timeout=max(0.2, wait))


class BinanceClient:
    def __init__(self):
        configured = os.environ.get("BINANCE_FAPI_BASE", "").strip().rstrip("/")
        candidates = []
        for base in (configured, "https://fapi.binance.com", "https://www.binance.com"):
            if base and base not in candidates:
                candidates.append(base)
        self.candidates = tuple(candidates)
        self.base: str | None = None
        self.limiter = Limiter()
        self._lock = threading.Lock()
        self._exchange = None
        self._exchange_at = 0.0

    def ensure(self) -> str:
        with self._lock:
            if self.base:
                return self.base
        last = None
        for base in self.candidates:
            try:
                self._request(base, "/fapi/v1/ping", None, 8, limited=False)
            except Exception as exc:  # noqa: BLE001 - probe every public host
                last = exc
                continue
            with self._lock:
                self.base = base
            log.info("binance base %s", base)
            return base
        raise BinanceError(_explain(last))

    def exchange_info(self) -> dict:
        now = time.monotonic()
        with self._lock:
            if self._exchange is not None and now - self._exchange_at < 1800:
                return self._exchange
        data = self.get_json("/fapi/v1/exchangeInfo", timeout=30)
        with self._lock:
            self._exchange = data
            self._exchange_at = time.monotonic()
        return data

    def get_json(self, path: str, params: dict | None = None, timeout: float = DEFAULT_HTTP_TIMEOUT):
        self.ensure()
        timeout = _positive_timeout(timeout)
        delay = 0.5
        last = None
        for attempt in range(HTTP_ATTEMPTS):
            saw_rate_limit = False
            for base in self._bases():
                try:
                    data = self._request(base, path, params, timeout, limited=True)
                except Restricted as exc:
                    last = exc
                    continue
                except RateLimited as exc:
                    last = exc
                    saw_rate_limit = True
                    break
                except BinanceError as exc:
                    last = exc
                    if exc.status in (400, 404):
                        raise
                    continue
                with self._lock:
                    self.base = base
                return data
            if saw_rate_limit and attempt + 1 < HTTP_ATTEMPTS:
                time.sleep(delay)
                delay = min(delay * 2, 4)
                continue
            break
        raise BinanceError(_explain(last))

    def _bases(self) -> list[str]:
        with self._lock:
            current = self.base
        if not current:
            return list(self.candidates)
        return [current, *[base for base in self.candidates if base != current]]

    def _request(self, base: str, path: str, params: dict | None, timeout: float, limited: bool):
        if limited and path.startswith("/futures/data/"):
            self.limiter.acquire()
        query = urllib.parse.urlencode(params) if params else ""
        url = base + path + (("?" + query) if query else "")
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            raw = read_http(request, timeout)
        except urllib.error.HTTPError as exc:
            raw_body = getattr(exc, "body_bytes", None)
            if not isinstance(raw_body, (bytes, bytearray)):
                raw_body = b""
            body = raw_body.decode("utf-8", "replace")
            if exc.code == 451 or "restricted location" in body:
                raise Restricted(body or "restricted location") from exc
            if exc.code in (429, 418):
                raise RateLimited(body or f"HTTP {exc.code}") from exc
            raise BinanceError(f"HTTP {exc.code} {path}: {body[:180]}", status=exc.code) from exc
        except BinanceError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BinanceError(f"网络错误：{exc}") from exc
        text = raw.lstrip()
        if text.startswith(b"<") or b"restricted location" in text:
            snippet = text[:180].decode("utf-8", "replace")
            if b"restricted location" in text or b"<html" in text.lower():
                raise Restricted(snippet)
            raise BinanceError(f"币安返回了非 JSON 内容：{snippet}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BinanceError("币安返回的 JSON 无法解析") from exc


def _explain(err: Exception | None) -> str:
    text = "" if err is None else str(err)
    if isinstance(err, Restricted) or "restricted location" in text:
        return (
            "币安合约接口在当前网络被地区限制。"
            "可以设置 https_proxy 后重试，或换一个能访问币安的网络。"
        )
    if isinstance(err, RateLimited):
        return "币安未平仓合约历史接口限流（约 1000 次 / 5 分钟），请稍后再扫描。"
    if not text:
        return "无法连接币安公开接口"
    return f"无法连接币安公开接口：{text[:240]}"
