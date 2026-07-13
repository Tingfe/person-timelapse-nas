#!/usr/bin/env bash
set -euo pipefail

days=$(find /input -maxdepth 1 -type f -name '*.mp4' -exec basename {} \; \
  | sed -E 's/.*_([0-9]{8})[0-9]{6}_[0-9]{14}\.mp4/\1/' | sort -u)

for day in $days; do
  echo "[$(date '+%F %T')] scanning $day"
  python /app/person_timelapse.py scan /input /output --date "$day" --sample-seconds 5 \
    --motion-threshold 3 --keepalive-seconds 60
done

echo "[$(date '+%F %T')] complete"
