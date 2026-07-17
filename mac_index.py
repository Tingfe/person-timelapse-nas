#!/usr/bin/env python3
"""Build the shared NAS video index from a mounted Mac SMB volume."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(ROOT / "app"))
from web_server import indexed_records  # noqa: E402

input_root = Path(os.environ.get("INPUT_ROOT", "/Volumes/sata11-155XXXX2337/摄像头文件备份"))
output_root = Path(os.environ.get("OUTPUT_ROOT", "/Volumes/sata11-155XXXX2337/摄像头文件备份/延时摄影"))

if not input_root.is_dir():
    raise SystemExit(f"NAS 录像目录未挂载：{input_root}")
if not output_root.is_dir():
    raise SystemExit(f"NAS 输出目录不可用：{output_root}")

print("开始通过 Mac 建立 NAS 录像索引…", flush=True)
records, mp4_files = indexed_records(input_root, output_root / "inventory.sqlite3")
days = {record["start"].strftime("%Y%m%d") for record in records}
print(f"索引完成：{mp4_files} 个 MP4，识别 {len(records)} 个录像文件，覆盖 {len(days)} 天。", flush=True)
