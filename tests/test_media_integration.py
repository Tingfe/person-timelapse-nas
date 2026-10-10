"""Opt-in real codec tests, using generated pictures only (never personal footage)."""
import importlib
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

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
        self.assertEqual(P.export(list(reversed(self.records)) * 2, events, self.output, "0", "20240101-20240102", 1, 4), 0)
        target = self.output / "people-timelapse-0-20240101-20240102.mp4"
        self.assertTrue(P.valid_video(target))
        self.assertEqual(json.loads(target.with_suffix(".json").read_text())["frames"], 8)
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


if __name__ == "__main__":
    unittest.main()
