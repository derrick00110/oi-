import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oi_monitor.memory import SHANGHAI, DayMemory, shanghai_parts
from oi_monitor.replay import hits_from_series


def epoch_ms(year, month, day, hour, minute, second=0):
    moment = datetime(year, month, day, hour, minute, second, tzinfo=SHANGHAI)
    return int(moment.timestamp() * 1000)


class MemoryTest(unittest.TestCase):
    def test_day_follows_shanghai(self):
        late = datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc)
        day, clock = shanghai_parts(int(late.timestamp() * 1000))
        self.assertEqual(day, "2026-10-26")
        self.assertEqual(clock, "00:00:00")

    def test_backup_keeps_every_hit_and_does_not_repeat_a_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = DayMemory(Path(directory))
            at = epoch_ms(2026, 10, 25, 14, 31, 5)
            hit = {
                "symbol": "AKEUSDT",
                "snapshotAt": 1000,
                "oiValue": 18_150_000,
                "move": 17.4,
                "pricePct": 14.4,
                "fact": "优先推报，做多信号",
                "text": "🟢 [AKEUSDT] 币安未平仓金额 +17.4%，优先推报，做多信号",
                "window": "1h",
            }
            announced = [{"id": 3, "at": at, "symbol": "AKEUSDT", "text": hit["text"], "fact": hit["fact"]}]
            memory.record_scan([hit], announced, at, 400, 398)
            memory.record_scan([hit], announced, at + 1000, 400, 398)
            memory.record_failure(at + 2000, "网络中断")

            text = (Path(directory) / "2026-10-25.md").read_text(encoding="utf-8")
            self.assertEqual(text.count("优先推报，做多信号"), 2)
            self.assertIn("| 14:31:05 | AKEUSDT | +17.4% | 1815 万美元 | +14.4% | 优先推报，做多信号 | 已播 |", text)
            self.assertIn("- 14:31:05 🟢 [AKEUSDT]", text)
            self.assertIn("拉到 398/400，越过门槛 1 个", text)
            self.assertIn("扫描失败：网络中断", text)
            self.assertEqual(memory.backfill_broadcasts(announced), 0)

            other = epoch_ms(2026, 10, 26, 0, 5)
            quiet = dict(hit, snapshotAt=2000, move=-15, fact="弃盘行情，需观望", text="⚪ [AKEUSDT] 弃盘行情，需观望")
            memory.record_scan([quiet], [], other, 400, 398)
            next_day = (Path(directory) / "2026-10-26.md").read_text(encoding="utf-8")
            self.assertIn("未播", next_day)
            self.assertIn("这一天还没有播报。", next_day)
            index = (Path(directory) / "index.md").read_text(encoding="utf-8")
            self.assertIn("[2026-10-25](2026-10-25.md)：播报 1 条，越过门槛 1 条，扫描 3 次", index)
            self.assertIn("[2026-10-26](2026-10-26.md)：播报 0 条，越过门槛 1 条，扫描 1 次", index)

    def test_replay_fills_moves_the_live_list_never_kept(self):
        step = 5 * 60 * 1000
        start = 1_760_000_000_000
        start -= start % (30 * 60 * 1000)
        series = []
        for index, value in enumerate([4_000_000, 3_900_000, 4_200_000, 4_100_000, 4_800_000]):
            series.append({"timestamp": start + index * step, "oi": 100, "oiValue": value, "supply": 0})
        hits = hits_from_series("AKEUSDT", "AKE", series)
        self.assertGreaterEqual(len(hits), 1)
        self.assertTrue(all(hit["source"] == "replay" for hit in hits))
        self.assertAlmostEqual(hits[0]["move"], 20.0, places=4)
        quiet = [{"timestamp": start + index * step, "oi": 100, "oiValue": 4_000_000, "supply": 0} for index in range(20)]
        self.assertEqual(hits_from_series("AKEUSDT", "AKE", quiet), [])

        with tempfile.TemporaryDirectory() as directory:
            memory = DayMemory(Path(directory))
            self.assertEqual(memory.add_hits(hits), len(hits))
            self.assertEqual(memory.add_hits(hits), 0)
            day, _clock = shanghai_parts(hits[0]["at"])
            text = (Path(directory) / f"{day}.md").read_text(encoding="utf-8")
            self.assertIn("回放", text)
            self.assertIn("AKEUSDT", text)


if __name__ == "__main__":
    unittest.main()
