#!/usr/bin/env python3
"""Local-only web console for the person timelapse workflow."""

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from person_timelapse import find_records


INPUT_ROOT = Path(os.environ.get("INPUT_ROOT", "/input"))
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/output"))
APP_ROOT = Path(__file__).parent
TASKS_PATH = OUTPUT_ROOT / "tasks.json"
DATE_PATTERN = re.compile(r"^\d{8}$")
CAMERA_PATTERN = re.compile(r"^\d+$")
LOCK = threading.Lock()


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


def event_summary(date):
    events = read_json(OUTPUT_ROOT / f"events-{date}.json", {})
    groups = events.get("events", {})
    return {
        "date": date,
        "events": sum(len(group) for group in groups.values()),
        "cameras": sorted(groups.keys()),
        "ready": bool(groups),
    }


def available_dates():
    source_days = {record["start"].strftime("%Y%m%d") for record in find_records(INPUT_ROOT, None)}
    result_days = {path.stem.removeprefix("events-") for path in OUTPUT_ROOT.glob("events-*.json")}
    return [event_summary(day) for day in sorted(source_days | result_days, reverse=True)]


def task_worker(task_id, command):
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        status = "completed" if result.returncode == 0 else "failed"
        detail = (result.stdout + result.stderr).strip()[-4000:]
    except Exception as error:  # pragma: no cover - defensive boundary for background work
        status = "failed"
        detail = str(error)
    with LOCK:
        tasks = load_tasks()
        for task in tasks["tasks"]:
            if task["id"] == task_id:
                task["status"] = status
                task["finished_at"] = datetime.now().isoformat(timespec="seconds")
                task["detail"] = detail
        save_tasks(tasks)


def create_task(payload):
    kind = payload.get("kind")
    date = payload.get("date", "")
    if kind not in {"scan", "export"} or not DATE_PATTERN.fullmatch(date):
        raise ValueError("任务类型或日期无效")
    camera = payload.get("camera", "")
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
            "status": "running",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "detail": "任务已启动",
        }
        tasks["tasks"].insert(0, task)
        save_tasks(tasks)

    command = [sys.executable, str(APP_ROOT / "person_timelapse.py")]
    if kind == "scan":
        command += ["scan", str(INPUT_ROOT), str(OUTPUT_ROOT), "--date", date,
                    "--sample-seconds", "5", "--motion-threshold", "3", "--keepalive-seconds", "60"]
    else:
        command += ["export", str(INPUT_ROOT), str(events_path), str(OUTPUT_ROOT), "--camera", camera]
    thread = threading.Thread(target=task_worker, args=(task["id"], command), daemon=True)
    thread.start()
    return task


class ConsoleHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"[web] {format % args}")

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
            self.send_json({"dates": available_dates(), "tasks": load_tasks()["tasks"][:20]})
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


def main():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8790"))), ConsoleHandler)
    print("Person Timelapse Console listening on port 8790")
    server.serve_forever()


if __name__ == "__main__":
    main()
