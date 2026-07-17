#!/bin/sh
# Native Apple-Silicon worker. Everything it installs stays in .mac-worker-venv.
set -eu

DATE=${1:?"Usage: ./mac-worker.sh YYYYMMDD [batches]"}
BATCHES=${2:-1}
INPUT_ROOT=${INPUT_ROOT:-"/Volumes/sata11-155XXXX2337/摄像头文件备份"}
OUTPUT_ROOT=${OUTPUT_ROOT:-"/Volumes/sata11-155XXXX2337/摄像头文件备份/延时摄影"}
VENV=${VENV:-"$PWD/.mac-worker-venv"}

[ -d "$INPUT_ROOT" ] || { echo "NAS 录像目录未挂载：$INPUT_ROOT"; exit 1; }
[ -d "$OUTPUT_ROOT" ] || { echo "NAS 输出目录不可用：$OUTPUT_ROOT"; exit 1; }

if [ ! -x "$VENV/bin/python" ]; then
  command -v uv >/dev/null || { echo "需要 uv：https://docs.astral.sh/uv/"; exit 1; }
  uv venv "$VENV" --python 3.11
  uv pip install --python "$VENV/bin/python" ultralytics
fi

batch=1
while [ "$batch" -le "$BATCHES" ]; do
  echo "[Mac worker] 第 $batch/$BATCHES 批：$DATE（每批 5 个未处理文件，可随时 Ctrl-C）"
  result=$("$VENV/bin/python" app/person_timelapse.py scan "$INPUT_ROOT" "$OUTPUT_ROOT" --date "$DATE" --limit 5 --device mps --sample-seconds 120 --motion-threshold 8 --imgsz 256 2>&1) || { printf '%s\n' "$result"; exit 1; }
  printf '%s\n' "$result"
  printf '%s' "$result" | grep -q "no new files to scan" && exit 0
  batch=$((batch + 1))
done
