"""Event-triggered 5-tick cycles. Tick 1 is an anomaly, not a clock boundary."""

from __future__ import annotations

from oi_monitor.metrics import (
    TICK_COUNT,
    TICK_MS,
    VERIFY_AFTER_MS,
    _implied_price,
    cycle_view,
    pct_change,
    screen_change,
)

# Prior closes / quote volumes used as the baseline. The current bar is not included.
BASELINE_BARS = 20
BARS_24H = 24 * 60 // 5
READY_OFFSET_MS = VERIFY_AFTER_MS
TICK5_OFFSET_MS = (TICK_COUNT - 1) * TICK_MS
KLINE_LIMIT = 48

DEFAULT_TRIGGER = {
    "mode": "trigger",
    "pricePct": 1.5,
    "volumeMult": 2.0,
    "oiPct": 3.0,
    "oiPctLarge": 2.0,
    "oiLargeUsd": 10_000_000.0,
    "minQuoteVolume": 5_000_000.0,
    "rearmMs": 30 * 60_000,
}


def trigger_settings(raw: dict | None = None) -> dict:
    merged = dict(DEFAULT_TRIGGER)
    for key, value in (raw or {}).items():
        if key in merged and value is not None:
            merged[key] = value
    merged["mode"] = "trigger"
    merged["pricePct"] = float(merged["pricePct"])
    merged["volumeMult"] = float(merged["volumeMult"])
    merged["oiPct"] = float(merged["oiPct"])
    merged["oiPctLarge"] = float(merged["oiPctLarge"])
    merged["oiLargeUsd"] = float(merged["oiLargeUsd"])
    merged["minQuoteVolume"] = float(merged["minQuoteVolume"])
    merged["rearmMs"] = int(merged["rearmMs"])
    return merged


def _floor(timestamp: int) -> int:
    return int(timestamp) - int(timestamp) % TICK_MS


def index_oi(series: list[dict]) -> dict[int, dict]:
    found = {}
    for point in series or []:
        found[_floor(point["timestamp"])] = point
    return found


def index_klines(klines: list[dict]) -> dict[int, dict]:
    found = {}
    for bar in klines or []:
        found[_floor(bar["timestamp"])] = bar
    return found


def price_anomaly(klines: dict[int, dict], timestamp: int, price_pct: float, volume_mult: float) -> bool:
    """Close leaves the prior-20 MA by >= price_pct, and quote volume is >= volume_mult times the prior 20."""
    current = klines.get(timestamp)
    if not current or current["close"] <= 0:
        return False
    closes = []
    volumes = []
    for step in range(1, BASELINE_BARS + 1):
        previous = klines.get(timestamp - step * TICK_MS)
        if previous is None or previous["close"] <= 0:
            return False
        closes.append(previous["close"])
        volumes.append(float(previous["quoteVolume"]))
    average = sum(closes) / BASELINE_BARS
    if average <= 0:
        return False
    deviation = abs(current["close"] - average) / average * 100.0
    if deviation < float(price_pct):
        return False
    baseline = sum(volumes) / BASELINE_BARS
    if baseline <= 0:
        return False
    return float(current["quoteVolume"]) >= float(volume_mult) * baseline


def oi_anomaly(
    oi_points: dict[int, dict],
    timestamp: int,
    oi_pct: float,
    oi_pct_large: float,
    oi_large_usd: float,
) -> bool:
    """Absolute 5-minute percent change of sumOpenInterestValue. Contract count is ignored."""
    current = oi_points.get(timestamp)
    previous = oi_points.get(timestamp - TICK_MS)
    if current is None or previous is None:
        return False
    change = pct_change(float(current["oiValue"]), float(previous["oiValue"]))
    if change is None:
        return False
    gate = float(oi_pct_large) if float(current["oiValue"]) >= float(oi_large_usd) else float(oi_pct)
    return abs(change) >= gate


def trigger_reason(oi_points: dict[int, dict], klines: dict[int, dict], timestamp: int, params: dict) -> str | None:
    price = price_anomaly(klines, timestamp, params["pricePct"], params["volumeMult"])
    oi = oi_anomaly(oi_points, timestamp, params["oiPct"], params["oiPctLarge"], params["oiLargeUsd"])
    if price and oi:
        return "both"
    if price:
        return "price"
    if oi:
        return "oi"
    return None


def _empty(mode: str = "trigger") -> dict:
    return {
        "start": None,
        "ready": False,
        "count": 0,
        "ticks": [None] * TICK_COUNT,
        "first": None,
        "fifth": None,
        "oiValuePct": None,
        "pricePct": None,
        "oiValueDelta": None,
        "mode": mode,
        "trigger": None,
    }


def _tick(point: dict | None) -> dict | None:
    if point is None:
        return None
    return {
        "at": point["timestamp"],
        "oiValue": point["oiValue"],
        "price": _implied_price(point["oi"], point["oiValue"]),
    }


def assemble_cycle(oi_points: dict[int, dict], start: int, now_ms: int, reason: str | None) -> dict:
    ticks = []
    for index in range(TICK_COUNT):
        ticks.append(_tick(oi_points.get(start + index * TICK_MS)))
    first = ticks[0]
    fifth = ticks[-1]
    oi_pct = price_pct = delta = None
    if first and fifth:
        oi_pct = pct_change(fifth["oiValue"], first["oiValue"])
        delta = fifth["oiValue"] - first["oiValue"]
        if first["price"] is not None and fifth["price"] is not None:
            price_pct = pct_change(fifth["price"], first["price"])
    return {
        "start": start,
        "ready": now_ms >= start + READY_OFFSET_MS and all(ticks),
        "count": sum(point is not None for point in ticks),
        "ticks": ticks,
        "first": None if not first else first["oiValue"],
        "fifth": None if not fifth else fifth["oiValue"],
        "oiValuePct": oi_pct,
        "pricePct": price_pct,
        "oiValueDelta": delta,
        "mode": "trigger",
        "trigger": reason,
    }


def _liquid(quote_volume_24h: float | None, minimum: float, *, unknown_ok: bool = True) -> bool:
    if minimum <= 0:
        return True
    if quote_volume_24h is None:
        return unknown_ok
    return float(quote_volume_24h) >= float(minimum)


def latest_trigger(
    oi_points: dict[int, dict],
    klines: dict[int, dict],
    now_ms: int,
    params: dict,
    quote_volume_24h: float | None = None,
    volume_at=None,
) -> tuple[int | None, str | None]:
    """Most recent trigger still inside the rearm window. Walking the whole history keeps the 30-minute lock."""
    if not oi_points and not klines:
        return None, None
    params = trigger_settings(params)
    times = sorted(set(oi_points) | set(klines))
    last = None
    reason = None
    rearm = int(params["rearmMs"])
    for timestamp in times:
        if timestamp + TICK_MS > now_ms:
            break
        if last is not None and timestamp < last + rearm:
            continue
        if volume_at is None:
            quote = quote_volume_24h
            unknown_ok = True
        else:
            quote = volume_at(timestamp)
            unknown_ok = False
        if not _liquid(quote, params["minQuoteVolume"], unknown_ok=unknown_ok):
            continue
        found = trigger_reason(oi_points, klines, timestamp, params)
        if found:
            last = timestamp
            reason = found
    if last is None or now_ms >= last + rearm:
        return None, None
    return last, reason


def trigger_cycle_view(
    series: list[dict],
    klines: list[dict],
    now_ms: int,
    params: dict | None = None,
    quote_volume_24h: float | None = None,
) -> dict:
    """Five ticks starting at the latest anomaly. Ready once the 5th bar has closed."""
    settings = trigger_settings(params)
    oi_points = index_oi(series)
    bars = index_klines(klines)
    start, reason = latest_trigger(oi_points, bars, int(now_ms), settings, quote_volume_24h)
    if start is None:
        return _empty()
    return assemble_cycle(oi_points, start, int(now_ms), reason)


def rolling_quote_volume(klines: dict[int, dict]) -> dict[int, float]:
    """Trailing 24h quote volume keyed by bar open. Missing bars count as zero; the first day stays absent."""
    if not klines:
        return {}
    start = min(klines)
    end = max(klines)
    window: list[float] = []
    running = 0.0
    found = {}
    timestamp = start
    while timestamp <= end:
        bar = klines.get(timestamp)
        quote = 0.0 if bar is None else float(bar["quoteVolume"])
        window.append(quote)
        running += quote
        if len(window) > BARS_24H:
            running -= window.pop(0)
        if len(window) == BARS_24H:
            found[timestamp] = running
        timestamp += TICK_MS
    return found


def _qualified(view: dict, chop: float, symbol: str, reason: str | None) -> dict | None:
    if not view.get("ready") or view.get("start") is None:
        return None
    screened = screen_change(view["fifth"], view["oiValuePct"], view["pricePct"], view["oiValueDelta"], chop)
    if not screened["qualified"]:
        return None
    return {
        "symbol": symbol,
        "start": int(view["start"]),
        "signalAt": int(view["start"]) + READY_OFFSET_MS,
        "oiValuePct": view["oiValuePct"],
        "pricePct": view["pricePct"],
        "oiValue": view["fifth"],
        "oiValueDelta": view["oiValueDelta"],
        "code": screened["code"],
        "reason": reason,
    }


def simulate_clock(
    series: list[dict],
    chop: float = 0.5,
    symbol: str = "",
    eval_from: int | None = None,
    eval_until: int | None = None,
) -> list[dict]:
    """One qualified broadcast per fixed 30-minute round, emitted when tick 5 closes."""
    if not series:
        return []
    oi_points = index_oi(series)
    found = []
    seen = set()
    for timestamp in oi_points:
        start = timestamp - timestamp % (30 * 60_000)
        if start in seen:
            continue
        seen.add(start)
        signal_at = start + READY_OFFSET_MS
        if eval_from is not None and signal_at < eval_from:
            continue
        if eval_until is not None and signal_at > eval_until:
            continue
        view = cycle_view(series, signal_at)
        if view.get("start") != start:
            continue
        row = _qualified(view, chop, symbol, None)
        if row:
            found.append(row)
    found.sort(key=lambda item: item["signalAt"])
    return found


def simulate_trigger(
    series: list[dict],
    klines: list[dict],
    params: dict | None = None,
    chop: float = 0.5,
    symbol: str = "",
    quote_volume_24h: float | None = None,
    rolling: bool = False,
    eval_from: int | None = None,
    eval_until: int | None = None,
) -> list[dict]:
    """Walk every closed bar once. A coin cannot arm again until 30 minutes after the previous trigger."""
    settings = trigger_settings(params)
    oi_points = index_oi(series)
    bars = index_klines(klines)
    volume_at = rolling_quote_volume(bars) if rolling else None
    times = sorted(set(oi_points) | set(bars))
    last = None
    reason = None
    emitted = set()
    found = []
    rearm = int(settings["rearmMs"])
    for timestamp in times:
        now = timestamp + TICK_MS
        if eval_until is not None and now > eval_until + TICK_MS:
            break
        if last is None or timestamp >= last + rearm:
            if volume_at is None:
                quote = quote_volume_24h
                unknown_ok = True
            else:
                quote = volume_at.get(timestamp)
                unknown_ok = False
            if _liquid(quote, settings["minQuoteVolume"], unknown_ok=unknown_ok):
                found_reason = trigger_reason(oi_points, bars, timestamp, settings)
                if found_reason:
                    last = timestamp
                    reason = found_reason
        if last is None or last in emitted or timestamp != last + TICK5_OFFSET_MS:
            continue
        signal_at = last + READY_OFFSET_MS
        if eval_from is not None and signal_at < eval_from:
            continue
        if eval_until is not None and signal_at > eval_until:
            continue
        view = assemble_cycle(oi_points, last, now, reason)
        row = _qualified(view, chop, symbol, reason)
        emitted.add(last)
        if row:
            found.append(row)
    return found


def continuation_label(
    broadcast: dict,
    klines: dict[int, dict],
    move_pct: float = 2.0,
    chop: float = 0.5,
    horizon_ms: int = 60 * 60_000,
) -> str:
    """'hit', 'miss', 'flat', or 'censored'.

    Direction is the tick-1-to-tick-5 price side. Chop is not a direction.
    A hit is any later 5-minute close within 60 minutes that reaches move_pct
    in that direction. The window has to be complete; otherwise the sample is censored.
    """
    price_pct = broadcast.get("pricePct")
    if price_pct is None or abs(float(price_pct)) < chop:
        return "flat"
    direction = 1 if float(price_pct) > 0 else -1
    ref_open = int(broadcast["start"]) + TICK5_OFFSET_MS
    ref = klines.get(ref_open)
    if not ref or ref["close"] <= 0:
        return "censored"
    steps = horizon_ms // TICK_MS
    if klines.get(ref_open + steps * TICK_MS) is None:
        return "censored"
    ref_close = float(ref["close"])
    for step in range(1, steps + 1):
        bar = klines.get(ref_open + step * TICK_MS)
        if bar is None or bar["close"] <= 0:
            return "censored"
        change = (float(bar["close"]) - ref_close) / ref_close * 100.0
        if direction > 0 and change >= move_pct:
            return "hit"
        if direction < 0 and change <= -move_pct:
            return "hit"
    return "miss"


def summarize_hits(broadcasts: list[dict], klines_by_symbol: dict[str, dict[int, dict]], chop: float = 0.5) -> dict:
    counts = {"hit": 0, "miss": 0, "flat": 0, "censored": 0, "longHit": 0, "longSeen": 0}
    for row in broadcasts:
        label = continuation_label(row, klines_by_symbol.get(row["symbol"]) or {}, chop=chop)
        counts[label] += 1
        if row.get("code") == "long" and label in {"hit", "miss"}:
            counts["longSeen"] += 1
            if label == "hit":
                counts["longHit"] += 1
    directional = counts["hit"] + counts["miss"]
    counts["directional"] = directional
    counts["hitRate"] = None if not directional else counts["hit"] / directional
    counts["longHitRate"] = None if not counts["longSeen"] else counts["longHit"] / counts["longSeen"]
    return counts


def match_earlier(clock_rows: list[dict], trigger_rows: list[dict], early_ms: int = 90 * 60_000, late_ms: int = 10 * 60_000) -> dict:
    """Pair the same coin and the same open-interest direction. Positive minutes means the trigger broadcast came first."""
    grouped: dict[str, list[dict]] = {}
    for row in trigger_rows:
        grouped.setdefault(row["symbol"], []).append(row)
    deltas = []
    for clock in clock_rows:
        sign = _sign(clock.get("oiValuePct"))
        if sign == 0:
            continue
        best = None
        for trig in grouped.get(clock["symbol"]) or []:
            if _sign(trig.get("oiValuePct")) != sign:
                continue
            delta = int(clock["signalAt"]) - int(trig["signalAt"])
            if not -late_ms <= delta <= early_ms:
                continue
            rank = (abs(delta), delta)
            if best is None or rank < best[0]:
                best = (rank, delta)
        if best is not None:
            deltas.append(best[1] / 60_000)
    deltas.sort()
    return {
        "matched": len(deltas),
        "clock": len(clock_rows),
        "trigger": len(trigger_rows),
        "medianMin": None if not deltas else _median(deltas),
        "matchRate": None if not clock_rows else len(deltas) / len(clock_rows),
    }


def _sign(value) -> int:
    if value is None or value == 0:
        return 0
    return 1 if float(value) > 0 else -1


def _median(values: list[float]) -> float:
    size = len(values)
    mid = size // 2
    if size % 2:
        return float(values[mid])
    return (float(values[mid - 1]) + float(values[mid])) / 2.0
