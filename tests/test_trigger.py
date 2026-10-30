import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oi_monitor.broadcast import BroadcastBook, compose, default_rules
from oi_monitor.metrics import TICK_MS, build_row
from oi_monitor.trigger import (
    continuation_label,
    index_klines,
    match_earlier,
    oi_anomaly,
    price_anomaly,
    simulate_trigger,
    trigger_cycle_view,
)

ORIGIN = 1_700_000_000_000
ORIGIN -= ORIGIN % TICK_MS


def _series(values, start=ORIGIN, price=100.0):
    rows = []
    for index, amount in enumerate(values):
        rows.append(
            {
                "timestamp": start + index * TICK_MS,
                "oi": amount / price,
                "oiValue": amount,
                "supply": 0,
            }
        )
    return rows


def _klines(count, close=100.0, volume=10.0, start=ORIGIN):
    return [
        {"timestamp": start + index * TICK_MS, "close": close, "quoteVolume": volume}
        for index in range(count)
    ]


def _params(**overrides):
    values = {
        "pricePct": 1.5,
        "volumeMult": 2.0,
        "oiPct": 3.0,
        "oiPctLarge": 2.0,
        "oiLargeUsd": 10_000_000,
        "minQuoteVolume": 5_000_000,
        "mode": "trigger",
        "rearmMs": 30 * 60_000,
    }
    values.update(overrides)
    return values


class TriggerRuleTest(unittest.TestCase):
    def test_price_anomaly_needs_both_deviation_and_volume(self):
        bars = {bar["timestamp"]: bar for bar in _klines(21)}
        stamp = ORIGIN + 20 * TICK_MS
        bars[stamp] = {"timestamp": stamp, "close": 101.5, "quoteVolume": 20}
        self.assertTrue(price_anomaly(bars, stamp, 1.5, 2))
        bars[stamp]["close"] = 101.4
        self.assertFalse(price_anomaly(bars, stamp, 1.5, 2))
        bars[stamp]["close"] = 101.5
        bars[stamp]["quoteVolume"] = 19
        self.assertFalse(price_anomaly(bars, stamp, 1.5, 2))
        self.assertFalse(price_anomaly(bars, stamp - 20 * TICK_MS, 1.5, 2))

    def test_oi_anomaly_uses_notional_and_lowers_the_gate_above_10m(self):
        points = {
            ORIGIN: {"oiValue": 1_000_000, "oi": 10},
            ORIGIN + TICK_MS: {"oiValue": 1_030_000, "oi": 10},
        }
        self.assertTrue(oi_anomaly(points, ORIGIN + TICK_MS, 3, 2, 10_000_000))
        points[ORIGIN + TICK_MS]["oiValue"] = 1_029_000
        self.assertFalse(oi_anomaly(points, ORIGIN + TICK_MS, 3, 2, 10_000_000))

        large = {
            ORIGIN: {"oiValue": 10_000_000, "oi": 1},
            ORIGIN + TICK_MS: {"oiValue": 10_200_000, "oi": 1},
        }
        self.assertTrue(oi_anomaly(large, ORIGIN + TICK_MS, 3, 2, 10_000_000))
        large[ORIGIN + TICK_MS]["oiValue"] = 10_190_000
        self.assertFalse(oi_anomaly(large, ORIGIN + TICK_MS, 3, 2, 10_000_000))

        count_only = {
            ORIGIN: {"oiValue": 1_000_000, "oi": 10},
            ORIGIN + TICK_MS: {"oiValue": 1_000_000, "oi": 30},
        }
        self.assertFalse(oi_anomaly(count_only, ORIGIN + TICK_MS, 3, 2, 10_000_000))

    def test_either_condition_starts_tick_one_off_the_clock(self):
        series = _series([1_000_000] * 21)
        bars = _klines(21)
        stamp = ORIGIN + 20 * TICK_MS
        bars[-1] = {"timestamp": stamp, "close": 101.5, "quoteVolume": 20}
        view = trigger_cycle_view(series, bars, stamp + TICK_MS, _params(), quote_volume_24h=6_000_000)
        self.assertEqual(view["start"], stamp)
        self.assertEqual(view["trigger"], "price")
        self.assertFalse(view["ready"])
        self.assertNotEqual(view["start"] % (30 * 60_000), 0)

        quiet = _klines(2)
        jumped = _series([1_000_000, 1_030_000])
        oi_view = trigger_cycle_view(jumped, quiet, ORIGIN + 2 * TICK_MS, _params(), quote_volume_24h=6_000_000)
        self.assertEqual(oi_view["start"], ORIGIN + TICK_MS)
        self.assertEqual(oi_view["trigger"], "oi")

    def test_low_volume_coins_and_rearm_window_do_not_retrigger(self):
        amounts = [1_000_000] * 40
        for index in (20, 22, 26):
            amounts[index] = 1_040_000
            if index:
                amounts[index - 1] = 1_000_000
        series = _series(amounts)
        bars = _klines(40)
        params = _params()
        blocked = trigger_cycle_view(series, bars, ORIGIN + 23 * TICK_MS, params, quote_volume_24h=6_000_000)
        self.assertEqual(blocked["start"], ORIGIN + 20 * TICK_MS)
        nxt = trigger_cycle_view(series, bars, ORIGIN + 27 * TICK_MS, params, quote_volume_24h=6_000_000)
        self.assertEqual(nxt["start"], ORIGIN + 26 * TICK_MS)
        quiet = trigger_cycle_view(series, bars, ORIGIN + 23 * TICK_MS, params, quote_volume_24h=4_000_000)
        self.assertIsNone(quiet["start"])
        self.assertFalse(quiet["ready"])

    def test_tick_five_uses_first_and_fifth_only_and_waits_for_the_bar_to_close(self):
        start = ORIGIN + 20 * TICK_MS
        amounts = [1_000_000] * 20 + [1_030_000, 9_000_000, 1_000_000, 8_000_000, 1_200_000]
        series = _series(amounts, price=100)
        bars = _klines(len(amounts))
        partial = [row for row in series if row["timestamp"] <= start + 3 * TICK_MS]
        too_soon = trigger_cycle_view(partial, bars, start + 20 * 60_000, _params(), quote_volume_24h=6_000_000)
        self.assertEqual(too_soon["start"], start)
        self.assertFalse(too_soon["ready"])
        self.assertEqual(too_soon["count"], 4)
        early_point = trigger_cycle_view(series, bars, start + 20 * 60_000, _params(), quote_volume_24h=6_000_000)
        self.assertFalse(early_point["ready"])

        ready = trigger_cycle_view(series, bars, start + 25 * 60_000, _params(), quote_volume_24h=6_000_000)
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["count"], 5)
        self.assertAlmostEqual(ready["oiValuePct"], 16.504854, places=4)
        self.assertEqual(ready["first"], 1_030_000)
        self.assertEqual(ready["fifth"], 1_200_000)

        amounts[22] = None
        gapped = _series([value if value is not None else 1_030_000 for value in amounts])
        # Remove the middle tick entirely so the cycle cannot be verified.
        gapped = [row for row in gapped if row["timestamp"] != start + 2 * TICK_MS]
        missing = trigger_cycle_view(gapped, bars, start + 25 * 60_000, _params(), quote_volume_24h=6_000_000)
        self.assertFalse(missing["ready"])
        self.assertEqual(missing["count"], 4)

    def test_flat_open_interest_after_a_price_trigger_does_not_broadcast(self):
        bars = _klines(30)
        stamp = ORIGIN + 20 * TICK_MS
        bars[20] = {"timestamp": stamp, "close": 103, "quoteVolume": 40}
        series = _series([8_000_000] * 30)
        found = simulate_trigger(series, bars, _params(), symbol="BTCUSDT", quote_volume_24h=9_000_000)
        self.assertEqual(found, [])
        amounts = [8_000_000] * 25
        amounts[24] = 9_600_000
        series = _series(amounts, price=100)
        # Keep implied price flat so the move is only notional.
        for row, amount in zip(series, amounts):
            row["oi"] = amount / 100
        found = simulate_trigger(series, bars, _params(), symbol="BTCUSDT", quote_volume_24h=9_000_000)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["start"], stamp)
        self.assertEqual(found[0]["reason"], "price")
        self.assertAlmostEqual(found[0]["oiValuePct"], 20.0, places=4)

    def test_broadcast_text_still_uses_the_existing_gate(self):
        start = ORIGIN + 20 * TICK_MS
        amounts = [6_000_000] * 20 + [6_200_000, 6_200_000, 6_200_000, 6_200_000, 7_440_000]
        series = _series(amounts, price=100)
        series[-1]["oi"] = 7_440_000 / 110
        bars = _klines(len(amounts))
        bars[20] = {"timestamp": start, "close": 102, "quoteVolume": 30}
        view = trigger_cycle_view(series, bars, start + 25 * 60_000, _params(), quote_volume_24h=8_000_000)
        self.assertTrue(view["ready"])
        row = {
            "symbol": "ETHUSDT",
            "base": "ETH",
            "oiValue": view["fifth"],
            "oiToMarketCapPct": 1.2,
            "priceChangePercent": 3,
            "cycle": view,
            "changes": {},
        }
        text = compose(row, default_rules()[0])
        self.assertIn("🟢 [ETHUSDT]", text)
        self.assertIn("优先推报，做多信号", text)
        self.assertIn("第 1 次刻度", text)
        self.assertIn("第 5 次刻度", text)

    def test_clock_mode_does_not_use_klines(self):
        ticker = {"lastPrice": "50", "priceChangePercent": "-1.5", "quoteVolume": "9000000"}
        series = _series([1_000_000 + index * 10_000 for index in range(30)])
        bars = _klines(30)
        bars[-1]["close"] = 180
        bars[-1]["quoteVolume"] = 500
        clock = build_row("BTCUSDT", "BTC", 1, ticker, series, klines=bars, cycle_params={"mode": "clock"})
        triggered = build_row(
            "BTCUSDT",
            "BTC",
            1,
            ticker,
            series,
            klines=bars,
            cycle_params=_params(),
            now_ms=series[-1]["timestamp"] + TICK_MS,
        )
        self.assertEqual(clock["cycle"]["mode"], "clock")
        self.assertEqual(triggered["cycle"]["mode"], "trigger")
        self.assertNotEqual(clock["cycle"]["start"], triggered["cycle"]["start"])


class TriggerScoreTest(unittest.TestCase):
    def test_hit_rate_direction_and_censoring(self):
        start = ORIGIN
        bars = _klines(20, close=100)
        ref = start + 4 * TICK_MS
        for step, close in enumerate((101, 101, 103), start=1):
            bars[4 + step]["close"] = close
        hit = {"start": start, "pricePct": 1.0, "code": "long", "symbol": "BTCUSDT"}
        self.assertEqual(continuation_label(hit, index_klines(bars)), "hit")
        for step in range(1, 13):
            bars[4 + step]["close"] = 101
        self.assertEqual(continuation_label(hit, index_klines(bars)), "miss")
        flat = dict(hit, pricePct=0.1)
        self.assertEqual(continuation_label(flat, index_klines(bars)), "flat")
        short = {"start": start, "pricePct": -1.0, "code": "abandon", "symbol": "BTCUSDT"}
        for step in range(1, 13):
            bars[4 + step]["close"] = 97
        self.assertEqual(continuation_label(short, index_klines(bars)), "hit")
        horizon_end = ref + 12 * TICK_MS
        bars = [bar for bar in bars if bar["timestamp"] != horizon_end]
        self.assertEqual(continuation_label(short, index_klines(bars)), "censored")

    def test_same_move_is_measured_against_the_clock_broadcast(self):
        clock = [
            {"symbol": "BTCUSDT", "signalAt": 1_000_000, "oiValuePct": 6},
            {"symbol": "ETHUSDT", "signalAt": 2_000_000, "oiValuePct": -4},
        ]
        trigger = [
            {"symbol": "BTCUSDT", "signalAt": 1_000_000 - 10 * 60_000, "oiValuePct": 8},
            {"symbol": "ETHUSDT", "signalAt": 2_000_000 + 5 * 60_000, "oiValuePct": -3},
            {"symbol": "ETHUSDT", "signalAt": 2_000_000 - 3 * 60 * 60_000, "oiValuePct": -9},
        ]
        paired = match_earlier(clock, trigger)
        self.assertEqual(paired["matched"], 2)
        self.assertEqual(paired["medianMin"], 2.5)


class TriggerSettingsTest(unittest.TestCase):
    def test_env_overrides_saved_cycle_mode_at_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "settings.json").write_text('{"cycleMode": "clock", "triggerPricePct": 1.5}', encoding="utf-8")
            with patch.dict(os.environ, {"OI_CYCLE_MODE": "trigger", "OI_TRIGGER_PRICE_PCT": "2.5"}):
                book = BroadcastBook(path)
            self.assertEqual(book.cycle_params()["mode"], "trigger")
            self.assertEqual(book.cycle_params()["pricePct"], 2.5)
            self.assertEqual(book.public_settings()["cycleMode"], "trigger")
            with patch.dict(os.environ, {"OI_CYCLE_MODE": "sideways", "OI_TRIGGER_OI_PCT": "nope"}):
                with self.assertLogs("oi-monitor", level="WARNING"):
                    ignored = BroadcastBook(path)
            self.assertEqual(ignored.cycle_params()["mode"], "clock")
            self.assertEqual(ignored.cycle_params()["oiPct"], 3.0)


if __name__ == "__main__":
    unittest.main()
