"""Deterministic encoding/concat watchdog tests; no private media or real waits."""
import importlib
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
P = importlib.import_module("person_timelapse")


class ExportTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.process = MagicMock()
        self.heartbeat = MagicMock()
        clock = patch.object(P.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        environment = patch.dict(P.os.environ, {"ENCODE_TIMEOUT_SECONDS": "300",
                                 "CONCAT_STALL_SECONDS": "600", "CONCAT_TIMEOUT_SECONDS": "21600"})
        environment.start()
        self.addCleanup(environment.stop)

    def advance(self, timeout):
        self.now += 200
        raise subprocess.TimeoutExpired("generated ffmpeg", timeout)

    def test_concat_with_real_progress_can_exceed_old_five_minute_limit(self):
        def wait(timeout):
            if self.now >= 800:
                return 0
            return self.advance(timeout)
        self.process.wait.side_effect = wait
        self.assertEqual(P.wait_export_process(self.process, self.heartbeat, activity=lambda: (self.now,)), 0)
        self.assertEqual(self.now, 800)
        self.assertEqual(self.heartbeat.call_count, 5)

    def test_heartbeat_without_media_progress_does_not_reset_stall_timeout(self):
        self.process.wait.side_effect = self.advance
        with self.assertRaisesRegex(RuntimeError, "拼接.*无进展"):
            P.wait_export_process(self.process, self.heartbeat, activity=lambda: (12, 1024))
        self.assertEqual(self.now, 600)
        self.assertEqual(self.heartbeat.call_count, 3)

    def test_faststart_file_writes_count_even_when_frame_count_and_size_stay_fixed(self):
        def wait(timeout):
            if self.now >= 800:
                return 0
            return self.advance(timeout)
        self.process.wait.side_effect = wait
        self.assertEqual(P.wait_export_process(self.process, self.heartbeat,
                         activity=lambda: (12, 1024, self.now)), 0)

    def test_advancing_concat_still_has_a_finite_total_deadline(self):
        self.process.wait.side_effect = self.advance
        with patch.dict(P.os.environ, {"CONCAT_TIMEOUT_SECONDS": "1000"}), \
                self.assertRaisesRegex(RuntimeError, "拼接.*总时限"):
            P.wait_export_process(self.process, self.heartbeat, activity=lambda: self.now)
        self.assertEqual(self.now, 1000)

    def test_encoder_keeps_its_separate_timeout(self):
        self.process.wait.side_effect = self.advance
        with self.assertRaisesRegex(RuntimeError, "编码.*超时"):
            P.wait_export_process(self.process, self.heartbeat)
        self.assertEqual(self.now, 400)

    def test_invalid_limits_are_rejected_instead_of_disabling_watchdog(self):
        for name in ("CONCAT_TIMEOUT_SECONDS", "CONCAT_STALL_SECONDS", "ENCODE_TIMEOUT_SECONDS"):
            for value in ("0", "-1", "nan", "inf", "not-a-number"):
                with self.subTest(name=name, value=value), patch.dict(P.os.environ, {name: value}):
                    with self.assertRaises(ValueError):
                        P.wait_export_process(self.process, self.heartbeat,
                                              **({} if name == "ENCODE_TIMEOUT_SECONDS" else {"activity": lambda: 0}))
        self.process.wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
