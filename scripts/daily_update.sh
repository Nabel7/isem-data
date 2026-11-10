#!/bin/zsh
# Download the last 7 days of every dataset into data/, including any new versions of
# SEMO's per-farm wind forecasts.
#
# launchd runs this once a day (see install_daily_update.sh), and it is safe to run
# by hand at any time: saving replaces rows that are already there, so the overlap
# between runs does no harm. Going back 7 days means a day the Mac was off gets
# filled in on the next run.

set -euo pipefail  # stop at the first error, and treat unset variables as errors

cd "$(dirname "$0")/.."               # the project folder: this script's parent folder
start=$(date -v-7d +%Y-%m-%d)         # 7 days ago (macOS date syntax)

echo "=== $(date '+%Y-%m-%d %H:%M:%S') daily update from $start ==="
.venv/bin/python -m isem_data --start "$start"
echo "=== $(date '+%Y-%m-%d %H:%M:%S') done ==="
