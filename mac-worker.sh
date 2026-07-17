#!/bin/sh
# Native Apple-Silicon worker. Everything it installs stays in .mac-worker-venv.
set -eu

DATE=${1:?"Usage: ./mac-worker.sh YYYYMMDD [batches]"}
BATCHES=${2:-1}
INPUT_ROOT=${INPUT_ROOT:-"/Volumes/sata11-155XXXX2337/摄像头文件备份"}
OUTPUT_ROOT=${OUTPUT_ROOT:-"/Volumes/sata11-155XXXX2337/摄像头文件备份/延时摄影"}
VENV=${VENV:-"$PWD/.mac-worker-venv"}
REQUIRE_AC=${REQUIRE_AC:-1}
MAC_PRIORITY=${MAC_PRIORITY:-1}
PRIORITY_PATH="$OUTPUT_ROOT/.mac-priority.lock"

[ -d "$INPUT_ROOT" ] || { echo "NAS 录像目录未挂载：$INPUT_ROOT"; exit 1; }
[ -d "$OUTPUT_ROOT" ] || { echo "NAS 输出目录不可用：$OUTPUT_ROOT"; exit 1; }

heartbeat=""
if [ "$MAC_PRIORITY" = "1" ]; then
  printf 'mac=%s started=%s\n' "$(hostname)" "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$PRIORITY_PATH"
  (while :; do sleep 60; touch "$PRIORITY_PATH" 2>/dev/null || exit; done) & heartbeat=$!
  trap 'kill "$heartbeat" 2>/dev/null || true; rm -f "$PRIORITY_PATH"' EXIT INT TERM
  echo "Mac 已取得计算优先权；NAS 不会启动新的扫描任务。"
fi

if [ ! -x "$VENV/bin/python" ]; then
  command -v uv >/dev/null || { echo "需要 uv：https://docs.astral.sh/uv/"; exit 1; }
  uv venv "$VENV" --python 3.11
  uv pip install --python "$VENV/bin/python" ultralytics
fi

MODEL_DIR="$VENV/models"
MODEL_PATH="$MODEL_DIR/yolo11n.pt"
mkdir -p "$MODEL_DIR"
if [ ! -f "$MODEL_PATH" ]; then
  echo "首次下载 Mac 专用人物识别模型…"
  (cd "$MODEL_DIR" && "$VENV/bin/python" -c "from ultralytics import YOLO; YOLO('yolo11n.pt')")
fi

batch=1
while [ "$batch" -le "$BATCHES" ]; do
  if [ "$REQUIRE_AC" = "1" ] && ! pmset -g batt | grep -q "AC Power"; then
    echo "Mac 未接电，停止领取下一批；接电后重新点击开始即可续接。"
    exit 0
  fi
  echo "[Mac worker] 第 $batch/$BATCHES 批：$DATE（每批 5 个未处理文件，可随时 Ctrl-C）"
  result=$(MODEL_PATH="$MODEL_PATH" "$VENV/bin/python" app/person_timelapse.py scan "$INPUT_ROOT" "$OUTPUT_ROOT" --date "$DATE" --limit 5 --device mps --sample-seconds 120 --motion-threshold 8 --imgsz 256 2>&1) || { printf '%s\n' "$result"; exit 1; }
  printf '%s\n' "$result"
  printf '%s' "$result" | grep -q "no new files to scan" && exit 0
  batch=$((batch + 1))
done
