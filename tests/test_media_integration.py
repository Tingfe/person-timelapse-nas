"""Opt-in real codec tests, using generated pictures only (never personal footage)."""
import importlib
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
P = importlib.import_module("person_timelapse")
W = importlib.import_module("web_server")


@unittest.skipUnless(os.environ.get("RUN_MEDIA_INTEGRATION") == "1", "requires cv2 and FFmpeg")
class MediaIntegrationTests(unittest.TestCase):
    def setUp(self):
        import cv2
        import numpy as np
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.records = []
        for day in (1, 2):
            file = self.root / f"0_202401{day:02}000000_202401{day:02}000004.mp4"
            writer = cv2.VideoWriter(str(file), cv2.VideoWriter_fourcc(*"mp4v"), 10, (160, 90))
            self.assertTrue(writer.isOpened())
            for frame in range(40):
                picture = np.full((90, 160, 3), (day * 70, frame * 5, 80), dtype=np.uint8)
                writer.write(picture)
            writer.release()
            self.records.append(P.parse_record(file))

    def test_multi_day_streaming_export_and_library(self):
        events = [(r["start"], r["end"]) for r in self.records]
        # Deliberate duplicates also verify overlap deduplication and ordering.
        with patch.object(P, "report_progress") as progress:
            self.assertEqual(P.export(list(reversed(self.records)) * 2, events, self.output, "0", "20240101-20240102", 1, 4), 0)
        muxed = [call.kwargs for call in progress.call_args_list if call.kwargs.get("phase") == "encoding"]
        # FFmpeg 6.1 omits frame statistics during streamcopy. Its media clock
        # must still advance; ffprobe below verifies the actual final 8 frames.
        self.assertTrue(any(item.get("mux_seconds", 0) > 0 and item.get("mux_bytes", 0) > 0 for item in muxed), muxed)
        self.assertTrue(all(item.get("mux_frames") is None or 0 <= item["mux_frames"] <= 8 for item in muxed), muxed)
        self.assertTrue(all(90 <= item["percent"] <= 99 for item in muxed))
        target = self.output / "people-timelapse-0-20240101-20240102.mp4"
        self.assertTrue(P.valid_video(target))
        self.assertEqual(json.loads(target.with_suffix(".json").read_text())["frames"], 8)
        stream = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height,nb_frames,duration", "-of", "json", str(target)],
                            capture_output=True, text=True, check=True).stdout)["streams"][0]
        self.assertEqual((stream["width"], stream["height"], int(stream["nb_frames"])), (1280, 720, 8))
        self.assertAlmostEqual(float(stream["duration"]), 2, places=2)
        with patch.object(W, "OUTPUT_ROOT", self.output):
            self.assertEqual(len(W.exports_for_date("20240102")), 1)

    def test_real_encoder_validation_failure_preserves_old_export(self):
        events = [(self.records[0]["start"], self.records[0]["end"])]
        P.export(self.records, events, self.output, "0", "20240101", 1, 4)
        target = self.output / "people-timelapse-0-20240101.mp4"
        original = target.read_bytes()
        with patch.object(P, "valid_video", return_value=False), self.assertRaises(RuntimeError):
            P.export(self.records, events, self.output, "0", "20240101", 1, 4)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.output.glob("*.partial.mp4")), [])

    def test_scan_failures_are_disclosed_in_playable_export(self):
        record = self.records[0]
        self.assertEqual(P.export(self.records, [(record["start"], record["end"])],
                                 self.output, "0", "20240101", 1, 4, scan_failed_files=1), 2)
        with patch.object(W, "OUTPUT_ROOT", self.output):
            exported = W.timelapses()[0]
            self.assertEqual(exported["status"], "partial")
            self.assertEqual(exported["scan_failed_files"], 1)

    def test_real_interrupt_resume_and_cross_range_reuse(self):
        events = [(r["start"], r["end"]) for r in self.records]
        decoder = P.decode_export_frames

        def interrupt_second(path, *args, **kwargs):
            if path == self.records[1]["path"]:
                raise KeyboardInterrupt()
            return decoder(path, *args, **kwargs)

        with patch.object(P, "decode_export_frames", side_effect=interrupt_second), self.assertRaises(KeyboardInterrupt):
            P.export(self.records, events, self.output, "0", "20240101-20240102", 1, 4)
        self.assertEqual(len(list((self.output / ".export-cache/v1").glob("*.mp4"))), 1)
        with patch.object(P, "decode_export_frames", wraps=decoder) as decode:
            self.assertEqual(P.export(self.records, events, self.output, "0", "20240101-20240102", 1, 4), 0)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(decode.call_args.args[0], self.records[1]["path"])
        metadata = json.loads((self.output / "people-timelapse-0-20240101-20240102.json").read_text())
        self.assertEqual((metadata["frames"], metadata["reused_files"]), (8, 1))
        with patch.object(P, "decode_export_frames", side_effect=AssertionError("should reuse real fragment")):
            self.assertEqual(P.export(self.records[:1], events[:1], self.output, "0", "20240101", 1, 4), 0)
        self.assertTrue(P.valid_video(self.output / "people-timelapse-0-20240101.mp4"))

    def test_real_duration_shorter_than_filename_is_corrected_without_false_failure(self):
        record = {**self.records[0], "end": self.records[0]["start"] + timedelta(seconds=10)}
        self.assertEqual(P.export([record], [(record["start"], record["end"])],
                                 self.output, "0", "20240101", 1, 4), 0)
        metadata = json.loads((self.output / "people-timelapse-0-20240101.json").read_text())
        self.assertEqual((metadata["frames"], metadata["status"], metadata["duration_adjusted_files"]),
                         (4, "completed", 1))
        self.assertAlmostEqual(metadata["duration_adjustments"][0]["trimmed_event_seconds"], 6, places=1)
        with patch.object(W, "OUTPUT_ROOT", self.output):
            self.assertEqual(W.timelapses()[0]["duration_adjusted_files"], 1)

    def test_mixed_aspect_ratios_produce_consistent_reusable_fragments(self):
        import cv2
        import numpy as np
        path = self.records[1]["path"]
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (96, 160))
        self.assertTrue(writer.isOpened())
        for _ in range(40):
            writer.write(np.full((160, 96, 3), (20, 80, 160), dtype=np.uint8))
        writer.release()
        events = [(r["start"], r["end"]) for r in self.records]
        self.assertEqual(P.export(self.records, events, self.output, "0", "20240101-20240102", 1, 4), 0)
        target = self.output / "people-timelapse-0-20240101-20240102.mp4"
        result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(target), "-f", "null", "-"],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertEqual(result.stderr, b"")
        self.assertEqual(json.loads(target.with_suffix(".json").read_text())["frames"], 8)

    def test_hevc_keyframe_sampling_preserves_tail_and_accurate_mode(self):
        import cv2
        import numpy as np
        file = self.root / "long-gop.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=160x96:rate=10:duration=8", "-c:v", "libx265", "-preset", "ultrafast",
                        "-x265-params", "keyint=20:min-keyint=20:scenecut=0:pools=1:frame-threads=1:log-level=error",
                        str(file)], check=True, capture_output=True, timeout=30)
        frames = P.decode_export_frames(file, 0, 1, 8, lambda: None, mode="keyframes")
        self.assertEqual(len(frames), 8)
        self.assertTrue(all(cv2.imdecode(np.frombuffer(frame, np.uint8), cv2.IMREAD_COLOR) is not None
                            for frame in frames))
        exact = P.decode_export_frames(file, 1.1, .5, 3, lambda: None, mode="accurate")
        self.assertEqual(len(exact), 3)

    def test_scan_hevc_seek_preserves_actual_pts_and_reads_tail(self):
        file = self.root / "scan-hevc.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=960x540:rate=10:duration=8", "-c:v", "libx265", "-preset", "ultrafast",
                        "-x265-params", "keyint=20:min-keyint=20:scenecut=0:pools=1:frame-threads=1:log-level=error",
                        str(file)], check=True, capture_output=True, timeout=30)
        image, actual = P.decode_scan_frame(file, 1.1, 8, 256, lambda: None, "keyframes")
        self.assertLessEqual(max(image.shape[:2]), 640)
        self.assertAlmostEqual(actual, 2, places=2)
        _, tail = P.decode_scan_frame(file, 7.1, 8, 256, lambda: None, "keyframes")
        self.assertAlmostEqual(tail, 7.1, places=2)
        _, exact = P.decode_scan_frame(file, 1.1, 8, 256, lambda: None, "accurate")
        self.assertAlmostEqual(exact, 1.1, places=2)

    def test_real_scan_checks_actual_duration_and_reuses_completed_file(self):
        record = {**self.records[0], "end": self.records[0]["start"] + timedelta(seconds=10)}
        yolo = SimpleNamespace(YOLO=lambda _: lambda *args, **kwargs: [SimpleNamespace(boxes=[1])])
        def checkpoint(record, settings, events, error):
            P.commit_result(self.output, record, settings, events, error)
        with patch.dict(sys.modules, {"ultralytics": yolo}):
            P.scan([record], self.output, 2, .45, 0, checkpoint=checkpoint)
        saved = list(P.scan_results(self.output))[0]
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(sum(event["hits"] for event in saved["events"]["0"]), 2)
        self.assertTrue(all(event["end"] <= (record["start"] + timedelta(seconds=4)).isoformat(timespec="seconds")
                            for event in saved["events"]["0"]))
        with patch.dict(sys.modules, {"ultralytics": yolo}), \
                patch.object(P, "decode_scan_frame", side_effect=AssertionError("completed file must not be decoded")):
            P.scan([record], self.output, 2, .45, 0, checkpoint=checkpoint)


if __name__ == "__main__":
    unittest.main()
