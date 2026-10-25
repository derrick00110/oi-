"""Daily Markdown backup of everything the monitor catches."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oi_monitor.broadcast import signed_pct, usd_text

SHANGHAI = timezone(timedelta(hours=8))


def default_memory_dir() -> Path:
    configured = os.environ.get("OI_MEMORY_DIR", "").strip()
    if configured:
        return Path(configured)
    store = Path("/cursor/stores/self")
    if store.is_dir():
        return store / "oi-memory"
    return Path(__file__).resolve().parents[1] / "data" / "memory"


def shanghai_parts(epoch_ms: int) -> tuple[str, str]:
    moment = datetime.fromtimestamp(epoch_ms / 1000, SHANGHAI)
    return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M:%S")


class DayMemory:
    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory) if directory is not None else default_memory_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seen = self._load_seen()

    def backfill_broadcasts(self, items: list[dict]) -> int:
        added = 0
        days = set()
        with self._lock:
            for item in items:
                day = self._append_broadcast(item)
                if day:
                    days.add(day)
                    added += 1
            for day in days:
                self._render(day)
            if days:
                self._render_index()
        return added

    def record_scan(self, hits: list[dict], announced: list[dict], at_ms: int, requested: int, returned: int) -> None:
        spoken = {item.get("symbol") for item in announced}
        with self._lock:
            day, _clock = shanghai_parts(at_ms)
            days = {day}
            self._append(day, {"type": "scan", "at": at_ms, "requested": requested, "returned": returned, "hitCount": len(hits)})
            for hit in hits:
                self._append_hit(hit, at_ms, hit.get("symbol") in spoken)
            for item in announced:
                written = self._append_broadcast(item)
                if written:
                    days.add(written)
            for name in days:
                self._render(name)
            self._render_index()

    def record_failure(self, at_ms: int, message: str) -> None:
        with self._lock:
            day, _clock = shanghai_parts(at_ms)
            self._append(day, {"type": "scan", "at": at_ms, "requested": 0, "returned": 0, "hitCount": 0, "error": message[:240]})
            self._render(day)
            self._render_index()

    def _load_seen(self) -> set[str]:
        seen = set()
        for path in self.directory.glob("*.jsonl"):
            for event in self._read_events(path):
                key = self._event_key(event)
                if key:
                    seen.add(key)
        return seen

    def _event_key(self, event: dict) -> str:
        kind = event.get("type")
        if kind == "broadcast" and event.get("id") is not None:
            return f"b:{event['id']}"
        if kind == "hit":
            sign = "up" if float(event.get("move") or 0) > 0 else "down"
            snap = event.get("snapshotAt") or event.get("at")
            return f"h:{event.get('symbol')}|{snap}|{event.get('window')}|{sign}"
        return ""

    def _append_broadcast(self, item: dict) -> str:
        if not isinstance(item, dict) or item.get("id") is None or not item.get("at"):
            return ""
        key = f"b:{item['id']}"
        if key in self._seen:
            return ""
        day, _clock = shanghai_parts(int(item["at"]))
        self._append(
            day,
            {
                "type": "broadcast",
                "id": item["id"],
                "at": int(item["at"]),
                "symbol": item.get("symbol") or "",
                "text": item.get("text") or "",
                "fact": item.get("fact") or "",
            },
        )
        self._seen.add(key)
        return day

    def _append_hit(self, hit: dict, at_ms: int, announced: bool) -> None:
        event = {
            "type": "hit",
            "at": at_ms,
            "symbol": hit.get("symbol") or "",
            "snapshotAt": hit.get("snapshotAt"),
            "oiValue": hit.get("oiValue"),
            "move": hit.get("move"),
            "pricePct": hit.get("pricePct"),
            "fact": hit.get("fact") or "",
            "text": hit.get("text") or "",
            "window": hit.get("window") or "",
            "announced": bool(announced),
        }
        key = self._event_key(event)
        if key in self._seen:
            return
        day, _clock = shanghai_parts(at_ms)
        self._append(day, event)
        self._seen.add(key)

    def _append(self, day: str, event: dict) -> None:
        path = self.directory / f"{day}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _read_events(self, path: Path) -> list[dict]:
        events = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return events
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _render(self, day: str) -> None:
        events = self._read_events(self.directory / f"{day}.jsonl")
        broadcasts = [event for event in events if event.get("type") == "broadcast"]
        hits = [event for event in events if event.get("type") == "hit"]
        scans = [event for event in events if event.get("type") == "scan"]
        lines = [
            f"# {day} 未平仓监测",
            "",
            "北京时间。这一天监测到的播报、越过门槛的合约、每一轮扫描都在下面。金额是未平仓金额。",
            "",
            "## 播报",
            "",
        ]
        if not broadcasts:
            lines.append("这一天还没有播报。")
        else:
            for event in sorted(broadcasts, key=lambda item: int(item.get("at") or 0)):
                _day, clock = shanghai_parts(int(event["at"]))
                lines.append(f"- {clock} {event.get('text') or ''}")
        lines.extend(["", "## 越过门槛", ""])
        if not hits:
            lines.append("这一天还没有合约越过门槛。")
        else:
            lines.extend(
                [
                    "| 时间 | 币种 | 金额涨跌 | 未平仓金额 | 同窗口价格 | 事实 | 播报 |",
                    "| --- | --- | --- | --- | --- | --- | --- |",
                ]
            )
            for event in sorted(hits, key=lambda item: int(item.get("at") or 0)):
                _day, clock = shanghai_parts(int(event["at"]))
                spoken = "已播" if event.get("announced") else "未播"
                fact = str(event.get("fact") or "").replace("|", " ")
                lines.append(
                    f"| {clock} | {event.get('symbol') or ''} | {signed_pct(event.get('move'))} | {usd_text(event.get('oiValue'))} | {signed_pct(event.get('pricePct'))} | {fact} | {spoken} |"
                )
        lines.extend(["", "## 扫描", ""])
        if not scans:
            lines.append("这一天还没有扫描记录。")
        else:
            for event in sorted(scans, key=lambda item: int(item.get("at") or 0)):
                _day, clock = shanghai_parts(int(event["at"]))
                if event.get("error"):
                    lines.append(f"- {clock} 扫描失败：{event['error']}")
                else:
                    lines.append(
                        f"- {clock} 拉到 {event.get('returned')}/{event.get('requested')}，越过门槛 {event.get('hitCount') or 0} 个"
                    )
        lines.append("")
        self._write(self.directory / f"{day}.md", "\n".join(lines))

    def _render_index(self) -> None:
        days = sorted(path.stem for path in self.directory.glob("*.jsonl"))
        lines = [
            "# 未平仓监测记忆",
            "",
            "每天一份，按北京时间切日。文件在这台云电脑上，用来留监测备份。",
            "",
        ]
        if not days:
            lines.append("还没有记录。")
        for day in days:
            events = self._read_events(self.directory / f"{day}.jsonl")
            broadcasts = sum(1 for event in events if event.get("type") == "broadcast")
            hits = sum(1 for event in events if event.get("type") == "hit")
            scans = sum(1 for event in events if event.get("type") == "scan")
            lines.append(f"- [{day}]({day}.md)：播报 {broadcasts} 条，越过门槛 {hits} 条，扫描 {scans} 次")
        lines.append("")
        self._write(self.directory / "index.md", "\n".join(lines))

    def _write(self, path: Path, text: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
