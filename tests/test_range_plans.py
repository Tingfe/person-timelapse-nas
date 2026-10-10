import copy
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
W = importlib.import_module("web_server")


class RangePlanTests(unittest.TestCase):
    def setUp(self):
        self.state = patch.multiple(W, OVERVIEW={"dates": [], "timelapses": []}, LIVE_PROGRESS={})
        self.state.start()
        self.addCleanup(self.state.stop)

    def document(self, count=2, export=True):
        dates = [f"202401{day:02d}" for day in range(1, count + 1)]
        scans = [{"id": "scan-" + day, "kind": "scan", "date": day, "status": "completed"}
                 for day in dates]
        plan = {"id": "plan", "kind": "timelapse" if export else "scan",
                "start_date": dates[0], "end_date": dates[-1], "dates": dates,
                "camera": "0", "scan_ids": [scan["id"] for scan in scans],
                "export_id": "export" if export else None, "created_at": "2026-01-01T00:00:00Z"}
        tasks = scans + ([{"id": "export", "kind": "export", "date": dates[0], "dates": dates,
                           "camera": "0", "status": "queued", "depends_on": plan["scan_ids"][:]}]
                         if export else [])
        return {"tasks": tasks, "plans": [plan]}

    def test_progress_counts_all_days_not_just_twenty_visible_tasks(self):
        document = self.document(count=25)
        document["tasks"][-2]["status"] = "running"
        W.LIVE_PROGRESS[document["tasks"][-2]["id"]] = {"percent": 50}
        plan = W.public_plans(document)[0]
        self.assertEqual(plan["completed_days"], 24)
        self.assertEqual(plan["total_days"], 25)
        self.assertEqual(plan["percent"], 94.2)
        self.assertEqual(plan["scan_percent"], 98)
        self.assertEqual(plan["export_percent"], 0)
        self.assertEqual(plan["current_date"], "20240125")
        self.assertEqual(plan["phase"], "scanning")
        self.assertEqual(plan["status"], "running")

    def test_export_phase_and_output_use_actual_recording_range(self):
        document = self.document()
        document["plans"][0].update(start_date="20231201", end_date="20241231")
        document["tasks"][-1]["status"] = "running"
        W.LIVE_PROGRESS["export"] = {"percent": 50}
        plan = W.public_plans(document)[0]
        self.assertEqual(plan["phase"], "exporting")
        self.assertEqual(plan["percent"], 83.3)
        self.assertEqual(plan["scan_percent"], 100)
        self.assertEqual(plan["export_percent"], 50)
        W.OVERVIEW["timelapses"] = [
            {"name": "correct.mp4", "start_date": "20240101", "end_date": "20240102", "camera": "0"},
            {"name": "other-camera.mp4", "start_date": "20240101", "end_date": "20240102", "camera": "10"},
        ]
        document["tasks"][-1]["status"] = "completed"
        plan = W.public_plans(document)[0]
        self.assertEqual(plan["percent"], 100)
        self.assertEqual(plan["export_percent"], 100)
        self.assertEqual(plan["outputs"], ["correct.mp4"])
        self.assertEqual(plan["start_date"], "20231201")

    def test_failed_and_partial_days_are_not_silently_completed(self):
        document = self.document()
        document["tasks"][0]["status"] = "partial"
        document["tasks"][1]["status"] = "failed"
        plan = W.public_plans(document)[0]
        self.assertEqual((plan["status"], plan["completed_days"], plan["partial_days"], plan["failed_days"]),
                         ("blocked", 1, 1, 1))
        self.assertEqual(set(plan["retry_ids"]), {"scan-20240101", "scan-20240102"})
        document["tasks"][-1]["status"] = "failed"
        self.assertEqual(W.public_plans(document)[0]["phase"], "export_failed")

    def test_repaired_dependency_does_not_retry_obsolete_failed_scan(self):
        document = self.document()
        document["tasks"][0]["status"] = "failed"
        document["tasks"].append({"id": "replacement", "kind": "scan", "date": "20240101", "status": "completed"})
        document["tasks"][2]["depends_on"] = ["replacement", "scan-20240102"]
        plan = W.public_plans(document)[0]
        self.assertEqual(plan["completed_days"], 2)
        self.assertEqual(plan["failed_days"], 0)
        self.assertEqual(plan["retry_ids"], [])

    def test_legacy_exports_are_grouped_without_mutating_saved_records(self):
        document = self.document()
        del document["plans"]
        original = copy.deepcopy(document)
        self.assertEqual(W.public_plans(document)[0]["id"], "legacy-export")
        self.assertEqual(document, original)
        document["tasks"] = [document["tasks"][-1]]
        document["tasks"][0]["status"] = "running"
        W.LIVE_PROGRESS["export"] = {"percent": 50}
        plan = W.public_plans(document)[0]
        self.assertEqual(plan["completed_days"], 2)
        self.assertEqual(plan["percent"], 83.3)

    def test_ready_snapshot_survives_index_unavailable_and_polling_is_memory_only(self):
        document = self.document()
        document["tasks"] = [document["tasks"][-1]]
        document["plans"][0]["ready_dates"] = document["plans"][0]["dates"][:]
        with patch.object(Path, "read_text", side_effect=AssertionError("poll read archive")), \
                patch.object(Path, "rglob", side_effect=AssertionError("poll walked archive")):
            plan = W.public_plans(document)[0]
        self.assertEqual(plan["completed_days"], 2)
        self.assertEqual(plan["phase"], "waiting_export")
        self.assertEqual(plan["percent"], 66.7)

    def test_cancelled_scan_plan_is_terminal_and_partial_remains_visible(self):
        document = self.document(export=False)
        for task in document["tasks"]:
            task["status"] = "cancelled"
        self.assertEqual(W.public_plans(document)[0]["status"], "cancelled")
        document["tasks"][0]["status"] = "partial"
        document["tasks"][1]["status"] = "completed"
        plan = W.public_plans(document)[0]
        self.assertEqual(plan["status"], "partial")
        self.assertEqual(plan["percent"], 100)
        self.assertEqual(plan["partial_days"], 1)

    def test_create_task_persists_parent_with_existing_scan_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summaries = [{"date": day, "scan_status": state, "source_cameras": ["0"], "cameras": []}
                         for day, state in [("20240101", "completed"), ("20240103", "scanning"), ("20240229", "pending")]]
            (root / "events-20240101.json").write_text('{"events": {}}', encoding="utf-8")
            existing = {"tasks": [{"id": "existing", "kind": "scan", "date": "20240103", "status": "running"}]}
            (root / "tasks.json").write_text(json.dumps(existing), encoding="utf-8")
            with patch.multiple(W, OUTPUT_ROOT=root, TASKS_PATH=root / "tasks.json", STATE_READY=False), \
                    patch.object(W, "inventory_snapshot", return_value={}), \
                    patch.object(W, "available_dates", return_value=summaries), \
                    patch.object(W, "start_next_task"):
                result = W.create_task({"kind": "timelapse", "date": "20240101", "end_date": "20241231",
                                        "camera": "0", "profile": "archive"})
            saved = json.loads((root / "tasks.json").read_text(encoding="utf-8"))
            parent = saved["plans"][0]
            self.assertEqual(parent["id"], result["plan_id"])
            self.assertEqual(parent["ready_dates"], ["20240101"])
            self.assertEqual(parent["dates"], ["20240101", "20240103", "20240229"])
            self.assertEqual(parent["end_date"], "20241231")
            self.assertIn("existing", parent["scan_ids"])
            self.assertEqual(parent["export_id"], saved["tasks"][-1]["id"])
            self.assertEqual(result["scan_created"], 1)


if __name__ == "__main__":
    unittest.main()
