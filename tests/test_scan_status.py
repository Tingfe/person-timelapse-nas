import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
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

    def test_range_scan_queues_only_pending_recording_days(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            original_output, original_tasks = WEB.OUTPUT_ROOT, WEB.TASKS_PATH
            original_snapshot, original_start = WEB.inventory_snapshot, WEB.start_next_task
            WEB.OUTPUT_ROOT, WEB.TASKS_PATH = output, output / "tasks.json"
            records = [
                {"camera": "0", "start": datetime(2026, 3, day, 1), "end": datetime(2026, 3, day, 2),
                 "path": Path(f"{day}.mp4"), "size": day}
                for day in (1, 3, 5)
            ]
            WEB.inventory_snapshot = lambda: {"records": records}
            WEB.start_next_task = lambda: None
            try:
                result = WEB.create_task({"kind": "scan", "date": "20260301", "end_date": "20260305", "profile": "archive"})
                self.assertEqual(result["created"], 3)
                tasks = json.loads((output / "tasks.json").read_text(encoding="utf-8"))["tasks"]
                self.assertEqual([task["date"] for task in tasks], ["20260301", "20260303", "20260305"])
            finally:
                WEB.OUTPUT_ROOT, WEB.TASKS_PATH = original_output, original_tasks
                WEB.inventory_snapshot, WEB.start_next_task = original_snapshot, original_start

    def test_range_export_collects_only_recording_days(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            original_output, original_tasks = WEB.OUTPUT_ROOT, WEB.TASKS_PATH
            original_snapshot, original_start = WEB.inventory_snapshot, WEB.start_next_task
            WEB.OUTPUT_ROOT, WEB.TASKS_PATH = output, output / "tasks.json"
            records = [
                {"camera": "0", "start": datetime(2026, 3, day, 1), "end": datetime(2026, 3, day, 2),
                 "path": Path(f"{day}.mp4"), "size": day}
                for day in (1, 3)
            ]
            WEB.inventory_snapshot = lambda: {"records": records}
            WEB.start_next_task = lambda: None
            try:
                for day in ("20260301", "20260303"):
                    (output / f"events-{day}.json").write_text(json.dumps({"date": day, "events": {}}), encoding="utf-8")
                result = WEB.create_task({"kind": "export", "date": "20260301", "end_date": "20260303", "camera": "0"})
                self.assertEqual(result["dates"], ["20260301", "20260303"])
                task = json.loads((output / "tasks.json").read_text(encoding="utf-8"))["tasks"][0]
                self.assertEqual(task["dates"], ["20260301", "20260303"])
            finally:
                WEB.OUTPUT_ROOT, WEB.TASKS_PATH = original_output, original_tasks
                WEB.inventory_snapshot, WEB.start_next_task = original_snapshot, original_start

    def test_timelapse_library_marks_every_day_in_a_range(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            original_output = WEB.OUTPUT_ROOT
            WEB.OUTPUT_ROOT = output
            (output / "people-timelapse-0-20260301-20260303.mp4").write_bytes(b"video")
            records = [
                {"camera": "0", "start": datetime(2026, 3, day, 1), "end": datetime(2026, 3, day, 2),
                 "path": Path(f"{day}.mp4"), "size": day}
                for day in (1, 2, 3, 4)
            ]
            try:
                self.assertEqual([item["name"] for item in WEB.exports_for_date("20260302")],
                                 ["people-timelapse-0-20260301-20260303.mp4"])
                summaries = {item["date"]: item for item in WEB.available_dates({"records": records})}
                self.assertEqual(summaries["20260301"]["timelapse_count"], 1)
                self.assertEqual(summaries["20260303"]["timelapse_count"], 1)
                self.assertEqual(summaries["20260304"]["timelapse_count"], 0)
            finally:
                WEB.OUTPUT_ROOT = original_output

    def test_one_click_timelapse_scans_missing_days_then_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            original_output, original_tasks = WEB.OUTPUT_ROOT, WEB.TASKS_PATH
            original_snapshot, original_start = WEB.inventory_snapshot, WEB.start_next_task
            WEB.OUTPUT_ROOT, WEB.TASKS_PATH = output, output / "tasks.json"
            records = [
                {"camera": "0", "start": datetime(2026, 3, day, 1), "end": datetime(2026, 3, day, 2),
                 "path": Path(f"{day}.mp4"), "size": day}
                for day in (1, 3)
            ]
            WEB.inventory_snapshot = lambda: {"records": records}
            WEB.start_next_task = lambda: None
            try:
                completed = records[0]
                (output / "processed.json").write_text(json.dumps({"sources": {
                    PROCESSOR.source_id(completed): {"processed_at": "2026-03-01T02:00:00"},
                }}), encoding="utf-8")
                (output / "events-20260301.json").write_text(json.dumps({"date": "20260301", "events": {}}), encoding="utf-8")
                result = WEB.create_task({"kind": "timelapse", "date": "20260301", "end_date": "20260303",
                                          "camera": "0", "profile": "archive"})
                self.assertEqual(result["scan_created"], 1)
                self.assertTrue(result["export_created"])
                tasks = json.loads((output / "tasks.json").read_text(encoding="utf-8"))["tasks"]
                self.assertEqual([task["kind"] for task in tasks], ["scan", "export"])
                self.assertEqual(tasks[-1]["dates"], ["20260301", "20260303"])
            finally:
                WEB.OUTPUT_ROOT, WEB.TASKS_PATH = original_output, original_tasks
                WEB.inventory_snapshot, WEB.start_next_task = original_snapshot, original_start

    def test_empty_mount_does_not_clear_existing_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, empty = root / "source", root / "empty"
            source.mkdir()
            empty.mkdir()
            (source / "0_20260301010000_20260301020000.mp4").write_bytes(b"x")
            database = root / "inventory.sqlite3"
            WEB.indexed_records(source, database)
            with self.assertRaisesRegex(RuntimeError, "未发现任何 MP4"):
                WEB.indexed_records(empty, database)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 1)
            self.assertTrue(database.with_suffix(".sqlite3.bak").exists())

    def test_refresh_keeps_cached_records_when_another_indexer_has_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source", root / "output"
            source.mkdir()
            output.mkdir()
            (source / "0_20260301010000_20260301020000.mp4").write_bytes(b"x")
            database = output / "inventory.sqlite3"
            WEB.indexed_records(source, database)
            (output / ".inventory-index.lock").write_text("other device", encoding="utf-8")
            original_input, original_output, original_database = WEB.INPUT_ROOT, WEB.OUTPUT_ROOT, WEB.INVENTORY_DB_PATH
            original_inventory = WEB.INVENTORY.copy()
            WEB.INPUT_ROOT, WEB.OUTPUT_ROOT, WEB.INVENTORY_DB_PATH = source, output, database
            WEB.INVENTORY = {"updated_at": 0.0, "records": [], "diagnostics": {}, "indexing": True}
            try:
                WEB.refresh_inventory()
                self.assertEqual(len(WEB.INVENTORY["records"]), 1)
                self.assertIn("另一台设备", WEB.INVENTORY["diagnostics"]["message"])
            finally:
                WEB.INPUT_ROOT, WEB.OUTPUT_ROOT, WEB.INVENTORY_DB_PATH = original_input, original_output, original_database
                WEB.INVENTORY = original_inventory

    def test_refresh_ttl_starts_when_the_index_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "source", root / "output"
            source.mkdir()
            output.mkdir()
            (source / "0_20260301010000_20260301020000.mp4").write_bytes(b"x")
            original_input, original_output, original_database = WEB.INPUT_ROOT, WEB.OUTPUT_ROOT, WEB.INVENTORY_DB_PATH
            original_inventory = WEB.INVENTORY.copy()
            WEB.INPUT_ROOT, WEB.OUTPUT_ROOT, WEB.INVENTORY_DB_PATH = source, output, output / "inventory.sqlite3"
            WEB.INVENTORY = {"updated_at": 0.0, "records": [], "diagnostics": {}, "indexing": True}
            try:
                with patch.object(WEB.time, "monotonic", side_effect=[10.0, 51.0]):
                    WEB.refresh_inventory()
                self.assertEqual(WEB.INVENTORY["updated_at"], 51.0)
            finally:
                WEB.INPUT_ROOT, WEB.OUTPUT_ROOT, WEB.INVENTORY_DB_PATH = original_input, original_output, original_database
                WEB.INVENTORY = original_inventory
