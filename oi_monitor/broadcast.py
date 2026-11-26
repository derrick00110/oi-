"""Turn open-interest scans into short Chinese broadcasts."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from oi_monitor.holdings import CLOSED_GREEN_LIMIT, HoldingsBook
from oi_monitor.metrics import judge, screen_change

SCAN_PERIOD_SEC = 300
SCAN_LAG_SEC = 90
# Same coin, same rule, same direction. A rolling 20-minute window is re-checked
# every 5 minutes, so the old 15-minute pause would fire again before that move
# had rolled out. Thirty minutes is the floor; a rule may set a longer pause.
MIN_DIRECTION_COOLDOWN_SEC = 30 * 60
WEBHOOK_TOKEN_LIMIT = 4096
LOG = logging.getLogger("oi-monitor")

WINDOWS = {"15m": "15分钟", "1h": "1小时", "4h": "4小时", "24h": "24小时"}
WINDOW_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "24h": 86400}
METRICS = {"oiValuePct": "未平仓金额", "oiPct": "未平仓合约"}
DIRECTIONS = {"up", "down", "both"}


def seconds_until_scan(now: float, period: int = SCAN_PERIOD_SEC, lag: int = SCAN_LAG_SEC) -> float:
    """Wait until `lag` seconds after each period boundary, so the new 5-minute bar is in."""
    if period <= 0 or lag < 0 or lag >= period:
        raise ValueError("扫描周期无效")
    slot = int(now) // period
    target = slot * period + lag
    if now >= target:
        target += period
    return float(target - now)


def default_rules() -> list[dict]:
    return [
        {
            "id": "oi-dynamic",
            "enabled": True,
            "window": "1h",
            "metric": "oiValuePct",
            "direction": "both",
            "minAbsPct": 10,
            "minOiValue": 0,
            "maxOiValue": None,
            "minPricePct": None,
            "dynamic": True,
            "cooldownSec": MIN_DIRECTION_COOLDOWN_SEC,
        },
    ]


def default_settings() -> dict:
    return {
        "limit": 400,
        "intervalSec": 60,
        "maxPerRound": 20,
        "webhookUrl": "",
        "webhookToken": "",
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
        raise ValueError("指标只能是未平仓金额或未平仓合约")
    if direction not in DIRECTIONS:
        raise ValueError("方向只能是 up、down、both")
    try:
        min_abs = float(raw.get("minAbsPct"))
        min_value = float(raw.get("minOiValue") or 0)
        cooldown = int(raw.get("cooldownSec") or MIN_DIRECTION_COOLDOWN_SEC)
        max_value = _optional_positive(raw.get("maxOiValue"))
        min_price = _optional_positive(raw.get("minPricePct"))
    except (TypeError, ValueError) as exc:
        raise ValueError("阈值或冷却时间不是数字") from exc
    if not 0.1 <= min_abs <= 500:
        raise ValueError("涨跌幅阈值需要在 0.1 到 500 之间")
    if min_value < 0:
        raise ValueError("最小未平仓金额不能是负数")
    if max_value is not None and max_value < min_value:
        raise ValueError("未平仓金额上限不能小于下限")
    if min_price is not None and min_price > 500:
        raise ValueError("价格涨幅阈值需要在 0 到 500 之间")
    if not MIN_DIRECTION_COOLDOWN_SEC <= cooldown <= 86_400:
        raise ValueError("冷却时间需要在 30 分钟到 1 天之间")
    rule_id = str(raw.get("id") or fallback_id).strip()
    if not rule_id or len(rule_id) > 40:
        raise ValueError("规则编号无效")
    dynamic = bool(raw.get("dynamic", False))
    if dynamic:
        metric = "oiValuePct"
    return {
        "id": rule_id,
        "enabled": bool(raw.get("enabled", True)),
        "window": window,
        "metric": metric,
        "direction": direction,
        "minAbsPct": round(min_abs, 4),
        "minOiValue": min_value,
        "maxOiValue": None if max_value is None else round(max_value, 2),
        "minPricePct": None if min_price is None else round(min_price, 4),
        "dynamic": dynamic,
        "cooldownSec": cooldown,
    }


def _optional_positive(raw):
    if raw in (None, "", 0, "0"):
        return None
    value = float(raw)
    if value <= 0:
        return None
    return value


def lift_short_cooldowns(raw):
    """Raise a saved cooldown that is still on the old 15-minute pause.

    Loading must not throw the whole rule file away just because cooldownSec
    predates the rolling window. Saving a value under 30 minutes still errors.
    """
    if not isinstance(raw, list):
        return raw
    lifted = []
    for item in raw:
        if not isinstance(item, dict):
            lifted.append(item)
            continue
        item = dict(item)
        try:
            cooldown = int(item.get("cooldownSec") or 0)
        except (TypeError, ValueError):
            cooldown = 0
        if cooldown < MIN_DIRECTION_COOLDOWN_SEC:
            item["cooldownSec"] = MIN_DIRECTION_COOLDOWN_SEC
        lifted.append(item)
    return lifted


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
    if "webhookToken" in raw:
        incoming = _clean_webhook_token(raw.get("webhookToken"))
        current = str(merged.get("webhookToken") or "")
        # The public API returns a mask. Posting that mask back must not replace the secret.
        if not (incoming and incoming == mask_token(current)):
            merged["webhookToken"] = incoming
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


def mask_token(token: str) -> str:
    """Hide a webhook secret the same way the URL query is hidden: keep the shape, replace the secret with ***."""
    if not token:
        return ""
    if str(token).startswith("Bearer "):
        return "Bearer ***"
    return "***"


def _clean_webhook_token(raw) -> str:
    if isinstance(raw, (bool, dict, list, bytes)):
        raise ValueError("Webhook 令牌无效")
    token = str(raw or "").strip()
    if not token:
        return ""
    if len(token) > WEBHOOK_TOKEN_LIMIT:
        raise ValueError("Webhook 令牌过长")
    if authorization_value(token) is None:
        raise ValueError("Webhook 令牌无效")
    return token


def authorization_value(token: str) -> str | None:
    """Return the Authorization header value, or None when there is nothing safe to send."""
    cleaned = str(token or "").strip()
    if not cleaned:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        return None
    try:
        cleaned.encode("latin-1")
    except UnicodeEncodeError:
        return None
    if cleaned.startswith("Bearer "):
        return cleaned
    return f"Bearer {cleaned}"


def apply_webhook_env(settings: dict) -> dict:
    """Apply OI_WEBHOOK_URL and OI_WEBHOOK_TOKEN over saved settings. Invalid values are skipped."""
    merged = dict(settings)
    url = os.environ.get("OI_WEBHOOK_URL", "").strip()
    token = os.environ.get("OI_WEBHOOK_TOKEN", "").strip()
    if url:
        try:
            merged = clean_settings({"webhookUrl": url}, merged)
        except ValueError:
            LOG.warning("OI_WEBHOOK_URL 无效，已忽略")
    if token:
        try:
            merged = clean_settings({"webhookToken": token}, merged)
        except ValueError:
            LOG.warning("OI_WEBHOOK_TOKEN 无效，已忽略")
    return merged


def _redact(text: str, token: str) -> str:
    secret = str(token or "").strip()
    redacted = text or ""
    if secret:
        redacted = redacted.replace(secret, "***")
        if secret.startswith("Bearer "):
            inner = secret[len("Bearer ") :]
            if inner:
                redacted = redacted.replace(inner, "***")
        else:
            redacted = redacted.replace(f"Bearer {secret}", "Bearer ***")
    return " ".join(redacted.split())[:180]


def format_price(value) -> str:
    """A price with trailing zeros trimmed. Not a USDT notional."""
    if isinstance(value, bool):
        return "暂无"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "暂无"
    if number != number:
        return "暂无"
    text = f"{number:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def stop_loss_text(symbol: str, entry, current) -> str:
    """Alert emoji first. One sentence: reference, current price, percent change."""
    change = (float(current) - float(entry)) / float(entry) * 100.0
    return (
        f"{STOP_LOSS_PREFIX} [{symbol}] "
        f"参考价 {format_price(entry)}，现价 {format_price(current)}，涨跌 {change:+.1f}%"
    )


def buyback_text(symbol: str, row: dict) -> str:
    """Alert emoji first, then the two green broadcasts and the latest open interest and price."""
    cycle = row.get("cycle") if isinstance(row, dict) else None
    if not isinstance(cycle, dict):
        cycle = {}
    changes = row.get("changes") if isinstance(row, dict) else None
    hour = (changes or {}).get("1h") if isinstance(changes, dict) else None
    if not isinstance(hour, dict):
        hour = {}
    oi_pct = cycle.get("oiValuePct")
    if oi_pct is None:
        oi_pct = hour.get("oiValuePct")
    price_pct = cycle.get("pricePct")
    if price_pct is None:
        price_pct = hour.get("pricePct")
    oi_value = None if not isinstance(row, dict) else row.get("oiValue")
    price = None if not isinstance(row, dict) else row.get("price")
    return (
        f"{BUYBACK_PREFIX} [{symbol}] 平仓后已出现 {CLOSED_GREEN_LIMIT} 次绿色播报（OI 与价格共振上涨），"
        f"未平仓金额：{usd_text(oi_value)}，价格 {format_price(price)}，"
        f"未平仓金额 {signed_pct(oi_pct)}，{_tick_price_phrase(price_pct)}"
    )


def _merged_prices(rows, extra) -> dict:
    """Row prices win. ``extra`` covers holdings the scan did not turn into rows."""
    found = {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            found[key] = value
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        price = row.get("price")
        if symbol is not None and price is not None:
            found[symbol] = price
    return found


def _symbol_base(symbol: str, rows) -> str:
    for row in rows or []:
        if isinstance(row, dict) and row.get("symbol") == symbol and row.get("base"):
            return str(row["base"])
    if isinstance(symbol, str) and symbol.endswith("USDT"):
        return symbol[:-4]
    return str(symbol or "")


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


# Display only. Tick 5 notional at or above this is a louder broadcast, not a new gate.
TICK_EMPHASIS_USD = 20_000_000
# Front cluster, left to right, one space apart: repeat, holding, fire, direction.
REPEAT_MARK = "\U0001F501"
FIRE_MARK = "\U0001F525"
# Holdings. Rising keeps the 10/9/5 tiers and only adds a mark. Falling uses the lower gate.
HOLDING_PRICE_DROP_PCT = 2.0
HOLDING_OI_MOVE_PCT = 3.0
HOLDING_MIN_DOWN_STEPS = 3
HOLDING_RISE_PREFIX = "\u3010\u6301\u4ed3\u3011"
HOLDING_WARN_PREFIX = "\U0001F6A8\u3010\u6301\u4ed3\u8b66\u793a\u3011"
HOLDING_STRONG_PREFIX = "\U0001F6A8\U0001F6A8\u3010\u6301\u4ed3\u91cd\u8981\u63d0\u9192\u00b7\u8fde\u8dccOI\u964d\u5171\u632f\u3011"
HOLDING_FALL_NOTE = "持仓币价格下跌，需紧盯"
HOLDING_STRONG_NOTE = "持仓币连续下跌且未平仓金额同步下降，重要提醒，需紧盯"
HOLDING_WARN_DIRECTION = "holding-warn"
# Separate from the front cluster. The alert emoji is the first character, and these
# are not green broadcasts, so they do not take the repeat mark or the daily count.
STOP_LOSS_PREFIX = "\U0001F6A8\u3010\u6301\u4ed3\u6b62\u635f\u63d0\u9192\u00b7\u8dcc\u7834-12.5%\u3011"
BUYBACK_PREFIX = "\U0001F514\u3010\u5e73\u4ed3\u5e01\u4e70\u56de\u63d0\u9192\u3011"
STOP_RULE_ID = "holding-stop"
BUYBACK_RULE_ID = "closed-buyback"
# Bullish direction emoji. Asia/Shanghai matches the daily memory files (UTC+8, no daylight saving).
GREEN_MARK = "\U0001F7E2"
SHANGHAI = timezone(timedelta(hours=8))


def shanghai_day(moment: float) -> str:
    """Calendar day in Asia/Shanghai for an epoch-seconds timestamp."""
    return datetime.fromtimestamp(float(moment), SHANGHAI).strftime("%Y-%m-%d")


def _holding_warning(_row: dict) -> bool:
    """A row by itself is not a holding alert. describe() fills the slot."""
    return False


def _front_cluster(direction: str, *, fire: bool = False, holding: str | bool = False, repeat: bool = False) -> str:
    """Emoji marks at the very front. Order is repeat, holding, fire, then the direction mark."""
    marks = []
    if repeat:
        marks.append(REPEAT_MARK)
    if holding is True:
        marks.append(HOLDING_WARN_PREFIX)
    elif holding:
        marks.append(str(holding))
    if fire:
        marks.append(FIRE_MARK)
    marks.append(direction)
    return " ".join(marks)


def _tick_lead(mark: str, fifth, *, holding: str | bool = False) -> str:
    """Direction emoji, and a fire mark when tick 5 is at least 20 million USD."""
    try:
        current = float(fifth)
    except (TypeError, ValueError):
        current = None
    fire = current is not None and current >= TICK_EMPHASIS_USD
    return _front_cluster(mark, fire=fire, holding=holding)


def _green_day_note(count: int) -> str:
    """Trailing note from the 4th green broadcast of that symbol that Shanghai day."""
    if count < 4:
        return ""
    return f"\u3002\u26a0\ufe0f\u4eca\u65e5\u7b2c {count} \u6b21\u7eff\u8272\u64ad\u62a5"


def _apply_green_repeat(text: str, count: int) -> str:
    """4th+ green of the day: repeat mark first, then the broadcast, then the trailing count.

    The broadcast already starts with the rest of the front cluster, so this stays outermost.
    """
    note = _green_day_note(count)
    if not note:
        return text
    return f"{REPEAT_MARK} {text}{note}"


def _clean_green_daily(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for symbol, slot in raw.items():
        if not isinstance(symbol, str) or not symbol or not isinstance(slot, dict):
            continue
        day = slot.get("day")
        if not isinstance(day, str) or len(day) != 10:
            continue
        try:
            count = int(slot.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if count < 0:
            continue
        cleaned[symbol] = {"day": day, "count": count}
    return cleaned


def _green_daily_from_items(items: list[dict]) -> dict:
    """Fill a missing counter from broadcasts already stored in this file."""
    totals: dict[tuple[str, str], int] = {}
    for item in items:
        if not isinstance(item, dict) or GREEN_MARK not in str(item.get("text") or ""):
            continue
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        try:
            at_ms = int(item.get("at") or 0)
        except (TypeError, ValueError):
            continue
        if at_ms <= 0:
            continue
        day = shanghai_day(at_ms / 1000.0)
        totals[(symbol, day)] = totals.get((symbol, day), 0) + 1
    latest: dict[str, dict] = {}
    for (symbol, day), count in totals.items():
        current = latest.get(symbol)
        if current is None or day > current["day"]:
            latest[symbol] = {"day": day, "count": count}
    return latest


def _tick_price_phrase(price_pct) -> str:
    if price_pct is None:
        return "第 1 次到第 5 次刻度的价格变动暂无"
    if price_pct > 0:
        direction = "上涨"
    elif price_pct < 0:
        direction = "下跌"
    else:
        return "第 1 次到第 5 次刻度价格持平"
    return f"第 1 次到第 5 次刻度价格{direction} {abs(price_pct):.1f}%"


def _cycle_sentence(row: dict, cycle: dict, conclusion: str, mark: str, holding: str | bool = False) -> str:
    ratio = "暂无" if row.get("oiToMarketCapPct") is None else f"{row['oiToMarketCapPct']:.1f}%"
    symbol = row.get("symbol") or row.get("base")
    lead = _tick_lead(mark, cycle.get("fifth"), holding=holding)
    return (
        f"{lead} [{symbol}] 币安未平仓金额 {signed_pct(cycle.get('oiValuePct'))}，"
        f"{_tick_price_phrase(cycle.get('pricePct'))}，"
        f"第 1 次刻度 {usd_text(cycle.get('first'))}，第 5 次刻度 {usd_text(cycle.get('fifth'))}，"
        f"未平仓金额：{usd_text(cycle.get('fifth'))}，"
        f"未平仓金额/市值比率：{ratio}，"
        f"24 小时价格变动：{signed_pct(row.get('priceChangePercent'))}。"
        f"{conclusion}"
    )


def _sentence(row: dict, rule: dict, change: dict, conclusion: str, mark: str, holding: str | bool = False) -> str:
    ratio = "暂无" if row.get("oiToMarketCapPct") is None else f"{row['oiToMarketCapPct']:.1f}%"
    symbol = row.get("symbol") or row.get("base")
    lead = _front_cluster(mark, holding=holding)
    return (
        f"{lead} [{symbol}] 币安未平仓金额 {signed_pct(change.get('oiValuePct'))}，"
        f"{price_phrase(WINDOW_SECONDS[rule['window']], change.get('pricePct'))}，"
        f"未平仓金额：{usd_text(row.get('oiValue'))}，"
        f"未平仓金额/市值比率：{ratio}，"
        f"24 小时价格变动：{signed_pct(row.get('priceChangePercent'))}。"
        f"{conclusion}"
    )


def _is_holding(symbol, holdings) -> bool:
    if not holdings or not symbol:
        return False
    if isinstance(holdings, str):
        holdings = [holdings]
    if not isinstance(holdings, (list, tuple, set)):
        return False
    wanted = {item.strip().upper() for item in holdings if isinstance(item, str) and item.strip()}
    return str(symbol).strip().upper() in wanted


def _price_is_falling(price_pct) -> bool:
    """True when the window price itself is down, matching the 下跌 phrase."""
    try:
        return float(price_pct) < 0
    except (TypeError, ValueError):
        return False


def _price_is_rising(price_pct) -> bool:
    """True when the window price itself is up, matching the 上涨 phrase."""
    try:
        return float(price_pct) > 0
    except (TypeError, ValueError):
        return False


def _down_steps(ticks) -> int | None:
    """Tick-to-tick price drops across the 5-tick window. None when a price is missing."""
    if not isinstance(ticks, list) or len(ticks) != 5:
        return None
    prices = []
    for tick in ticks:
        if not isinstance(tick, dict):
            return None
        raw = tick.get("price")
        if raw is None:
            return None
        try:
            prices.append(float(raw))
        except (TypeError, ValueError):
            return None
    return sum(1 for previous, nxt in zip(prices, prices[1:]) if nxt < previous)


def _window_numbers(cycle: dict) -> tuple[float | None, float | None]:
    try:
        price = float(cycle.get("pricePct"))
    except (TypeError, ValueError):
        price = None
    try:
        oi_pct = float(cycle.get("oiValuePct"))
    except (TypeError, ValueError):
        oi_pct = None
    return price, oi_pct


def _holding_mark(cycle: dict, qualified: bool) -> tuple[str | bool, str | None, bool]:
    """Slot label, extra sentence, and whether this uses the holding cooldown.

    Rising keeps the normal gate and only adds 【持仓】. Falling uses the lower
    gate. Consecutive price drops plus falling open interest is the stronger alert.
    """
    price, oi_pct = _window_numbers(cycle)
    if (
        price is not None
        and oi_pct is not None
        and price <= -HOLDING_PRICE_DROP_PCT
        and abs(oi_pct) >= HOLDING_OI_MOVE_PCT
    ):
        steps = _down_steps(cycle.get("ticks"))
        if oi_pct <= -HOLDING_OI_MOVE_PCT and steps is not None and steps >= HOLDING_MIN_DOWN_STEPS:
            return HOLDING_STRONG_PREFIX, HOLDING_STRONG_NOTE, True
        return HOLDING_WARN_PREFIX, HOLDING_FALL_NOTE, True
    if qualified and price is not None and price > 0:
        return HOLDING_RISE_PREFIX, None, False
    return False, None, False


def describe(row: dict, rule: dict, chop_pct: float = 0.5, holdings=None) -> dict | None:
    change = (row.get("changes") or {}).get(rule["window"]) or {}
    holding = _is_holding(row.get("symbol"), holdings)
    if rule.get("dynamic"):
        cycle = row.get("cycle") or {}
        if not cycle.get("ready"):
            return None
        screened = screen_change(
            cycle.get("fifth"),
            cycle.get("oiValuePct"),
            cycle.get("pricePct"),
            cycle.get("oiValueDelta"),
            chop_pct,
        )
        label, note, warn = _holding_mark(cycle, bool(screened["qualified"])) if holding else (False, None, False)
        if not screened["qualified"] and not warn:
            return None
        notes = []
        if screened["doubled"]:
            notes.append("最高级别")
            notes.append(
                f"未平仓金额从 {usd_text(screened['previous'])} 增至 {usd_text(cycle.get('fifth'))}，短时间翻倍"
            )
        if screened["watershed"]:
            notes.append("未平仓金额达到2000万关键分水岭，庄家常在此离场或加速拉升")
        if note:
            notes.append(note)
        notes.append(screened["fact"])
        mark = "🟢" if screened["doubled"] or screened["code"] == "long" else "⚪"
        rank = 3 if screened["doubled"] else (2 if screened["code"] == "long" else 1)
        return {
            "text": _cycle_sentence(row, cycle, "。".join(notes), mark, label),
            "move": cycle.get("oiValuePct") or 0,
            "rank": rank,
            "fact": screened["fact"],
            "holdingWarn": warn,
            "longGreen": screened["code"] == "long",
        }
    label = HOLDING_RISE_PREFIX if holding and _price_is_rising(change.get("pricePct")) else False
    text = _compose_fixed(row, rule, chop_pct, change, label)
    if text is None:
        return None
    verdict = judge(change.get("oiValuePct"), change.get("pricePct"), chop_pct)
    return {
        "text": text,
        "move": change.get(rule["metric"]) or 0,
        "rank": 1,
        "fact": verdict["text"],
        "holdingWarn": False,
        "longGreen": verdict["code"] == "long",
    }


def compose(row: dict, rule: dict, chop_pct: float = 0.5, holdings=None) -> str | None:
    hit = describe(row, rule, chop_pct, holdings)
    return None if hit is None else hit["text"]


def _compose_fixed(row: dict, rule: dict, chop_pct: float, change: dict | None = None, holding: str | bool = False) -> str | None:
    change = (row.get("changes") or {}).get(rule["window"]) or {}
    move = change.get(rule["metric"])
    if move is None or abs(move) < rule["minAbsPct"]:
        return None
    if rule["direction"] == "up" and move <= 0:
        return None
    if rule["direction"] == "down" and move >= 0:
        return None
    oi_value = float(row.get("oiValue") or 0)
    if oi_value < float(rule.get("minOiValue") or 0):
        return None
    max_value = rule.get("maxOiValue")
    if max_value not in (None, "", 0) and oi_value > float(max_value):
        return None
    min_price = rule.get("minPricePct")
    if min_price not in (None, "", 0):
        price_pct = change.get("pricePct")
        if price_pct is None or float(price_pct) < float(min_price):
            return None
    verdict = judge(change.get("oiValuePct"), change.get("pricePct"), chop_pct)
    mark = "🟢" if verdict["code"] == "long" else "⚪"
    return _sentence(row, rule, change, verdict["text"], mark, holding)


class BroadcastBook:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self.rules = default_rules()
        self.settings = default_settings()
        self.holdings = HoldingsBook(self.directory)
        self.seen: dict[str, dict] = {}
        self.green_daily: dict[str, dict] = {}
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
                self.rules = clean_rules(lift_short_cooldowns(rules))
            except ValueError:
                self.rules = default_rules()
        if isinstance(settings, dict):
            try:
                self.settings = clean_settings(settings, default_settings())
            except ValueError:
                self.settings = default_settings()
        self.settings = apply_webhook_env(self.settings)
        if isinstance(state, dict):
            items = state.get("items") or []
            self.items = [item for item in items if isinstance(item, dict)][-5000:]
            self.next_id = int(state.get("nextId") or (max((item.get("id", 0) for item in self.items), default=0) + 1))
            seen = state.get("seen") or {}
            if isinstance(seen, dict):
                self.seen = seen
            if "greenDaily" in state:
                self.green_daily = _clean_green_daily(state.get("greenDaily"))
            else:
                self.green_daily = _green_daily_from_items(self.items)

    def qualify(self, rows: list[dict]) -> list[dict]:
        """Every enabled-rule hit, before the per-round cap and cooldown."""
        with self._lock:
            rules = [rule for rule in self.rules if rule.get("enabled")]
            chop = float(self.settings.get("priceChopPct") or 0.5)
        holdings = self.holdings.list()
        found = []
        for row in rows:
            for rule in rules:
                hit = describe(row, rule, chop, holdings)
                if hit is None:
                    continue
                change = (row.get("changes") or {}).get(rule["window"]) or {}
                move = float(hit["move"])
                found.append(
                    {
                        "symbol": row.get("symbol") or "",
                        "base": row.get("base") or row.get("symbol") or "",
                        "snapshotAt": row.get("snapshotAt"),
                        "oiValue": row.get("oiValue"),
                        "move": round(move, 4),
                        "pricePct": change.get("pricePct"),
                        "fact": hit["fact"],
                        "text": hit["text"],
                        "rank": hit["rank"],
                        "window": rule["window"],
                    }
                )
        found.sort(key=lambda item: (item["rank"], abs(item["move"])), reverse=True)
        return found

    def evaluate(self, rows: list[dict], snapshot_at: int | None, now: float | None = None, prices: dict | None = None) -> list[dict]:
        moment = time.time() if now is None else now
        snapshot = int(snapshot_at or 0)
        matches = []
        with self._lock:
            rules = [rule for rule in self.rules if rule.get("enabled")]
            cap = int(self.settings.get("maxPerRound") or 5)
            chop = float(self.settings.get("priceChopPct") or 0.5)
        holdings = self.holdings.list()
        for row in rows:
            for rule in rules:
                hit = describe(row, rule, chop, holdings)
                if hit is None:
                    continue
                move = hit["move"]
                side = HOLDING_WARN_DIRECTION if hit.get("holdingWarn") else ("up" if move > 0 else "down")
                key = f"{rule['id']}|{row['symbol']}|{side}"
                matches.append(
                    (hit["rank"], abs(move), key, hit["text"], row, rule, move, hit["fact"], bool(hit.get("longGreen")))
                )
        matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
        created = []
        buyback_rows = []
        with self._lock:
            for _rank, _score, key, text, row, rule, move, fact, long_green in matches:
                if len(created) >= cap:
                    break
                previous = self.seen.get(key) or {}
                if int(previous.get("snapshot") or 0) == snapshot and snapshot != 0:
                    continue
                seen_at = previous.get("at")
                if seen_at is not None and moment - float(seen_at) < rule["cooldownSec"]:
                    continue
                if GREEN_MARK in text:
                    count = self._next_green_count(row["symbol"], moment)
                    text = _apply_green_repeat(text, count)
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
                    "fact": fact,
                }
                self.next_id += 1
                self.items.append(item)
                self.seen[key] = {"snapshot": snapshot, "at": moment}
                created.append(item)
                # Only a broadcast that actually went out counts. Resonance means
                # open interest and price both rose; a green mark from a doubling
                # while price fell does not.
                if long_green:
                    fired = self.holdings.note_long_green(row["symbol"], int(moment * 1000))
                    if fired is not None:
                        buyback_rows.append(row)
            for event in self.holdings.observe_prices(_merged_prices(rows, prices)):
                created.append(self._alert_item(self._stop_item(event, moment, rows)))
            for row in buyback_rows:
                created.append(self._alert_item(self._buyback_item(row, moment)))
            if created:
                self.items = self.items[-5000:]
                self._write_state()
        return created

    def _alert_item(self, item: dict) -> dict:
        self.next_id += 1
        self.items.append(item)
        return item

    def _stop_item(self, event: dict, moment: float, rows) -> dict:
        symbol = event["symbol"]
        entry = float(event["entry_price"])
        current = float(event["price"])
        change = (current - entry) / entry * 100.0
        return {
            "id": self.next_id,
            "at": int(moment * 1000),
            "symbol": symbol,
            "base": _symbol_base(symbol, rows),
            "ruleId": STOP_RULE_ID,
            "window": "5m",
            "metric": "price",
            "move": round(change, 4),
            "text": stop_loss_text(symbol, entry, current),
            "fact": "持仓币跌破参考价 12.5%，止损提醒",
        }

    def _buyback_item(self, row: dict, moment: float) -> dict:
        symbol = row.get("symbol") or ""
        return {
            "id": self.next_id,
            "at": int(moment * 1000),
            "symbol": symbol,
            "base": row.get("base") or _symbol_base(symbol, [row]),
            "ruleId": BUYBACK_RULE_ID,
            "window": "5m",
            "metric": "oiValuePct",
            "move": round(float((row.get("cycle") or {}).get("oiValuePct") or 0), 4),
            "text": buyback_text(symbol, row),
            "fact": "平仓后已出现 2 次绿色播报，买回提醒",
        }

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
        webhook = str(settings.get("webhookUrl") or "")
        token = str(settings.get("webhookToken") or "")
        settings["webhookSet"] = bool(webhook)
        settings["webhookUrl"] = mask_webhook(webhook)
        settings["webhookTokenSet"] = bool(token)
        settings["webhookToken"] = mask_token(token)
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

    def webhook_delivery(self) -> tuple[str, str]:
        with self._lock:
            return (
                str(self.settings.get("webhookUrl") or ""),
                str(self.settings.get("webhookToken") or ""),
            )

    def _write_state(self) -> None:
        self._write_json(
            "broadcasts.json",
            {
                "nextId": self.next_id,
                "items": self.items[-5000:],
                "seen": self.seen,
                "greenDaily": self.green_daily,
            },
        )

    def _next_green_count(self, symbol: str, moment: float) -> int:
        day = shanghai_day(moment)
        slot = self.green_daily.get(symbol) or {}
        if slot.get("day") != day:
            count = 1
        else:
            try:
                count = int(slot.get("count") or 0) + 1
            except (TypeError, ValueError):
                count = 1
        self.green_daily[symbol] = {"day": day, "count": count}
        return count

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


def post_webhook(url: str, item: dict, timeout: float = 5, token: str = "") -> None:
    body = json.dumps(
        {"text": item["text"], "symbol": item["symbol"], "id": item["id"]},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "oi-monitor"}
    if str(token or "").strip():
        auth = authorization_value(token)
        if not auth:
            raise ValueError("Webhook 令牌无效")
        headers["Authorization"] = auth
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def deliver_webhook(url: str, item: dict, token: str = "") -> str | None:
    if not url:
        return None
    try:
        post_webhook(url, item, token=token)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return _redact(str(exc), token)[:180]
    return None


def queue_webhooks(url: str, items: list[dict], token: str = "") -> threading.Thread | None:
    """POST broadcasts off the scan thread. Failures are logged without the token."""
    if not url or not items:
        return None
    pending = [dict(item) for item in items]
    secret = str(token or "")

    def run() -> None:
        for item in pending:
            try:
                error = deliver_webhook(url, item, secret)
            except Exception:
                error = "发送失败"
            if error:
                LOG.warning("webhook 发送失败：%s", _redact(error, secret))

    thread = threading.Thread(target=run, name="webhook", daemon=True)
    thread.start()
    return thread
