"""Compare fixed-clock cycles with event-triggered cycles on recent Binance data.

Fetch is cached under OI_BACKTEST_CACHE (default /tmp/oi-trigger-backtest) so a rerun
does not hit the public API again. The grid itself is local.

    python3 -m oi_monitor.backtest_trigger --limit 400 --days 8 --out docs/backtest-trigger.md
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from oi_monitor.client import BinanceClient, Limiter
from oi_monitor.metrics import index_tickers, normalize, parse_klines, parse_limit, usdt_perpetuals
from oi_monitor.trigger import index_klines, match_earlier, simulate_clock, simulate_trigger, summarize_hits

log = logging.getLogger("oi-monitor")

TICK_MS = 5 * 60_000
DAY_MS = 24 * 60 * 60_000
WARMUP_MS = DAY_MS
PRICE_GRID = (1.0, 1.5, 2.0)
VOLUME_GRID = (1.5, 2.0, 3.0)
OI_GRID = (2.0, 3.0, 5.0)
LARGE_OI_PCT = 2.0


def _cache_dir() -> Path:
    import os

    path = Path(os.environ.get("OI_BACKTEST_CACHE", "/tmp/oi-trigger-backtest"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_cache(symbol: str) -> dict | None:
    path = _cache_dir() / f"{symbol}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(symbol: str, payload: dict) -> None:
    path = _cache_dir() / f"{symbol}.json"
    path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def _pages(client: BinanceClient, path: str, params: dict, cutoff: int) -> list:
    rows = []
    end = None
    for _ in range(8):
        query = dict(params)
        if end is not None:
            query["endTime"] = end
        raw = client.get_json(path, query, timeout=20)
        if not isinstance(raw, list) or not raw:
            break
        rows.extend(raw)
        if path.endswith("klines"):
            oldest = int(raw[0][0])
        else:
            oldest = min(int(item["timestamp"]) for item in raw if isinstance(item, dict) and item.get("timestamp"))
        if oldest <= cutoff:
            break
        nxt = oldest - 1
        if end is not None and nxt >= end:
            break
        end = nxt
    return rows


def _covers(payload: dict, cutoff: int) -> bool:
    oi = payload.get("oi") or []
    klines = payload.get("klines") or []
    if len(oi) < 300 or len(klines) < 300:
        return False
    slack = 6 * 60 * 60_000
    return min(row["timestamp"] for row in oi) <= cutoff + slack and min(row["timestamp"] for row in klines) <= cutoff + slack


def _fetch_symbol(client: BinanceClient, kline_limiter: Limiter, symbol: str, cutoff: int) -> dict:
    cached = _read_cache(symbol)
    if cached and _covers(cached, cutoff):
        return cached
    oi_raw = _pages(
        client,
        "/futures/data/openInterestHist",
        {"symbol": symbol, "period": "5m", "limit": 500},
        cutoff,
    )
    kline_raw = []
    end = None
    for _ in range(4):
        kline_limiter.acquire()
        query = {"symbol": symbol, "interval": "5m", "limit": 1500}
        if end is not None:
            query["endTime"] = end
        raw = client.get_json("/fapi/v1/klines", query, timeout=20)
        if not isinstance(raw, list) or not raw:
            break
        kline_raw.extend(raw)
        oldest = int(raw[0][0])
        if oldest <= cutoff:
            break
        nxt = oldest - 1
        if end is not None and nxt >= end:
            break
        end = nxt
    payload = {"oi": normalize(oi_raw), "klines": parse_klines(kline_raw)}
    if payload["oi"] and payload["klines"]:
        _write_cache(symbol, payload)
    return payload


def load_universe(client: BinanceClient, limit: int, days: int, workers: int = 6) -> tuple[list[dict], int, int]:
    now = int(time.time() * 1000)
    cutoff = now - days * DAY_MS
    info = client.exchange_info()
    tickers = index_tickers(client.get_json("/fapi/v1/ticker/24hr", None, 30))
    ranked = []
    for symbol, base in usdt_perpetuals(info).items():
        ticker = tickers.get(symbol)
        if not ticker:
            continue
        try:
            volume = float(ticker.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        ranked.append((volume, symbol, base))
    ranked.sort(reverse=True)
    ranked = ranked[:limit]
    kline_limiter = Limiter(max_calls=120, per_seconds=60)
    loaded = []
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_symbol, client, kline_limiter, symbol, cutoff): (volume, symbol, base)
            for volume, symbol, base in ranked
        }
        done = 0
        for future in as_completed(futures):
            volume, symbol, base = futures[future]
            done += 1
            try:
                payload = future.result()
            except Exception as exc:  # noqa: BLE001 - one symbol must not stop the grid
                failed += 1
                log.warning("回测 %s 失败：%s", symbol, exc)
                continue
            oi = [row for row in payload.get("oi") or [] if row["timestamp"] >= cutoff]
            klines = [row for row in payload.get("klines") or [] if row["timestamp"] >= cutoff]
            if len(oi) < 300 or len(klines) < 300:
                failed += 1
                log.warning("回测 %s 历史太短：oi %s 根，k线 %s 根", symbol, len(oi), len(klines))
                continue
            loaded.append({"symbol": symbol, "base": base, "quoteVolume": volume, "oi": oi, "klines": klines})
            if done % 25 == 0 or done == len(ranked):
                log.info("已处理 %s/%s，可用 %s，失败 %s", done, len(ranked), len(loaded), failed)
    return loaded, len(ranked), failed


def _per_day(count: int, span_ms: int) -> float:
    days = span_ms / DAY_MS
    if days <= 0:
        return 0.0
    return count / days


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _fmt_num(value) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}"


def evaluate(symbols: list[dict], eval_from: int, eval_until: int) -> tuple[dict, list[dict]]:
    clock_rows = []
    klines_by_symbol = {}
    prepared = []
    for item in symbols:
        bars = index_klines(item["klines"])
        klines_by_symbol[item["symbol"]] = bars
        clock = simulate_clock(item["oi"], symbol=item["symbol"], eval_from=eval_from, eval_until=eval_until)
        clock_rows.extend(clock)
        prepared.append(item)
    clock_hits = summarize_hits(clock_rows, klines_by_symbol)
    span = max(1, eval_until - eval_from)
    clock_summary = {
        "count": len(clock_rows),
        "perDay": _per_day(len(clock_rows), span),
        **clock_hits,
    }
    rows = []
    for price_pct in PRICE_GRID:
        for volume_mult in VOLUME_GRID:
            for oi_pct in OI_GRID:
                params = {
                    "pricePct": price_pct,
                    "volumeMult": volume_mult,
                    "oiPct": oi_pct,
                    "oiPctLarge": min(LARGE_OI_PCT, oi_pct),
                    "oiLargeUsd": 10_000_000,
                    "minQuoteVolume": 5_000_000,
                    "rearmMs": 30 * 60_000,
                }
                triggered = []
                reasons = {"price": 0, "oi": 0, "both": 0}
                for item in prepared:
                    found = simulate_trigger(
                        item["oi"],
                        item["klines"],
                        params,
                        symbol=item["symbol"],
                        rolling=True,
                        eval_from=eval_from,
                        eval_until=eval_until,
                    )
                    for row in found:
                        reasons[row.get("reason") or "oi"] = reasons.get(row.get("reason") or "oi", 0) + 1
                    triggered.extend(found)
                hits = summarize_hits(triggered, klines_by_symbol)
                paired = match_earlier(clock_rows, triggered)
                rows.append(
                    {
                        "pricePct": price_pct,
                        "volumeMult": volume_mult,
                        "oiPct": oi_pct,
                        "oiPctLarge": params["oiPctLarge"],
                        "count": len(triggered),
                        "perDay": _per_day(len(triggered), span),
                        "medianMin": paired["medianMin"],
                        "matched": paired["matched"],
                        "matchRate": paired["matchRate"],
                        "hitRate": hits["hitRate"],
                        "longHitRate": hits["longHitRate"],
                        "directional": hits["directional"],
                        "flat": hits["flat"],
                        "censored": hits["censored"],
                        "reasons": reasons,
                    }
                )
                log.info(
                    "X=%s V=%s Y=%s 播报 %s 条/天 提前 %s 分钟 命中 %s",
                    price_pct,
                    volume_mult,
                    oi_pct,
                    f"{rows[-1]['perDay']:.1f}",
                    _fmt_num(rows[-1]["medianMin"]),
                    _fmt_pct(rows[-1]["hitRate"]),
                )
    return clock_summary, rows


def recommend(clock: dict, rows: list[dict]) -> dict | None:
    """A combo is usable as the default only when it is clearly earlier without flooding or a much worse hit rate."""
    eligible = []
    for row in rows:
        if row["medianMin"] is None or row["medianMin"] < 5:
            continue
        if row["matched"] < 20:
            continue
        if clock["perDay"] > 0 and not (0.3 * clock["perDay"] <= row["perDay"] <= 3 * clock["perDay"]):
            continue
        if clock["hitRate"] is not None and row["hitRate"] is not None and row["hitRate"] < clock["hitRate"] - 0.05:
            continue
        ratio = 1.0 if clock["perDay"] <= 0 else row["perDay"] / clock["perDay"]
        hit_gap = 0.0 if clock["hitRate"] is None or row["hitRate"] is None else row["hitRate"] - clock["hitRate"]
        score = float(row["medianMin"]) + 20 * hit_gap - 2 * max(0.0, ratio - 1)
        # Stay near the proposed 1.5 / 2 / 3 start when two scores are close.
        distance = abs(row["pricePct"] - 1.5) + abs(row["volumeMult"] - 2) + abs(row["oiPct"] - 3)
        eligible.append((score, -distance, row))
    if not eligible:
        return None
    eligible.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return eligible[0][2]


def render(clock: dict, rows: list[dict], meta: dict, choice: dict | None) -> str:
    lines = [
        "# 触发模式回测",
        "",
        f"样本是币安 U 本位成交额前 {meta['requested']} 个永续里，历史够长的 {meta['used']} 个。失败或历史太短 {meta['failed']} 个。",
        f"拉取约 {meta['days']} 天，前 24 小时只用来算均线和滚动成交额，不计入播报。比较窗口 {meta['evalFrom']} 到 {meta['evalUntil']}，共 {meta['evalDays']:.2f} 天。",
        "",
        "时钟模式：第 1 次刻度卡在 :00 或 :30，第 5 次那根 5 分钟 K 线收盘后播报。",
        "触发模式：价格偏离前 20 根均线且成交额放大，或未平仓金额 5 分钟变化过线，才把那根记成第 1 次。第 5 次仍是 20 分钟后那根，收盘后用原来的分档、形态、翻倍和 2000 万分水岭决定播不播。同一合约 30 分钟内不重复触发。滚动 24 小时成交额不足 500 万美元的跳过。未平仓金额达到 1000 万美元时，触发阈值用 min(2%, Y)。",
        "",
        "提前多少分钟：同一合约、未平仓金额同向，并且触发播报落在时钟播报前 90 分钟到后 10 分钟里，取最近的一对。正数表示触发更早。中位数只统计配上的那些。",
        "命中：有价格方向的播报（价格侧不是震荡）里，之后 60 分钟内任一 5 分钟收盘价顺着该方向走出至少 2%。窗口不完整的不进分母。震荡播报只计入条数，不进命中率。",
        "",
        f"时钟模式：{clock['perDay']:.1f} 条/天，方向命中 {_fmt_pct(clock['hitRate'])}，做多命中 {_fmt_pct(clock['longHitRate'])}，有方向样本 {clock['directional']}，震荡 {clock['flat']}，窗口不够 {clock['censored']}。",
        "",
        "| X% | V | Y% | 条/天 | 中位提前(分钟) | 配上时钟 | 命中 | 做多命中 | 价格/未平仓/两者 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        reasons = row["reasons"]
        lines.append(
            "| {price:g} | {vol:g} | {oi:g} | {per:.1f} | {med} | {matched} ({rate}) | {hit} | {long} | {price_n}/{oi_n}/{both} |".format(
                price=row["pricePct"],
                vol=row["volumeMult"],
                oi=row["oiPct"],
                per=row["perDay"],
                med=_fmt_num(row["medianMin"]),
                matched=row["matched"],
                rate=_fmt_pct(row["matchRate"]),
                hit=_fmt_pct(row["hitRate"]),
                long=_fmt_pct(row["longHitRate"]),
                price_n=reasons.get("price", 0),
                oi_n=reasons.get("oi", 0),
                both=reasons.get("both", 0),
            )
        )
    lines.extend(["", "## 建议", ""])
    if choice is None:
        lines.append(
            "没有一组同时满足：中位至少早 5 分钟、配上至少 20 条时钟播报、条数在时钟模式的 0.3 到 3 倍之间、方向命中率不比时钟模式低过 5 个百分点。默认保持 `cycleMode=clock`。"
        )
    else:
        lines.append(
            "建议默认用触发模式，参数 X={price:g}%、V={vol:g}、Y={oi:g}%（大额未平仓用 {large:g}%）。"
            "这组中位早 {med} 分钟，{per:.1f} 条/天，方向命中 {hit}。".format(
                price=choice["pricePct"],
                vol=choice["volumeMult"],
                oi=choice["oiPct"],
                large=choice["oiPctLarge"],
                med=_fmt_num(choice["medianMin"]),
                per=choice["perDay"],
                hit=_fmt_pct(choice["hitRate"]),
            )
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对比时钟周期和触发周期")
    parser.add_argument("--limit", default="400")
    parser.add_argument("--days", type=int, default=8, help="含 1 天预热，比较的是其后的天数")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--out", default="docs/backtest-trigger.md")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    limit = parse_limit(args.limit)
    if limit is None:
        limit = 600
    client = BinanceClient()
    symbols, requested, failed = load_universe(client, limit, args.days, args.workers)
    if not symbols:
        print("没有拉到可用历史")
        return 1
    now = int(time.time() * 1000)
    earliest = min(min(row["timestamp"] for row in item["oi"]) for item in symbols)
    eval_from = earliest + WARMUP_MS
    eval_until = now
    clock, rows = evaluate(symbols, eval_from, eval_until)
    choice = recommend(clock, rows)
    meta = {
        "requested": requested,
        "used": len(symbols),
        "failed": failed,
        "days": args.days,
        "evalFrom": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(eval_from / 1000)),
        "evalUntil": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(eval_until / 1000)),
        "evalDays": (eval_until - eval_from) / DAY_MS,
    }
    text = render(clock, rows, meta, choice)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
