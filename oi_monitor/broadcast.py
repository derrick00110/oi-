"""Turn open-interest scans into short Chinese broadcasts."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from oi_monitor.metrics import judge

WINDOWS = {"15m": "15分钟", "1h": "1小时", "4h": "4小时", "24h": "24小时"}
WINDOW_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "24h": 86400}
METRICS = {"oiValuePct": "持仓价值", "oiPct": "持仓量"}
DIRECTIONS = {"up", "down", "both"}


def default_rules() -> list[dict]:
    return [
        {
            "id": "oi-1h",
            "enabled": True,
            "window": "1h",
            "metric": "oiPct",
            "direction": "both",
            "minAbsPct": 5,
            "minOiValue": 10_000_000,
            "cooldownSec": 900,
        },
        {
            "id": "oi-24h",
            "enabled": True,
            "window": "24h",
            "metric": "oiPct",
            "direction": "both",
            "minAbsPct": 15,
            "minOiValue": 10_000_000,
            "cooldownSec": 1800,
        },
    ]


def default_settings() -> dict:
    return {
        "limit": 60,
        "intervalSec": 60,
        "maxPerRound": 5,
        "webhookUrl": "",
        "priceChopPct": 0.5,
    }


def _clean_rule(raw: dict, fallback_id: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("规则必须是对象")
    window = str(raw.get("window") or "")
    metric = str(raw.get("metric") or "")
    direction = str(raw.get("direction") or "")
    if window not in WINDOWS:
        raise ValueError("窗口只能是 15m、1h、4h、24h")
    if metric not in METRICS:
        raise ValueError("指标只能是持仓价值或持仓量")
    if direction not in DIRECTIONS:
        raise ValueError("方向只能是 up、down、both")
    try:
        min_abs = float(raw.get("minAbsPct"))
        min_value = float(raw.get("minOiValue") or 0)
        cooldown = int(raw.get("cooldownSec") or 900)
    except (TypeError, ValueError) as exc:
        raise ValueError("阈值或冷却时间不是数字") from exc
    if not 0.1 <= min_abs <= 500:
        raise ValueError("涨跌幅阈值需要在 0.1 到 500 之间")
    if min_value < 0:
        raise ValueError("最小持仓价值不能是负数")
    if not 60 <= cooldown <= 86_400:
        raise ValueError("冷却时间需要在 60 秒到 1 天之间")
    rule_id = str(raw.get("id") or fallback_id).strip()
    if not rule_id or len(rule_id) > 40:
        raise ValueError("规则编号无效")
    return {
        "id": rule_id,
        "enabled": bool(raw.get("enabled", True)),
        "window": window,
        "metric": metric,
        "direction": direction,
        "minAbsPct": round(min_abs, 4),
        "minOiValue": min_value,
        "cooldownSec": cooldown,
    }


def clean_rules(raw) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("规则需要是数组")
    if len(raw) > 8:
        raise ValueError("最多 8 条播报规则")
    cleaned = []
    seen = set()
    for index, item in enumerate(raw, start=1):
        rule = _clean_rule(item, f"rule-{index}")
        if rule["id"] in seen:
            raise ValueError("规则编号重复")
        seen.add(rule["id"])
        cleaned.append(rule)
    return cleaned


def clean_settings(raw: dict, current: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("设置必须是对象")
    merged = dict(current)
    if "limit" in raw:
        limit = raw["limit"]
        if limit in {None, "all", "全市场", 0, "0"}:
            merged["limit"] = None
        else:
            try:
                limit = int(limit)
            except (TypeError, ValueError) as exc:
                raise ValueError("扫描范围无效") from exc
            if not 1 <= limit <= 600:
                raise ValueError("扫描范围需要在 1 到 600，或 all")
            merged["limit"] = limit
    if "intervalSec" in raw:
        try:
            interval = int(raw["intervalSec"])
        except (TypeError, ValueError) as exc:
            raise ValueError("播报间隔无效") from exc
        if not 30 <= interval <= 3600:
            raise ValueError("播报间隔需要在 30 秒到 1 小时之间")
        merged["intervalSec"] = interval
    if "maxPerRound" in raw:
        try:
            cap = int(raw["maxPerRound"])
        except (TypeError, ValueError) as exc:
            raise ValueError("每轮条数无效") from exc
        if not 1 <= cap <= 20:
            raise ValueError("每轮最多播报 1 到 20 条")
        merged["maxPerRound"] = cap
    if "webhookUrl" in raw:
        webhook = str(raw.get("webhookUrl") or "").strip()
        if webhook and not webhook.startswith(("https://", "http://")):
            raise ValueError("Webhook 只接受 http 或 https 地址")
        if len(webhook) > 500:
            raise ValueError("Webhook 地址过长")
        merged["webhookUrl"] = webhook
    if "priceChopPct" in raw:
        try:
            chop = float(raw["priceChopPct"])
        except (TypeError, ValueError) as exc:
            raise ValueError("价格震荡带不是数字") from exc
        if not 0.05 <= chop <= 20:
            raise ValueError("价格震荡带需要在 0.05% 到 20% 之间")
        merged["priceChopPct"] = round(chop, 4)
    return merged


def mask_webhook(url: str) -> str:
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    query = "***" if parts.query else ""
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def signed_pct(value) -> str:
    if value is None:
        return "暂无"
    return f"{value:+.1f}%"


def usd_text(value) -> str:
    if value is None:
        return "暂无"
    absolute = abs(float(value))
    if absolute >= 1e8:
        return f"{absolute / 1e8:.2f} 亿美元"
    wan = absolute / 1e4
    if wan >= 100:
        return f"{wan:.0f} 万美元"
    if wan >= 10:
        return f"{wan:.1f} 万美元"
    return f"{wan:.2f} 万美元"


def price_phrase(seconds: int, price_pct) -> str:
    if price_pct is None:
        return f"过去 {seconds} 秒价格变动暂无"
    if price_pct > 0:
        direction = "上涨"
    elif price_pct < 0:
        direction = "下跌"
    else:
        return f"过去 {seconds} 秒价格持平"
    return f"过去 {seconds} 秒价格{direction} {abs(price_pct):.1f}%"


def funding_phrase(rate) -> str:
    if rate is None:
        return "资金费率暂无"
    percent = float(rate) * 100
    if percent > 0:
        who = "多头付给空头"
    elif percent < 0:
        who = "空头付给多头"
    else:
        who = "当前费率为零"
    return f"资金费率 {percent:+.4f}%，{who}"


def compose(row: dict, rule: dict, chop_pct: float = 0.5) -> str | None:
    change = (row.get("changes") or {}).get(rule["window"]) or {}
    move = change.get(rule["metric"])
    if move is None or abs(move) < rule["minAbsPct"]:
        return None
    if rule["direction"] == "up" and move <= 0:
        return None
    if rule["direction"] == "down" and move >= 0:
        return None
    if float(row.get("oiValue") or 0) < rule["minOiValue"]:
        return None
    oi_change = change.get("oiPct")
    verdict = judge(oi_change, change.get("pricePct"), chop_pct)
    mark = {"up": "🟢", "down": "🔴"}.get(verdict["bias"], "⚪")
    conclusion = verdict["text"]
    if verdict["code"] in {"unclear", "oi-down-chop"}:
        conclusion = f"{conclusion}，{funding_phrase(row.get('fundingRate'))}"
    ratio = "暂无" if row.get("oiToMarketCapPct") is None else f"{row['oiToMarketCapPct']:.1f}%"
    symbol = row.get("symbol") or row.get("base")
    return (
        f"{mark} [{symbol}] 币安未平仓合约量 {signed_pct(oi_change)}，"
        f"{price_phrase(WINDOW_SECONDS[rule['window']], change.get('pricePct'))}，"
        f"未平仓合约量：{usd_text(row.get('oiValue'))}，"
        f"未平仓合约量/市值比率：{ratio}，"
        f"24 小时价格变动：{signed_pct(row.get('priceChangePercent'))}。"
        f"{conclusion}"
    )


class BroadcastBook:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self.rules = default_rules()
        self.settings = default_settings()
        self.seen: dict[str, dict] = {}
        self.next_id = 1
        self.last_error = ""
        self.last_scan_at = 0
        self.load()

    def load(self) -> None:
        rules = self._read_json("rules.json")
        settings = self._read_json("settings.json")
        state = self._read_json("broadcasts.json")
        if isinstance(rules, list):
            try:
                self.rules = clean_rules(rules)
            except ValueError:
                self.rules = default_rules()
        if isinstance(settings, dict):
            try:
                self.settings = clean_settings(settings, default_settings())
            except ValueError:
                self.settings = default_settings()
        if isinstance(state, dict):
            items = state.get("items") or []
            self.items = [item for item in items if isinstance(item, dict)][-200:]
            self.next_id = int(state.get("nextId") or (max((item.get("id", 0) for item in self.items), default=0) + 1))
            seen = state.get("seen") or {}
            if isinstance(seen, dict):
                self.seen = seen

    def evaluate(self, rows: list[dict], snapshot_at: int | None, now: float | None = None) -> list[dict]:
        moment = time.time() if now is None else now
        snapshot = int(snapshot_at or 0)
        matches = []
        with self._lock:
            rules = [rule for rule in self.rules if rule.get("enabled")]
            cap = int(self.settings.get("maxPerRound") or 5)
            chop = float(self.settings.get("priceChopPct") or 0.5)
        for row in rows:
            for rule in rules:
                text = compose(row, rule, chop)
                if text is None:
                    continue
                change = row["changes"][rule["window"]]
                move = change[rule["metric"]]
                key = f"{rule['id']}|{row['symbol']}|{'up' if move > 0 else 'down'}"
                matches.append((abs(move), key, text, row, rule, move))
        matches.sort(key=lambda item: item[0], reverse=True)
        created = []
        with self._lock:
            for _score, key, text, row, rule, move in matches:
                if len(created) >= cap:
                    break
                previous = self.seen.get(key) or {}
                if int(previous.get("snapshot") or 0) == snapshot and snapshot != 0:
                    continue
                if moment - float(previous.get("at") or 0) < rule["cooldownSec"]:
                    continue
                item = {
                    "id": self.next_id,
                    "at": int(moment * 1000),
                    "symbol": row["symbol"],
                    "base": row.get("base") or row["symbol"],
                    "ruleId": rule["id"],
                    "window": rule["window"],
                    "metric": rule["metric"],
                    "move": round(float(move), 4),
                    "text": text,
                    "fact": judge(
                        (row.get("changes") or {}).get(rule["window"], {}).get("oiPct"),
                        (row.get("changes") or {}).get(rule["window"], {}).get("pricePct"),
                        chop,
                    )["text"],
                }
                self.next_id += 1
                self.items.append(item)
                self.seen[key] = {"snapshot": snapshot, "at": moment}
                created.append(item)
            if created:
                self.items = self.items[-200:]
                self._write_state()
        return created

    def replace_rules(self, raw) -> list[dict]:
        cleaned = clean_rules(raw)
        with self._lock:
            self.rules = cleaned
            self._write_json("rules.json", cleaned)
        return cleaned

    def update_settings(self, raw: dict) -> dict:
        with self._lock:
            self.settings = clean_settings(raw, self.settings)
            stored = dict(self.settings)
            self._write_json("settings.json", stored)
            return self.public_settings()

    def public_settings(self) -> dict:
        settings = dict(self.settings)
        webhook = settings.get("webhookUrl") or ""
        settings["webhookSet"] = bool(webhook)
        settings["webhookUrl"] = mask_webhook(webhook)
        return settings

    def snapshot(self) -> dict:
        with self._lock:
            items = list(reversed(self.items[-100:]))
            return {
                "items": items,
                "rules": list(self.rules),
                "settings": self.public_settings(),
                "lastError": self.last_error,
                "lastScanAt": self.last_scan_at,
            }

    def mark_scan(self, error: str = "") -> None:
        with self._lock:
            self.last_error = error
            self.last_scan_at = int(time.time() * 1000)

    def webhook_target(self) -> str:
        with self._lock:
            return str(self.settings.get("webhookUrl") or "")

    def _write_state(self) -> None:
        self._write_json(
            "broadcasts.json",
            {"nextId": self.next_id, "items": self.items[-200:], "seen": self.seen},
        )

    def _read_json(self, name: str):
        path = self.directory / name
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_json(self, name: str, payload) -> None:
        path = self.directory / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def post_webhook(url: str, item: dict, timeout: float = 5) -> None:
    body = json.dumps(
        {"text": item["text"], "symbol": item["symbol"], "id": item["id"]},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "oi-monitor"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def deliver_webhook(url: str, item: dict) -> str | None:
    if not url:
        return None
    try:
        post_webhook(url, item)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return str(exc)[:180]
    return None
