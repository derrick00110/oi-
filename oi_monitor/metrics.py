"""Pure calculations for open interest, notional value, and market cap."""

from __future__ import annotations

import time

BAR_MINUTES = 5
TICK_MS = 5 * 60_000
CYCLE_MS = 30 * 60_000
TICK_COUNT = 5
VERIFY_AFTER_MS = 25 * 60_000
WINDOWS = (
    ("5m", 5),
    ("15m", 15),
    ("1h", 60),
    ("4h", 240),
    ("24h", 1440),
)


def pct_change(new: float, old: float) -> float | None:
    if old == 0:
        return None
    return (new - old) / old * 100.0


def normalize(raw) -> list[dict]:
    """Turn Binance openInterestHist rows into sorted numeric points."""
    if not isinstance(raw, list):
        raise ValueError("openInterestHist 返回的不是数组")
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            rows.append(
                {
                    "timestamp": int(item["timestamp"]),
                    "oi": float(item["sumOpenInterest"]),
                    "oiValue": float(item["sumOpenInterestValue"]),
                    "supply": float(item.get("CMCCirculatingSupply") or 0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    dedup: dict[int, dict] = {}
    for row in rows:
        dedup[row["timestamp"]] = row
    return [dedup[key] for key in sorted(dedup)]


def point_at(series: list[dict], latest_ts: int, minutes: int, bar_minutes: int = BAR_MINUTES):
    """Latest point at or before latest_ts - minutes, if it is close enough."""
    target = latest_ts - minutes * 60_000
    previous = None
    for row in series:
        if row["timestamp"] <= target:
            previous = row
        else:
            break
    if previous is None:
        return None
    tolerance = int(1.5 * bar_minutes * 60_000)
    if target - previous["timestamp"] > tolerance:
        return None
    return previous


def _implied_price(oi: float, oi_value: float) -> float | None:
    if oi == 0:
        return None
    return oi_value / oi


def changes_for(series: list[dict]) -> dict:
    latest = series[-1]
    price_now = _implied_price(latest["oi"], latest["oiValue"])
    out = {}
    for name, minutes in WINDOWS:
        previous = point_at(series, latest["timestamp"], minutes)
        if previous is None:
            out[name] = {
                "oiPct": None,
                "oiValuePct": None,
                "oiDelta": None,
                "oiValueDelta": None,
                "pricePct": None,
            }
            continue
        price_then = _implied_price(previous["oi"], previous["oiValue"])
        price_pct = None
        if price_now is not None and price_then is not None:
            price_pct = pct_change(price_now, price_then)
        out[name] = {
            "oiPct": pct_change(latest["oi"], previous["oi"]),
            "oiValuePct": pct_change(latest["oiValue"], previous["oiValue"]),
            "oiDelta": latest["oi"] - previous["oi"],
            "oiValueDelta": latest["oiValue"] - previous["oiValue"],
            "pricePct": price_pct,
        }
    return out


def _round(value, digits):
    if value is None:
        return None
    return round(float(value), digits)


def _round_changes(changes: dict) -> dict:
    rounded = {}
    for name, item in changes.items():
        rounded[name] = {
            "oiPct": _round(item["oiPct"], 4),
            "oiValuePct": _round(item["oiValuePct"], 4),
            "oiDelta": _round(item["oiDelta"], 8),
            "oiValueDelta": _round(item["oiValueDelta"], 2),
            "pricePct": _round(item["pricePct"], 4),
        }
    return rounded


def _point_at_stamp(series: list[dict], target: int, tolerance_ms: int = 60_000):
    best = None
    best_gap = tolerance_ms + 1
    for row in series:
        gap = abs(row["timestamp"] - target)
        if gap <= tolerance_ms and gap < best_gap:
            best = row
            best_gap = gap
    return best


def cycle_view(series: list[dict], now_ms: int | None = None) -> dict:
    """Five 5-minute readings in the current 30-minute round. The gate uses the 1st and the 5th."""
    empty = {
        "start": None,
        "ready": False,
        "count": 0,
        "ticks": [None] * TICK_COUNT,
        "first": None,
        "fifth": None,
        "oiValuePct": None,
        "pricePct": None,
        "oiValueDelta": None,
    }
    if not series:
        return empty
    clock = int(series[-1]["timestamp"] if now_ms is None else now_ms)
    start = clock - (clock % CYCLE_MS)
    ticks = []
    for index in range(TICK_COUNT):
        point = _point_at_stamp(series, start + index * TICK_MS)
        if point is None:
            ticks.append(None)
            continue
        ticks.append(
            {
                "at": point["timestamp"],
                "oiValue": point["oiValue"],
                "price": _implied_price(point["oi"], point["oiValue"]),
            }
        )
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
        "ready": clock >= start + VERIFY_AFTER_MS and all(ticks),
        "count": sum(point is not None for point in ticks),
        "ticks": ticks,
        "first": None if not first else first["oiValue"],
        "fifth": None if not fifth else fifth["oiValue"],
        "oiValuePct": oi_pct,
        "pricePct": price_pct,
        "oiValueDelta": delta,
    }


def _public_cycle(cycle: dict) -> dict:
    ticks = []
    for point in cycle["ticks"]:
        if point is None:
            ticks.append(None)
            continue
        ticks.append(
            {
                "at": point["at"],
                "oiValue": _round(point["oiValue"], 2),
                "price": _round(point["price"], 8),
            }
        )
    return {
        "start": cycle["start"],
        "ready": cycle["ready"],
        "count": cycle["count"],
        "ticks": ticks,
        "first": _round(cycle["first"], 2),
        "fifth": _round(cycle["fifth"], 2),
        "oiValuePct": _round(cycle["oiValuePct"], 4),
        "pricePct": _round(cycle["pricePct"], 4),
        "oiValueDelta": _round(cycle["oiValueDelta"], 2),
    }


def build_row(symbol: str, base: str, volume_rank: int, ticker: dict, series: list[dict], premium: dict | None = None, now_ms: int | None = None) -> dict | None:
    if not series:
        return None
    latest = series[-1]
    price = float(ticker["lastPrice"])
    supply = latest["supply"]
    market_cap = price * supply if price > 0 and supply > 0 else None
    oi_value = latest["oiValue"]
    ratio = (oi_value / market_cap * 100.0) if market_cap else None
    funding = None if not premium else premium.get("fundingRate")
    next_funding = None if not premium else premium.get("nextFundingTime")
    return {
        "symbol": symbol,
        "base": base,
        "volumeRank": volume_rank,
        "price": round(price, 8),
        "priceChangePercent": round(float(ticker["priceChangePercent"]), 4),
        "quoteVolume": round(float(ticker["quoteVolume"]), 2),
        "oi": round(latest["oi"], 8),
        "oiValue": round(oi_value, 2),
        "circulatingSupply": round(supply, 4) if supply > 0 else None,
        "marketCap": round(market_cap, 2) if market_cap else None,
        "oiToMarketCapPct": round(ratio, 4) if ratio is not None else None,
        "fundingRate": None if funding is None else round(float(funding), 8),
        "nextFundingTime": next_funding,
        "snapshotAt": latest["timestamp"],
        "changes": _round_changes(changes_for(series)),
        "cycle": _public_cycle(cycle_view(series, int(time.time() * 1000) if now_ms is None else now_ms)),
        "spark": [round(point["oiValue"], 2) for point in series],
    }


def usdt_perpetuals(exchange_info: dict) -> dict[str, str]:
    found = {}
    for item in exchange_info.get("symbols", []):
        if item.get("contractType") != "PERPETUAL":
            continue
        if item.get("quoteAsset") != "USDT":
            continue
        if item.get("status") != "TRADING":
            continue
        symbol = item.get("symbol")
        if not symbol:
            continue
        found[symbol] = item.get("baseAsset") or symbol.removesuffix("USDT")
    return found


FACTS = {
    ("up", "up"): ("long", "优先推报，做多信号", "up"),
    ("up", "down"): ("wash", "可能为出货、洗盘或蓄力", "flat"),
    ("up", "chop"): ("wash", "可能为出货、洗盘或蓄力", "flat"),
    ("down", "up"): ("watch-up", "未平仓减少、价格上涨，需观望", "flat"),
    ("down", "down"): ("abandon", "弃盘行情，需观望", "flat"),
    ("down", "chop"): ("watch-flat", "未平仓减少、价格持平，需观望", "flat"),
}


def oi_move_threshold(oi_value: float) -> float | None:
    """USDT open-interest percent gate. It steps down as the notional grows."""
    if oi_value < 5_000_000:
        return 15.0
    if oi_value <= 10_000_000:
        return 10.0
    if oi_value <= 15_000_000:
        return 5.0
    return 5.0


def judge(oi_pct, price_pct, chop_pct: float = 0.5) -> dict:
    """Four forms. The open-interest side is the USDT notional percent."""
    chop = abs(float(chop_pct))
    if oi_pct is None:
        return {"code": "no-oi", "text": "未平仓合约缺失，暂不判断", "oiSide": None, "priceSide": None, "bias": "flat"}
    if oi_pct == 0:
        return {"code": "oi-flat", "text": "未平仓合约没有变化，暂不判断", "oiSide": "flat", "priceSide": None, "bias": "flat"}
    oi_side = "up" if oi_pct > 0 else "down"
    if price_pct is None:
        return {"code": "no-price", "text": "价格缺失，暂不判断", "oiSide": oi_side, "priceSide": None, "bias": "flat"}
    if abs(price_pct) < chop:
        price_side = "chop"
    elif price_pct > 0:
        price_side = "up"
    else:
        price_side = "down"
    code, text, bias = FACTS[(oi_side, price_side)]
    return {"code": code, "text": text, "oiSide": oi_side, "priceSide": price_side, "bias": bias}


def screen_change(oi_value, oi_value_pct, price_pct, oi_value_delta, chop_pct: float = 0.5) -> dict:
    """Apply the tiered notional-percent gate, the 20M watershed, and the doubling alert."""
    verdict = judge(oi_value_pct, price_pct, chop_pct)
    current = None if oi_value is None else float(oi_value)
    previous = None
    if current is not None and oi_value_delta is not None:
        previous = current - float(oi_value_delta)
    doubled = bool(previous and previous > 0 and current is not None and current >= 5_000_000 and current >= previous * 2)
    watershed = bool(previous is not None and current is not None and previous < 20_000_000 <= current)
    threshold = None if current is None else oi_move_threshold(current)
    moved = oi_value_pct is not None and threshold is not None and abs(oi_value_pct) >= threshold
    qualified = bool(moved or doubled or watershed)
    fact = verdict["text"]
    if qualified and doubled:
        shown = f"最高级别。{fact}"
    elif qualified and watershed:
        shown = f"{fact}。未平仓金额达到2000万关键分水岭"
    elif qualified:
        shown = fact
    else:
        shown = "未达门槛"
    return {
        **verdict,
        "fact": fact,
        "text": shown,
        "qualified": qualified,
        "doubled": doubled,
        "watershed": watershed,
        "threshold": threshold,
        "previous": previous,
    }


def attach_reads(rows: list[dict], chop_pct: float) -> list[dict]:
    attached = []
    for row in rows:
        item = dict(row)
        item["reads"] = {
            name: screen_change(
                row.get("oiValue"),
                change.get("oiValuePct"),
                change.get("pricePct"),
                change.get("oiValueDelta"),
                chop_pct,
            )
            for name, change in (row.get("changes") or {}).items()
        }
        cycle = row.get("cycle") or {}
        if cycle.get("ready"):
            item["cycleRead"] = screen_change(
                cycle.get("fifth"),
                cycle.get("oiValuePct"),
                cycle.get("pricePct"),
                cycle.get("oiValueDelta"),
                chop_pct,
            )
        else:
            count = int(cycle.get("count") or 0)
            item["cycleRead"] = {
                "code": "pending",
                "text": f"刻度 {count}/5，最后 5 分钟再核实",
                "bias": "flat",
                "qualified": False,
            }
        attached.append(item)
    return attached


def index_premium(raw) -> dict[str, dict]:
    if isinstance(raw, dict):
        raw = [raw]
    indexed = {}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        if not symbol:
            continue
        rate = item.get("lastFundingRate")
        try:
            rate_value = None if rate in (None, "") else float(rate)
        except (TypeError, ValueError):
            rate_value = None
        nxt = item.get("nextFundingTime")
        try:
            nxt_value = int(nxt) if nxt else None
        except (TypeError, ValueError):
            nxt_value = None
        indexed[symbol] = {"fundingRate": rate_value, "nextFundingTime": nxt_value}
    return indexed


def index_tickers(raw) -> dict[str, dict]:
    if isinstance(raw, dict):
        raw = [raw]
    indexed = {}
    for item in raw or []:
        symbol = item.get("symbol")
        if symbol:
            indexed[symbol] = item
    return indexed


def parse_limit(raw: str) -> int | None:
    text = (raw or "").strip().lower()
    if text in {"all", "0", "full", "全市场"}:
        return None
    try:
        limit = int(text)
    except ValueError as exc:
        raise ValueError("limit 需要是 1 到 600 的整数，或 all") from exc
    if limit < 1 or limit > 600:
        raise ValueError("limit 需要在 1 到 600 之间，或使用 all")
    return limit
