"""Bounded, observable validation without private recordings or NAS access."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
P = importlib.import_module("person_timelapse")


class VideoValidationTests(unittest.TestCase):
    metadata = {"format": {"duration": "1.5"},
                "streams": [{"codec_type": "video", "width": 1280, "height": 720}]}

    def test_library_bool_contract_uses_short_bounded_read(self):
        with patch.object(P, "scan_media_process", return_value=(json.dumps(self.metadata).encode(), "")) as read:
            self.assertTrue(P.valid_video(Path("generated.mp4")))
        self.assertEqual(read.call_args.args[2], 30)
        with patch.object(P, "scan_media_process", side_effect=RuntimeError("ffprobe: invalid data")):
            self.assertFalse(P.valid_video(Path("generated.mp4")))

    def test_export_validation_has_heartbeat_and_configurable_finite_limit(self):
        heartbeat = MagicMock()
        with patch.dict(P.os.environ, {"VIDEO_VALIDATION_TIMEOUT_SECONDS": "180"}), \
                patch.object(P, "scan_media_process", return_value=(json.dumps(self.metadata).encode(), "")) as read:
            self.assertTrue(P.valid_video(Path("generated.mp4"), heartbeat, raise_errors=True))
        self.assertIs(read.call_args.args[1], heartbeat)
        self.assertEqual(read.call_args.args[2], 180)
        for value in ("0", "-1", "nan", "inf", "invalid"):
            with self.subTest(value=value), patch.dict(P.os.environ, {"VIDEO_VALIDATION_TIMEOUT_SECONDS": value}), \
                    patch.object(P, "scan_media_process") as read:
                with self.assertRaisesRegex(RuntimeError, "VIDEO_VALIDATION_TIMEOUT_SECONDS"):
                    P.valid_video(Path("generated.mp4"), heartbeat, raise_errors=True)
                read.assert_not_called()

    def test_invalid_metadata_has_specific_reason_and_is_never_valid(self):
        cases = [(b"not json", "JSON"), (b"[]", "结构"),
                 (b'{"format":{"duration":"NaN"},"streams":[]}', "时长"),
                 (b'{"format":{"duration":"Infinity"},"streams":[]}', "时长"),
                 (b'{"format":{"duration":"0"},"streams":[]}', "时长"),
                 (b'{"format":{"duration":"1"},"streams":[{"codec_type":"audio"}]}', "视频流"),
                 (b'{"format":{"duration":"1"},"streams":[{"codec_type":"video","width":1280,"height":0}]}', "视频流")]
        for payload, reason in cases:
            with self.subTest(payload=payload), patch.object(P, "scan_media_process", return_value=(payload, "")):
                self.assertFalse(P.valid_video(Path("generated.mp4")))
                with self.assertRaisesRegex(RuntimeError, reason):
                    P.valid_video(Path("generated.mp4"), lambda: None, raise_errors=True)

    def test_nonzero_probe_exit_preserves_bounded_stderr(self):
        process = MagicMock()
        process.poll.return_value = process.returncode = 1
        process.communicate.return_value = (b"", None)

        def start(command, **kwargs):
            kwargs["stderr"].write(b"x" * 5000 + b"\nmoov atom not found\xff")
            return process

        with patch.object(P.subprocess, "Popen", side_effect=start):
            with self.assertRaisesRegex(RuntimeError, "ffprobe.*退出码 1.*moov atom not found") as error:
                P.valid_video(Path("generated.mp4"), lambda: None, raise_errors=True)
        self.assertLess(len(str(error.exception)), 2300)
        process.stdout.close.assert_called_once()

    def test_slow_probe_heartbeats_then_succeeds_after_old_thirty_second_limit(self):
        process, heartbeat = MagicMock(), MagicMock()
        process.poll.return_value = process.returncode = 0
        process.communicate.side_effect = [subprocess.TimeoutExpired("ffprobe", 2),
                                          (json.dumps(self.metadata).encode(), None)]
        with patch.object(P.subprocess, "Popen", return_value=process), \
                patch.object(P.time, "monotonic", side_effect=[0, 0, 35]):
            self.assertTrue(P.valid_video(Path("generated.mp4"), heartbeat, raise_errors=True))
        heartbeat.assert_called_once()
        process.kill.assert_not_called()

    def test_timeout_and_user_interrupt_reap_probe(self):
        for interruption in (False, True):
            process, heartbeat = MagicMock(), MagicMock()
            process.poll.return_value = None
            process.communicate.side_effect = KeyboardInterrupt() if interruption else subprocess.TimeoutExpired("ffprobe", 2)
            with self.subTest(interruption=interruption), patch.object(P.subprocess, "Popen", return_value=process), \
                    patch.dict(P.os.environ, {"VIDEO_VALIDATION_TIMEOUT_SECONDS": "3"}), \
                    patch.object(P.time, "monotonic", side_effect=[0, 0, 2, 4]):
                if interruption:
                    with self.assertRaises(KeyboardInterrupt):
                        P.valid_video(Path("generated.mp4"), heartbeat, raise_errors=True)
                else:
                    with self.assertRaisesRegex(RuntimeError, "超时.*3"):
                        P.valid_video(Path("generated.mp4"), heartbeat, raise_errors=True)
                    self.assertEqual(heartbeat.call_count, 2)
            process.kill.assert_called_once()
            process.wait.assert_called_once()
            process.stdout.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
