"""Pure calculations for open interest, notional value, and market cap."""

from __future__ import annotations

BAR_MINUTES = 5
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


def build_row(symbol: str, base: str, volume_rank: int, ticker: dict, series: list[dict], premium: dict | None = None) -> dict | None:
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
    """OI-quantity percent gate. It steps down as the USDT open interest grows."""
    if oi_value < 5_000_000:
        return None
    if oi_value <= 10_000_000:
        return 10.0
    if oi_value <= 15_000_000:
        return 5.0
    return 5.0


def judge(oi_pct, price_pct, chop_pct: float = 0.5) -> dict:
    """Four OI-and-price forms. Quantity moves, not USDT notional."""
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


def screen_change(oi_value, oi_pct, price_pct, oi_value_delta, chop_pct: float = 0.5) -> dict:
    """Apply the tiered gate, the 20M watershed, and the short-term doubling alert."""
    verdict = judge(oi_pct, price_pct, chop_pct)
    current = None if oi_value is None else float(oi_value)
    previous = None
    if current is not None and oi_value_delta is not None:
        previous = current - float(oi_value_delta)
    doubled = bool(previous and previous > 0 and current is not None and current >= 5_000_000 and current >= previous * 2)
    watershed = bool(previous is not None and current is not None and previous < 20_000_000 <= current)
    threshold = None if current is None else oi_move_threshold(current)
    moved = oi_pct is not None and threshold is not None and abs(oi_pct) >= threshold
    qualified = bool(moved or doubled or watershed)
    fact = verdict["text"]
    if qualified and doubled:
        shown = f"最高级别。{fact}"
    elif qualified and watershed:
        shown = f"{fact}。未平仓合约量达到2000万关键分水岭"
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
                change.get("oiPct"),
                change.get("pricePct"),
                change.get("oiValueDelta"),
                chop_pct,
            )
            for name, change in (row.get("changes") or {}).items()
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
