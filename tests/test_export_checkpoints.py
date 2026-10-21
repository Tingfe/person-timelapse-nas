"""Deterministic recovery tests; generated metadata only, no private recordings."""
import importlib
import json
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
P = importlib.import_module("person_timelapse")


class ExportCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / "output"
        self.records = []
        for day in (1, 2):
            file = self.root / f"0_202401{day:02}000000_202401{day:02}000100.mp4"
            file.write_bytes(b"generated source")
            self.records.append(P.parse_record(file))
        self.events = [(r["start"], r["end"]) for r in self.records]
        encoder = patch.object(P.subprocess, "Popen", side_effect=self.fake_encoder)
        probe = patch.object(P, "probe_video_duration", return_value=60)
        validation = patch.object(P, "valid_video", return_value=True)
        for patcher in (encoder, probe, validation):
            mock = patcher.start()
            self.addCleanup(patcher.stop)
            if patcher is probe:
                self.probe = mock

    @staticmethod
    def fake_encoder(command, **kwargs):
        Path(command[-1]).write_bytes(b"generated encoded video")
        process = MagicMock()
        process.wait.return_value, process.poll.return_value = 0, 0
        return process

    def run_export(self, records=None, events=None, day="20240101-20240102", frame_seconds=10, fps=25):
        return P.export(self.records if records is None else records, self.events if events is None else events,
                        self.output, "0", day, frame_seconds, fps)

    def metadata(self, day="20240101-20240102"):
        return json.loads((self.output / f"people-timelapse-0-{day}.json").read_text())

    def test_interruption_keeps_completed_fragment_and_resume_only_decodes_missing_file(self):
        with patch.object(P, "decode_export_frames", side_effect=[[b"jpeg"] * 6, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                self.run_export()
        self.assertEqual(len(list((self.output / ".export-cache/v1").glob("*.mp4"))), 1)
        self.assertEqual(list(self.output.rglob("*.partial.mp4")), [])
        with patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 6) as decode:
            self.assertEqual(self.run_export(), 0)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(decode.call_args.args[0], self.records[1]["path"])
        self.assertEqual((self.metadata()["frames"], self.metadata()["reused_files"]), (12, 1))

    def test_completed_fragment_reused_in_larger_date_range(self):
        with patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 6):
            self.run_export(self.records[:1], self.events[:1], day="20240101")
        with patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 6) as decode:
            self.run_export()
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(self.metadata()["reused_files"], 1)
        self.assertEqual(self.probe.call_count, 2, "unchanged sources reuse their probed duration")

    def test_source_events_and_output_parameters_invalidate_cache(self):
        record = self.records[0]
        events = self.events[:1]
        with patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 30) as decode:
            self.run_export([record], events)
            decode.reset_mock()
            self.run_export([record], events)
            decode.assert_not_called()
            events = [(record["start"], record["end"] - timedelta(seconds=10))]
            self.run_export([record], events)
            self.assertEqual(decode.call_count, 1)
            self.run_export([record], events, fps=20)
            self.assertEqual(decode.call_count, 2)
            self.run_export([record], events, fps=20, frame_seconds=5)
            self.assertEqual(decode.call_count, 3)
            record["path"].write_bytes(b"changed source, different size")
            self.run_export([record], events, fps=20, frame_seconds=5)
            self.assertEqual(decode.call_count, 4)
            with patch.dict(P.os.environ, {"EXPORT_DECODE_MODE": "accurate"}):
                self.run_export([record], events, fps=20, frame_seconds=5)
            self.assertEqual(decode.call_count, 5)

    def test_partial_fragment_is_playable_but_not_reused_as_complete(self):
        with patch.object(P, "decode_export_frames", side_effect=[RuntimeError("decode timeout"), [b"jpeg"] * 30]):
            self.assertEqual(self.run_export(self.records[:1], self.events[:1], frame_seconds=1), 2)
        self.assertEqual(self.metadata()["frames"], 30)
        self.assertEqual(len(list((self.output / ".export-cache/v1").glob("*.mp4"))), 0)
        with patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 30) as decode:
            self.assertEqual(self.run_export(self.records[:1], self.events[:1], frame_seconds=1), 0)
        self.assertEqual(decode.call_count, 2)
        self.assertEqual(self.metadata()["failed_files"], [])

    def test_duration_clipping_is_disclosed_without_false_decode_failure(self):
        self.probe.return_value = 40
        with patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 4) as decode:
            self.assertEqual(self.run_export(self.records[:1], self.events[:1]), 0)
        self.assertEqual(decode.call_args.args[1:4], (0.0, 10, 4))
        self.assertEqual(self.metadata()["duration_adjusted_files"], 1)
        self.assertEqual(self.metadata()["duration_adjustments"][0]["trimmed_event_seconds"], 20)
        self.assertEqual(self.metadata()["failed_files"], [])

    def test_corrupted_cache_metadata_or_modified_fragment_requires_rebuild(self):
        with patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 6) as decode:
            self.run_export(self.records[:1], self.events[:1])
            fragment = next((self.output / ".export-cache/v1").glob("*.mp4"))
            fragment.write_bytes(b"truncated")
            self.run_export(self.records[:1], self.events[:1])
            self.assertEqual(decode.call_count, 2)
            fragment.with_suffix(".json").write_text("not json")
            self.run_export(self.records[:1], self.events[:1])
            self.assertEqual(decode.call_count, 3)

    def test_concat_failure_preserves_old_video_and_checkpoints_for_retry(self):
        self.output.mkdir()
        target = self.output / "people-timelapse-0-20240101-20240102.mp4"
        target.write_bytes(b"previous good video")

        def fail_concat(command, **kwargs):
            if "concat" not in command:
                return self.fake_encoder(command, **kwargs)
            process = MagicMock()
            process.wait.return_value, process.poll.return_value = 1, 1
            return process

        with patch.object(P.subprocess, "Popen", side_effect=fail_concat), \
                patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 6):
            with self.assertRaisesRegex(RuntimeError, "拼接失败"):
                self.run_export()
        self.assertEqual(target.read_bytes(), b"previous good video")
        with patch.object(P, "decode_export_frames", side_effect=AssertionError("checkpoint was decoded again")):
            self.assertEqual(self.run_export(), 0)
        self.assertEqual(self.metadata()["reused_files"], 2)
        self.assertEqual(list(self.output.rglob("*.partial.mp4")), [])

    def test_concat_timeout_preserves_old_video_and_reuses_all_completed_fragments(self):
        self.output.mkdir()
        target = self.output / "people-timelapse-0-20240101-20240102.mp4"
        target.write_bytes(b"previous good video")
        wait = P.wait_export_process

        def timeout_concat(process, heartbeat, **kwargs):
            if kwargs.get("activity") is not None:
                raise RuntimeError("视频拼接连续 600 秒无进展")
            return wait(process, heartbeat, **kwargs)

        with patch.object(P, "wait_export_process", side_effect=timeout_concat), \
                patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 6):
            with self.assertRaisesRegex(RuntimeError, "拼接.*无进展"):
                self.run_export()
        self.assertEqual(target.read_bytes(), b"previous good video")
        self.assertEqual(len(list((self.output / ".export-cache/v1").glob("*.mp4"))), 2)
        self.assertEqual(list(self.output.rglob("*.partial.mp4")), [])
        with patch.object(P, "decode_export_frames", side_effect=AssertionError("completed fragment decoded again")):
            self.assertEqual(self.run_export(), 0)
        self.assertEqual(self.metadata()["reused_files"], 2)

    def test_repeated_ffmpeg_status_is_not_mistaken_for_actual_concat_progress(self):
        clock = [0]
        processes = []

        def stalled_concat(command, **kwargs):
            process = self.fake_encoder(command, **kwargs)
            if "concat" not in command:
                return process
            processes.append(process)
            killed = [False]
            process.poll.side_effect = lambda: -9 if killed[0] else None
            process.kill.side_effect = lambda: killed.__setitem__(0, True)
            kwargs["stdout"].write(b"frame=12\nout_time_us=480000\nprogress=continue\n")
            kwargs["stdout"].flush()

            def wait(timeout):
                if killed[0]:
                    return -9
                clock[0] += 200
                kwargs["stdout"].write(b"frame=12\nout_time_us=480000\nprogress=continue\n")
                kwargs["stdout"].flush()
                raise P.subprocess.TimeoutExpired("generated ffmpeg", timeout)
            process.wait.side_effect = wait
            return process

        with patch.object(P.subprocess, "Popen", side_effect=stalled_concat), \
                patch.object(P.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 6), \
                patch.dict(P.os.environ, {"CONCAT_STALL_SECONDS": "600", "CONCAT_TIMEOUT_SECONDS": "21600"}):
            with self.assertRaisesRegex(RuntimeError, "拼接.*无进展"):
                self.run_export()
        self.assertEqual(clock[0], 600)
        processes[0].kill.assert_called_once()
        self.assertEqual(len(list((self.output / ".export-cache/v1").glob("*.mp4"))), 2)
        self.assertEqual(list(self.output.rglob("*.partial.mp4")), [])

    def test_progress_advances_inside_a_file_and_exposes_estimated_duration(self):
        with patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 30), \
                patch.object(P, "report_progress") as progress:
            self.run_export(self.records[:1], self.events[:1], frame_seconds=1)
        halfway = next(call.kwargs for call in progress.call_args_list if call.kwargs.get("frames") == 30)
        self.assertEqual(halfway["percent"], 45)
        self.assertEqual(halfway["estimated_output_seconds"], 2.4)
        self.assertIsNotNone(halfway["eta_seconds"])

    def test_overlapping_events_and_duplicate_records_are_not_encoded_twice(self):
        record = self.records[0]
        events = [self.events[0], (record["start"] + timedelta(seconds=5), record["end"])]
        with patch.object(P, "decode_export_frames", return_value=[b"jpeg"] * 6) as decode:
            self.run_export([record, record], events)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(self.metadata()["frames"], 6)


class ProbeDurationTests(unittest.TestCase):
    def test_video_stream_duration_takes_precedence_over_longer_audio_container(self):
        result = MagicMock(stdout=json.dumps({"streams": [{"duration": "40.5"}], "format": {"duration": "60"}}))
        with patch.object(P.subprocess, "run", return_value=result):
            self.assertEqual(P.probe_video_duration(Path("generated.mp4")), 40.5)

    def test_unknown_or_nonfinite_duration_is_rejected(self):
        for duration in ("N/A", "nan", "inf", "0", "-1"):
            result = MagicMock(stdout=json.dumps({"streams": [{"duration": duration}]}))
            with self.subTest(duration=duration), patch.object(P.subprocess, "run", return_value=result):
                with self.assertRaises(RuntimeError):
                    P.probe_video_duration(Path("generated.mp4"))
