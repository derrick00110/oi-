"""Holdings list for the closer open-interest watch.

The list lives in ``data/holdings.json``. Older files are a JSON array of
USDT perpetual symbols. The current file is an object: each holding has a
reference price, and ``closed_watch`` remembers coins removed from the list.
The scan reads it again every round, and the CLI edits that same file, so an
add or a remove applies on the next scan while the server is up.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_HOLDINGS = ["MANTRAUSDT"]
FILE_NAME = "holdings.json"
# A holding is through this fraction of its reference price when the stop alert fires.
STOP_LOSS_FRACTION = 0.125
CLOSED_GREEN_LIMIT = 2


def default_data_dir() -> Path:
    """Same directory ``server.py`` uses for rules, settings, and broadcasts."""
    return Path(__file__).resolve().parents[1] / "data"


def normalize_symbol(raw) -> str:
    """Accept a bare base (MANTRA, 牛来) or a full perpetual (MANTRAUSDT)."""
    if not isinstance(raw, str):
        raise ValueError("币种无效")
    text = raw.strip().upper()
    if not text:
        raise ValueError("币种无效")
    if not text.endswith("USDT"):
        text += "USDT"
    if text == "USDT" or len(text) > 32 or not text.isalnum():
        raise ValueError("币种无效")
    return text


def clean_entry_price(raw) -> float:
    """A positive reference price. Booleans are rejected; ``float(True)`` is 1."""
    if isinstance(raw, bool) or isinstance(raw, (dict, list, bytes)):
        raise ValueError("参考价无效")
    try:
        price = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("参考价无效") from exc
    if not math.isfinite(price) or price <= 0:
        raise ValueError("参考价无效")
    return round(price, 8)


def _entry(symbol: str, entry_price=None, stop_armed: bool = True) -> dict:
    return {"symbol": symbol, "entry_price": entry_price, "stop_armed": bool(stop_armed)}


def _default_state() -> dict:
    return {
        "holdings": [_entry(symbol) for symbol in DEFAULT_HOLDINGS],
        "closed_watch": [],
    }


def _clean_entry(raw) -> dict | None:
    if isinstance(raw, str):
        try:
            return _entry(normalize_symbol(raw))
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        symbol = normalize_symbol(str(raw.get("symbol") or ""))
    except ValueError:
        return None
    price_raw = raw.get("entry_price", raw.get("entryPrice"))
    price = None
    if price_raw not in (None, ""):
        try:
            price = clean_entry_price(price_raw)
        except ValueError:
            price = None
    if "stop_armed" in raw:
        armed = raw.get("stop_armed") is not False
    elif "stopArmed" in raw:
        armed = raw.get("stopArmed") is not False
    else:
        armed = True
    return _entry(symbol, price, armed)


def _clean_holdings(raw) -> list[dict]:
    if not isinstance(raw, list):
        return [_entry(symbol) for symbol in DEFAULT_HOLDINGS]
    cleaned = []
    seen = set()
    for item in raw:
        entry = _clean_entry(item)
        if entry is None or entry["symbol"] in seen:
            continue
        seen.add(entry["symbol"])
        cleaned.append(entry)
    return cleaned


def _clean_watch_item(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        symbol = normalize_symbol(str(raw.get("symbol") or ""))
    except ValueError:
        return None
    try:
        closed_at = int(raw.get("closed_at", raw.get("closedAt")) or 0)
    except (TypeError, ValueError):
        closed_at = 0
    if closed_at < 0:
        closed_at = 0
    try:
        green_count = int(raw.get("green_count", raw.get("greenCount")) or 0)
    except (TypeError, ValueError):
        green_count = 0
    if green_count < 0:
        green_count = 0
    return {"symbol": symbol, "closed_at": closed_at, "green_count": green_count}


def _clean_watch(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    cleaned = []
    seen = set()
    for item in raw:
        watch = _clean_watch_item(item)
        if watch is None or watch["symbol"] in seen:
            continue
        seen.add(watch["symbol"])
        cleaned.append(watch)
    return cleaned


def _detach_watched_holdings(state: dict) -> bool:
    """A coin cannot be held and watched at the same time. Holding wins."""
    held = {item["symbol"] for item in state["holdings"]}
    watch = [item for item in state["closed_watch"] if item["symbol"] not in held]
    if len(watch) == len(state["closed_watch"]):
        return False
    state["closed_watch"] = watch
    return True


def _public_holdings(state: dict) -> list[dict]:
    return [{"symbol": item["symbol"], "entry_price": item["entry_price"]} for item in state["holdings"]]


def _public_watch(state: dict) -> list[dict]:
    return [
        {"symbol": item["symbol"], "closed_at": item["closed_at"], "green_count": item["green_count"]}
        for item in state["closed_watch"]
    ]


def _symbols(state: dict) -> list[str]:
    return [item["symbol"] for item in state["holdings"]]


class _FileLock:
    """Exclusive lock shared by the server and the CLI."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.handle is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            if self.handle is not None:
                self.handle.close()
        return False


class HoldingsBook:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / FILE_NAME
        self._lock_path = self.directory / "holdings.lock"

    def list(self) -> list[str]:
        with _FileLock(self._lock_path):
            return _symbols(self._load())

    def public(self) -> dict:
        """GET shape: holdings carry the reference price, closed_watch is read-only."""
        with _FileLock(self._lock_path):
            state = self._load()
            return {"holdings": _public_holdings(state), "closed_watch": _public_watch(state)}

    def add(self, raw, known: set[str] | None = None, entry_price=None, price_lookup=None) -> dict:
        """Append one symbol. ``known`` is the live USDT-perpetual set, when the process has it.

        ``entry_price`` overrides the reference. Otherwise ``price_lookup(symbol)`` may
        supply the price at this moment (a public ticker read). A missing price is
        left empty and filled on the next scan. Adding a coin drops it from the
        closed-watch list. Passing a new reference for a coin already held re-arms
        the stop alert.
        """
        symbol = normalize_symbol(raw)
        if known is not None and symbol not in known:
            raise ValueError(f"{symbol} 不是交易中的 USDT 永续")
        explicit = entry_price is not None
        if explicit:
            entry_price = clean_entry_price(entry_price)
        elif price_lookup is not None:
            # Outside the file lock: a public ticker read must not stall a scan.
            entry_price = _lookup_price(price_lookup, symbol)
        with _FileLock(self._lock_path):
            state = self._load()
            watch_before = len(state["closed_watch"])
            state["closed_watch"] = [item for item in state["closed_watch"] if item["symbol"] != symbol]
            existing = next((item for item in state["holdings"] if item["symbol"] == symbol), None)
            changed = len(state["closed_watch"]) != watch_before
            if existing is None:
                state["holdings"].append(_entry(symbol, entry_price, True))
                changed = True
            elif explicit:
                existing["entry_price"] = entry_price
                existing["stop_armed"] = True
                changed = True
            if changed:
                self._write(state)
            stored = next(item for item in state["holdings"] if item["symbol"] == symbol)
            return {
                "symbol": symbol,
                "entry_price": stored["entry_price"],
                "holdings": _symbols(state),
                "closed_watch": _public_watch(state),
            }

    def remove(self, raw, now: float | None = None) -> dict:
        """Drop a holding and remember it on the closed-watch list."""
        symbol = normalize_symbol(raw)
        moment = time.time() if now is None else float(now)
        closed_at = int(moment * 1000)
        with _FileLock(self._lock_path):
            state = self._load()
            removed = any(item["symbol"] == symbol for item in state["holdings"])
            if removed:
                state["holdings"] = [item for item in state["holdings"] if item["symbol"] != symbol]
                state["closed_watch"] = [item for item in state["closed_watch"] if item["symbol"] != symbol]
                state["closed_watch"].append({"symbol": symbol, "closed_at": closed_at, "green_count": 0})
                self._write(state)
            return {
                "symbol": symbol,
                "removed": removed,
                "holdings": _symbols(state),
                "closed_watch": _public_watch(state),
            }

    def note_long_green(self, raw, at_ms: int) -> dict | None:
        """Count one rising open-interest and price broadcast after the close.

        The second one drops the coin from the watch list and returns that record.
        Greens from before the close are ignored. A coin that is not watched
        returns None.
        """
        try:
            symbol = normalize_symbol(raw)
        except ValueError:
            return None
        try:
            at = int(at_ms)
        except (TypeError, ValueError):
            return None
        with _FileLock(self._lock_path):
            state = self._load()
            watch = next((item for item in state["closed_watch"] if item["symbol"] == symbol), None)
            if watch is None or at < int(watch["closed_at"]):
                return None
            watch["green_count"] = int(watch["green_count"]) + 1
            fired = watch["green_count"] >= CLOSED_GREEN_LIMIT
            record = {
                "symbol": symbol,
                "closed_at": watch["closed_at"],
                "green_count": watch["green_count"],
            }
            if fired:
                state["closed_watch"] = [item for item in state["closed_watch"] if item["symbol"] != symbol]
            self._write(state)
            return record if fired else None

    def observe_prices(self, prices: dict | None) -> list[dict]:
        """Fill missing reference prices, then return stop-loss breaches.

        A breach is a current price at or below ``reference * (1 - 0.125)``.
        Each breach is returned once. The alert arms again only after the price
        is back above that line. A reference filled on this call is not compared
        with the same price, so the first sight of a coin does not alert.
        """
        normalized = _price_map(prices)
        if not normalized:
            with _FileLock(self._lock_path):
                self._load()
            return []
        events = []
        with _FileLock(self._lock_path):
            state = self._load()
            changed = False
            line_ratio = 1.0 - STOP_LOSS_FRACTION
            for entry in state["holdings"]:
                price = normalized.get(entry["symbol"])
                if price is None:
                    continue
                if entry["entry_price"] is None:
                    entry["entry_price"] = price
                    entry["stop_armed"] = True
                    changed = True
                    continue
                line = float(entry["entry_price"]) * line_ratio
                if price <= line:
                    if entry["stop_armed"]:
                        entry["stop_armed"] = False
                        changed = True
                        events.append(
                            {
                                "symbol": entry["symbol"],
                                "entry_price": entry["entry_price"],
                                "price": price,
                            }
                        )
                elif not entry["stop_armed"]:
                    entry["stop_armed"] = True
                    changed = True
            if changed:
                self._write(state)
        return events

    def _load(self) -> dict:
        """Read the file, migrate an old array when needed, and return the state.

        Caller holds the file lock.
        """
        state, dirty = self._parse()
        if _detach_watched_holdings(state):
            dirty = True
        if dirty:
            self._write(state)
        return state

    def _parse(self) -> tuple[dict, bool]:
        if not self.path.is_file():
            return _default_state(), True
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _default_state(), True
        if isinstance(raw, list):
            return {"holdings": _clean_holdings(raw), "closed_watch": []}, True
        if not isinstance(raw, dict):
            return _default_state(), True
        holdings_raw = raw.get("holdings", raw.get("symbols"))
        if holdings_raw is None:
            return _default_state(), True
        watch_raw = raw.get("closed_watch", raw.get("closedWatch", []))
        legacy = "closed_watch" not in raw and "closedWatch" not in raw
        state = {"holdings": _clean_holdings(holdings_raw), "closed_watch": _clean_watch(watch_raw)}
        return state, legacy

    def _write(self, state: dict) -> None:
        payload = {
            "holdings": [
                {
                    "symbol": item["symbol"],
                    "entry_price": item["entry_price"],
                    "stop_armed": bool(item["stop_armed"]),
                }
                for item in state["holdings"]
            ],
            "closed_watch": [
                {
                    "symbol": item["symbol"],
                    "closed_at": int(item["closed_at"]),
                    "green_count": int(item["green_count"]),
                }
                for item in state["closed_watch"]
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".holdings-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _lookup_price(price_lookup, symbol: str):
    try:
        looked = price_lookup(symbol)
    except Exception:
        return None
    if looked is None:
        return None
    return clean_entry_price(looked)


def _price_map(prices: dict | None) -> dict[str, float]:
    found = {}
    if not isinstance(prices, dict):
        return found
    for key, value in prices.items():
        if isinstance(value, bool):
            continue
        try:
            symbol = normalize_symbol(key)
            price = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            found[symbol] = round(price, 8)
    return found


def main(argv: list[str] | None = None, directory: Path | None = None, known: set[str] | None = None) -> int:
    """CLI: ``python -m oi_monitor.holdings add|remove|list SYMBOL``.

    ``known`` is only for tests and the server. The CLI itself does not call
    Binance; the HTTP add checks the symbol list once a scan has cached it.
    """
    parser = argparse.ArgumentParser(description="编辑持仓列表 data/holdings.json")
    parser.add_argument("action", choices=("add", "remove", "list"))
    parser.add_argument("symbol", nargs="?")
    args = parser.parse_args(argv)
    if args.action in {"add", "remove"} and not args.symbol:
        print("需要币种", file=sys.stderr)
        return 2
    if args.action == "list" and args.symbol:
        print("list 不接收币种", file=sys.stderr)
        return 2
    book = HoldingsBook(Path(directory) if directory is not None else default_data_dir())
    try:
        if args.action == "list":
            items = book.list()
        elif args.action == "add":
            items = book.add(args.symbol, known)["holdings"]
        else:
            items = book.remove(args.symbol)["holdings"]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(items, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
