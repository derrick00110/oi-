"""Holdings list for the closer open-interest watch.

The list lives in ``data/holdings.json`` as a JSON array of USDT perpetual
symbols. The scan reads it again every round, and the CLI edits that same
file, so an add or a remove applies on the next scan while the server is up.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_HOLDINGS = ["MANTRAUSDT"]
FILE_NAME = "holdings.json"


def default_data_dir() -> Path:
    """Same directory ``server.py`` uses for rules, settings, and broadcasts."""
    return Path(__file__).resolve().parents[1] / "data"


def normalize_symbol(raw) -> str:
    """Accept a bare base (MANTRA) or a full perpetual (MANTRAUSDT)."""
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


def _clean_list(raw) -> list[str]:
    if isinstance(raw, dict):
        raw = raw.get("holdings")
    if not isinstance(raw, list):
        return list(DEFAULT_HOLDINGS)
    cleaned = []
    seen = set()
    for item in raw:
        try:
            symbol = normalize_symbol(item)
        except ValueError:
            continue
        if symbol not in seen:
            seen.add(symbol)
            cleaned.append(symbol)
    return cleaned


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
            return self._read()

    def add(self, raw, known: set[str] | None = None) -> dict:
        """Append one symbol. ``known`` is the live USDT-perpetual set, when the process has it."""
        symbol = normalize_symbol(raw)
        if known is not None and symbol not in known:
            raise ValueError(f"{symbol} 不是交易中的 USDT 永续")
        with _FileLock(self._lock_path):
            items = self._read()
            if symbol not in items:
                items.append(symbol)
                self._write(items)
            return {"symbol": symbol, "holdings": list(items)}

    def remove(self, raw) -> dict:
        symbol = normalize_symbol(raw)
        with _FileLock(self._lock_path):
            items = self._read()
            removed = symbol in items
            if removed:
                items = [item for item in items if item != symbol]
                self._write(items)
            return {"symbol": symbol, "removed": removed, "holdings": list(items)}

    def _read(self) -> list[str]:
        if not self.path.is_file():
            items = list(DEFAULT_HOLDINGS)
            self._write(items)
            return items
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            items = list(DEFAULT_HOLDINGS)
            self._write(items)
            return items
        return _clean_list(raw)

    def _write(self, items: list[str]) -> None:
        payload = json.dumps(list(items), ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".holdings-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


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
