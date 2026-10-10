#!/usr/bin/env python3
"""Local-only web console for the person timelapse workflow."""

import json
import copy
import hmac
import os
import re
import secrets
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from person_timelapse import (commit_result, load_ledger, parse_record, source_id,
                             scan_settings, settings_satisfied, valid_video)
from storage import atomic_json, shared_lock


INPUT_ROOT = Path(os.environ.get("INPUT_ROOT", "/input"))
OUTPUT_ROOT = Path(os.environ.get("OUTPUT_ROOT", "/output"))
APP_ROOT = Path(__file__).parent
TASKS_PATH = OUTPUT_ROOT / "tasks.json"
INVENTORY_DB_PATH = OUTPUT_ROOT / "inventory.sqlite3"
MAC_PRIORITY_PATH = OUTPUT_ROOT / ".mac-priority.lock"
PASSWORD_PATH = OUTPUT_ROOT / ".access-password"
DATE_PATTERN = re.compile(r"^\d{8}$")
CAMERA_PATTERN = re.compile(r"^(?:\d+|legacy)$")
TIMELAPSE_PATTERN = re.compile(r"^people-timelapse-(\d+|legacy)-(\d{8})(?:-(\d{8}))?\.mp4$")
LOCK = threading.Lock()
INVENTORY_LOCK = threading.Lock()
INVENTORY = {"updated_at": 0.0, "records": [], "diagnostics": {}, "indexing": False}
INVENTORY_TTL_SECONDS = float(os.environ.get("INDEX_REFRESH_SECONDS", "3600"))
PROCESSES = {}
SESSIONS = set()
ACCESS_PASSWORD = ""
TASK_CACHE = None
TASK_CACHE_PATH = None
STATE_READY = False
LIVE_PROGRESS = {}
OVERVIEW = {"dates": [], "timelapses": [], "diagnostics": {}, "indexing": False}
METADATA_DIRTY = threading.Event()
SCHEDULER_WAKE = threading.Event()
STALLED_SECONDS = float(os.environ.get("TASK_STALL_SECONDS", "300"))
PROFILES = {
    "archive": {"label": "超极速（历史回放）", "sample_seconds": "120", "motion_threshold": "8", "imgsz": "256"},
    "turbo": {"label": "极速（Z2 推荐）", "sample_seconds": "30", "motion_threshold": "5", "imgsz": "320"},
    "balanced": {"label": "平衡", "sample_seconds": "10", "motion_threshold": "4", "imgsz": "416"},
    "precise": {"label": "精细", "sample_seconds": "5", "motion_threshold": "2", "imgsz": "640"},
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fallback


def write_json(path, value):
    atomic_json(path, value)


def load_tasks():
    if STATE_READY and TASK_CACHE_PATH == TASKS_PATH and TASK_CACHE is not None:
        return copy.deepcopy(TASK_CACHE)
    if not TASKS_PATH.exists():
        return {"tasks": []}
    # Never turn an unreadable queue into an empty one and overwrite it later.
    tasks = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    if not isinstance(tasks, dict) or not isinstance(tasks.get("tasks"), list):
        raise ValueError("任务文件格式损坏，请先备份并恢复 tasks.json")
    return tasks


def save_tasks(tasks):
    global TASK_CACHE, TASK_CACHE_PATH
    write_json(TASKS_PATH, tasks)
    TASK_CACHE, TASK_CACHE_PATH = copy.deepcopy(tasks), TASKS_PATH
    METADATA_DIRTY.set()


def recover_interrupted_tasks():
    """Resume interrupted work from per-file checkpoints, preserving its dependency IDs."""
    tasks = load_tasks()
    changed = False
    for task in tasks["tasks"]:
        if task.get("status") in {"running", "interrupted"}:
            task["status"] = "cancelled" if task.get("cancel_requested") else "paused" if task.get("pause_requested") else "queued"
            task.pop("finished_at", None)
            task["detail"] = "服务重启，已从文件检查点恢复排队。"
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
    tasks = load_tasks()["tasks"]
    active = [task for task in tasks if task.get("status") in {"queued", "running", "blocked", "paused"}]
    finished = [task for task in tasks if task not in active]
    tasks = active + list(reversed(finished))[:max(0, 20 - len(active))]
    result = []
    for task in tasks:
        item = dict(task)
        if task["id"] in LIVE_PROGRESS:
            item["progress"] = dict(LIVE_PROGRESS[task["id"]])
        elif not STATE_READY and task.get("progress_file"):
            item["progress"] = read_json(OUTPUT_ROOT / task["progress_file"], {})
        result.append(item)
    return result


def public_plans(document=None):
    """Aggregate existing task records in memory; never walk footage on polling."""
    document = document if document is not None else load_tasks()
    tasks = {task["id"]: task for task in document["tasks"] if task.get("id")}
    plans = list(document.get("plans", []))
    grouped_exports = {plan.get("export_id") for plan in plans}
    # Older range exports already carry enough information to reconstruct a plan.
    for task in tasks.values():
        if task.get("kind") == "export" and task["id"] not in grouped_exports:
            plans.append({"id": "legacy-" + task["id"], "kind": "timelapse",
                          "start_date": task["date"], "end_date": max(task.get("dates") or [task["date"]]),
                          "dates": task.get("dates") or [task["date"]], "camera": task.get("camera"),
                          "created_at": task.get("created_at", ""), "scan_ids": task.get("depends_on", []),
                          "export_id": task["id"]})
    days = {day["date"]: day for day in OVERVIEW.get("dates", [])}
    results = []
    for plan in plans:
        if not plan.get("dates"):
            continue
        export = tasks.get(plan.get("export_id"))
        scan_ids = list(dict.fromkeys(plan.get("scan_ids", []) + (export or {}).get("depends_on", [])))
        scans = {tasks[identity]["date"]: tasks[identity] for identity in scan_ids
                 if identity in tasks and tasks[identity].get("kind") == "scan"}
        done = partial = failed = 0
        units = 0.0
        states = []
        ready_dates = set(plan.get("ready_dates", []))
        for day in plan["dates"]:
            scan = scans.get(day)
            state = scan.get("status") if scan else "completed" if day in ready_dates else days.get(day, {}).get("scan_status", "pending")
            states.append(state)
            if state in {"completed", "partial"}:
                done += 1
                partial += state == "partial"
                units += 1
            elif state in {"failed", "cancelled", "interrupted", "blocked"}:
                failed += 1
            elif scan and state == "running":
                progress = LIVE_PROGRESS.get(scan["id"], scan.get("progress", {}))
                units += max(0, min(100, progress.get("percent", 0))) / 100
        status = "queued"
        phase = "scanning" if done < len(plan["dates"]) else "waiting_export" if export else "completed"
        if export and export["status"] in {"completed", "partial", "failed", "cancelled", "blocked", "running"}:
            status = export["status"]
            if status == "running":
                phase = "exporting"
                units = len(plan["dates"])
                done = len(plan["dates"])
            elif status == "failed":
                phase = "export_failed"
            elif status in {"completed", "partial"}:
                phase = "completed"
                units = len(plan["dates"])
                done = len(plan["dates"])
        elif states and all(state == "cancelled" for state in states):
            status = "cancelled"
        elif failed:
            status = "blocked"
        elif "running" in states:
            status = "running"
        elif not export and done == len(plan["dates"]):
            status = "partial" if partial else "completed"
        elif document.get("paused") or "paused" in states or (export or {}).get("status") == "paused":
            status = "paused"
        if export and export["status"] in {"completed", "partial"}:
            units += 1
        elif export and export["status"] == "running":
            progress = LIVE_PROGRESS.get(export["id"], export.get("progress", {}))
            units += max(0, min(100, progress.get("percent", 0))) / 100
        total = len(plan["dates"]) + bool(export)
        percent = round(units / max(total, 1) * 100, 1)
        if status not in {"completed", "partial"}:
            percent = min(percent, 99.9)
        first, last = min(plan["dates"]), max(plan["dates"])
        camera = (export or {}).get("camera", plan.get("camera"))
        outputs = [item["name"] for item in OVERVIEW.get("timelapses", [])
                   if export and export["status"] in {"completed", "partial"}
                   and item["start_date"] == first and item["end_date"] == last and item["camera"] == camera]
        current = next((scan for scan in scans.values() if scan["status"] == "running"), None)
        results.append({**plan, "camera": camera, "status": status, "phase": phase,
                        "completed_days": done, "total_days": len(plan["dates"]),
                        "partial_days": partial, "failed_days": failed, "percent": percent,
                        "current_date": current["date"] if current else None, "outputs": outputs,
                        "detail": (export or {}).get("detail", ""),
                        "retry_ids": [identity for identity in [scan["id"] for scan in scans.values()] + ([export["id"]] if export else [])
                                      if identity in tasks and tasks[identity]["status"] in
                                      {"failed", "partial", "interrupted", "blocked", "paused", "cancelled"}]})
    active = [plan for plan in results if plan["status"] not in {"completed", "partial", "cancelled"}]
    active.sort(key=lambda plan: (plan["status"] == "running", plan.get("created_at", "")), reverse=True)
    finished = sorted((plan for plan in results if plan not in active),
                      key=lambda plan: plan.get("created_at", ""), reverse=True)
    return active + finished[:12]


def event_summary(date, total_files=0, processed_files=0, scan_status="pending", events=None):
    events = events if events is not None else read_json(OUTPUT_ROOT / f"events-{date}.json", {})
    groups = events.get("events", {})
    return {
        "date": date,
        "events": sum(len(group) for group in groups.values()),
        "cameras": sorted(groups.keys()),
        "ready": bool(groups),
        "total_files": total_files,
        "processed_files": processed_files,
        "scan_status": scan_status,
        "failed_files": events.get("failed_files", 0), "settings": events.get("settings", []),
        "sample_seconds": events.get("sample_seconds"),
    }


def timelapses():
    """List finished videos once so the console can surface them as first-class results."""
    items = []
    for file in OUTPUT_ROOT.glob("people-timelapse-*.mp4"):
        match = TIMELAPSE_PATTERN.fullmatch(file.name)
        if not match:
            continue
        camera, start_date, end_date = match.groups()
        try:
            stat = file.stat()
        except OSError:
            continue
        if not stat.st_size:
            continue
        metadata = read_json(file.with_suffix(".json"), {})
        if metadata.get("size_bytes") != stat.st_size or metadata.get("mtime_ns") != stat.st_mtime_ns or not metadata.get("validated"):
            if not valid_video(file):
                continue
            metadata = {"status": "completed", "validated": True, "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            write_json(file.with_suffix(".json"), metadata)
        items.append({"name": file.name, "camera": camera, "start_date": start_date,
                      "end_date": end_date or start_date, "size_bytes": stat.st_size,
                      "status": metadata.get("status", "completed"),
                      "failed_files": len(metadata.get("failed_files", [])),
                      "scan_failed_files": metadata.get("scan_failed_files", 0),
                      "duration_seconds": round(metadata["frames"] / metadata["fps"], 2)
                      if metadata.get("frames") and metadata.get("fps", 0) > 0 else None,
                      "created_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds")})
    return sorted(items, key=lambda item: item["created_at"], reverse=True)


def exports_for_date(date, items=None):
    return [item for item in (items if items is not None else timelapses())
            if item["start_date"] <= date <= item["end_date"]]


def indexed_records(root, database):
    """Reuse parsed filename metadata unless the NAS file changed."""
    try:
        with shared_lock(database.parent / ".inventory-index.lock", timeout=1):
            return build_inventory(root, database)
    except TimeoutError as error:
        raise RuntimeError("另一台设备正在建立录像索引，请稍后重试") from error


def build_inventory(root, database):
    backup_path = database.with_suffix(".sqlite3.bak")
    connection = sqlite3.connect(database)
    try:
        if database.exists():
            with sqlite3.connect(backup_path) as backup:
                connection.backup(backup)
        connection.execute("""CREATE TABLE IF NOT EXISTS videos (
            path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL,
            camera TEXT NOT NULL, start TEXT NOT NULL, ending TEXT NOT NULL, seen TEXT NOT NULL)""")
        connection.execute("CREATE INDEX IF NOT EXISTS videos_start ON videos(start)")
        scan_id = uuid.uuid4().hex
        records, mp4_files = [], 0
        previous_count = connection.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
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
        if previous_count and not mp4_files:
            connection.rollback()
            raise RuntimeError("本次未发现任何 MP4，已保留原有索引；请检查录像目录挂载。")
        connection.execute("DELETE FROM videos WHERE seen != ?", (scan_id,))
        connection.commit()
    finally:
        connection.close()
    return sorted(records, key=lambda item: item["start"]), mp4_files


def cached_records(root, database):
    """Load the last complete index immediately while a fresh scan runs in the background."""
    if not database.exists():
        return []
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT path,size,camera,start,ending FROM videos").fetchall()
    except sqlite3.Error:
        return []
    records = []
    for relative, size, camera, start, ending in rows:
        try:
            records.append({"path": root / relative, "size": size, "camera": camera,
                            "start": datetime.strptime(start, "%Y%m%d%H%M%S"),
                            "end": datetime.strptime(ending, "%Y%m%d%H%M%S")})
        except ValueError:
            continue
    return sorted(records, key=lambda item: item["start"])


def refresh_inventory():
    """Build the expensive recursive index off the HTTP request path."""
    now = time.monotonic()
    records = []
    try:
        if not INPUT_ROOT.is_dir():
            snapshot = {
                "updated_at": now, "records": [],
                "diagnostics": {"path": str(INPUT_ROOT), "available": False,
                                "message": "容器内未找到 /input 挂载目录"},
            }
        else:
            children = sorted(path.name for path in INPUT_ROOT.iterdir())[:8]
            records = cached_records(INPUT_ROOT, INVENTORY_DB_PATH)
            if records:
                with INVENTORY_LOCK:
                    INVENTORY.update({
                        "updated_at": now, "records": records,
                        "diagnostics": {"path": str(INPUT_ROOT), "available": True,
                                        "mp4_files": len(records), "recognized_files": len(records),
                                        "index_database": str(INVENTORY_DB_PATH),
                                        "message": "正在后台校验录像目录，当前显示上次完成的索引。"},
                    })
            records, mp4_files = indexed_records(INPUT_ROOT, INVENTORY_DB_PATH)
            snapshot = {
                "updated_at": now, "records": records,
                "diagnostics": {"path": str(INPUT_ROOT), "available": True, "children": children,
                                "mp4_files": mp4_files, "recognized_files": len(records),
                                "index_database": str(INVENTORY_DB_PATH),
                                "examples": [str(record["path"].relative_to(INPUT_ROOT)) for record in records[:3]]},
            }
    except Exception as error:
        snapshot = {"updated_at": now, "records": records,
                    "diagnostics": {"path": str(INPUT_ROOT), "available": bool(records), "message": str(error)}}
    # A large NAS scan can take longer than the cache TTL.  Stamp completion
    # time here, rather than its start time, to avoid immediately re-indexing.
    snapshot["updated_at"] = time.monotonic()
    with INVENTORY_LOCK:
        INVENTORY.update(snapshot)
        INVENTORY["indexing"] = False
    METADATA_DIRTY.set()


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
            snapshot["diagnostics"] = {"path": str(INPUT_ROOT), "available": False,
                                       "message": "正在后台建立历史录像索引，请稍候…"}
        return snapshot


def available_dates(snapshot=None):
    records = (snapshot or inventory_snapshot())["records"]
    records_by_day = {}
    for record in records:
        records_by_day.setdefault(record["start"].strftime("%Y%m%d"), []).append(record)
    source_days = set(records_by_day)
    result_days = {path.stem.removeprefix("events-") for path in OUTPUT_ROOT.glob("events-*.json")}
    processed_sources = set(load_ledger(OUTPUT_ROOT, include_results=False).get("sources", {}))
    active_days = {}
    exported = timelapses()
    for task in load_tasks()["tasks"]:
        if task.get("kind") == "scan" and task.get("status") in {"queued", "running"}:
            for day in task.get("dates", [task["date"]]):
                active_days[day] = task["status"]
    summaries = []
    for day in sorted(source_days | result_days, reverse=True):
        day_records = records_by_day.get(day, [])
        events = read_json(OUTPUT_ROOT / f"events-{day}.json", {})
        day_processed = (processed_sources | set(events.get("processed_sources", []))) - set(events.get("failed_sources", []))
        total_files = len(day_records)
        processed_files = sum(source_id(record) in day_processed for record in day_records)
        if day in active_days:
            scan_status = "scanning" if active_days[day] == "running" else "queued"
        elif total_files and processed_files == total_files:
            scan_status = "completed"
        elif processed_files or events.get("failed_files"):
            scan_status = "partial"
        elif day in result_days and events.get("schema_version") != 2:
            # Older releases may have a completed event file without a ledger entry.
            scan_status = "completed"
        else:
            scan_status = "pending"
        summary = event_summary(day, total_files, processed_files, scan_status, events)
        summary["source_cameras"] = sorted({record["camera"] for record in day_records})
        summary["timelapse_count"] = len(exports_for_date(day, exported))
        summaries.append(summary)
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


def refresh_overview():
    """All archive reads happen off the HTTP request path."""
    snapshot = inventory_snapshot()
    try:
        dates = available_dates(snapshot)
        library = timelapses()
        OVERVIEW.update({"dates": dates, "timelapses": library, "diagnostics": snapshot["diagnostics"],
                         "indexing": snapshot["indexing"], "updated_at": time.time()})
    except Exception as error:
        OVERVIEW["diagnostics"] = {"message": f"结果索引读取失败：{error}"}


def metadata_loop():
    while True:
        METADATA_DIRTY.clear()
        refresh_overview()
        METADATA_DIRTY.wait(30)


def status_snapshot():
    tasks = public_tasks()
    document = load_tasks()
    return {"tasks": tasks, "plans": public_plans(document), "queue_paused": document.get("paused", False),
            "queued_count": sum(t["status"] == "queued" for t in tasks),
            "indexing": inventory_status()["indexing"]}


def task_command(task):
    command = [sys.executable, "-u", str(APP_ROOT / "person_timelapse.py")]
    if task["kind"] == "scan":
        settings = PROFILES[task["profile"]]
        command += ["scan", str(INPUT_ROOT), str(OUTPUT_ROOT), "--date", task["date"],
                    "--run-id", task["id"],
                    "--sample-seconds", settings["sample_seconds"], "--motion-threshold", settings["motion_threshold"],
                    "--keepalive-seconds", "60", "--imgsz", settings["imgsz"]]
        return command + (["--force"] if task.get("force") else []) + (["--retry-failed"] if task.get("retry_failed") else [])
    events = [str(OUTPUT_ROOT / f"events-{day}.json") for day in task["dates"]]
    return command + ["export", str(INPUT_ROOT), str(OUTPUT_ROOT), *events, "--camera", task["camera"]]


def dependency_state(task, tasks):
    """Return ready/wait/error state for a queued task with explicit prerequisites."""
    dependencies = task.get("depends_on", [])
    if not dependencies:
        return "ready", ""
    by_id = {item.get("id"): item for item in tasks}
    missing = [task_id for task_id in dependencies if task_id not in by_id]
    if missing:
        return "error", "前置扫描任务记录缺失，无法安全导出。"
    failed = [by_id[task_id] for task_id in dependencies
              if by_id[task_id].get("status") in {"failed", "cancelled", "interrupted", "blocked"}]
    if failed:
        days = "、".join(item.get("date", "未知日期") for item in failed)
        return "error", f"前置扫描未成功（{days}），范围延时未执行。"
    if any(by_id[task_id].get("status") not in {"completed", "partial"} for task_id in dependencies):
        return "waiting", "等待范围内的扫描任务完成"
    return "ready", ""


def validate_export_task(task):
    missing = [day for day in task["dates"] if not (OUTPUT_ROOT / f"events-{day}.json").exists()]
    if missing:
        raise RuntimeError(f"缺少人物扫描结果：{missing[0]}{' 等' if len(missing) > 1 else ''}")
    event_count = 0
    summaries = []
    for day in task["dates"]:
        events = read_json(OUTPUT_ROOT / f"events-{day}.json", {"events": {}})
        summaries.append({"cameras": [camera for camera, found in events.get("events", {}).items() if found]})
    task["camera"] = resolve_camera(summaries, task["camera"])
    for day in task["dates"]:
        events = read_json(OUTPUT_ROOT / f"events-{day}.json", {"events": {}})
        event_count += len(events.get("events", {}).get(task["camera"], []))
    if not event_count:
        raise RuntimeError(f"所选范围内没有 {task['camera']} 路的人物事件；请切换摄像头后重新制作。")


def resolve_camera(summaries, requested_camera):
    """Prefer the only camera that actually has person events in a range."""
    cameras = {camera for summary in summaries for camera in summary.get("cameras", [])}
    if not cameras:
        cameras = {camera for summary in summaries for camera in summary.get("source_cameras", [])}
    if requested_camera not in cameras and len(cameras) == 1:
        return cameras.pop()
    return requested_camera


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
    while True:
        with LOCK:
            if mac_priority_active():
                return
            tasks = load_tasks()
            if tasks.get("paused"):
                return
            if any(task["status"] == "running" for task in tasks["tasks"]):
                return
            changed, task = repair_dependencies(tasks["tasks"]), None
            for candidate in tasks["tasks"]:
                if candidate.get("status") != "queued":
                    continue
                if candidate.get("not_before", 0) > time.time():
                    continue
                state, detail = dependency_state(candidate, tasks["tasks"])
                if state == "waiting":
                    if candidate.get("detail") != detail:
                        candidate["detail"] = detail
                        changed = True
                    continue
                if state == "error":
                    candidate["status"] = "blocked"
                    candidate["detail"] = detail
                    candidate["finished_at"] = now_iso()
                    changed = True
                    continue
                task = candidate
                task["status"] = "running"
                task["started_at"] = now_iso()
                task["detail"] = "任务已启动"
                changed = True
                break
            if changed:
                save_tasks(tasks)
            if task:
                break
            if not changed:
                return
    thread = threading.Thread(target=task_worker, args=(task, None, task["progress_file"]), daemon=True)
    thread.start()


def repair_dependencies(tasks):
    changed = False
    for task in tasks:
        if task.get("kind") != "export" or task.get("status") not in {"queued", "blocked"}:
            continue
        by_id = {item["id"]: item for item in tasks}
        dependencies = list(task.get("depends_on", []))
        if not dependencies:
            for day in task["dates"]:
                latest = next((item for item in reversed(tasks) if item.get("kind") == "scan" and item.get("date") == day
                               and item.get("status") in {"queued", "running", "paused", "completed", "partial"}), None)
                if latest:
                    dependencies.append(latest["id"])
        replacement = []
        for identity in dependencies:
            previous = by_id.get(identity, {})
            if previous.get("status") in {"failed", "interrupted", "cancelled", "blocked"}:
                newer = next((item for item in reversed(tasks) if item.get("kind") == "scan"
                              and item.get("date") == previous.get("date")
                              and item.get("status") in {"queued", "running", "completed", "partial", "paused"}), None)
                if newer:
                    identity = newer["id"]
            replacement.append(identity)
        if task.get("depends_on", []) != replacement:
            task["depends_on"] = replacement
            changed = True
        if task["status"] == "blocked" and dependency_state(task, tasks)[0] != "error":
            task["status"], task["detail"] = "queued", "前置扫描已恢复，重新等待合成。"
            task.pop("finished_at", None)
            changed = True
    return changed


def terminate_process(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except ProcessLookupError:
        pass


def scheduler_loop():
    while True:
        try:
            start_next_task()
        except Exception as error:
            print(f"[scheduler] {error}", flush=True)
        SCHEDULER_WAKE.wait(5)
        SCHEDULER_WAKE.clear()


def task_worker(task, command, progress_file):
    task_id = task["id"]
    environment = os.environ.copy()
    runtime = Path(os.environ.get("RUNTIME_ROOT", tempfile.gettempdir())) / "person-timelapse"
    progress_path = runtime / progress_file
    environment["PROGRESS_PATH"] = str(progress_path)
    environment["INVENTORY_DB_PATH"] = str(INVENTORY_DB_PATH)
    initial = {
        "phase": "starting",
        "percent": 0,
        "elapsed_seconds": 0,
        "current_file": "正在加载人物识别模型…",
        "heartbeat_at": time.time(),
    }
    LIVE_PROGRESS[task_id] = initial
    process = None
    log_path = None
    timed_out = False
    try:
        runtime.mkdir(parents=True, exist_ok=True)
        write_json(progress_path, initial)
        if task["kind"] == "export":
            validate_export_task(task)
        command = task_command(task)
        with LOCK:
            current = load_tasks()
            saved = next(item for item in current["tasks"] if item["id"] == task_id)
            saved["camera"] = task["camera"]
            if saved.get("cancel_requested") or saved.get("pause_requested"):
                raise RuntimeError("任务在启动前已请求停止")
            save_tasks(current)
        log_path = runtime / f"task-{task_id}.log"
        print(f"[task {task_id}] starting {task['kind']} {task['date']}", flush=True)
        with log_path.open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=environment, start_new_session=True)
            PROCESSES[task_id] = process
            while process.poll() is None:
                progress = read_json(progress_path, LIVE_PROGRESS[task_id])
                LIVE_PROGRESS[task_id] = progress
                heartbeat = progress.get("heartbeat_at", initial["heartbeat_at"])
                if time.time() - heartbeat > STALLED_SECONDS:
                    timed_out = True
                    terminate_process(process)
                    break
                with LOCK:
                    saved = next(item for item in load_tasks()["tasks"] if item["id"] == task_id)
                if saved.get("cancel_requested") or saved.get("pause_requested"):
                    terminate_process(process)
                    break
                time.sleep(1)
        with log_path.open("rb") as log:
            log.seek(max(0, log_path.stat().st_size - 4000))
            detail = log.read().decode("utf-8", errors="replace").strip()
        status = ("completed" if process.returncode == 0 else "partial" if process.returncode == 2 else
                  "queued" if process.returncode == 3 else "failed")
        if timed_out:
            source = LIVE_PROGRESS[task_id].get("source")
            if task["kind"] == "scan" and source:
                record = {"path": Path(source["path"]), "camera": source["camera"],
                          "start": datetime.fromisoformat(source["start"]), "end": datetime.fromisoformat(source["end"]),
                          "size": int(source["source_id"].rsplit(":", 1)[1])}
                commit_result(OUTPUT_ROOT, record, source["settings"], error="文件处理超时，已跳过；可单独重试", run_id=task_id)
                status = "queued" if task.get("timeout_retries", 0) < 10 else "partial"
            detail = f"超过 {STALLED_SECONDS:g} 秒没有进度，已停止卡住的进程。" + detail[-2000:]
        LIVE_PROGRESS[task_id] = read_json(progress_path, LIVE_PROGRESS[task_id])
    except Exception as error:  # pragma: no cover - defensive boundary for background work
        status = "failed"
        detail = str(error)
    finally:
        if process and process.poll() is None:
            terminate_process(process)
    final_status = status
    with LOCK:
        PROCESSES.pop(task_id, None)
        tasks = load_tasks()
        for task in tasks["tasks"]:
            if task["id"] == task_id:
                task["status"] = ("cancelled" if task.get("cancel_requested") else
                                  ("paused" if tasks.get("paused") else "queued") if task.get("pause_requested") else status)
                if task["status"] == "queued":
                    task.pop("pause_requested", None)
                    task.pop("finished_at", None)
                else:
                    task["finished_at"] = now_iso()
                if task["status"] == "paused":
                    detail = "已暂停；已完成文件的检查点已保留。"
                elif task["status"] == "cancelled":
                    detail = "任务已取消；已完成的结果已保留。"
                task["detail"] = detail
                final_status = task["status"]
                if status == "queued":
                    if timed_out:
                        task["timeout_retries"] = task.get("timeout_retries", 0) + 1
                        task["retry_failed"] = False
                    else:
                        task["not_before"] = time.time() + 30
                else:
                    task.pop("not_before", None)
        save_tasks(tasks)
    if log_path and log_path.exists():
        try:
            shutil.copy2(log_path, OUTPUT_ROOT / log_path.name)
        except OSError as error:
            print(f"[task {task_id}] 日志归档失败：{error}", flush=True)
    start_next_task()
    print(f"[task {task_id}] {final_status}: {detail[-2000:]}", flush=True)


def create_task(payload):
    kind = payload.get("kind")
    date = payload.get("date", "")
    if kind not in {"scan", "export", "timelapse"} or not DATE_PATTERN.fullmatch(date):
        raise ValueError("任务类型或日期无效")
    camera = payload.get("camera", "")
    profile = payload.get("profile", "balanced")
    if profile not in PROFILES:
        raise ValueError("性能档位无效")
    scan_requested = kind in {"scan", "timelapse"}
    export_requested = kind in {"export", "timelapse"}
    if export_requested and not CAMERA_PATTERN.fullmatch(camera):
        raise ValueError("导出任务需要摄像头编号")
    end_date = payload.get("end_date") or date
    if not DATE_PATTERN.fullmatch(end_date) or end_date < date:
        raise ValueError("结束日期无效")
    all_dates = OVERVIEW["dates"] if STATE_READY else available_dates(inventory_snapshot())
    summaries = [item for item in all_dates if date <= item["date"] <= end_date]
    dates = [item["date"] for item in sorted(summaries, key=lambda item: item["date"])]
    if not dates:
        raise ValueError("该日期范围内没有已索引的录像。")
    if export_requested:
        camera = resolve_camera(summaries, camera)
    if kind == "export":
        missing = [day for day in dates if not (OUTPUT_ROOT / f"events-{day}.json").exists()]
        if missing:
            raise ValueError(f"请先完成日期扫描：{missing[0]}{' 等' if len(missing) > 1 else ''}")

    with LOCK:
        tasks = load_tasks()
        if export_requested and any(
            item.get("kind") == "export" and item.get("status") in {"queued", "running"}
            and item.get("camera") == camera and item.get("dates") == dates
            for item in tasks["tasks"]
        ):
            raise ValueError("这个日期范围的延时任务已在队列中。")
        created, scan_created = [], 0
        dependency_ids = []
        if scan_requested:
            active_scans = {
                day: item for item in tasks["tasks"]
                if item.get("kind") == "scan" and item.get("status") in {"queued", "running"}
                for day in item.get("dates", [item.get("date")])
            }
            selected = []
            settings = PROFILES[profile]
            requested = scan_settings(float(settings["sample_seconds"]), .45, 20,
                                      float(settings["motion_threshold"]), 60, int(settings["imgsz"]))
            for item in sorted(summaries, key=lambda item: item["date"]):
                event_exists = (OUTPUT_ROOT / f"events-{item['date']}.json").exists()
                quality_change = any(not settings_satisfied({"settings": previous}, requested)
                                     for previous in item.get("settings", []))
                legacy_upgrade = item.get("sample_seconds") and item["sample_seconds"] > requested["sample_seconds"]
                force = payload.get("force") is True or bool(legacy_upgrade)
                needs_scan = item["scan_status"] not in {"completed", "queued", "scanning"} or quality_change or force
                if kind == "timelapse":
                    needs_scan = needs_scan or not event_exists
                active_task = active_scans.get(item["date"])
                if active_task:
                    dependency_ids.append(active_task["id"])
                elif needs_scan:
                    selected.append((item, force or (item["scan_status"] == "completed" and not event_exists)))
            if kind == "scan" and not selected:
                raise ValueError("这个范围内的录像均已完成扫描，或已在队列中。")
            for item, force in selected:
                task = {
                    "id": uuid.uuid4().hex[:8], "kind": "scan", "date": item["date"],
                    "camera": None, "profile": profile, "status": "queued",
                    "created_at": now_iso(),
                    "detail": "等待扫描", "dates": [item["date"]], "force": force,
                    "retry_failed": payload.get("retry_failed") is True or kind == "scan" or force,
                }
                task["progress_file"] = f"progress-{task['id']}.json"
                tasks["tasks"].append(task)
                created.append(task)
                scan_created += 1
                dependency_ids.append(task["id"])
        if export_requested:
            task = {
                "id": uuid.uuid4().hex[:8], "kind": "export", "date": date,
                "camera": camera or None, "profile": None, "status": "queued",
                "created_at": now_iso(),
                "detail": "等待范围扫描后导出" if kind == "timelapse" else "等待导出", "dates": dates,
                "depends_on": sorted(set(dependency_ids)),
            }
            task["progress_file"] = f"progress-{task['id']}.json"
            tasks["tasks"].append(task)
            created.append(task)
        plan_id = uuid.uuid4().hex[:8]
        tasks.setdefault("plans", []).append({
            "id": plan_id, "kind": kind, "start_date": date, "end_date": end_date,
            "dates": dates, "camera": camera or None, "created_at": now_iso(),
            "scan_ids": sorted(set(dependency_ids)),
            "export_id": created[-1]["id"] if export_requested else None,
            "ready_dates": [item["date"] for item in summaries if item["scan_status"] == "completed"],
        })
        save_tasks(tasks)
    start_next_task()
    return {"id": created[0]["id"], "plan_id": plan_id, "created": len(created), "scan_created": scan_created,
            "export_created": export_requested, "dates": dates, "camera": camera}


def cancel_task(task_id):
    with LOCK:
        tasks = load_tasks()
        task = next((item for item in tasks["tasks"] if item["id"] == task_id), None)
        if not task or task["status"] not in {"running", "queued", "paused", "blocked"}:
            raise ValueError("没有可取消的任务")
        if task["status"] != "running":
            task["status"] = "cancelled"
            task["detail"] = "已从队列移除"
            save_tasks(tasks)
            return {"id": task_id, "status": "cancelled"}
        task["cancel_requested"] = True
        task["detail"] = "正在停止任务…"
        save_tasks(tasks)
    return {"id": task_id, "status": "cancelling"}


def retry_task(task_id):
    with LOCK:
        tasks = load_tasks()
        task = next((item for item in tasks["tasks"] if item["id"] == task_id), None)
        if not task or task["status"] in {"running", "queued"}:
            raise ValueError("该任务不需要重试")
        task.update(status="queued", detail="已重新排队，将跳过已成功处理的文件。", retry_failed=True, timeout_retries=0)
        for key in ("cancel_requested", "pause_requested", "finished_at", "not_before"):
            task.pop(key, None)
        save_tasks(tasks)
    SCHEDULER_WAKE.set()
    return {"id": task_id, "status": "queued"}


def queue_action(action):
    if action not in {"pause", "resume"}:
        raise ValueError("队列操作无效")
    with LOCK:
        tasks = load_tasks()
        tasks["paused"] = action == "pause"
        for task in tasks["tasks"]:
            if action == "pause" and task["status"] == "running":
                task["pause_requested"] = True
                task["detail"] = "正在保存检查点并暂停…"
            elif action == "resume" and task["status"] == "paused":
                task["status"] = "queued"
                task.pop("pause_requested", None)
        save_tasks(tasks)
    SCHEDULER_WAKE.set()
    return {"queue_paused": tasks["paused"]}


class ConsoleHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(APP_ROOT), **kwargs)

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
        try:
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
            self.send_json({**OVERVIEW, **status_snapshot(), "profiles": PROFILES})
            return
        if path == "/api/status":
            self.send_json(status_snapshot())
            return
        if path.startswith("/api/date/"):
            date = path.rsplit("/", 1)[-1]
            if not DATE_PATTERN.fullmatch(date):
                self.send_json({"error": "日期无效"}, HTTPStatus.BAD_REQUEST)
                return
            events = read_json(OUTPUT_ROOT / f"events-{date}.json", {"date": date, "events": {}})
            self.send_json({"events": events, "exports": exports_for_date(date, OVERVIEW["timelapses"])})
            return
        if path.startswith("/media/"):
            relative = Path(unquote(path.removeprefix("/media/")))
            target = (OUTPUT_ROOT / relative).resolve()
            if (OUTPUT_ROOT.resolve() not in target.parents or not target.is_file()
                    or target.suffix.lower() not in {".mp4", ".jpg", ".jpeg", ".png"}
                    or any(part.startswith(".") for part in relative.parts)):
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
        elif path in {"/web/index.html", "/web/login.html", "/web/app.js", "/web/style.css"}:
            self.path = path
        else:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        return super().do_GET()

    def do_HEAD(self):
        # The inherited handler would inspect arbitrary files in the working
        # directory without going through our authentication and route checks.
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, POST, DELETE")
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                password = json.loads(self.rfile.read(length)).get("password", "")
                if not isinstance(password, str) or not hmac.compare_digest(password.encode(), ACCESS_PASSWORD.encode()):
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
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length)) if length else {}
            if path == "/api/tasks":
                self.send_json(create_task(payload), HTTPStatus.CREATED)
            elif path == "/api/queue":
                self.send_json(queue_action(payload.get("action")))
            elif path.startswith("/api/tasks/") and path.endswith("/retry"):
                self.send_json(retry_task(path.split("/")[-2]))
            elif path == "/api/index":
                with INVENTORY_LOCK:
                    INVENTORY["updated_at"] = 0
                inventory_snapshot()
                self.send_json({"indexing": True})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
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
    global ACCESS_PASSWORD, STATE_READY, TASK_CACHE, TASK_CACHE_PATH
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    ACCESS_PASSWORD = load_access_password()
    recover_interrupted_tasks()
    TASK_CACHE, TASK_CACHE_PATH = load_tasks(), TASKS_PATH
    STATE_READY = True
    threading.Thread(target=metadata_loop, daemon=True, name="result-metadata").start()
    threading.Thread(target=scheduler_loop, daemon=True, name="task-scheduler").start()
    inventory_snapshot()
    port = int(os.environ.get("PORT", "8790"))
    server = ThreadingHTTPServer(("0.0.0.0", port), ConsoleHandler)
    print(f"Person Timelapse Console listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
