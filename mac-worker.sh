#!/bin/sh
# Native Apple-Silicon worker. Everything it installs stays in .mac-worker-venv.
set -eu

DATE=${1:?"Usage: ./mac-worker.sh YYYYMMDD [max_files]"}
MAX_FILES=${2:-5}
INPUT_ROOT=${INPUT_ROOT:-"/Volumes/camera-recordings"}
OUTPUT_ROOT=${OUTPUT_ROOT:-"$INPUT_ROOT/延时摄影"}
VENV=${VENV:-"$PWD/.mac-worker-venv"}
REQUIRE_AC=${REQUIRE_AC:-1}
MAC_PRIORITY=${MAC_PRIORITY:-1}
PRIORITY_PATH="$OUTPUT_ROOT/.mac-priority.lock"
PROGRESS_PATH=${PROGRESS_PATH:-"$PWD/.mac-worker-state/progress.json"}
INVENTORY_DB_PATH="$OUTPUT_ROOT/inventory.sqlite3"

[ -d "$INPUT_ROOT" ] || { echo "NAS 录像目录未挂载：$INPUT_ROOT"; exit 1; }
[ -d "$OUTPUT_ROOT" ] || { echo "NAS 输出目录不可用：$OUTPUT_ROOT"; exit 1; }
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "人物扫描需要 ffmpeg 和 ffprobe。请先安装并确认在 PATH 中，再重新启动控制台。"
  echo "不会自动安装全局软件，也不会将录像误标为处理失败。"
  exit 1
fi
mkdir -p "$(dirname "$PROGRESS_PATH")"
rm -f "$PROGRESS_PATH"

heartbeat=""
if [ "$MAC_PRIORITY" = "1" ]; then
  printf 'mac=%s started=%s\n' "$(hostname)" "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$PRIORITY_PATH"
  (while :; do sleep 60; touch "$PRIORITY_PATH" 2>/dev/null || exit; done) & heartbeat=$!
  trap 'kill "$heartbeat" 2>/dev/null || true; rm -f "$PRIORITY_PATH"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
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

if [ "$REQUIRE_AC" = "1" ] && ! pmset -g batt | grep -q "AC Power"; then
  echo "Mac 未接电，停止领取任务；接电后重新点击开始即可续接。"
  exit 3
fi
echo "[Mac worker] ${DATE}：本次最多处理 ${MAX_FILES} 个未处理文件，可随时 Ctrl-C"
MODEL_PATH="$MODEL_PATH" PROGRESS_PATH="$PROGRESS_PATH" INVENTORY_DB_PATH="$INVENTORY_DB_PATH" "$VENV/bin/python" app/person_timelapse.py scan "$INPUT_ROOT" "$OUTPUT_ROOT" --date "$DATE" --limit "$MAX_FILES" --device mps --sample-seconds 120 --motion-threshold 8 --imgsz 256 --retry-failed
