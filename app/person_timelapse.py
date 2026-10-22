#!/usr/bin/env python3
"""Read-only person-event indexing and timelapse export for Xiaomi MP4 files."""

import argparse
import hashlib
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from storage import atomic_json, shared_lock

VIDEO_PATTERN = re.compile(
    r"^video_\d+_(?P<camera>\d+)_\d+_(?P<start>\d{14})_(?P<end>\d{14})\.mp4$", re.IGNORECASE
)
NAME_PATTERN = re.compile(
    r"^(?P<camera>\d+)_(?P<start>\d{14})_(?P<end>\d{14})\.mp4$", re.IGNORECASE
)
LEGACY_PATTERN = re.compile(
    r"^(?P<start>\d{14})_(?P<end>\d{14})\.mp4$", re.IGNORECASE
)
TIME_FORMAT = "%Y%m%d%H%M%S"


def video_paths(root):
    """Include .MP4 while never following symlinks outside a recording mount."""
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                elif entry.name.lower().endswith(".mp4") and entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)


def parse_record(path: Path):
    match = VIDEO_PATTERN.match(path.name) or NAME_PATTERN.match(path.name)
    legacy = False
    if not match:
        match = LEGACY_PATTERN.match(path.name)
        legacy = bool(match)
    if not match:
        return None
    values = match.groupdict()
    if legacy:
        values["camera"] = "legacy"
    try:
        values["start"] = datetime.strptime(values["start"], TIME_FORMAT)
        values["end"] = datetime.strptime(values["end"], TIME_FORMAT)
    except ValueError:
        return None
    if values["end"] <= values["start"]:
        return None
    values["path"] = path
    return values


def find_records(root: Path, day: Optional[str] = None, days=None, database=None):
    # An existing index is authoritative for a queued task. Do not walk the
    # entire archive again for every individual recording date.
    if database and Path(database).exists():
        selected_days = sorted(set(days or ([day] if day else [])))
        query = "SELECT path,size,camera,start,ending FROM videos"
        arguments = []
        if selected_days:
            query += " WHERE " + " OR ".join("(start >= ? AND start < ?)" for _ in selected_days)
            for selected in selected_days:
                following = (datetime.strptime(selected, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
                arguments.extend((selected, following))
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2) as connection:
            rows = connection.execute(query, arguments).fetchall()
        indexed = []
        for relative, size, camera, start, end in rows:
            if (day and start[:8] != day) or (days and start[:8] not in days):
                continue
            indexed.append({"path": root / relative, "size": size, "camera": camera,
                            "start": datetime.strptime(start, TIME_FORMAT),
                            "end": datetime.strptime(end, TIME_FORMAT)})
        return sorted(indexed, key=lambda item: (item["camera"], item["start"], item["path"].name))
    records = []
    for path in video_paths(root):
        record = parse_record(path)
        if record and (not day or record["start"].strftime("%Y%m%d") == day) and (
            not days or record["start"].strftime("%Y%m%d") in days
        ):
            records.append(record)
    return sorted(records, key=lambda item: (item["camera"], item["start"], item["path"].name))


def source_id(record):
    size = record.get("size")
    if size is None:
        size = record["path"].stat().st_size
    return ":".join((
        record["camera"], record["start"].strftime(TIME_FORMAT), record["end"].strftime(TIME_FORMAT), str(size)
    ))


def result_path(output_root, identity):
    day = identity.split(":")[1][:8]
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return output_root / "scan-results" / day / f"{digest}.json"


def scan_results(output_root, day=None):
    directory = output_root / "scan-results"
    directory = directory / day if day else directory
    for path in directory.glob("*.json" if day else "*/*.json"):
        yield json.loads(path.read_text(encoding="utf-8"))


def load_ledger(output_root: Path, day=None, include_results=True):
    path = output_root / "processed.json"
    ledger = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"sources": {}}
    for result in (scan_results(output_root, day) if include_results else []):
        identity = result["source_id"]
        if result["status"] == "completed":
            ledger["sources"][identity] = result
        else:
            ledger["sources"].pop(identity, None)
    return ledger


def save_ledger(output_root: Path, ledger):
    # Compatibility for older callers. New scans commit one independent file
    # at a time and never replace the shared legacy ledger.
    with shared_lock(output_root / ".ledger.lock"):
        target = output_root / "processed.json"
        current = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"sources": {}}
        current["sources"].update(ledger["sources"])
        atomic_json(target, current)


def scan_settings(sample_seconds, confidence, merge_seconds, motion_threshold, keepalive_seconds, imgsz):
    return {"sample_seconds": sample_seconds, "confidence": confidence, "merge_seconds": merge_seconds,
            "motion_threshold": motion_threshold, "keepalive_seconds": keepalive_seconds, "imgsz": imgsz,
            "decode_mode": scan_decode_mode(sample_seconds)}


def scan_decode_mode(sample_seconds):
    mode = os.environ.get("SCAN_DECODE_MODE", "auto")
    if mode not in {"auto", "keyframes", "accurate"}:
        raise ValueError("SCAN_DECODE_MODE must be auto, keyframes or accurate")
    return ("keyframes" if sample_seconds >= 10 else "accurate") if mode == "auto" else mode


def settings_satisfied(entry, requested):
    previous = entry.get("settings")
    if not previous:
        return True  # Existing archives remain usable; explicit rescan upgrades them.
    old_motion, new_motion = previous.get("motion_threshold"), requested.get("motion_threshold")
    return (previous["sample_seconds"] <= requested["sample_seconds"]
            and previous["imgsz"] >= requested["imgsz"]
            and previous["confidence"] == requested["confidence"]
            and (requested.get("decode_mode", "accurate") != "accurate"
                 or previous.get("decode_mode", "accurate") == "accurate")
            and (old_motion is None or (new_motion is not None and old_motion <= new_motion)))


def rebuild_events(output_root, day, merge_seconds=20):
    """Rebuild a date snapshot without overwriting another node's file results."""
    with shared_lock(output_root / "locks" / f"events-{day}", timeout=30):
        target = output_root / f"events-{day}.json"
        backup = output_root / "legacy-events" / f"events-{day}.json"
        if not backup.exists():
            previous = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
            atomic_json(backup, previous.get("events", {}) if previous.get("schema_version") != 2 else {})
        groups = json.loads(backup.read_text(encoding="utf-8"))
        results = list(scan_results(output_root, day))
        for result in results:
            camera = result["camera"]
            retained = []
            for event in groups.get(camera, []):
                if not overlaps(datetime.fromisoformat(result["start"]), datetime.fromisoformat(result["end"]),
                                datetime.fromisoformat(event["start"]), datetime.fromisoformat(event["end"])):
                    retained.append(event)
                else:
                    if event["start"] < result["start"]:
                        retained.append({**event, "end": result["start"]})
                    if event["end"] > result["end"]:
                        retained.append({**event, "start": result["end"]})
            groups[camera] = retained
        for result in results:
            for camera, events in result.get("events", {}).items():
                groups.setdefault(camera, []).extend(events)
        groups = {camera: events for camera, events in groups.items() if events}
        payload = {"date": day, "schema_version": 2, "events": merge_events(groups, merge_seconds),
                   "failed_files": sum(r["status"] == "failed" for r in results),
                   "processed_sources": [r["source_id"] for r in results if r["status"] == "completed"],
                   "failed_sources": [r["source_id"] for r in results if r["status"] == "failed"],
                   "settings": list({json.dumps(r["settings"], sort_keys=True): r["settings"]
                                     for r in results if r["status"] == "completed"}.values())}
        atomic_json(target, payload)
        return payload


def commit_result(output_root, record, settings, events=None, error=None, run_id=None):
    identity = source_id(record)
    target = result_path(output_root, identity)
    if error and target.exists():
        events = json.loads(target.read_text(encoding="utf-8")).get("events", {})
    elif error:
        # A failed upgrade must not erase useful results made by older versions.
        previous = output_root / f"events-{record['start']:%Y%m%d}.json"
        groups = json.loads(previous.read_text(encoding="utf-8")).get("events", {}) if previous.exists() else {}
        preserved = []
        for event in groups.get(record["camera"], []):
            start, end = datetime.fromisoformat(event["start"]), datetime.fromisoformat(event["end"])
            if overlaps(record["start"], record["end"], start, end):
                preserved.append({**event, "start": max(start, record["start"]).isoformat(timespec="seconds"),
                                  "end": min(end, record["end"]).isoformat(timespec="seconds")})
        events = {record["camera"]: preserved} if preserved else {}
    result = {"source_id": identity, "path": str(record["path"]), "camera": record["camera"],
              "start": record["start"].isoformat(timespec="seconds"), "end": record["end"].isoformat(timespec="seconds"),
              "status": "failed" if error else "completed", "settings": settings,
              "run_id": run_id,
              "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "events": events or {}, "error": error}
    atomic_json(target, result)
    rebuild_events(output_root, record["start"].strftime("%Y%m%d"), settings.get("merge_seconds", 20))


def report_progress(**values):
    """Write a replace-in-place progress snapshot when launched by the Web console."""
    path = os.environ.get("PROGRESS_PATH")
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    values["heartbeat_at"] = time.time()
    atomic_json(target, values, durable=False)


def merge_events(event_groups, merge_seconds):
    merged = defaultdict(list)
    for camera, events in event_groups.items():
        for event in sorted(events, key=lambda item: item["start"]):
            if merged[camera] and (
                datetime.fromisoformat(event["start"]) - datetime.fromisoformat(merged[camera][-1]["end"])
            ).total_seconds() <= merge_seconds:
                merged[camera][-1]["end"] = max(merged[camera][-1]["end"], event["end"])
                merged[camera][-1]["hits"] += event["hits"]
            else:
                merged[camera].append(dict(event))
    return dict(merged)


def summarize(records):
    cameras = defaultdict(lambda: {"files": 0, "seconds": 0.0, "bytes": 0})
    for record in records:
        camera = cameras[record["camera"]]
        camera["files"] += 1
        camera["seconds"] += (record["end"] - record["start"]).total_seconds()
        camera["bytes"] += record["path"].stat().st_size
    return {
        "files": len(records),
        "seconds": sum(camera["seconds"] for camera in cameras.values()),
        "bytes": sum(camera["bytes"] for camera in cameras.values()),
        "first": records[0]["start"].isoformat(sep=" ") if records else None,
        "last": max((record["end"] for record in records), default=None).isoformat(sep=" ") if records else None,
        "cameras": dict(cameras),
        "days": sorted({record["start"].strftime("%Y%m%d") for record in records}),
    }


def save_thumbnail(frame, output_root: Path, camera: str, moment: datetime):
    import cv2

    directory = output_root / "thumbnails" / camera
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{moment.strftime(TIME_FORMAT)}.jpg"
    if not target.exists():
        cv2.imwrite(str(target), frame)
    return target


def scan_media_process(command, heartbeat, timeout, allow_empty=False):
    """A bounded media read; cancellation always reaps its decoder child."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise RuntimeError(f"视频读取超时（{timeout:g} 秒）")
                try:
                    data, _ = process.communicate(timeout=min(2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    heartbeat()
            errors.seek(0)
            detail = errors.read().decode("utf-8", errors="replace")
            if process.returncode and (data or not allow_empty):
                reason = " ".join(detail[-2000:].split())
                raise RuntimeError(f"视频读取失败（{Path(command[0]).name} 退出码 {process.returncode}）"
                                   + (f"：{reason}" if reason else ""))
            return data, detail
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stdout.close()


def decode_scan_frame(path, offset, duration, imgsz, heartbeat, mode):
    """Seek at the demuxer, decode one picture, and retain its actual PTS.

    Sparse scans skip inter frames *in the decoder*, not after full-resolution
    decoding. Do not use an fps filter: it can silently duplicate keyframes and
    assign them a different timestamp. A missing final keyframe gets one exact
    tail read, never a full-file retry.
    """
    import cv2
    import numpy as np

    timeout = float(os.environ.get("SCAN_DECODE_TIMEOUT_SECONDS", "90"))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("SCAN_DECODE_TIMEOUT_SECONDS must be finite and positive")
    edge = max(640, imgsz)
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "1"]
    if mode == "keyframes":
        command += ["-skip_frame", "nokey"]
    command += ["-ss", str(offset), "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
                "-t", str(duration - offset), "-vf",
                f"scale='min({edge},iw)':'min({edge},ih)':force_original_aspect_ratio=decrease,showinfo=checksum=0",
                "-frames:v", "1", "-threads", "1", "-c:v", "mjpeg", "-q:v", "3", "-f", "image2pipe", "pipe:1"]
    # Some FFmpeg versions fail encoder initialization when no keyframe remains.
    # Only empty keyframe output may fall back; never accept a nonzero exit with
    # partially written image data as a successful sample.
    data, detail = scan_media_process(command, heartbeat, timeout, allow_empty=mode == "keyframes")
    timestamps = re.findall(r"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:\s*([-+\d.eE]+)", detail)
    actual = offset + float(timestamps[0]) if timestamps else float("nan")
    if mode == "keyframes" and (not data or (math.isfinite(actual) and actual >= duration)):
        return decode_scan_frame(path, offset, duration, imgsz, heartbeat, "accurate")
    if not data or not math.isfinite(actual) or actual < 0 or actual >= duration:
        raise RuntimeError("无法读取有效采样画面或画面时间戳")
    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("采样画面解码失败")
    return frame, actual


def scan(records, output_root: Path, sample_seconds: float, confidence: float, merge_seconds: float,
         motion_threshold: Optional[float] = None, keepalive_seconds: float = 60.0, imgsz: int = 640,
         device: Optional[str] = None, checkpoint=None, force=False, run_id=None):
    import cv2
    from ultralytics import YOLO

    if not math.isfinite(sample_seconds) or sample_seconds <= 0 or imgsz <= 0:
        raise ValueError("采样间隔与识别尺寸必须大于零")
    events = defaultdict(list)
    samples = 0
    inferences = 0
    planned = [math.ceil((record["end"] - record["start"]).total_seconds() / sample_seconds) for record in records]
    work_done, file_samples, completed_files, failed_files, duration_adjusted_files = 0, 0, 0, 0, 0
    started = time.monotonic()
    settings = scan_settings(sample_seconds, confidence, merge_seconds, motion_threshold, keepalive_seconds, imgsz)
    step, step_started, timings = "model", started, defaultdict(float)

    def set_step(value):
        nonlocal step, step_started
        now = time.monotonic()
        timings[step] += now - step_started
        step, step_started = value, now

    def timing(value):
        return round(timings[value] + (time.monotonic() - step_started if step == value else 0), 1)

    def update(index, record, offset):
        elapsed = max(time.monotonic() - started, 0.001)
        estimated_samples = max(1, sum(planned))
        processed = work_done + file_samples
        percent = min(99.9, processed / estimated_samples * 100)
        eta = elapsed / processed * max(estimated_samples - processed, 0) if processed >= 2 else None
        report_progress(
            phase="scanning", percent=round(percent, 1), current_file=record["path"].name,
            file_index=index, total_files=len(records), offset_seconds=round(offset, 1),
            samples=samples, estimated_samples=estimated_samples, yolo_inferences=inferences,
            step=step, step_elapsed_seconds=round(time.monotonic() - step_started, 1),
            decode_mode=settings["decode_mode"], decode_seconds=timing("decoding"),
            inference_seconds=timing("inference"), metadata_seconds=timing("metadata"),
            completed_files=completed_files, failed_files=failed_files, duration_adjusted_files=duration_adjusted_files,
            elapsed_seconds=round(elapsed, 1), eta_seconds=round(eta, 1) if eta is not None else None,
            source={"source_id": source_id(record), "path": str(record["path"]), "camera": record["camera"],
                    "start": record["start"].isoformat(), "end": record["end"].isoformat(), "settings": settings},
        )

    if records:
        update(1, records[0], 0)
    model = YOLO(os.environ.get("MODEL_PATH", "/models/yolo11n.pt"))
    for index, record in enumerate(records, start=1):
        file_samples = 0
        set_step("preparing")
        print(f"[{index}/{len(records)}] {record['path'].name}", flush=True)
        update(index, record, 0)
        try:
            lease = shared_lock(output_root / "locks" / result_path(output_root, source_id(record)).stem, timeout=1)
            with lease:
                saved = result_path(output_root, source_id(record))
                if checkpoint and saved.exists():
                    entry = json.loads(saved.read_text(encoding="utf-8"))
                    if (entry["status"] == "completed" and settings_satisfied(entry, settings)
                            and (not force or (run_id and entry.get("run_id") == run_id))):
                        work_done += planned[index - 1]
                        completed_files += 1
                        continue
                file_events, error, offset = defaultdict(list), None, 0.0
                try:
                    set_step("metadata")
                    update(index, record, offset)
                    nominal_duration = (record["end"] - record["start"]).total_seconds()
                    duration = min(probe_video_duration(record["path"], heartbeat=lambda: update(index, record, 0)),
                                   nominal_duration)
                    if not math.isfinite(duration) or duration <= 0:
                        raise RuntimeError("视频没有有效时长")
                    planned[index - 1] = math.ceil(duration / sample_seconds)
                    duration_adjusted_files += int(nominal_duration - duration > 1)
                    previous_gray, last_person, last_sample = None, datetime.min, -1.0
                    while offset < duration:
                        set_step("decoding")
                        update(index, record, offset)
                        frame, actual = decode_scan_frame(record["path"], offset, duration, imgsz,
                                                         lambda: update(index, record, offset), settings["decode_mode"])
                        set_step("motion")
                        file_samples += 1
                        samples += 1
                        offset = file_samples * sample_seconds
                        if actual <= last_sample:
                            continue  # Long GOPs can map adjacent requests to the same picture.
                        last_sample = actual
                        moment = record["start"] + timedelta(seconds=actual)
                        current_gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
                        motion = (previous_gray is None or motion_threshold is None
                                  or cv2.mean(cv2.absdiff(current_gray, previous_gray))[0] >= motion_threshold)
                        previous_gray = current_gray
                        recently_seen = (moment - last_person).total_seconds() <= keepalive_seconds
                        if motion_threshold is not None and not motion and not recently_seen:
                            continue
                        set_step("inference")
                        update(index, record, actual)
                        inferences += 1
                        result = model(frame, classes=[0], conf=confidence, imgsz=imgsz, verbose=False, device=device)[0]
                        set_step("saving")
                        if len(result.boxes):
                            last_person = moment
                            thumbnail = save_thumbnail(frame, output_root, record["camera"], moment)
                            # Keep a short context around a hit instead of producing a zero-duration event.
                            padding = timedelta(seconds=min(5, sample_seconds / 2))
                            file_events[record["camera"]].append({
                                "start": max(record["start"], moment - padding).isoformat(timespec="seconds"),
                                "end": min(record["start"] + timedelta(seconds=duration), moment + padding).isoformat(timespec="seconds"),
                                "hits": 1, "thumbnail": str(thumbnail.relative_to(output_root)),
                            })
                except Exception as exception:
                    error = str(exception)
                    print(f"scan failed: {record['path'].name}: {error}", file=sys.stderr, flush=True)
                set_step("checkpoint")
                update(index, record, offset)
                if checkpoint:
                    checkpoint(record, settings, merge_events(file_events, merge_seconds), error)
                work_done += planned[index - 1]
                file_samples = 0
                completed_files += int(not error)
                failed_files += int(bool(error))
                if not error:
                    for camera, found in file_events.items():
                        events[camera].extend(found)
                update(index, record, offset)
                print(f"file {'failed' if error else 'completed'}; decode={timing('decoding')}s "
                      f"inference={timing('inference')}s samples={samples}", flush=True)
        except TimeoutError:
            work_done += planned[index - 1]
            print(f"另一个节点正在扫描，稍后重试：{record['path'].name}", flush=True)
    print(f"samples={samples} yolo_inferences={inferences}", flush=True)
    report_progress(
        phase="completed", percent=100, file_index=len(records), total_files=len(records),
        samples=samples, estimated_samples=max(1, sum(planned)), yolo_inferences=inferences,
        decode_mode=settings["decode_mode"], decode_seconds=timing("decoding"), inference_seconds=timing("inference"),
        metadata_seconds=timing("metadata"), completed_files=completed_files, failed_files=failed_files,
        duration_adjusted_files=duration_adjusted_files,
        elapsed_seconds=round(time.monotonic() - started, 1), eta_seconds=0,
    )
    return merge_events(events, merge_seconds)


def read_events(events_path: Path, camera: str):
    data = json.loads(events_path.read_text(encoding="utf-8"))
    events = []
    for event in data["events"].get(camera, []):
        start, end = datetime.fromisoformat(event["start"]), datetime.fromisoformat(event["end"])
        # Older sparse scans stored point events. Give those existing archives
        # context too, without requiring another expensive person scan.
        if start == end:
            start, end = start - timedelta(seconds=5), end + timedelta(seconds=5)
        events.append((start, end))
    return events


def overlaps(start, end, event_start, event_end):
    return start <= event_end and end >= event_start


def valid_video(path, heartbeat=None, *, raise_errors=False):
    """Keep the library's bool API; exports get bounded waits and real errors."""
    try:
        timeout = 30.0
        if heartbeat is not None:
            try:
                timeout = float(os.environ.get("VIDEO_VALIDATION_TIMEOUT_SECONDS", "180"))
            except ValueError:
                timeout = 0
            if not math.isfinite(timeout) or timeout <= 0:
                raise RuntimeError("VIDEO_VALIDATION_TIMEOUT_SECONDS 必须为有限正数")
        data, _ = scan_media_process(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
             "-of", "json", str(path)], heartbeat if heartbeat is not None else lambda: None, timeout)
        try:
            metadata = json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise RuntimeError(f"ffprobe 返回了无效 JSON：{error}") from None
        if (not isinstance(metadata, dict) or not isinstance(metadata.get("format"), dict)
                or not isinstance(metadata.get("streams"), list)):
            raise RuntimeError("ffprobe 返回的视频元数据结构不完整")
        try:
            duration = float(metadata["format"].get("duration", 0))
        except (ValueError, TypeError):
            duration = 0
        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError("ffprobe 未读取到有效的正数视频时长")
        if not any(isinstance(stream, dict) and stream.get("codec_type") == "video"
                   and all(isinstance(stream.get(edge), int) and stream[edge] > 0 for edge in ("width", "height"))
                   for stream in metadata["streams"]):
            raise RuntimeError("ffprobe 未读取到具有有效宽高的视频流")
        return True
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
        if raise_errors:
            raise RuntimeError(f"视频校验失败：{error}") from None
        return False


def decode_export_frames(path, offset, frame_seconds, frame_count, heartbeat, mode=None):
    # Seek once per bounded batch, not once per output picture. On small ARM
    # NAS CPUs, decoding every 3K HEVC frame just to retain 1 fps is prohibitive.
    mode = mode or os.environ.get("EXPORT_DECODE_MODE", "keyframes")
    if mode not in {"keyframes", "accurate"}:
        raise ValueError("EXPORT_DECODE_MODE must be keyframes or accurate")
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "2"]
    if mode == "keyframes":
        command += ["-skip_frame", "nokey"]
    command += ["-ss", str(offset), "-i", str(path), "-map", "0:v:0", "-an",
                "-vf", f"fps=1/{frame_seconds}:start_time=0:round=up:eof_action=pass,scale=1280:-2",
                "-frames:v", str(frame_count), "-threads", "2", "-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-q:v", "3",
                "-f", "image2pipe", "pipe:1"]
    started = time.monotonic()
    timeout = float(os.environ.get("EXPORT_DECODE_TIMEOUT_SECONDS", "120"))
    with tempfile.TemporaryFile() as errors:
        decoder = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        try:
            while True:
                if time.monotonic() - started >= timeout:
                    raise RuntimeError(f"视频抽帧超时（{timeout:g} 秒）")
                try:
                    data, _ = decoder.communicate(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    heartbeat()
            if decoder.returncode:
                errors.seek(0)
                raise RuntimeError(errors.read().decode("utf-8", errors="replace")[-1000:] or "视频抽帧失败")
        finally:
            if decoder.poll() is None:
                decoder.kill()
                decoder.wait(timeout=10)
            decoder.stdout.close()
    # FFmpeg's MJPEG output has no embedded thumbnails. Keep only complete
    # JPEGs; a truncated batch is disclosed by the caller as a partial export.
    frames = []
    position = 0
    while len(frames) < frame_count:
        start = data.find(b"\xff\xd8", position)
        end = data.find(b"\xff\xd9", start + 2) if start >= 0 else -1
        if end < 0:
            break
        frames.append(data[start:end + 2])
        position = end + 2
    if mode == "keyframes" and len(frames) < frame_count:
        # The last GOP may contain no more keyframes, even though readable
        # pictures remain. Decode only that missing tail precisely.
        frames += decode_export_frames(path, offset + len(frames) * frame_seconds,
                                       frame_seconds, frame_count - len(frames), heartbeat, mode="accurate")
    elif mode == "accurate" and len(frames) == frame_count - 1:
        tail = decode_export_tail(path, offset + len(frames) * frame_seconds, frame_seconds, heartbeat)
        if tail is not None:
            frames.append(tail)
    return frames


def decode_export_tail(path, moment, frame_seconds, heartbeat):
    """Recover one real EOF picture discarded by accurate seek, without padding."""
    try:
        duration = probe_video_duration(path, heartbeat)
        if not 0 < duration - moment <= min(frame_seconds, 1):
            return None
        timeout = float(os.environ.get("EXPORT_DECODE_TIMEOUT_SECONDS", "120"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("EXPORT_DECODE_TIMEOUT_SECONDS must be finite and positive")
        data, errors = scan_media_process([
            "ffprobe", "-v", "error", "-threads", "2", "-select_streams", "v:0",
            "-read_intervals", f"{max(0, duration - 2)}%", "-show_frames", "-show_entries",
            "stream=start_time:frame=best_effort_timestamp_time,duration_time,pkt_duration_time",
            "-of", "json", str(path)], heartbeat, timeout)
        metadata = json.loads(data)
        last = metadata["frames"][-1]
        start = float(metadata.get("streams", [{}])[0].get("start_time", 0))
        pts = float(last["best_effort_timestamp_time"])
        span = float(last.get("duration_time", last.get("pkt_duration_time", 0)))
        relative = pts - start
        if (errors.strip() or not all(math.isfinite(value) for value in (start, pts, span))
                or not 0 < span <= 1 or not 0 <= relative <= moment < relative + span):
            return None
        # Seek just before the proven last frame. Keep original PTS and disable
        # frame-rate conversion so no duplicate/future picture can fill a gap.
        image, detail = scan_media_process([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2", "-copyts",
            "-ss", str(max(0, relative - .0001)), "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
            "-vf", "scale=1280:-2,settb=AVTB,showinfo=checksum=0", "-frames:v", "1", "-fps_mode", "passthrough",
            "-threads", "2", "-c:v", "mjpeg", "-pix_fmt", "yuvj420p", "-q:v", "3",
            "-f", "image2pipe", "pipe:1"], heartbeat, timeout)
        # showinfo's decimal pts_time is rounded; use integer microseconds.
        timestamps = re.findall(r"\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:", detail)
        if (image.startswith(b"\xff\xd8") and image.endswith(b"\xff\xd9") and timestamps
                and math.isclose(int(timestamps[0]) / 1000000, pts, abs_tol=.000001, rel_tol=0)):
            return image
    except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"tail recovery unconfirmed: {Path(path).name}: {error}", file=sys.stderr, flush=True)
    return None


def probe_video_duration(path, heartbeat=None):
    command = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=duration:format=duration", "-of", "json", str(path)]
    if heartbeat is None:
        data = subprocess.run(command, capture_output=True, text=True, timeout=15, check=True).stdout
    else:
        data, _ = scan_media_process(command, heartbeat, 45)
    metadata = json.loads(data)
    for entry in metadata.get("streams", []) + [metadata.get("format", {})]:
        try:
            duration = float(entry.get("duration", 0))
            if math.isfinite(duration) and duration > 0:
                return duration
        except (TypeError, ValueError):
            pass
    raise RuntimeError("视频没有可读取的有效时长")


def cache_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def export_intervals(record, events, duration=None):
    limit = (record["end"] - record["start"]).total_seconds()
    if duration is not None:
        limit = min(limit, duration)
    intervals = []
    for start, end in sorted(events):
        left = max(0.0, (start - record["start"]).total_seconds())
        right = min(limit, (end - record["start"]).total_seconds())
        if left >= right:
            continue
        if intervals and left <= intervals[-1][1]:
            intervals[-1] = (intervals[-1][0], max(right, intervals[-1][1]))
        else:
            intervals.append((left, right))
    return intervals


def export_batches(record, intervals, frame_seconds, excluded):
    for left, right in intervals:
        while left < right:
            count = max(1, min(30, math.ceil((right - left) / frame_seconds - 1e-9)))
            moments = [record["start"] + timedelta(seconds=left + index * frame_seconds) for index in range(count)]
            keep = [index for index, moment in enumerate(moments) if moment not in excluded]
            if keep:
                yield left, count, moments, keep
            left += count * frame_seconds


def wait_export_process(process, heartbeat, *, activity=None):
    # Encoding drains one source file. Final concatenation can span a year;
    # distinguish real media/file progress from a merely responsive heartbeat.
    name, default = ("CONCAT_TIMEOUT_SECONDS", "21600") if activity else ("ENCODE_TIMEOUT_SECONDS", "300")
    timeout = float(os.environ.get(name, default))
    stall = float(os.environ.get("CONCAT_STALL_SECONDS", "600")) if activity else timeout
    if not all(math.isfinite(value) and value > 0 for value in (timeout, stall)):
        raise ValueError("视频编码/拼接超时设置必须是有限的正数")
    started = last_activity = time.monotonic()
    previous = activity() if activity else None
    while True:
        try:
            result = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if activity:
                current = activity()
                if current != previous:
                    previous, last_activity = current, now
            heartbeat()
            if now - started >= timeout:
                label = "视频拼接达到总时限" if activity else "视频编码超时"
                raise RuntimeError(f"{label}（{timeout:g} 秒）；已完成片段已保留，可重试")
            if activity and now - last_activity >= stall:
                raise RuntimeError(f"视频拼接连续 {stall:g} 秒无进展；已完成片段已保留，可重试")
        else:
            if activity:
                activity()
                heartbeat()
            return result


def export(records, selected_events, output_root: Path, camera: str, day: str, frame_seconds: float, fps: int,
           scan_failed_files=0, destination_root=None):
    if not math.isfinite(frame_seconds) or frame_seconds <= 0 or fps <= 0:
        raise ValueError("抽帧间隔与输出帧率必须大于零")
    if not selected_events:
        raise SystemExit(f"camera {camera} has no events to export")
    output_root.mkdir(parents=True, exist_ok=True)
    cache_root = output_root / ".export-cache" / "v1"
    cache_root.mkdir(parents=True, exist_ok=True)
    candidates = sorted({str(r["path"]): r for r in records if r["camera"] == camera}.values(),
                        key=lambda r: (r["start"], str(r["path"])))
    merged = []
    for start, end in sorted(selected_events):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    starts, ends = [a for a, _ in merged], [b for _, b in merged]
    jobs = []
    for record in candidates:
        events = merged[bisect_right(ends, record["start"]):bisect_left(starts, record["end"])]
        intervals = export_intervals(record, events)
        if intervals:
            jobs.append((record, events, intervals))
    planned = [sum(max(1, math.ceil((b - a) / frame_seconds - 1e-9)) for a, b in intervals)
               for _, _, intervals in jobs]
    total_work, done_work = sum(planned), 0
    started = time.monotonic()
    destination_root = destination_root or output_root
    destination_root.mkdir(parents=True, exist_ok=True)
    target = destination_root / f"people-timelapse-{camera}-{day}.mp4"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.stem}-", suffix=".partial.mp4", dir=destination_root)
    os.close(descriptor)
    process, manifest = None, None
    transient, fragments, failures, adjustments = [], [], {}, []
    count = reused_frames = reused_files = file_count = 0
    written_moments = set()
    mux = {"frames": None, "seconds": 0.0, "bytes": 0}
    validation_started = None

    def heartbeat(phase="exporting"):
        elapsed = max(time.monotonic() - started, .001)
        fresh = count - reused_frames
        rate = fresh / elapsed
        eta = max(0, total_work - done_work - file_count) / rate if fresh >= 10 else None
        mux_fraction = min(1, mux["frames"] / max(count, 1) if mux["frames"] is not None else
                           mux["seconds"] / max(count / fps, .001))
        mux_position = (f"{mux['frames']}/{count} 帧" if mux["frames"] is not None else
                        f"媒体时间 {mux['seconds']:.1f}/{count / fps:.1f} 秒")
        current_file = (f"{'正在完成 MP4 封装' if mux_fraction == 1 else '最终拼接'} · "
                        f"{mux_position} · 文件 {mux['bytes'] / 1048576:.1f} MB") if phase == "encoding" else (
                            record["path"].name if jobs else "没有可合成素材")
        if phase == "validating":
            current_file = f"正在校验成片 · 已等待 {max(0, time.monotonic() - validation_started):.0f} 秒"
        percent = 99 if phase == "validating" else (round(90 + mux_fraction * 9, 1) if phase == "encoding" else
                  round(min(90, (done_work + file_count) / max(total_work, 1) * 90), 1))
        report_progress(phase=phase, percent=percent,
                        current_file=current_file, file_index=index if jobs else 0,
                        total_files=len(jobs), frames=count, estimated_frames=total_work,
                        output_seconds=round(count / fps, 1), estimated_output_seconds=round(total_work / fps, 1),
                        reused_files=reused_files, reused_frames=reused_frames, frames_per_second=round(rate, 2),
                        failed_files=len(failures), duration_adjusted_files=len(adjustments),
                        mux_frames=mux["frames"], mux_bytes=mux["bytes"], mux_seconds=mux["seconds"],
                        decode_mode=os.environ.get("EXPORT_DECODE_MODE", "keyframes"),
                        elapsed_seconds=round(elapsed, 1), eta_seconds=round(eta) if eta is not None and phase == "exporting" else None)

    try:
        for index, (record, events, original_intervals) in enumerate(jobs, start=1):
            file_count, file_moments = 0, set()
            fragment_temp = None
            written_moments = {moment for moment in written_moments if moment >= record["start"]}
            heartbeat()
            try:
                stat = record["path"].stat()
                source_stamp = {"source": source_id({**record, "size": stat.st_size}), "mtime_ns": stat.st_mtime_ns}
                source_key = hashlib.sha256(json.dumps(source_stamp, sort_keys=True).encode()).hexdigest()
                duration_path = cache_root / f"duration-{source_key}.json"
                duration = cache_json(duration_path).get("duration")
                if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
                    duration = probe_video_duration(record["path"])
                    atomic_json(duration_path, {"duration": duration})
                intervals = export_intervals(record, events, duration)
                trimmed = sum(b - a for a, b in original_intervals) - sum(b - a for a, b in intervals)
                if trimmed > .001:
                    adjustments.append({"file": record["path"].name, "actual_seconds": duration,
                                        "trimmed_event_seconds": round(trimmed, 3)})
                batches = list(export_batches(record, intervals, frame_seconds, written_moments))
                expected = sum(len(batch[3]) for batch in batches)
                total_work += expected - planned[index - 1]
                planned[index - 1] = expected
                if not expected:
                    continue
                recipe = {"source": source_stamp, "intervals": intervals, "frame_seconds": frame_seconds, "fps": fps,
                          "mode": os.environ.get("EXPORT_DECODE_MODE", "keyframes"),
                          "keep": [batch[3] for batch in batches], "offsets": [batch[0] for batch in batches],
                          "video": "1280x720-x264-veryfast-crf26-v1"}
                key = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()
                fragment = cache_root / f"{key}.mp4"
                metadata = cache_json(fragment.with_suffix(".json"))
                if fragment.exists():
                    cached_stat = fragment.stat()
                    if (metadata.get("frames") == expected and metadata.get("validated")
                            and cached_stat.st_size > 0
                            and metadata.get("size_bytes") == cached_stat.st_size
                            and metadata.get("mtime_ns") == cached_stat.st_mtime_ns):
                        count += expected
                        reused_frames += expected
                        reused_files += 1
                        written_moments.update(moment for _, _, moments, keep in batches for i, moment in enumerate(moments) if i in keep)
                        fragments.append(fragment)
                        done_work += expected
                        heartbeat()
                        continue
                descriptor, fragment_temp = tempfile.mkstemp(prefix=f".{key}-", suffix=".partial.mp4", dir=cache_root)
                os.close(descriptor)
                with tempfile.TemporaryFile() as errors:
                    process = subprocess.Popen([
                        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "image2pipe", "-vcodec", "mjpeg",
                        "-framerate", str(fps), "-i", "pipe:0", "-vf",
                        "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1",
                        "-c:v", "libx264", "-crf", "26", "-preset", "veryfast", "-threads", "2",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", fragment_temp,
                    ], stdin=subprocess.PIPE, stderr=errors)
                    for offset, batch_count, moments, keep in batches:
                        try:
                            frames = decode_export_frames(record["path"], offset, frame_seconds, batch_count, heartbeat)
                        except (OSError, RuntimeError) as error:
                            failures[record["path"].name] = str(error)
                            frames = []
                        if any(i >= len(frames) for i in keep):
                            failures.setdefault(record["path"].name, f"{offset:g} 秒处解码缺帧")
                        for i in keep:
                            if i < len(frames):
                                process.stdin.write(frames[i])
                                file_moments.add(moments[i])
                                file_count += 1
                                count += 1
                        heartbeat()
                    process.stdin.close()
                    if not file_count or wait_export_process(process, heartbeat) != 0:
                        errors.seek(0)
                        raise RuntimeError(errors.read().decode("utf-8", errors="replace")[-1000:] or "未能编码有效片段")
                if not valid_video(fragment_temp, heartbeat, raise_errors=True):
                    raise RuntimeError("片段校验失败")
                after = record["path"].stat()
                if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                    raise RuntimeError("源文件处理期间发生变化，请稍后重试")
                if record["path"].name not in failures:
                    os.replace(fragment_temp, fragment)
                    cached_stat = fragment.stat()
                    atomic_json(fragment.with_suffix(".json"), {"frames": file_count, "validated": True,
                                "size_bytes": cached_stat.st_size, "mtime_ns": cached_stat.st_mtime_ns})
                    fragments.append(fragment)
                else:
                    fragments.append(Path(fragment_temp))
                    transient.append(Path(fragment_temp))
                written_moments.update(file_moments)
                fragment_temp = None
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                failures[record["path"].name] = str(error)
                count -= file_count
                print(f"export failed: {record['path'].name}: {error}", file=sys.stderr, flush=True)
            finally:
                if process and process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                if process and process.stdin and not process.stdin.closed:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                process = None
                if fragment_temp:
                    Path(fragment_temp).unlink(missing_ok=True)
            done_work += planned[index - 1]
            file_count = 0
            heartbeat()
        if not count:
            raise RuntimeError("所选事件未能读取画面或已超出实际录像时长，原有成片已保留")
        descriptor, manifest = tempfile.mkstemp(prefix=".export-concat-", suffix=".txt", dir=output_root)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for fragment in fragments:
                stream.write(f"file '{fragment.relative_to(output_root).as_posix()}'\n")
        heartbeat("encoding")
        with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as progress:
            def concat_activity():
                # pread does not move FFmpeg's shared stdout file offset. Read a
                # bounded tail, ignoring an incomplete record during a write.
                length = os.fstat(progress.fileno()).st_size
                data = os.pread(progress.fileno(), 8192, max(0, length - 8192)).rpartition(b"\n")[0]
                for line in data.splitlines():
                    key, separator, value = line.partition(b"=")
                    field = {b"frame": "frames", b"out_time_us": "seconds"}.get(key)
                    if separator and field:
                        try:
                            number = int(value)
                            mux[field] = max(mux[field] or 0, number / 1000000 if field == "seconds" else number)
                        except ValueError:
                            pass
                stat = Path(temporary).stat()
                mux["bytes"] = stat.st_size
                # faststart may rewrite the file without changing its size or
                # frame count; actual writes still update mtime on disk.
                return mux["frames"], mux["seconds"], stat.st_size, stat.st_mtime_ns

            process = subprocess.Popen(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                                        "-nostats", "-stats_period", "2", "-progress", "pipe:1",
                                        "-f", "concat", "-safe", "0", "-i", manifest,
                                        "-c", "copy", "-movflags", "+faststart", temporary], stdout=progress, stderr=errors)
            if wait_export_process(process, lambda: heartbeat("encoding"), activity=concat_activity) != 0:
                errors.seek(0)
                raise RuntimeError(errors.read().decode("utf-8", errors="replace")[-2000:] or "片段拼接失败")
        validation_started = time.monotonic()
        heartbeat("validating")
        try:
            if not valid_video(temporary, lambda: heartbeat("validating"), raise_errors=True):
                raise RuntimeError("校验器未确认有效视频")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            reason = str(error)[-3000:]
            failed = output_root / ".failed-exports" / f"{target.stem}-{uuid.uuid4().hex}.mp4"
            warnings = []
            try:
                failed.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, failed)
            except OSError as preserve_error:
                failed = Path(temporary)
                warnings.append(f"诊断目录保存失败，临时成片保留在原位：{preserve_error}")
            temporary = None  # Never delete the only evidence of a failed validation.
            try:
                atomic_json(failed.with_suffix(".json"), {"stage": "validation", "error": reason,
                            "created_at": datetime.now(timezone.utc).isoformat(), "target": target.name,
                            "frames": count, "fps": fps, "size_bytes": failed.stat().st_size})
            except OSError as metadata_error:
                warnings.append(f"诊断记录保存失败：{metadata_error}")
            raise RuntimeError(f"成片校验失败：{reason}。原有视频和完整片段已保留；"
                               f"待诊断成片：{failed.relative_to(output_root) if failed.is_relative_to(output_root) else failed}"
                               + (f"；{'；'.join(warnings)}" if warnings else "")) from None
        os.replace(temporary, target)
        stat = target.stat()
        atomic_json(target.with_suffix(".json"), {"status": "partial" if failures or scan_failed_files else "completed",
                    "failed_files": sorted(failures), "failure_details": failures, "scan_failed_files": scan_failed_files,
                    "frames": count, "fps": fps, "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "validated": True,
                    "duration_adjustments": adjustments, "duration_adjusted_files": len(adjustments),
                    "reused_files": reused_files, "decode_mode": os.environ.get("EXPORT_DECODE_MODE", "keyframes")})
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for path in [*([Path(temporary)] if temporary else []), *transient, *([Path(manifest)] if manifest else [])]:
            path.unlink(missing_ok=True)
    report_progress(phase="partial" if failures or scan_failed_files else "completed", percent=100,
                    file_index=len(jobs), total_files=len(jobs), frames=count, failed_files=len(failures),
                    reused_files=reused_files, reused_frames=reused_frames, duration_adjusted_files=len(adjustments),
                    output_seconds=round(count / fps, 1), elapsed_seconds=round(time.monotonic() - started, 1), eta_seconds=0)
    if failures:
        print(f"已生成可用成片，但 {len(failures)} 个源文件有读取失败；重试会复用完整片段。", file=sys.stderr)
    if scan_failed_files:
        print(f"前置人物扫描中有 {scan_failed_files} 个失败文件，成片并非完整扫描结果。", file=sys.stderr)
    print(target)
    return 2 if failures or scan_failed_files else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("input", type=Path)
    scan_parser.add_argument("output", type=Path)
    scan_parser.add_argument("--date", required=True, help="YYYYMMDD, e.g. 20000101")
    scan_parser.add_argument("--sample-seconds", type=float, default=2.0)
    scan_parser.add_argument("--confidence", type=float, default=0.45)
    scan_parser.add_argument("--imgsz", type=int, default=640,
                             help="YOLO input edge length; 320 is much faster on low-power ARM NAS")
    scan_parser.add_argument("--device", help="inference device, e.g. mps on Apple Silicon")
    scan_parser.add_argument("--merge-seconds", type=float, default=20.0)
    scan_parser.add_argument("--limit", type=int, help="only scan the first N files, for a trial")
    scan_parser.add_argument("--force", action="store_true", help="rescan files already present in the ledger")
    scan_parser.add_argument("--run-id", help="stable queue task identity for resumable forced rescans")
    scan_parser.add_argument("--retry-failed", action="store_true", help="retry files previously recorded as failed")
    scan_parser.add_argument("--motion-threshold", type=float,
                             help="skip low-motion samples; values near 2-5 suit 8-bit grayscale frames")
    scan_parser.add_argument("--keepalive-seconds", type=float, default=60.0,
                             help="continue person checks after a recent detection when motion gating is enabled")
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("input", type=Path)
    export_parser.add_argument("output", type=Path)
    export_parser.add_argument("events", type=Path, nargs="+")
    export_parser.add_argument("--camera", required=True)
    export_parser.add_argument("--fps", type=int, default=25)
    export_parser.add_argument("--frame-seconds", type=float, default=1.0)
    export_parser.add_argument("--destination", type=Path, help="finished MP4 directory; state and cache stay in output")
    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("input", type=Path)
    inventory_parser.add_argument("--date", required=True, help="YYYYMMDD, e.g. 20000101")
    summary_parser = subparsers.add_parser("summary")
    summary_parser.add_argument("input", type=Path)
    args = parser.parse_args()
    if getattr(args, "sample_seconds", 1) <= 0 or getattr(args, "frame_seconds", 1) <= 0 or getattr(args, "fps", 1) <= 0:
        parser.error("采样间隔、抽帧间隔和帧率必须大于零")
    database = os.environ.get("INVENTORY_DB_PATH")
    if args.command == "scan":
        if args.force and not args.run_id:
            args.run_id = uuid.uuid4().hex
        records = find_records(args.input, args.date, database=database)
        if not records:
            raise SystemExit(f"no matching MP4 files for {args.date} under {args.input}")
        args.output.mkdir(parents=True, exist_ok=True)
        ledger = load_ledger(args.output, args.date)
        settings = scan_settings(args.sample_seconds, args.confidence, args.merge_seconds,
                                 args.motion_threshold, args.keepalive_seconds, args.imgsz)
        failures = {r["source_id"] for r in scan_results(args.output, args.date) if r["status"] == "failed"}
        def needs_scan(record):
            identity = source_id(record)
            previous = ledger["sources"].get(identity)
            force_needed = args.force and (not args.run_id or not previous or previous.get("run_id") != args.run_id)
            return force_needed or not previous or not settings_satisfied(previous, settings)

        pending = [record for record in records if needs_scan(record)
                   and (args.retry_failed or source_id(record) not in failures)]
        if args.limit:
            pending = pending[:args.limit]
        if not pending:
            rebuild_events(args.output, args.date, args.merge_seconds)
            print("no new files to scan")
            return 2 if failures else 0
        scan(
            pending, args.output, args.sample_seconds, args.confidence, args.merge_seconds,
            args.motion_threshold, args.keepalive_seconds, args.imgsz, args.device,
            checkpoint=lambda record, settings, events, error: commit_result(args.output, record, settings, events, error, args.run_id),
            force=args.force, run_id=args.run_id,
        )
        ledger = load_ledger(args.output, args.date)
        incomplete = {source_id(record) for record in records if needs_scan(record)}
        failures = {r["source_id"] for r in scan_results(args.output, args.date) if r["status"] == "failed"}
        deferred = bool(incomplete - failures) and not args.limit
        progress = cache_json(Path(os.environ["PROGRESS_PATH"])) if os.environ.get("PROGRESS_PATH") else {}
        report_progress(**{**progress, "phase": "waiting" if deferred else "partial" if incomplete else "completed",
                           "percent": 0 if deferred else 100, "failed_files": len(incomplete & failures),
                           "pending_files": len(incomplete)})
        print(args.output / f"events-{args.date}.json")
        if deferred:
            print("部分源文件正在由其他节点处理，稍后自动续排。", flush=True)
            return 3
        return 2 if incomplete else 0
    elif args.command == "export":
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in args.events]
        days = [payload["date"] for payload in payloads]
        records = find_records(args.input, days=set(days), database=database)
        events = [event for path in args.events for event in read_events(path, args.camera)]
        label = days[0] if len(days) == 1 else f"{min(days)}-{max(days)}"
        return export(records, events, args.output, args.camera, label, args.frame_seconds, args.fps,
                      sum(payload.get("failed_files", 0) for payload in payloads), destination_root=args.destination)
    elif args.command == "inventory":
        records = find_records(args.input, args.date)
        print(json.dumps([{
            "camera": record["camera"],
            "start": record["start"].isoformat(sep=" "),
            "end": record["end"].isoformat(sep=" "),
            "path": str(record["path"]),
        } for record in records], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(summarize(find_records(args.input, None)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    # Let context managers release shared leases on a normal container/worker stop.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main() or 0)
