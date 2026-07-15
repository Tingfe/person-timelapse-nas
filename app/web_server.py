#!/usr/bin/env python3
"""Local-only web console for the person timelapse workflow."""

import json
import os
import re
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

from person_timelapse import parse_record


INPUT_ROOT = Path(os.environ.get("INPUT_ROOT", "/input"))
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/output"))
APP_ROOT = Path(__file__).parent
TASKS_PATH = OUTPUT_ROOT / "tasks.json"
DATE_PATTERN = re.compile(r"^\d{8}$")
CAMERA_PATTERN = re.compile(r"^(?:\d+|legacy)$")
LOCK = threading.Lock()
INVENTORY_LOCK = threading.Lock()
INVENTORY = {"updated_at": 0.0, "records": [], "diagnostics": {}}
INVENTORY_TTL_SECONDS = 30
PROCESSES = {}
PROFILES = {
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


def public_tasks():
    tasks = load_tasks()["tasks"][:20]
    result = []
    for task in tasks:
        item = dict(task)
        if task.get("progress_file"):
            item["progress"] = read_json(OUTPUT_ROOT / task["progress_file"], {})
        result.append(item)
    return result


def event_summary(date):
    events = read_json(OUTPUT_ROOT / f"events-{date}.json", {})
    groups = events.get("events", {})
    return {
        "date": date,
        "events": sum(len(group) for group in groups.values()),
        "cameras": sorted(groups.keys()),
        "ready": bool(groups),
    }


def inventory_snapshot():
    """Cache the recursive source walk so automatic page refreshes stay cheap on a NAS."""
    now = time.monotonic()
    with INVENTORY_LOCK:
        if INVENTORY["diagnostics"] and now - INVENTORY["updated_at"] < INVENTORY_TTL_SECONDS:
            return INVENTORY
        if not INPUT_ROOT.is_dir():
            snapshot = {
                "updated_at": now, "records": [],
                "diagnostics": {"path": str(INPUT_ROOT), "available": False,
                                "message": "容器内未找到 /input 挂载目录"},
            }
        else:
            children = sorted(path.name for path in INPUT_ROOT.iterdir())[:8]
            videos = [path for path in INPUT_ROOT.rglob("*") if path.is_file() and path.suffix.lower() == ".mp4"]
            records = [record for path in videos if (record := parse_record(path))]
            snapshot = {
                "updated_at": now, "records": records,
                "diagnostics": {"path": str(INPUT_ROOT), "available": True, "children": children,
                                "mp4_files": len(videos), "recognized_files": len(records),
                                "examples": [str(path.relative_to(INPUT_ROOT)) for path in videos[:3]]},
            }
        INVENTORY.update(snapshot)
        return INVENTORY


def available_dates():
    source_days = {record["start"].strftime("%Y%m%d") for record in inventory_snapshot()["records"]}
    result_days = {path.stem.removeprefix("events-") for path in OUTPUT_ROOT.glob("events-*.json")}
    return [event_summary(day) for day in sorted(source_days | result_days, reverse=True)]


def source_diagnostics():
    """Expose just enough read-only mount information to diagnose an empty index."""
    return inventory_snapshot()["diagnostics"]


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
    events_path = OUTPUT_ROOT / f"events-{date}.json"
    if kind == "export" and not events_path.exists():
        raise ValueError("请先完成该日期的扫描任务")

    with LOCK:
        tasks = load_tasks()
        if any(task["status"] == "running" for task in tasks["tasks"]):
            raise RuntimeError("已有任务正在运行。为保护 NAS，管理页一次只运行一个任务。")
        task = {
            "id": uuid.uuid4().hex[:8],
            "kind": kind,
            "date": date,
            "camera": camera or None,
            "profile": profile if kind == "scan" else None,
            "status": "running",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "detail": "任务已启动",
        }
        task["progress_file"] = f"progress-{task['id']}.json"
        tasks["tasks"].insert(0, task)
        save_tasks(tasks)

    command = [sys.executable, str(APP_ROOT / "person_timelapse.py")]
    if kind == "scan":
        settings = PROFILES[profile]
        command += ["scan", str(INPUT_ROOT), str(OUTPUT_ROOT), "--date", date,
                    "--sample-seconds", settings["sample_seconds"], "--motion-threshold", settings["motion_threshold"],
                    "--keepalive-seconds", "60", "--imgsz", settings["imgsz"]]
    else:
        command += ["export", str(INPUT_ROOT), str(events_path), str(OUTPUT_ROOT), "--camera", camera]
    thread = threading.Thread(target=task_worker, args=(task["id"], command, task["progress_file"]), daemon=True)
    thread.start()
    return task


def cancel_task(task_id):
    with LOCK:
        tasks = load_tasks()
        task = next((item for item in tasks["tasks"] if item["id"] == task_id), None)
        if not task or task["status"] != "running":
            raise ValueError("没有可取消的运行中任务")
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

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/overview":
            self.send_json({"dates": available_dates(), "tasks": public_tasks(), "profiles": PROFILES,
                            "diagnostics": source_diagnostics()})
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
        if urlparse(self.path).path != "/api/tasks":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            self.send_json(create_task(payload), HTTPStatus.CREATED)
        except (ValueError, RuntimeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/tasks/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            self.send_json(cancel_task(path.rsplit("/", 1)[-1]))
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


def main():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    recover_interrupted_tasks()
    port = int(os.environ.get("PORT", "8790"))
    server = ThreadingHTTPServer(("0.0.0.0", port), ConsoleHandler)
    print(f"Person Timelapse Console listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
