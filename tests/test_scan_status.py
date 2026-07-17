import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


APP_ROOT = Path(__file__).parents[1] / "app"
sys.path.insert(0, str(APP_ROOT))
WEB = importlib.import_module("web_server")
PROCESSOR = importlib.import_module("person_timelapse")


class ScanStatusTests(unittest.TestCase):
    def test_date_status_uses_processed_ledger_and_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            original_output, original_tasks = WEB.OUTPUT_ROOT, WEB.TASKS_PATH
            WEB.OUTPUT_ROOT, WEB.TASKS_PATH = output, output / "tasks.json"
            try:
                completed = {"camera": "0", "start": datetime(2026, 3, 24, 1),
                             "end": datetime(2026, 3, 24, 2), "path": Path("done.mp4"), "size": 10}
                partial = {"camera": "0", "start": datetime(2026, 3, 23, 1),
                           "end": datetime(2026, 3, 23, 2), "path": Path("part.mp4"), "size": 20}
                queued = {"camera": "0", "start": datetime(2026, 3, 22, 1),
                          "end": datetime(2026, 3, 22, 2), "path": Path("queued.mp4"), "size": 30}
                (output / "processed.json").write_text(json.dumps({"sources": {
                    PROCESSOR.source_id(completed): {"processed_at": "2026-03-24T02:00:00"},
                    PROCESSOR.source_id(partial): {"processed_at": "2026-03-23T02:00:00"},
                }}), encoding="utf-8")
                (output / "tasks.json").write_text(json.dumps({"tasks": [{
                    "kind": "scan", "status": "queued", "date": "20260322", "dates": ["20260322"],
                }]}), encoding="utf-8")
                statuses = {item["date"]: item for item in WEB.available_dates({"records": [
                    completed, partial, {**partial, "path": Path("part-second.mp4"), "size": 21}, queued,
                ]})}
                self.assertEqual(statuses["20260324"]["scan_status"], "completed")
                self.assertEqual(statuses["20260323"]["scan_status"], "partial")
                self.assertEqual(statuses["20260322"]["scan_status"], "queued")
            finally:
                WEB.OUTPUT_ROOT, WEB.TASKS_PATH = original_output, original_tasks

