#!/bin/zsh
set -euo pipefail

source_dir=$1
output_dir=$2
models_dir=$3
project_dir=${0:A:h}

days=("${(@f)$(find "$source_dir" -maxdepth 1 -type f -name '*.mp4' -exec basename {} \; \
  | sed -E 's/.*_([0-9]{8})[0-9]{6}_[0-9]{14}\.mp4/\1/' | sort -u)}")

for day in $days; do
  print "[$(date '+%F %T')] scanning $day"
  docker run --rm --ipc=host \
    -e MODEL_PATH=/models/yolo11n.pt \
    -v "$project_dir/app:/app:ro" \
    -v "$source_dir:/input:ro" \
    -v "$models_dir:/models" \
    -v "$output_dir:/output" \
    person-timelapse:local \
    python /app/person_timelapse.py scan /input /output --date "$day" --sample-seconds 5
done

print "[$(date '+%F %T')] complete"
