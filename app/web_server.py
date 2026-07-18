#!/usr/bin/env python3
"""Local-only web console for the person timelapse workflow."""

import json
import hmac
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from person_timelapse import load_ledger, parse_record, source_id


INPUT_ROOT = Path(os.environ.get("INPUT_ROOT", "/input"))
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/output"))
APP_ROOT = Path(__file__).parent
TASKS_PATH = OUTPUT_ROOT / "tasks.json"
INVENTORY_DB_PATH = OUTPUT_ROOT / "inventory.sqlite3"
MAC_PRIORITY_PATH = OUTPUT_ROOT / ".mac-priority.lock"
PASSWORD_PATH = OUTPUT_ROOT / ".access-password"
DATE_PATTERN = re.compile(r"^\d{8}$")
CAMERA_PATTERN = re.compile(r"^(?:\d+|legacy)$")
LOCK = threading.Lock()
INVENTORY_LOCK = threading.Lock()
INVENTORY = {"updated_at": 0.0, "records": [], "diagnostics": {}, "indexing": False}
INVENTORY_TTL_SECONDS = 30
PROCESSES = {}
SESSIONS = set()
ACCESS_PASSWORD = ""
PROFILES = {
    "archive": {"label": "超极速（历史回放）", "sample_seconds": "120", "motion_threshold": "8", "imgsz": "256"},
    "turbo": {"label": "极速（Z2 推荐）", "sample_seconds": "30", "motion_threshold": "5", "imgsz": "320"},
    "balanced": {"label": "平衡", "sample_seconds": "10", "motion_threshold": "4", "imgsz": "416"},
    "precise": {"label": "精细", "sample_seconds": "5", "motion_threshold": "2", "imgsz": "640"},
}


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return fallback


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_tasks():
    return read_json(TASKS_PATH, {"tasks": []})


def save_tasks(tasks):
    write_json(TASKS_PATH, tasks)


def recover_interrupted_tasks():
    """A process cannot survive a console restart, so do not leave stale running tasks behind."""
    tasks = load_tasks()
    changed = False
    for task in tasks["tasks"]:
        if task.get("status") == "running":
            task["status"] = "interrupted"
            task["finished_at"] = datetime.now().isoformat(timespec="seconds")
            task["detail"] = "管理页或 NAS 重启，任务已中断；可重新创建任务继续。"
            changed = True
    if changed:
        save_tasks(tasks)


def load_access_password():
    configured = os.environ.get("AUTH_PASSWORD", "").strip()
    if configured:
        return configured
    if PASSWORD_PATH.exists():
        return PASSWORD_PATH.read_text(encoding="utf-8").strip()
    password = secrets.token_urlsafe(12)
    PASSWORD_PATH.write_text(password, encoding="utf-8")
    os.chmod(PASSWORD_PATH, 0o600)
    print(f"Generated local access password: {password}", flush=True)
    return password


def session_from_headers(headers):
    for item in headers.get("Cookie", "").split(";"):
        name, _, value = item.strip().partition("=")
        if name == "person_timelapse_session" and value in SESSIONS:
            return value
    return None


def public_tasks():
    tasks = load_tasks()["tasks"][:20]
    result = []
    for task in tasks:
        item = dict(task)
        if task.get("progress_file"):
            item["progress"] = read_json(OUTPUT_ROOT / task["progress_file"], {})
        result.append(item)
    return result


def event_summary(date, total_files=0, processed_files=0, scan_status="pending"):
    events = read_json(OUTPUT_ROOT / f"events-{date}.json", {})
    groups = events.get("events", {})
    return {
        "date": date,
        "events": sum(len(group) for group in groups.values()),
        "cameras": sorted(groups.keys()),
        "ready": bool(groups),
        "total_files": total_files,
        "processed_files": processed_files,
        "scan_status": scan_status,
    }


def indexed_records(root, database):
    """Reuse parsed filename metadata unless the NAS file changed."""
    lock_path = database.parent / ".inventory-index.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age > 12 * 3600:
                lock_path.unlink()
                return indexed_records(root, database)
        except OSError:
            pass
        raise RuntimeError("另一台设备正在建立录像索引，请稍后重试")
    with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
        lock_file.write(f"pid={os.getpid()} started={datetime.now().isoformat()}\n")
    connection = sqlite3.connect(database)
    connection.execute("""CREATE TABLE IF NOT EXISTS videos (
        path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
        camera TEXT NOT NULL, start TEXT NOT NULL, ending TEXT NOT NULL, seen TEXT NOT NULL)""")
    scan_id = uuid.uuid4().hex
    records, mp4_files = [], 0
    try:
        for path in root.rglob("*.mp4"):
            if not path.is_file():
                continue
            mp4_files += 1
            relative = str(path.relative_to(root))
            stat = path.stat()
            row = connection.execute(
                "SELECT mtime_ns,size,camera,start,ending FROM videos WHERE path=?", (relative,)
            ).fetchone()
            if row and row[:2] == (stat.st_mtime_ns, stat.st_size):
                camera, start, ending = row[2:]
                record = {"path": path, "camera": camera,
                          "start": datetime.strptime(start, "%Y%m%d%H%M%S"),
                          "end": datetime.strptime(ending, "%Y%m%d%H%M%S")}
            else:
                record = parse_record(path)
                if record:
                    connection.execute(
                        "INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,?,?)",
                        (relative, stat.st_mtime_ns, stat.st_size, record["camera"],
                         record["start"].strftime("%Y%m%d%H%M%S"), record["end"].strftime("%Y%m%d%H%M%S"), scan_id),
                    )
            if record:
                record["size"] = stat.st_size
                connection.execute("UPDATE videos SET seen=? WHERE path=?", (scan_id, relative))
                records.append(record)
        connection.execute("DELETE FROM videos WHERE seen != ?", (scan_id,))
        connection.commit()
    finally:
        connection.close()
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
    return sorted(records, key=lambda item: item["start"]), mp4_files


def refresh_inventory():
    """Build the expensive recursive index off the HTTP request path."""
    now = time.monotonic()
    try:
        if not INPUT_ROOT.is_dir():
            snapshot = {
                "updated_at": now, "records": [],
                "diagnostics": {"path": str(INPUT_ROOT), "available": False,
                                "message": "容器内未找到 /input 挂载目录"},
            }
        else:
            children = sorted(path.name for path in INPUT_ROOT.iterdir())[:8]
            records, mp4_files = indexed_records(INPUT_ROOT, INVENTORY_DB_PATH)
            snapshot = {
                "updated_at": now, "records": records,
                "diagnostics": {"path": str(INPUT_ROOT), "available": True, "children": children,
                                "mp4_files": mp4_files, "recognized_files": len(records),
                                "index_database": str(INVENTORY_DB_PATH),
                                "examples": [str(record["path"].relative_to(INPUT_ROOT)) for record in records[:3]]},
            }
    except (OSError, RuntimeError) as error:  # A removable disk/share may disappear or another node owns the index.
        snapshot = {"updated_at": now, "records": [],
                    "diagnostics": {"path": str(INPUT_ROOT), "available": INPUT_ROOT.is_dir(), "message": str(error)}}
    with INVENTORY_LOCK:
        INVENTORY.update(snapshot)
        INVENTORY["indexing"] = False


def inventory_snapshot():
    """Return immediately; refresh the large NAS inventory in one background worker."""
    now = time.monotonic()
    with INVENTORY_LOCK:
        stale = not INVENTORY["diagnostics"] or now - INVENTORY["updated_at"] >= INVENTORY_TTL_SECONDS
        if stale and not INVENTORY["indexing"]:
            INVENTORY["indexing"] = True
            threading.Thread(target=refresh_inventory, daemon=True, name="inventory-index").start()
        snapshot = dict(INVENTORY)
        snapshot["records"] = list(INVENTORY["records"])
        snapshot["diagnostics"] = dict(INVENTORY["diagnostics"])
        if snapshot["indexing"] and not snapshot["diagnostics"]:
            snapshot["diagnostics"] = {"path": str(INPUT_ROOT), "available": INPUT_ROOT.is_dir(),
                                       "message": "正在后台建立历史录像索引，请稍候…"}
        return snapshot


def available_dates(snapshot=None):
    records = (snapshot or inventory_snapshot())["records"]
    records_by_day = {}
    for record in records:
        records_by_day.setdefault(record["start"].strftime("%Y%m%d"), []).append(record)
    source_days = set(records_by_day)
    result_days = {path.stem.removeprefix("events-") for path in OUTPUT_ROOT.glob("events-*.json")}
    processed_sources = set(load_ledger(OUTPUT_ROOT).get("sources", {}))
    active_days = {}
    for task in load_tasks()["tasks"]:
        if task.get("kind") == "scan" and task.get("status") in {"queued", "running"}:
            for day in task.get("dates", [task["date"]]):
                active_days[day] = task["status"]
    summaries = []
    for day in sorted(source_days | result_days, reverse=True):
        day_records = records_by_day.get(day, [])
        total_files = len(day_records)
        processed_files = sum(source_id(record) in processed_sources for record in day_records)
        if day in active_days:
            scan_status = "scanning" if active_days[day] == "running" else "queued"
        elif total_files and processed_files == total_files:
            scan_status = "completed"
        elif processed_files:
            scan_status = "partial"
        elif day in result_days:
            # Older releases may have a completed event file without a ledger entry.
            scan_status = "completed"
        else:
            scan_status = "pending"
        summaries.append(event_summary(day, total_files, processed_files, scan_status))
    return summaries


def source_diagnostics(snapshot=None):
    """Expose just enough read-only mount information to diagnose an empty index."""
    return (snapshot or inventory_snapshot())["diagnostics"]


def inventory_status():
    """A mount-free status probe for diagnosing a slow source index."""
    with INVENTORY_LOCK:
        return {
            "indexing": INVENTORY["indexing"],
            "updated_at": INVENTORY["updated_at"],
            "has_index": bool(INVENTORY["diagnostics"]),
        }


def task_command(task):
    command = [sys.executable, str(APP_ROOT / "person_timelapse.py")]
    if task["kind"] == "scan":
        settings = PROFILES[task["profile"]]
        return command + ["scan", str(INPUT_ROOT), str(OUTPUT_ROOT), "--date", task["date"],
                          "--sample-seconds", settings["sample_seconds"], "--motion-threshold", settings["motion_threshold"],
                          "--keepalive-seconds", "60", "--imgsz", settings["imgsz"]]
    events = [str(OUTPUT_ROOT / f"events-{day}.json") for day in task["dates"]]
    return command + ["export", str(INPUT_ROOT), str(OUTPUT_ROOT), *events, "--camera", task["camera"]]


def mac_priority_active():
    """Mac worker renews this lease every minute while it owns compute priority."""
    try:
        if time.time() - MAC_PRIORITY_PATH.stat().st_mtime > 15 * 60:
            MAC_PRIORITY_PATH.unlink()
            return False
        return True
    except FileNotFoundError:
        return False


def start_next_task():
    with LOCK:
        if mac_priority_active():
            return
        tasks = load_tasks()
        if any(task["status"] == "running" for task in tasks["tasks"]):
            return
        task = next((item for item in tasks["tasks"] if item["status"] == "queued"), None)
        if not task:
            return
        task["status"] = "running"
        task["started_at"] = datetime.now().isoformat(timespec="seconds")
        task["detail"] = "任务已启动"
        save_tasks(tasks)
    thread = threading.Thread(target=task_worker, args=(task["id"], task_command(task), task["progress_file"]), daemon=True)
    thread.start()


def task_worker(task_id, command, progress_file):
    environment = os.environ.copy()
    progress_path = OUTPUT_ROOT / progress_file
    environment["PROGRESS_PATH"] = str(progress_path)
    write_json(progress_path, {
        "phase": "starting",
        "percent": 0,
        "elapsed_seconds": 0,
        "current_file": "正在加载人物识别模型…",
    })
    process = None
    try:
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment)
        PROCESSES[task_id] = process
        detail, _ = process.communicate()
        status = "completed" if process.returncode == 0 else "failed"
        detail = detail.strip()[-4000:]
    except Exception as error:  # pragma: no cover - defensive boundary for background work
        status = "failed"
        detail = str(error)
    with LOCK:
        PROCESSES.pop(task_id, None)
        tasks = load_tasks()
        for task in tasks["tasks"]:
            if task["id"] == task_id:
                task["status"] = "cancelled" if task.get("cancel_requested") else status
                task["finished_at"] = datetime.now().isoformat(timespec="seconds")
                task["detail"] = detail
        save_tasks(tasks)
    start_next_task()


def create_task(payload):
    kind = payload.get("kind")
    date = payload.get("date", "")
    if kind not in {"scan", "export"} or not DATE_PATTERN.fullmatch(date):
        raise ValueError("任务类型或日期无效")
    camera = payload.get("camera", "")
    profile = payload.get("profile", "balanced")
    if profile not in PROFILES:
        raise ValueError("性能档位无效")
    if kind == "export" and not CAMERA_PATTERN.fullmatch(camera):
        raise ValueError("导出任务需要摄像头编号")
    end_date = payload.get("end_date") or date
    if not DATE_PATTERN.fullmatch(end_date) or end_date < date:
        raise ValueError("结束日期无效")
    summaries = [item for item in available_dates(inventory_snapshot()) if date <= item["date"] <= end_date]
    dates = [item["date"] for item in sorted(summaries, key=lambda item: item["date"])]
    if not dates:
        raise ValueError("该日期范围内没有已索引的录像。")
    if kind == "export":
        missing = [day for day in dates if not (OUTPUT_ROOT / f"events-{day}.json").exists()]
        if missing:
            raise ValueError(f"请先完成日期扫描：{missing[0]}{' 等' if len(missing) > 1 else ''}")

    with LOCK:
        tasks = load_tasks()
        if kind == "scan":
            active_days = {
                day for item in tasks["tasks"]
                if item.get("kind") == "scan" and item.get("status") in {"queued", "running"}
                for day in item.get("dates", [item.get("date")])
            }
            selected = [item for item in sorted(summaries, key=lambda item: item["date"])
                        if item["scan_status"] not in {"completed", "queued", "scanning"}
                        and item["date"] not in active_days]
            if not selected:
                raise ValueError("这个范围内的录像均已完成扫描，或已在队列中。")
            created = []
            for item in selected:
                task = {
                    "id": uuid.uuid4().hex[:8], "kind": "scan", "date": item["date"],
                    "camera": None, "profile": profile, "status": "queued",
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "detail": "等待扫描", "dates": [item["date"]],
                }
                task["progress_file"] = f"progress-{task['id']}.json"
                tasks["tasks"].append(task)
                created.append(task)
        else:
            task = {
                "id": uuid.uuid4().hex[:8], "kind": kind, "date": date,
                "camera": camera or None, "profile": None, "status": "queued",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "detail": "等待导出", "dates": dates,
            }
            task["progress_file"] = f"progress-{task['id']}.json"
            tasks["tasks"].append(task)
            created = [task]
        save_tasks(tasks)
    start_next_task()
    return {"id": created[0]["id"], "created": len(created), "dates": dates}


def cancel_task(task_id):
    with LOCK:
        tasks = load_tasks()
        task = next((item for item in tasks["tasks"] if item["id"] == task_id), None)
        if not task or task["status"] not in {"running", "queued"}:
            raise ValueError("没有可取消的任务")
        if task["status"] == "queued":
            task["status"] = "cancelled"
            task["detail"] = "已从队列移除"
            save_tasks(tasks)
            return {"id": task_id, "status": "cancelled"}
        task["cancel_requested"] = True
        task["detail"] = "正在停止任务…"
        save_tasks(tasks)
        process = PROCESSES.get(task_id)
        if process:
            process.terminate()
    return {"id": task_id, "status": "cancelling"}


class ConsoleHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[web] {format % args}", flush=True)

    def end_headers(self):
        # The console is often upgraded in-place under the same LAN URL.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, body, status=HTTPStatus.OK):
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def authenticated(self):
        return bool(session_from_headers(self.headers))

    def require_login(self):
        if self.authenticated():
            return False
        if self.path.startswith("/api/"):
            self.send_json({"error": "请先登录"}, HTTPStatus.UNAUTHORIZED)
        else:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/login")
            self.end_headers()
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/login":
            self.path = "/web/login.html"
            return super().do_GET()
        if self.require_login():
            return
        if path == "/api/health":
            self.send_json({"ok": True, "inventory": inventory_status()})
            return
        if path == "/api/overview":
            snapshot = inventory_snapshot()
            self.send_json({"dates": available_dates(snapshot), "tasks": public_tasks(), "profiles": PROFILES,
                            "diagnostics": source_diagnostics(snapshot), "indexing": snapshot["indexing"]})
            return
        if path.startswith("/api/date/"):
            date = path.rsplit("/", 1)[-1]
            if not DATE_PATTERN.fullmatch(date):
                self.send_json({"error": "日期无效"}, HTTPStatus.BAD_REQUEST)
                return
            events = read_json(OUTPUT_ROOT / f"events-{date}.json", {"date": date, "events": {}})
            exports = [file.name for file in OUTPUT_ROOT.glob(f"people-timelapse-*-{date}.mp4")]
            self.send_json({"events": events, "exports": sorted(exports)})
            return
        if path.startswith("/media/"):
            relative = Path(unquote(path.removeprefix("/media/")))
            target = (OUTPUT_ROOT / relative).resolve()
            if OUTPUT_ROOT.resolve() not in target.parents or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "video/mp4" if target.suffix == ".mp4" else "image/jpeg")
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            with target.open("rb") as source:
                self.copyfile(source, self.wfile)
            return
        if path in {"/", "/index.html"}:
            self.path = "/web/index.html"
        elif path.startswith("/web/"):
            self.path = path
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                password = json.loads(self.rfile.read(length)).get("password", "")
                if not hmac.compare_digest(password, ACCESS_PASSWORD):
                    self.send_json({"error": "密码错误"}, HTTPStatus.UNAUTHORIZED)
                    return
                token = secrets.token_urlsafe(32)
                SESSIONS.add(token)
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Set-Cookie", f"person_timelapse_session={token}; HttpOnly; SameSite=Strict; Path=/")
                self.end_headers()
            except (ValueError, json.JSONDecodeError):
                self.send_json({"error": "登录请求无效"}, HTTPStatus.BAD_REQUEST)
            return
        if self.require_login():
            return
        if path != "/api/tasks":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            self.send_json(create_task(payload), HTTPStatus.CREATED)
        except (ValueError, RuntimeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self):
        if self.require_login():
            return
        path = urlparse(self.path).path
        if not path.startswith("/api/tasks/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            self.send_json(cancel_task(path.rsplit("/", 1)[-1]))
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


def main():
    global ACCESS_PASSWORD
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    ACCESS_PASSWORD = load_access_password()
    recover_interrupted_tasks()
    start_next_task()
    inventory_snapshot()
    port = int(os.environ.get("PORT", "8790"))
    server = ThreadingHTTPServer(("0.0.0.0", port), ConsoleHandler)
    print(f"Person Timelapse Console listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
