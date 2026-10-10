import importlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from datetime import datetime
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
P = importlib.import_module("person_timelapse")
W = importlib.import_module("web_server")
S = importlib.import_module("storage")


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source, self.output = self.root / "input", self.root / "output"
        self.source.mkdir()
        self.output.mkdir()
        self.settings = P.scan_settings(120, .45, 20, 8, 60, 256)

    def record(self, day=1, minute=0, camera="0"):
        prefix = "" if camera == "legacy" else f"{camera}_"
        file = self.source / f"{prefix}202401{day:02}00{minute:02}00_202401{day:02}00{minute + 1:02}00.mp4"
        file.write_bytes(b"x")
        return P.parse_record(file)

    def web_roots(self):
        return patch.multiple(W, INPUT_ROOT=self.source, OUTPUT_ROOT=self.output,
                              TASKS_PATH=self.output / "tasks.json", INVENTORY_DB_PATH=self.output / "inventory.sqlite3",
                              STATE_READY=False, LIVE_PROGRESS={})

    def fake_video_modules(self, readable=True, present=False, interrupt_second=False):
        calls = []

        def capture(path):
            calls.append(path)
            if interrupt_second and len(calls) == 2:
                raise KeyboardInterrupt()
            return SimpleNamespace(isOpened=lambda: readable,
                                   get=lambda prop: 1 if prop == 1 else 60,
                                   set=lambda *args: True, read=lambda: (True, object()), release=lambda: None)

        cv = SimpleNamespace(VideoCapture=capture, CAP_PROP_FPS=1, CAP_PROP_FRAME_COUNT=2,
                             CAP_PROP_POS_MSEC=3, COLOR_BGR2GRAY=4, cvtColor=lambda *args: object(),
                             resize=lambda *args: object(), mean=lambda *args: [0], absdiff=lambda *args: object())
        yolo = SimpleNamespace(YOLO=lambda _: lambda *args, **kwargs: [SimpleNamespace(boxes=[1] if present else [])])
        return patch.dict(sys.modules, {"cv2": cv, "ultralytics": yolo})

    def scan_main(self, *extra):
        argv = ["person_timelapse.py", "scan", str(self.source), str(self.output),
                "--date", "20240101", "--sample-seconds", "120", *extra]
        with patch.object(sys, "argv", argv):
            return P.main()

    def test_unreadable_video_is_failed_and_not_processed(self):
        record = self.record()
        with self.fake_video_modules(readable=False):
            self.assertEqual(self.scan_main(), 2)
        self.assertNotIn(P.source_id(record), P.load_ledger(self.output)["sources"])
        result = list(P.scan_results(self.output))[0]
        self.assertEqual(result["status"], "failed")
        self.assertIn("无法打开", result["error"])

    def test_first_file_checkpoint_survives_second_file_interruption(self):
        first, second = self.record(), self.record(minute=1)
        with self.fake_video_modules(interrupt_second=True), self.assertRaises(KeyboardInterrupt):
            self.scan_main()
        ledger = P.load_ledger(self.output)["sources"]
        self.assertIn(P.source_id(first), ledger)
        self.assertNotIn(P.source_id(second), ledger)

    def test_forced_rescan_resumes_without_redoing_completed_file(self):
        self.record()
        second = self.record(minute=1)
        with self.fake_video_modules(interrupt_second=True), self.assertRaises(KeyboardInterrupt):
            self.scan_main("--force", "--run-id", "same-task")
        with self.fake_video_modules(), patch.object(P, "scan", wraps=P.scan) as scanner:
            self.assertEqual(self.scan_main("--force", "--run-id", "same-task"), 0)
            self.assertEqual([r["path"] for r in scanner.call_args.args[0]], [second["path"]])

    def test_file_claimed_by_another_node_defers_instead_of_finishing(self):
        self.record()
        with self.fake_video_modules(), patch.object(P, "shared_lock", side_effect=TimeoutError("busy")):
            self.assertEqual(self.scan_main(), 3)

    def test_concurrent_nodes_preserve_both_file_results(self):
        first, second = self.record(), self.record(day=2)
        errors = []

        def save(record):
            try:
                P.commit_result(self.output, record, self.settings)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=save, args=(r,)) for r in (first, second)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(set(P.load_ledger(self.output)["sources"]), {P.source_id(first), P.source_id(second)})

    def test_concurrent_same_day_results_are_both_in_event_snapshot(self):
        records = [self.record(), self.record(minute=1)]
        threads = [threading.Thread(target=P.commit_result, args=(self.output, record, self.settings)) for record in records]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        saved = json.loads((self.output / "events-20240101.json").read_text())
        self.assertEqual(set(saved["processed_sources"]), {P.source_id(record) for record in records})

    def test_failed_upgrade_preserves_legacy_material(self):
        record = self.record()
        event = {"start": "2024-01-01T00:00:05", "end": "2024-01-01T00:00:15", "hits": 1}
        S.atomic_json(self.output / "events-20240101.json", {"events": {"0": [event]}})
        P.commit_result(self.output, record, self.settings, error="decode failed")
        saved = json.loads((self.output / "events-20240101.json").read_text())
        self.assertEqual(saved["events"]["0"], [event])
        self.assertEqual(saved["failed_files"], 1)

    def test_dead_local_process_lease_is_reclaimed_immediately(self):
        lease = self.output / "dead-lock"
        lease.mkdir()
        (lease / "owner").write_text(json.dumps({"token": "old", "host": socket.gethostname(), "pid": 12345}))
        with patch.object(S.os, "kill", side_effect=ProcessLookupError()), S.shared_lock(lease, timeout=.1):
            self.assertTrue(lease.exists())
        self.assertFalse(lease.exists())

    def test_corrupt_task_file_is_not_silently_replaced(self):
        with self.web_roots():
            W.TASKS_PATH.write_text("broken JSON")
            with self.assertRaises(json.JSONDecodeError):
                W.recover_interrupted_tasks()
            self.assertEqual(W.TASKS_PATH.read_text(), "broken JSON")

    def test_task_timestamp_contains_timezone(self):
        self.assertIsNotNone(datetime.fromisoformat(W.now_iso()).tzinfo)

    def test_http_auth_cached_status_and_private_files(self):
        cache = {"tasks": []}
        with self.web_roots(), patch.multiple(W, STATE_READY=True, TASK_CACHE=cache,
                TASK_CACHE_PATH=W.TASKS_PATH, ACCESS_PASSWORD="测试 strong password", SESSIONS=set(),
                OVERVIEW={"dates": [{"date": "20240101"}], "timelapses": [], "diagnostics": {}}):
            server = W.ThreadingHTTPServer(("127.0.0.1", 0), W.ConsoleHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = HTTPConnection(*server.server_address, timeout=2)
                client.request("GET", "/api/overview")
                response = client.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
                client.request("POST", "/api/login", json.dumps({"password": "测试 strong password"}).encode(),
                               {"Content-Type": "application/json"})
                response = client.getresponse()
                self.assertEqual(response.status, 204)
                cookie = response.getheader("Set-Cookie").split(";", 1)[0]
                response.read()
                with patch.object(Path, "read_text", side_effect=AssertionError("archive access on request")):
                    client.request("GET", "/api/overview", headers={"Cookie": cookie})
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())["dates"][0]["date"], "20240101")
                (self.output / ".access-password").write_text("secret")
                for path in ("/media/.access-password", "/web/../person_timelapse.py"):
                    client.request("GET", path, headers={"Cookie": cookie})
                    response = client.getresponse()
                    self.assertEqual(response.status, 404)
                    response.read()
                client.request("HEAD", "/app/person_timelapse.py")
                response = client.getresponse()
                self.assertEqual(response.status, 405)
                response.read()
                client.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_database_date_lookup_does_not_walk_archive(self):
        records = [self.record(), self.record(day=2)]
        database = self.output / "inventory.sqlite3"
        W.build_inventory(self.source, database)
        with patch.object(Path, "rglob", side_effect=AssertionError("archive walked")):
            found = P.find_records(self.source, day="20240102", database=database)
        self.assertEqual([r["path"] for r in found], [records[1]["path"]])

    def test_encoder_failure_preserves_previous_video(self):
        record = self.record()
        target = self.output / "people-timelapse-0-20240101.mp4"
        target.write_bytes(b"previous good video")
        process = MagicMock()
        process.wait.return_value, process.poll.return_value = 1, 1
        cv = SimpleNamespace(VideoCapture=lambda _: SimpleNamespace(isOpened=lambda: True,
                             set=lambda *args: True, read=lambda: (True, object()), release=lambda: None),
                             CAP_PROP_POS_MSEC=3, imencode=lambda *args: (True, SimpleNamespace(tobytes=lambda: b"jpeg")))
        with patch.dict(sys.modules, {"cv2": cv}), patch.object(P.subprocess, "Popen", return_value=process):
            with self.assertRaises(RuntimeError):
                P.export([record], [(record["start"], record["end"])], self.output, "0", "20240101", 5, 25)
        self.assertEqual(target.read_bytes(), b"previous good video")
        self.assertEqual(list(self.output.glob("*.partial.mp4")), [])

    def test_legacy_point_event_gets_playable_context(self):
        file = self.output / "events-20240101.json"
        S.atomic_json(file, {"events": {"0": [{"start": "2024-01-01T00:00:05", "end": "2024-01-01T00:00:05"}]}})
        start, end = P.read_events(file, "0")[0]
        self.assertEqual((end - start).total_seconds(), 10)

    def test_legacy_ledger_merge_does_not_lose_stale_writer_changes(self):
        first, second = {"sources": {"first": {}}}, {"sources": {"second": {}}}
        P.save_ledger(self.output, first)
        P.save_ledger(self.output, second)
        self.assertEqual(set(P.load_ledger(self.output)["sources"]), {"first", "second"})

    def test_force_rescan_replaces_events_without_duplicating_hits(self):
        record = self.record()
        event = {"start": "2024-01-01T00:00:00", "end": "2024-01-01T00:00:05", "hits": 1, "thumbnail": "x.jpg"}
        for _ in range(2):
            P.commit_result(self.output, record, self.settings, {"0": [event]})
        saved = json.loads((self.output / "events-20240101.json").read_text())
        self.assertEqual(saved["events"]["0"][0]["hits"], 1)

    def test_quality_upgrade_is_not_skipped(self):
        record = self.record()
        P.commit_result(self.output, record, self.settings)
        with self.web_roots(), patch.object(W, "inventory_snapshot", return_value={"records": [{**record, "size": 1}]}), \
                patch.object(W, "start_next_task"):
            result = W.create_task({"kind": "scan", "date": "20240101", "profile": "precise"})
            self.assertEqual(result["scan_created"], 1)

    def test_restart_requeues_same_scan_and_keeps_export_dependency(self):
        tasks = {"tasks": [
            {"id": "scan", "kind": "scan", "date": "20240101", "status": "running"},
            {"id": "export", "kind": "export", "date": "20240101", "dates": ["20240101"],
             "status": "blocked", "depends_on": ["scan"]},
        ]}
        with self.web_roots():
            W.save_tasks(tasks)
            W.recover_interrupted_tasks()
            recovered = W.load_tasks()["tasks"]
            W.repair_dependencies(recovered)
            self.assertEqual([t["status"] for t in recovered], ["queued", "queued"])
            self.assertEqual(recovered[1]["depends_on"], ["scan"])

    def test_completed_replacement_scan_unblocks_old_export(self):
        tasks = [
            {"id": "old", "kind": "scan", "date": "20240101", "status": "interrupted"},
            {"id": "export", "kind": "export", "date": "20240101", "dates": ["20240101"],
             "status": "blocked", "depends_on": ["old"]},
            {"id": "new", "kind": "scan", "date": "20240101", "status": "completed"},
        ]
        W.repair_dependencies(tasks)
        self.assertEqual(tasks[1]["status"], "queued")
        self.assertEqual(tasks[1]["depends_on"], ["new"])

    def test_first_legacy_one_click_uses_source_camera(self):
        record = self.record(camera="legacy")
        # Legacy filenames contain no camera prefix.
        with self.web_roots(), patch.object(W, "inventory_snapshot", return_value={"records": [{**record, "size": 1}]}), \
                patch.object(W, "start_next_task"):
            result = W.create_task({"kind": "timelapse", "date": "20240101", "camera": "0", "profile": "archive"})
            self.assertEqual(result["camera"], "legacy")

    def test_index_db_error_clears_state_and_lease(self):
        inventory = {"updated_at": 0, "records": [], "diagnostics": {}, "indexing": True}
        with self.web_roots(), patch.object(W, "INVENTORY", inventory), \
                patch.object(W.sqlite3, "connect", side_effect=W.sqlite3.OperationalError("locked")):
            W.refresh_inventory()
            self.assertFalse(W.INVENTORY["indexing"])
            self.assertFalse((self.output / ".inventory-index.lock").exists())

    def test_zero_or_invalid_video_is_not_in_library(self):
        (self.output / "people-timelapse-0-20240101.mp4").write_bytes(b"")
        (self.output / "people-timelapse-0-20240102.mp4").write_bytes(b"invalid")
        with self.web_roots(), patch.object(W, "valid_video", return_value=False):
            self.assertEqual(W.timelapses(), [])

    def test_status_has_no_filesystem_reads(self):
        cache = {"tasks": [{"id": "one", "kind": "scan", "date": "20240101", "status": "running", "progress_file": "progress.json"}]}
        with self.web_roots(), patch.multiple(W, STATE_READY=True, TASK_CACHE=cache, TASK_CACHE_PATH=W.TASKS_PATH), \
                patch.object(W, "read_json", side_effect=AssertionError("disk read")):
            self.assertEqual(W.status_snapshot()["tasks"][0]["id"], "one")

    def test_pause_and_resume_preserves_queued_tasks(self):
        with self.web_roots():
            W.save_tasks({"tasks": [{"id": "one", "status": "queued"}]})
            W.queue_action("pause")
            self.assertTrue(W.load_tasks()["paused"])
            W.queue_action("resume")
            self.assertFalse(W.load_tasks()["paused"])
            self.assertEqual(W.load_tasks()["tasks"][0]["status"], "queued")

    def test_archive_hits_have_context_not_zero_length(self):
        record = self.record()
        with self.fake_video_modules(present=True), patch.object(P, "save_thumbnail", return_value=self.output / "x.jpg"):
            events = P.scan([record], self.output, 120, .45, 20)
        event = events["0"][0]
        self.assertGreater(event["end"], event["start"])

    def test_stalled_worker_is_terminated(self):
        task = {"id": "stalled", "kind": "scan", "date": "20240101", "camera": None,
                "status": "running", "profile": "archive", "progress_file": "progress.json"}
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        with self.web_roots(), patch.dict(os.environ, {"RUNTIME_ROOT": str(self.root)}), \
                patch.object(W, "STALLED_SECONDS", .01), patch.object(W, "task_command", return_value=command), \
                patch.object(W, "start_next_task"):
            W.save_tasks({"tasks": [task]})
            W.task_worker(task, None, "progress.json")
            self.assertEqual(W.load_tasks()["tasks"][0]["status"], "failed")
            self.assertIn("没有进度", W.load_tasks()["tasks"][0]["detail"])

    def test_pause_terminates_worker_but_keeps_remaining_queue(self):
        task = {"id": "pause-test", "kind": "scan", "date": "20240101", "camera": None,
                "status": "running", "profile": "archive", "progress_file": "pause-progress.json"}
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        with self.web_roots(), patch.dict(os.environ, {"RUNTIME_ROOT": str(self.root)}), \
                patch.object(W, "task_command", return_value=command), patch.object(W, "start_next_task"):
            W.save_tasks({"tasks": [task, {"id": "next", "status": "queued"}]})
            thread = threading.Thread(target=W.task_worker, args=(task, None, task["progress_file"]))
            thread.start()
            deadline = time.monotonic() + 3
            while task["id"] not in W.PROCESSES and time.monotonic() < deadline:
                time.sleep(.02)
            process = W.PROCESSES.get(task["id"])
            self.assertIsNotNone(process)
            W.queue_action("pause")
            thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
            self.assertIsNotNone(process.poll())
            self.assertEqual([t["status"] for t in W.load_tasks()["tasks"]], ["paused", "queued"])

    def test_worker_error_does_not_leave_orphan_process(self):
        task = {"id": "error-test", "kind": "scan", "date": "20240101", "camera": None,
                "status": "running", "profile": "archive", "progress_file": "error-progress.json"}
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        processes, original = [], W.subprocess.Popen

        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        with self.web_roots(), patch.dict(os.environ, {"RUNTIME_ROOT": str(self.root)}), \
                patch.object(W, "task_command", return_value=command), patch.object(W, "start_next_task"), \
                patch.object(W.subprocess, "Popen", side_effect=launch), \
                patch.object(W, "read_json", side_effect=RuntimeError("progress read error")):
            W.save_tasks({"tasks": [task]})
            W.task_worker(task, None, task["progress_file"])
            self.assertEqual(W.load_tasks()["tasks"][0]["status"], "failed")
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_scheduler_checks_again_after_mac_releases_priority(self):
        stop = threading.Event()
        invoked = []

        def advance():
            invoked.append(True)
            if len(invoked) == 2:
                raise KeyboardInterrupt()

        with patch.object(W, "start_next_task", side_effect=advance), patch.object(W, "SCHEDULER_WAKE", stop), \
                patch.object(stop, "wait", return_value=True), self.assertRaises(KeyboardInterrupt):
            W.scheduler_loop()
        self.assertEqual(len(invoked), 2)


if __name__ == "__main__":
    unittest.main()
