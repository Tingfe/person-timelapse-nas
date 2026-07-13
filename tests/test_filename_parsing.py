import importlib.util
import unittest
from datetime import datetime
from pathlib import Path


MODULE = Path(__file__).parents[1] / "app" / "person_timelapse.py"
SPEC = importlib.util.spec_from_file_location("person_timelapse", MODULE)
MODULE_UNDER_TEST = importlib.util.module_from_spec(SPEC)


class StubCV2:
    pass


class StubYOLO:
    pass


import sys
sys.modules.setdefault("cv2", StubCV2())
ultralytics = type(sys)("ultralytics")
ultralytics.YOLO = StubYOLO
sys.modules.setdefault("ultralytics", ultralytics)
SPEC.loader.exec_module(MODULE_UNDER_TEST)


class FilenameParsingTests(unittest.TestCase):
    def test_parse_xiaomi_filename(self):
        record = MODULE_UNDER_TEST.parse_record(Path("10_20260324195528_20260324200632.mp4"))
        self.assertEqual(record["camera"], "10")
        self.assertEqual(record["start"], datetime(2026, 3, 24, 19, 55, 28))
        self.assertEqual(record["end"], datetime(2026, 3, 24, 20, 6, 32))

    def test_reject_non_xiaomi_filename(self):
        self.assertIsNone(MODULE_UNDER_TEST.parse_record(Path("holiday.mp4")))

    def test_accepts_nas_added_prefix(self):
        record = MODULE_UNDER_TEST.parse_record(
            Path("video_0001_10_10_20260303202252_20260303204922.mp4")
        )
        self.assertEqual(record["camera"], "10")

    def test_parses_tf_card_video_stream(self):
        record = MODULE_UNDER_TEST.parse_record(
            Path("video_0000_0_10_20260303202252_20260303204922.mp4")
        )
        self.assertEqual(record["camera"], "0")

    def test_source_id_changes_when_file_size_changes(self):
        path = Path(self.id().replace(".", "_"))
        record = {
            "camera": "10",
            "start": datetime(2026, 3, 3, 20, 22, 52),
            "end": datetime(2026, 3, 3, 20, 49, 22),
            "path": path,
        }
        with open(path, "wb") as handle:
            handle.write(b"a")
        first = MODULE_UNDER_TEST.source_id(record)
        with open(path, "ab") as handle:
            handle.write(b"b")
        self.assertNotEqual(first, MODULE_UNDER_TEST.source_id(record))
        path.unlink()

    def test_summary_empty_records(self):
        summary = MODULE_UNDER_TEST.summarize([])
        self.assertEqual(summary["files"], 0)
        self.assertIsNone(summary["first"])
