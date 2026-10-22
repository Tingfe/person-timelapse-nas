"""Recover only a timestamp-proven final picture, never pad missing footage."""
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
P = importlib.import_module("person_timelapse")
JPEG = b"\xff\xd8generated final picture\xff\xd9"


class ExportTailTests(unittest.TestCase):
    def metadata(self, pts="2.9871", duration="0.040678", start="0", field="duration_time"):
        return json.dumps({"streams": [{"start_time": start}], "frames": [
            {"best_effort_timestamp_time": pts, field: duration}]}).encode()

    def recover(self, metadata=None, detail="", image=JPEG, actual="2.9871", moment=3):
        with patch.object(P, "probe_video_duration", return_value=3.0315), \
                patch.object(P, "scan_media_process", side_effect=[
                    (self.metadata() if metadata is None else metadata, detail),
                    (image, f"[Parsed_showinfo_0] n: 0 pts: {round(float(actual) * 1000000)} pts_time:{actual}")]) as read:
            frame = P.decode_export_tail(Path("generated.mp4"), moment, 1, lambda: None)
        return frame, read

    def test_recovers_real_last_frame_whose_display_interval_covers_sample(self):
        frame, read = self.recover()
        self.assertEqual(frame, JPEG)
        probe, decode = [call.args[0] for call in read.call_args_list]
        self.assertEqual(probe[0], "ffprobe")
        self.assertIn("-show_frames", probe)
        self.assertIn("-copyts", decode)
        self.assertIn("passthrough", decode)
        self.assertNotIn("fps=", decode[decode.index("-vf") + 1])
        self.assertLess(float(decode[decode.index("-ss") + 1]), 2.9871)

    def test_ffmpeg_six_packet_duration_field_and_nonzero_start_are_supported(self):
        frame, _ = self.recover(self.metadata(pts="12.9871", start="10", field="pkt_duration_time"), actual="12.9871")
        self.assertEqual(frame, JPEG)

    def test_not_attempted_for_middle_or_outside_recording(self):
        for moment in (1, 3.0315, 4):
            with self.subTest(moment=moment):
                frame, read = self.recover(moment=moment)
                self.assertIsNone(frame)
                read.assert_not_called()

    def test_gap_future_frame_or_invalid_duration_cannot_become_a_success(self):
        for pts, duration in (("2.8", ".04"), ("3.01", ".04"), ("2.9871", "0"),
                              ("NaN", ".04"), ("2.9871", "NaN"), ("2.9871", "9")):
            with self.subTest(pts=pts, duration=duration):
                frame, read = self.recover(self.metadata(pts=pts, duration=duration))
                self.assertIsNone(frame)
                self.assertEqual(read.call_count, 1)

    def test_missing_metadata_corruption_and_unconfirmed_picture_remain_failures(self):
        cases = [dict(metadata=b"not json"), dict(metadata=b'{"frames":[]}'),
                 dict(detail="decoder: corrupted picture"), dict(image=b"\xff\xd8truncated"),
                 dict(actual="2.9")]
        for case in cases:
            with self.subTest(case=case):
                self.assertIsNone(self.recover(**case)[0])

    def test_timeout_keeps_partial_result_and_cancellation_propagates(self):
        for error in (RuntimeError("read timeout"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), patch.object(P, "probe_video_duration", side_effect=error):
                if isinstance(error, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        P.decode_export_tail(Path("generated.mp4"), 3, 1, lambda: None)
                else:
                    self.assertIsNone(P.decode_export_tail(Path("generated.mp4"), 3, 1, lambda: None))

    def test_decoder_only_requests_one_last_missing_sample(self):
        decoder = MagicMock()
        decoder.returncode = decoder.poll.return_value = 0
        for returned, expected, call in ((b"", 1, True), (JPEG, 2, True), (b"", 2, False)):
            decoder.communicate.return_value = (returned, None)
            with self.subTest(expected=expected, returned=bool(returned)), \
                    patch.object(P.subprocess, "Popen", return_value=decoder), \
                    patch.object(P, "decode_export_tail", return_value=JPEG) as tail:
                frames = P.decode_export_frames(Path("generated.mp4"), 2, 1, expected, lambda: None, mode="accurate")
                self.assertEqual(tail.call_count, int(call))
                if call:
                    self.assertEqual(tail.call_args.args[1], 2 + int(bool(returned)))
                    self.assertEqual(len(frames), expected)
                else:
                    self.assertEqual(frames, [])
