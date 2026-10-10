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
    for path in root.rglob("*.mp4"):
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
            "motion_threshold": motion_threshold, "keepalive_seconds": keepalive_seconds, "imgsz": imgsz}


def settings_satisfied(entry, requested):
    previous = entry.get("settings")
    if not previous:
        return True  # Existing archives remain usable; explicit rescan upgrades them.
    old_motion, new_motion = previous.get("motion_threshold"), requested.get("motion_threshold")
    return (previous["sample_seconds"] <= requested["sample_seconds"]
            and previous["imgsz"] >= requested["imgsz"]
            and previous["confidence"] == requested["confidence"]
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


def scan(records, output_root: Path, sample_seconds: float, confidence: float, merge_seconds: float,
         motion_threshold: Optional[float] = None, keepalive_seconds: float = 60.0, imgsz: int = 640,
         device: Optional[str] = None, checkpoint=None, force=False, run_id=None):
    import cv2
    from ultralytics import YOLO

    model = YOLO(os.environ.get("MODEL_PATH", "/models/yolo11n.pt"))
    events = defaultdict(list)
    samples = 0
    inferences = 0
    estimated_samples = max(1, sum(
        math.ceil((record["end"] - record["start"]).total_seconds() / sample_seconds) for record in records
    ))
    started = time.monotonic()
    settings = scan_settings(sample_seconds, confidence, merge_seconds, motion_threshold, keepalive_seconds, imgsz)

    def update(index, record, offset):
        elapsed = max(time.monotonic() - started, 0.001)
        percent = min(99.9, samples / estimated_samples * 100)
        eta = (elapsed / samples * max(estimated_samples - samples, 0)) if samples else None
        report_progress(
            phase="scanning", percent=round(percent, 1), current_file=record["path"].name,
            file_index=index, total_files=len(records), offset_seconds=round(offset, 1),
            samples=samples, estimated_samples=estimated_samples, yolo_inferences=inferences,
            elapsed_seconds=round(elapsed, 1), eta_seconds=round(eta, 1) if eta is not None else None,
            source={"source_id": source_id(record), "path": str(record["path"]), "camera": record["camera"],
                    "start": record["start"].isoformat(), "end": record["end"].isoformat(), "settings": settings},
        )

    for index, record in enumerate(records, start=1):
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
                        continue
                capture, file_events, error, offset = None, defaultdict(list), None, 0.0
                try:
                    capture = cv2.VideoCapture(str(record["path"]))
                    if not capture.isOpened():
                        raise RuntimeError("视频无法打开")
                    fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
                    frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                    duration = min(frame_count / fps, (record["end"] - record["start"]).total_seconds())
                    if not math.isfinite(duration) or duration <= 0:
                        raise RuntimeError("视频没有有效时长")
                    offset, decoded, failed_reads = 0.0, 0, 0
                    previous_gray, last_person = None, datetime.min
                    while offset < duration:
                        update(index, record, offset)
                        capture.set(cv2.CAP_PROP_POS_MSEC, offset * 1000)
                        ok, frame = capture.read()
                        if not ok:
                            failed_reads += 1
                            offset += sample_seconds
                            continue
                        decoded += 1
                        samples += 1
                        moment = record["start"] + timedelta(seconds=offset)
                        current_gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
                        motion = (previous_gray is None or motion_threshold is None
                                  or cv2.mean(cv2.absdiff(current_gray, previous_gray))[0] >= motion_threshold)
                        previous_gray = current_gray
                        recently_seen = (moment - last_person).total_seconds() <= keepalive_seconds
                        if motion_threshold is not None and not motion and not recently_seen:
                            offset += sample_seconds
                            continue
                        inferences += 1
                        result = model(frame, classes=[0], conf=confidence, imgsz=imgsz, verbose=False, device=device)[0]
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
                        offset += sample_seconds
                    if not decoded or failed_reads:
                        raise RuntimeError(f"解码不完整：成功 {decoded} 次，失败 {failed_reads} 次")
                except Exception as exception:
                    error = str(exception)
                    print(f"scan failed: {record['path'].name}: {error}", file=sys.stderr, flush=True)
                finally:
                    if capture is not None:
                        capture.release()
                if checkpoint:
                    checkpoint(record, settings, merge_events(file_events, merge_seconds), error)
                if not error:
                    for camera, found in file_events.items():
                        events[camera].extend(found)
                update(index, record, offset)
        except TimeoutError:
            print(f"另一个节点正在扫描，稍后重试：{record['path'].name}", flush=True)
    print(f"samples={samples} yolo_inferences={inferences}", flush=True)
    report_progress(
        phase="completed", percent=100, file_index=len(records), total_files=len(records),
        samples=samples, estimated_samples=estimated_samples, yolo_inferences=inferences,
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


def valid_video(path):
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
                                 "-of", "json", str(path)], capture_output=True, text=True, timeout=30, check=True)
        metadata = json.loads(result.stdout)
        return float(metadata.get("format", {}).get("duration", 0)) > 0 and any(
            stream.get("codec_type") == "video" and stream.get("width", 0) > 0 for stream in metadata.get("streams", []))
    except (OSError, ValueError, subprocess.SubprocessError):
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
                "-frames:v", str(frame_count), "-threads", "2", "-c:v", "mjpeg", "-q:v", "3",
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
    return frames


def probe_video_duration(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=duration:format=duration", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=15, check=True)
    metadata = json.loads(result.stdout)
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


def wait_export_process(process, heartbeat):
    started = time.monotonic()
    timeout = float(os.environ.get("ENCODE_TIMEOUT_SECONDS", "300"))
    while True:
        if time.monotonic() - started >= timeout:
            raise RuntimeError("视频编码或拼接超时")
        try:
            return process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            heartbeat()


def export(records, selected_events, output_root: Path, camera: str, day: str, frame_seconds: float, fps: int,
           scan_failed_files=0):
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
    target = output_root / f"people-timelapse-{camera}-{day}.mp4"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.stem}-", suffix=".partial.mp4", dir=output_root)
    os.close(descriptor)
    process, manifest = None, None
    transient, fragments, failures, adjustments = [], [], {}, []
    count = reused_frames = reused_files = file_count = 0
    written_moments = set()

    def heartbeat(phase="exporting"):
        elapsed = max(time.monotonic() - started, .001)
        fresh = count - reused_frames
        rate = fresh / elapsed
        eta = max(0, total_work - done_work - file_count) / rate if fresh >= 10 else None
        report_progress(phase=phase, percent=95 if phase == "encoding" else
                        round(min(90, (done_work + file_count) / max(total_work, 1) * 90), 1),
                        current_file=record["path"].name if jobs else "没有可合成素材", file_index=index if jobs else 0,
                        total_files=len(jobs), frames=count, estimated_frames=total_work,
                        output_seconds=round(count / fps, 1), estimated_output_seconds=round(total_work / fps, 1),
                        reused_files=reused_files, reused_frames=reused_frames, frames_per_second=round(rate, 2),
                        failed_files=len(failures), duration_adjusted_files=len(adjustments),
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
                if not valid_video(fragment_temp):
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
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                                        "-f", "concat", "-safe", "0", "-i", manifest,
                                        "-c", "copy", "-movflags", "+faststart", temporary], stderr=errors)
            if wait_export_process(process, lambda: heartbeat("encoding")) != 0:
                errors.seek(0)
                raise RuntimeError(errors.read().decode("utf-8", errors="replace")[-2000:] or "片段拼接失败")
        if not valid_video(temporary):
            raise RuntimeError("成片校验失败，原有视频已保留")
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
        for path in [Path(temporary), *transient, *([Path(manifest)] if manifest else [])]:
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
        report_progress(phase="waiting" if deferred else "partial" if incomplete else "completed",
                        percent=0 if deferred else 100, failed_files=len(incomplete & failures), pending_files=len(incomplete))
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
                      sum(payload.get("failed_files", 0) for payload in payloads))
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
