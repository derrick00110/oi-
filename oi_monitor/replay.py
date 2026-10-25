"""Rebuild threshold crossings from Binance 5-minute open-interest history."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from oi_monitor.broadcast import compose, default_rules
from oi_monitor.client import BinanceClient
from oi_monitor.metrics import index_tickers, normalize, parse_limit, pct_change, point_at, usdt_perpetuals

log = logging.getLogger("oi-monitor")

HIST_PERIOD = "5m"
HIST_LIMIT = 500
WINDOW_MINUTES = 60


def hits_from_series(symbol: str, base: str, series: list[dict], rule: dict | None = None, chop: float = 0.5) -> list[dict]:
    """Every 1-hour notional move that clears the same gate as a live broadcast."""
    rule = dict(default_rules()[0] if rule is None else rule)
    found = []
    for point in series:
        previous = point_at(series, point["timestamp"], WINDOW_MINUTES)
        if previous is None or previous["timestamp"] >= point["timestamp"]:
            continue
        if not point["oi"] or not previous["oi"]:
            continue
        move = pct_change(point["oiValue"], previous["oiValue"])
        price_now = point["oiValue"] / point["oi"]
        price_then = previous["oiValue"] / previous["oi"]
        price_pct = pct_change(price_now, price_then)
        delta = point["oiValue"] - previous["oiValue"]
        supply = point.get("supply") or 0
        market_cap = price_now * supply if price_now > 0 and supply > 0 else None
        ratio = (point["oiValue"] / market_cap * 100.0) if market_cap else None
        row = {
            "symbol": symbol,
            "base": base,
            "oiValue": point["oiValue"],
            "oiToMarketCapPct": ratio,
            "priceChangePercent": None,
            "changes": {
                rule["window"]: {
                    "oiValuePct": move,
                    "pricePct": price_pct,
                    "oiValueDelta": delta,
                }
            },
        }
        text = compose(row, rule, chop)
        if not text:
            continue
        found.append(
            {
                "at": int(point["timestamp"]),
                "symbol": symbol,
                "snapshotAt": int(point["timestamp"]),
                "oiValue": point["oiValue"],
                "move": None if move is None else round(move, 4),
                "pricePct": None if price_pct is None else round(price_pct, 4),
                "fact": text.split("。")[-1],
                "text": text,
                "window": rule["window"],
                "announced": False,
                "source": "replay",
            }
        )
    return found


def _one(client: BinanceClient, symbol: str, base: str, end_time: int | None) -> tuple[str, list[dict], str | None]:
    params = {"symbol": symbol, "period": HIST_PERIOD, "limit": HIST_LIMIT}
    if end_time is not None:
        params["endTime"] = end_time
    try:
        raw = client.get_json("/futures/data/openInterestHist", params, timeout=20)
    except Exception as exc:  # noqa: BLE001 - one symbol must not stop the replay
        return symbol, [], str(exc)[:180]
    if isinstance(raw, dict):
        return symbol, [], str(raw.get("msg") or raw.get("code") or "未平仓合约历史返回异常")
    try:
        series = normalize(raw)
    except ValueError as exc:
        return symbol, [], str(exc)
    return symbol, hits_from_series(symbol, base, series), None


def replay_universe(client: BinanceClient | None = None, limit: int | None = 400, pages: int = 2) -> list[dict]:
    client = client or BinanceClient()
    exchange_info = client.exchange_info()
    tickers = index_tickers(client.get_json("/fapi/v1/ticker/24hr", None, 30))
    bases = usdt_perpetuals(exchange_info)
    ranked = []
    for symbol, base in bases.items():
        ticker = tickers.get(symbol)
        if not ticker:
            continue
        try:
            volume = float(ticker.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        ranked.append((volume, symbol, base))
    ranked.sort(reverse=True)
    if limit is not None:
        ranked = ranked[:limit]
    hits = []
    errors = 0
    end_time = None
    for page in range(max(1, pages)):
        oldest = None
        workers = 8 if len(ranked) > 200 else 10
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, client, symbol, base, end_time) for _volume, symbol, base in ranked]
            for future in as_completed(futures):
                symbol, found, error = future.result()
                if error:
                    errors += 1
                    log.warning("回放 %s 失败：%s", symbol, error)
                    continue
                hits.extend(found)
                if found:
                    first = min(item["snapshotAt"] for item in found)
                    oldest = first if oldest is None else min(oldest, first)
        log.info("回放第 %s 页，累计越过门槛 %s 条，失败 %s", page + 1, len(hits), errors)
        if oldest is None:
            break
        end_time = oldest - 1
    return hits


def main(argv: list[str] | None = None) -> int:
    import argparse

    from oi_monitor.memory import DayMemory

    parser = argparse.ArgumentParser(description="把币安还留着的未平仓历史补进每天的 Markdown")
    parser.add_argument("--limit", default="400")
    parser.add_argument("--pages", type=int, default=2)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    hits = replay_universe(limit=parse_limit(args.limit), pages=args.pages)
    added = DayMemory().add_hits(hits)
    print(f"历史里越过门槛 {len(hits)} 条，新写入 {added} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
