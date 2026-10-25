"""Rebuild threshold crossings from Binance 5-minute open-interest history."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from oi_monitor.broadcast import compose, default_rules
from oi_monitor.client import BinanceClient
from oi_monitor.metrics import cycle_view, index_tickers, normalize, parse_limit, usdt_perpetuals

log = logging.getLogger("oi-monitor")

HIST_PERIOD = "5m"
HIST_LIMIT = 500


def hits_from_series(symbol: str, base: str, series: list[dict], rule: dict | None = None, chop: float = 0.5) -> list[dict]:
    """One hit per 30-minute round whose 1st and 5th ticks clear the gate."""
    rule = dict(default_rules()[0] if rule is None else rule)
    if not series:
        return []
    starts = sorted({point["timestamp"] - (point["timestamp"] % (30 * 60_000)) for point in series})
    found = []
    for start in starts:
        view = cycle_view(series, start + 26 * 60_000)
        if not view.get("ready"):
            continue
        row = {
            "symbol": symbol,
            "base": base,
            "oiValue": view.get("fifth"),
            "oiToMarketCapPct": None,
            "priceChangePercent": None,
            "cycle": view,
            "changes": {},
        }
        text = compose(row, rule, chop)
        if not text:
            continue
        fifth = (view.get("ticks") or [None])[-1] or {}
        found.append(
            {
                "at": int(fifth.get("at") or start),
                "symbol": symbol,
                "snapshotAt": int(fifth.get("at") or start),
                "oiValue": view.get("fifth"),
                "move": None if view.get("oiValuePct") is None else round(view["oiValuePct"], 4),
                "pricePct": None if view.get("pricePct") is None else round(view["pricePct"], 4),
                "fact": text.split("。")[-1],
                "text": text,
                "window": "30m",
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
