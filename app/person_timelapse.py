#!/usr/bin/env python3
"""Read-only person-event indexing and timelapse export for Xiaomi MP4 files."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

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


def find_records(root: Path, day: Optional[str] = None, days=None):
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


def load_ledger(output_root: Path):
    path = output_root / "processed.json"
    if not path.exists():
        return {"sources": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_ledger(output_root: Path, ledger):
    (output_root / "processed.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def report_progress(**values):
    """Write a replace-in-place progress snapshot when launched by the Web console."""
    path = os.environ.get("PROGRESS_PATH")
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


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
                merged[camera].append(event)
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
         device: Optional[str] = None):
    import cv2
    from ultralytics import YOLO

    model = YOLO(os.environ.get("MODEL_PATH", "/models/yolo11n.pt"))
    events = defaultdict(list)
    last_person = {}
    samples = 0
    inferences = 0
    estimated_samples = max(1, sum(
        int((record["end"] - record["start"]).total_seconds() / sample_seconds) for record in records
    ))
    started = time.monotonic()

    def update(index, record, offset):
        elapsed = max(time.monotonic() - started, 0.001)
        percent = min(99.9, samples / estimated_samples * 100)
        eta = (elapsed / samples * max(estimated_samples - samples, 0)) if samples else None
        report_progress(
            phase="scanning", percent=round(percent, 1), current_file=record["path"].name,
            file_index=index, total_files=len(records), offset_seconds=round(offset, 1),
            samples=samples, estimated_samples=estimated_samples, yolo_inferences=inferences,
            elapsed_seconds=round(elapsed, 1), eta_seconds=round(eta, 1) if eta is not None else None,
        )

    for index, record in enumerate(records, start=1):
        print(f"[{index}/{len(records)}] {record['path'].name}", flush=True)
        update(index, record, 0)
        capture = cv2.VideoCapture(str(record["path"]))
        if not capture.isOpened():
            print(f"skip unreadable: {record['path']}", file=sys.stderr)
            continue
        fps = capture.get(cv2.CAP_PROP_FPS) or 25.0
        frame_count = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        duration = min(frame_count / fps, (record["end"] - record["start"]).total_seconds())
        offset = 0.0
        previous_gray = None
        while offset < duration:
            capture.set(cv2.CAP_PROP_POS_MSEC, offset * 1000)
            ok, frame = capture.read()
            if not ok:
                offset += sample_seconds
                continue
            samples += 1
            moment = record["start"] + timedelta(seconds=offset)
            current_gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
            motion = (
                previous_gray is None
                or motion_threshold is None
                or cv2.mean(cv2.absdiff(current_gray, previous_gray))[0] >= motion_threshold
            )
            previous_gray = current_gray
            recently_seen = (moment - last_person.get(record["camera"], datetime.min)).total_seconds() <= keepalive_seconds
            if motion_threshold is not None and not motion and not recently_seen:
                if samples % 5 == 0:
                    update(index, record, offset)
                offset += sample_seconds
                continue
            inferences += 1
            result = model(frame, classes=[0], conf=confidence, imgsz=imgsz, verbose=False, device=device)[0]
            if len(result.boxes):
                last_person[record["camera"]] = moment
                thumbnail = save_thumbnail(frame, output_root, record["camera"], moment)
                camera_events = events[record["camera"]]
                if camera_events and (moment - datetime.fromisoformat(camera_events[-1]["end"])).total_seconds() <= merge_seconds:
                    camera_events[-1]["end"] = moment.isoformat(timespec="seconds")
                    camera_events[-1]["hits"] += 1
                else:
                    camera_events.append({
                        "start": moment.isoformat(timespec="seconds"),
                        "end": moment.isoformat(timespec="seconds"),
                        "hits": 1,
                        "thumbnail": str(thumbnail.relative_to(output_root)),
                    })
            if samples % 5 == 0:
                update(index, record, offset)
            offset += sample_seconds
        capture.release()
    print(f"samples={samples} yolo_inferences={inferences}", flush=True)
    report_progress(
        phase="completed", percent=100, file_index=len(records), total_files=len(records),
        samples=samples, estimated_samples=estimated_samples, yolo_inferences=inferences,
        elapsed_seconds=round(time.monotonic() - started, 1), eta_seconds=0,
    )
    return dict(events)


def read_events(events_path: Path, camera: str):
    data = json.loads(events_path.read_text(encoding="utf-8"))
    return [
        (datetime.fromisoformat(event["start"]), datetime.fromisoformat(event["end"]))
        for event in data["events"].get(camera, [])
    ]


def overlaps(start, end, event_start, event_end):
    return start <= event_end and end >= event_start


def export(records, selected_events, output_root: Path, camera: str, day: str, frame_seconds: float, fps: int):
    import cv2

    output_root.mkdir(parents=True, exist_ok=True)
    candidates = [record for record in records if record["camera"] == camera]
    if not selected_events:
        raise SystemExit(f"camera {camera} has no events to export")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="person-timelapse-") as temporary:
        frames_dir = Path(temporary)
        count = 0
        written_moments = set()
        for record_index, record in enumerate(candidates, start=1):
            relevant = [event for event in selected_events if overlaps(record["start"], record["end"], *event)]
            if not relevant:
                continue
            report_progress(
                phase="exporting", percent=round((record_index - 1) / max(len(candidates), 1) * 90, 1),
                current_file=record["path"].name, file_index=record_index, total_files=len(candidates),
                frames=count, elapsed_seconds=round(time.monotonic() - started, 1), eta_seconds=None,
            )
            capture = cv2.VideoCapture(str(record["path"]))
            if not capture.isOpened():
                continue
            for event_start, event_end in relevant:
                start = max(record["start"], event_start)
                end = min(record["end"], event_end)
                offset = (start - record["start"]).total_seconds()
                stop = (end - record["start"]).total_seconds()
                while offset <= stop:
                    moment = record["start"] + timedelta(seconds=offset)
                    moment_key = moment.isoformat(timespec="seconds")
                    if moment_key not in written_moments:
                        capture.set(cv2.CAP_PROP_POS_MSEC, offset * 1000)
                        ok, frame = capture.read()
                        if ok:
                            written_moments.add(moment_key)
                            count += 1
                            cv2.imwrite(str(frames_dir / f"frame-{count:08d}.jpg"), frame)
                    offset += frame_seconds
            capture.release()
        if not count:
            raise SystemExit("no frames could be read for the selected events")
        target = output_root / f"people-timelapse-{camera}-{day}.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-framerate", str(fps),
            "-i", str(frames_dir / "frame-%08d.jpg"), "-vf", "scale=1280:-2", "-c:v", "libx264", "-crf", "26",
            "-pix_fmt", "yuv420p", str(target),
        ], check=True)
    report_progress(
        phase="completed", percent=100, file_index=len(candidates), total_files=len(candidates),
        frames=count, elapsed_seconds=round(time.monotonic() - started, 1), eta_seconds=0,
    )
    print(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan")
    scan_parser.add_argument("input", type=Path)
    scan_parser.add_argument("output", type=Path)
    scan_parser.add_argument("--date", required=True, help="YYYYMMDD, e.g. 20260324")
    scan_parser.add_argument("--sample-seconds", type=float, default=2.0)
    scan_parser.add_argument("--confidence", type=float, default=0.45)
    scan_parser.add_argument("--imgsz", type=int, default=640,
                             help="YOLO input edge length; 320 is much faster on low-power ARM NAS")
    scan_parser.add_argument("--device", help="inference device, e.g. mps on Apple Silicon")
    scan_parser.add_argument("--merge-seconds", type=float, default=20.0)
    scan_parser.add_argument("--limit", type=int, help="only scan the first N files, for a trial")
    scan_parser.add_argument("--force", action="store_true", help="rescan files already present in the ledger")
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
    inventory_parser.add_argument("--date", required=True, help="YYYYMMDD, e.g. 20260324")
    summary_parser = subparsers.add_parser("summary")
    summary_parser.add_argument("input", type=Path)
    args = parser.parse_args()
    if args.command == "scan":
        records = find_records(args.input, args.date)
        if not records:
            raise SystemExit(f"no matching MP4 files for {args.date} under {args.input}")
        args.output.mkdir(parents=True, exist_ok=True)
        ledger = load_ledger(args.output)
        pending = records if args.force else [record for record in records if source_id(record) not in ledger["sources"]]
        if args.limit:
            pending = pending[:args.limit]
        if not pending:
            print("no new files to scan")
            return
        target = args.output / f"events-{args.date}.json"
        old_events = json.loads(target.read_text(encoding="utf-8"))["events"] if target.exists() else {}
        new_events = scan(
            pending, args.output, args.sample_seconds, args.confidence, args.merge_seconds,
            args.motion_threshold, args.keepalive_seconds, args.imgsz, args.device,
        )
        payload = {
            "date": args.date,
            "sample_seconds": args.sample_seconds,
            "events": merge_events({
                camera: old_events.get(camera, []) + new_events.get(camera, [])
                for camera in set(old_events) | set(new_events)
            }, args.merge_seconds),
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for record in pending:
            ledger["sources"][source_id(record)] = {
                "path": str(record["path"]),
                "processed_at": datetime.now().isoformat(timespec="seconds"),
            }
        save_ledger(args.output, ledger)
        print(target)
    elif args.command == "export":
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in args.events]
        days = [payload["date"] for payload in payloads]
        records = find_records(args.input, days=set(days))
        events = [event for path in args.events for event in read_events(path, args.camera)]
        label = days[0] if len(days) == 1 else f"{min(days)}-{max(days)}"
        export(records, events, args.output, args.camera, label, args.frame_seconds, args.fps)
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
    main()
