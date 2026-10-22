import importlib
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
W = importlib.import_module("web_server")
P = importlib.import_module("person_timelapse")
D = importlib.import_module("directory_settings")


class DirectorySettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source, self.state, self.exports = (self.root / name for name in ("input", "output", "exports"))
        for folder in (self.source, self.state, self.exports):
            folder.mkdir()
        self.child = self.source / "archive"
        self.child.mkdir()
        self.settings = self.state / ".directory-settings.json"
        roots = patch.multiple(W, INPUT_ROOT=self.source, DEFAULT_INPUT_ROOT=self.source, OUTPUT_ROOT=self.state,
            EXPORT_ROOT=None, EXPORT_HISTORY=[], SCAN_ROOTS=[self.source], EXPORT_ROOTS=[self.state, self.exports],
            DIRECTORY_SETTINGS_PATH=self.settings, DIRECTORY_SETTINGS_ERROR="", TASKS_PATH=self.state / "tasks.json",
            INVENTORY_DB_PATH=self.state / "inventory.sqlite3", MAC_PRIORITY_PATH=self.state / ".mac-priority.lock",
            INVENTORY={"updated_at": 0., "records": [], "diagnostics": {}, "indexing": False},
            OVERVIEW={"dates": [], "timelapses": [], "diagnostics": {}, "indexing": False},
            STATE_READY=False, TASK_CACHE=None, SESSIONS={"local-test-session"})
        roots.start()
        self.addCleanup(roots.stop)

    def save(self, **changes):
        payload = {"input_root": str(self.child), "export_root": str(self.exports / "films"), **changes}
        with patch.object(W, "inventory_snapshot", return_value={}):
            return W.update_directories(payload)

    def test_save_load_keeps_state_password_queue_and_default_index(self):
        index = self.state / "inventory.sqlite3"
        index.write_bytes(b"existing shared index")
        password = self.state / ".access-password"
        password.write_text("local-test-only")
        result = self.save()
        self.assertEqual(result["input_root"], str(self.child))
        self.assertEqual(result["state_root"], str(self.state))
        self.assertNotEqual(W.INVENTORY_DB_PATH, index)
        self.assertTrue((self.exports / "films").is_dir())
        self.assertEqual(index.read_bytes(), b"existing shared index")
        self.assertEqual(password.read_text(), "local-test-only")
        self.assertEqual(W.TASKS_PATH, self.state / "tasks.json")
        W.INPUT_ROOT, W.EXPORT_ROOT, W.EXPORT_HISTORY = self.source, None, []
        W.load_directory_settings()
        self.assertEqual(W.INPUT_ROOT, self.child)
        self.assertEqual(W.EXPORT_ROOT, self.exports / "films")
        self.assertIn(self.state, W.export_directories())
        self.assertEqual(W.inventory_database(self.source), index)

    def test_active_tasks_block_changes_without_creating_output(self):
        for status in ("running", "queued", "paused", "blocked"):
            with self.subTest(status=status):
                W.save_tasks({"tasks": [{"id": "test", "status": status}]})
                with self.assertRaisesRegex(RuntimeError, "完成或取消"):
                    self.save()
                self.assertFalse(self.settings.exists())
                self.assertFalse((self.exports / "films").exists())

    def test_index_and_mac_lease_block_changes(self):
        W.INVENTORY["indexing"] = True
        with self.assertRaisesRegex(RuntimeError, "正在建立索引"):
            self.save()
        W.INVENTORY["indexing"] = False
        W.MAC_PRIORITY_PATH.write_text("test")
        with self.assertRaisesRegex(RuntimeError, "Mac"):
            self.save()

    def test_old_retry_keeps_original_roots(self):
        W.save_tasks({"tasks": [{"id": "old", "status": "failed", "kind": "export", "date": "20000101",
                                 "dates": ["20000101"], "camera": "0"}]})
        self.save()
        task = W.load_tasks()["tasks"][0]
        command = W.task_command(task)
        self.assertIn(str(self.source), command)
        self.assertEqual(command[-2:], ["--destination", str(self.state)])
        self.assertEqual(task["inventory_database"], str(self.state / "inventory.sqlite3"))

    def test_invalid_saved_settings_keep_console_available_and_stop_queue(self):
        self.settings.write_text('{"input_root":"/etc","export_root":"/tmp"}')
        W.load_directory_settings()
        self.assertIn("暂不启动", W.directory_snapshot()["settings_error"])
        with patch.object(W, "load_tasks") as tasks:
            W.start_next_task()
            tasks.assert_not_called()
        with self.assertRaisesRegex(ValueError, "目录不可用"):
            W.create_task({})

    def test_paths_cannot_escape_or_browse_hidden_files(self):
        (self.source / "escape").symlink_to(self.exports, target_is_directory=True)
        (self.source / ".private").mkdir()
        for value in ("relative", "/etc", str(self.source / ".." / "exports"), str(self.source / "escape"),
                      str(self.source / ".private")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                D.checked_directory(value, [self.source])
        listing = D.list_directories(str(self.source), [self.source])
        self.assertEqual([item["name"] for item in listing["children"]], ["archive"])

    def test_read_only_output_rejected(self):
        with patch.object(D.tempfile, "TemporaryFile", side_effect=PermissionError("read only")), \
                self.assertRaisesRegex(ValueError, "不可写"):
            self.save(export_root=str(self.exports))
        self.assertFalse(self.settings.exists())

    def test_slow_metadata_does_not_lock_settings_or_publish_old_root(self):
        acquired = []
        def read_dates(snapshot):
            def probe():
                held = W.DIRECTORY_LOCK.acquire(blocking=False)
                acquired.append(held)
                if held:
                    W.DIRECTORY_LOCK.release()
            worker = threading.Thread(target=probe)
            worker.start()
            worker.join(timeout=2)
            W.INPUT_ROOT = self.child
            return [{"date": "20000101"}]
        with patch.object(W, "inventory_snapshot", return_value={"diagnostics": {}, "indexing": False}), \
                patch.object(W, "available_dates", side_effect=read_dates), patch.object(W, "timelapses", return_value=[]):
            W.refresh_overview()
        self.assertEqual(acquired, [True])
        self.assertEqual(W.OVERVIEW["dates"], [])

    def test_uppercase_mp4_is_indexed_but_symlinks_are_not(self):
        name = "0_20000101000000_20000101000100.MP4"
        (self.child / name).write_bytes(b"synthetic")
        (self.source / "linked.MP4").symlink_to(self.child / name)
        (self.source / "unrelated.mp4").write_bytes(b"synthetic")
        records, count = W.build_inventory(self.source, W.INVENTORY_DB_PATH)
        self.assertEqual(count, 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["path"].name, name)
        self.assertEqual(len(P.find_records(self.source)), 1)

    def test_library_keeps_old_and_new_directories_and_prefers_current_duplicate(self):
        def video(folder, name):
            path = folder / name
            path.write_bytes(b"synthetic")
            W.write_json(path.with_suffix(".json"), {"validated": True, "size_bytes": path.stat().st_size,
                                                    "mtime_ns": path.stat().st_mtime_ns})
        name = "people-timelapse-0-20000101.mp4"
        video(self.state, name)
        video(self.exports, name)
        video(self.state, "people-timelapse-0-20000102.mp4")
        W.EXPORT_ROOT = self.exports
        items = W.timelapses()
        self.assertEqual(len(items), 2)
        item = next(item for item in items if item["name"] == name)
        self.assertEqual(item["directory"], str(self.exports))
        self.assertEqual(item["media_path"], f"exports/{W.directory_key(self.exports)}/{name}")

    def test_directory_endpoints_require_login_and_media_remains_contained(self):
        file = self.exports / "people-timelapse-0-20000101.mp4"
        file.write_bytes(b"synthetic")
        (self.exports / "leak.mp4").symlink_to(self.source / "secret.mp4")
        (self.source / "secret.mp4").write_bytes(b"never serve")
        W.EXPORT_ROOT = self.exports
        server = ThreadingHTTPServer(("127.0.0.1", 0), W.ConsoleHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            connection = HTTPConnection(*server.server_address, timeout=5)
            for method, path in (("GET", "/api/directories"), ("GET", "/api/directories/browse?kind=scan&path=/etc"),
                                 ("POST", "/api/directories")):
                connection.request(method, path)
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
            headers = {"Cookie": "person_timelapse_session=local-test-session"}
            key = W.directory_key(self.exports)
            for path, expected in (("/api/directories", 200),
                                   ("/api/directories/browse?kind=scan&path=/etc", 400),
                                   (f"/media/exports/{key}/{file.name}", 200),
                                   (f"/media/exports/{key}/leak.mp4", 404),
                                   (f"/media/exports/{key}/.access-password", 404)):
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, expected, path)
                response.read()
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
