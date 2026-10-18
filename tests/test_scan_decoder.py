"""Sparse scan decoding, timestamp fidelity, progress and cancellation contracts."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
P = importlib.import_module("person_timelapse")


class ScanDecoderTests(unittest.TestCase):
    def test_mac_missing_codec_tools_exits_before_claiming_priority_or_writing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "PATH": "", "INPUT_ROOT": directory, "OUTPUT_ROOT": directory,
                           "PROGRESS_PATH": str(Path(directory) / "progress.json")}
            result = subprocess.run(["/bin/sh", str(Path(__file__).parents[1] / "mac-worker.sh"), "20000101"],
                                    env=environment, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn("ffprobe", result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])

    @contextmanager
    def image_runtime(self):
        frame = object()
        cv = SimpleNamespace(IMREAD_COLOR=1, imdecode=lambda *args: frame,
                             COLOR_BGR2GRAY=2, cvtColor=lambda *args: frame,
                             resize=lambda *args: frame, absdiff=lambda *args: frame, mean=lambda *args: [10])
        np = SimpleNamespace(uint8=object(), frombuffer=lambda *args: b"pixels")
        with patch.dict(sys.modules, {"cv2": cv, "numpy": np}):
            yield frame

    def test_sparse_decode_skips_inter_frames_before_input_and_uses_actual_pts(self):
        info = "[Parsed_showinfo_1] n: 0 pts: 900 pts_time:0.9 duration:1"
        with self.image_runtime() as frame, patch.object(P, "scan_media_process", return_value=(b"jpeg", info)) as run:
            result, actual = P.decode_scan_frame(Path("example.mp4"), 120, 180, 256, lambda: None, "keyframes")
        command = run.call_args.args[0]
        self.assertIs(result, frame)
        self.assertAlmostEqual(actual, 120.9)
        self.assertLess(command.index("-skip_frame"), command.index("-i"))
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertIn("scale=", command[command.index("-vf") + 1])
        self.assertNotIn("fps=", command[command.index("-vf") + 1])
        self.assertEqual(command[command.index("-frames:v") + 1], "1")

    def test_missing_final_keyframe_gets_one_exact_tail_read(self):
        with self.image_runtime(), patch.object(P, "scan_media_process", side_effect=[
                (b"", ""), (b"jpeg", "n: 0 pts: 0 pts_time:0")]) as run:
            _, actual = P.decode_scan_frame(Path("example.mp4"), 7, 8, 256, lambda: None, "keyframes")
        self.assertEqual(actual, 7)
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("-skip_frame", run.call_args.args[0])
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index("-ss") + 1], "7")

    def test_missing_timestamp_is_failure_not_a_fabricated_event_time(self):
        with self.image_runtime(), patch.object(P, "scan_media_process", return_value=(b"jpeg", "")):
            with self.assertRaisesRegex(RuntimeError, "时间戳"):
                P.decode_scan_frame(Path("example.mp4"), 0, 8, 256, lambda: None, "keyframes")

    def test_timeout_heartbeats_but_always_kills_and_reaps_child(self):
        process, heartbeat = MagicMock(), MagicMock()
        process.poll.return_value = None
        process.communicate.side_effect = subprocess.TimeoutExpired("ffmpeg", 2)
        with patch.object(P.subprocess, "Popen", return_value=process), \
                patch.object(P.time, "monotonic", side_effect=[0, 0, 2, 4]):
            with self.assertRaisesRegex(RuntimeError, "超时"):
                P.scan_media_process(["ffmpeg"], heartbeat, 3)
        self.assertEqual(heartbeat.call_count, 2)
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        process.stdout.close.assert_called_once()

    def test_interrupt_reaps_decoder_without_marking_file_completed(self):
        process = MagicMock()
        process.poll.return_value = None
        process.communicate.side_effect = KeyboardInterrupt()
        with patch.object(P.subprocess, "Popen", return_value=process), self.assertRaises(KeyboardInterrupt):
            P.scan_media_process(["ffmpeg"], lambda: None, 90)
        process.kill.assert_called_once()
        process.wait.assert_called_once()

    def test_nonzero_decoder_exit_with_image_data_is_never_accepted(self):
        process = MagicMock()
        process.poll.return_value = process.returncode = 234
        process.communicate.return_value = (b"incomplete jpeg", None)
        with patch.object(P.subprocess, "Popen", return_value=process), self.assertRaises(RuntimeError):
            P.scan_media_process(["ffmpeg"], lambda: None, 90, allow_empty=True)

    def test_empty_keyframe_encoder_failure_can_trigger_exact_tail_fallback(self):
        process = MagicMock()
        process.poll.return_value = process.returncode = 234
        process.communicate.return_value = (b"", None)
        with patch.object(P.subprocess, "Popen", return_value=process):
            self.assertEqual(P.scan_media_process(["ffmpeg"], lambda: None, 90, allow_empty=True), (b"", ""))

    def test_auto_modes_and_old_checkpoint_compatibility(self):
        with patch.dict(os.environ, {"SCAN_DECODE_MODE": "auto"}):
            fast = P.scan_settings(120, .45, 20, 8, 60, 256)
            precise = P.scan_settings(5, .45, 20, 8, 60, 256)
        self.assertEqual(fast["decode_mode"], "keyframes")
        self.assertEqual(precise["decode_mode"], "accurate")
        old = {key: value for key, value in fast.items() if key != "decode_mode"}
        self.assertTrue(P.settings_satisfied({"settings": old}, fast))
        self.assertFalse(P.settings_satisfied({"settings": fast}, {**fast, "decode_mode": "accurate"}))
        self.assertTrue(P.settings_satisfied({"settings": {**fast, "decode_mode": "accurate"}}, fast))

    def test_progress_corrects_duration_counts_and_events_use_frame_time(self):
        start = datetime(2000, 1, 1)
        record = {"path": Path("example.mp4"), "size": 1, "camera": "0", "start": start,
                  "end": start + timedelta(seconds=600)}
        snapshots, results = [], []
        yolo = SimpleNamespace(YOLO=lambda _: lambda *args, **kwargs: [SimpleNamespace(boxes=[1])])
        with tempfile.TemporaryDirectory() as directory, self.image_runtime() as frame, \
                patch.dict(sys.modules, {"ultralytics": yolo}), \
                patch.object(P, "probe_video_duration", return_value=125), \
                patch.object(P, "decode_scan_frame", side_effect=[(frame, 1.5), (frame, 122)]), \
                patch.object(P, "save_thumbnail", return_value=Path(directory) / "x.jpg"), \
                patch.object(P, "report_progress", side_effect=lambda **values: snapshots.append(values)):
            events = P.scan([record], Path(directory), 120, .45, 0,
                            checkpoint=lambda *args: results.append(args))
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0][-1])
        final = snapshots[-1]
        self.assertEqual((final["samples"], final["estimated_samples"], final["duration_adjusted_files"]), (2, 2, 1))
        self.assertEqual(final["completed_files"], 1)
        self.assertIn("decode_seconds", final)
        self.assertIn("inference_seconds", final)
        self.assertTrue(any(item.get("step") == "inference" for item in snapshots))
        self.assertEqual(events["0"][1]["start"], (start + timedelta(seconds=117)).isoformat(timespec="seconds"))

    def test_read_error_records_failure_and_continues_next_file(self):
        start = datetime(2000, 1, 1)
        records = [{"path": Path(f"example{n}.mp4"), "size": 1, "camera": "0",
                    "start": start + timedelta(minutes=n), "end": start + timedelta(minutes=n + 1)} for n in range(2)]
        saved = []
        yolo = SimpleNamespace(YOLO=lambda _: lambda *args, **kwargs: [SimpleNamespace(boxes=[])])
        with tempfile.TemporaryDirectory() as directory, self.image_runtime() as frame, \
                patch.dict(sys.modules, {"ultralytics": yolo}), \
                patch.object(P, "probe_video_duration", return_value=60), \
                patch.object(P, "decode_scan_frame", side_effect=[RuntimeError("timeout"), (frame, 0)]):
            P.scan(records, Path(directory), 120, .45, 20, checkpoint=lambda *args: saved.append(args))
        self.assertEqual([item[-1] for item in saved], ["timeout", None])


if __name__ == "__main__":
    unittest.main()
