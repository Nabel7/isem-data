#!/bin/zsh
# Download the last 7 days of every dataset into data/, including any new versions of
# SEMO's per-farm wind forecasts, then back up data/.
#
# launchd runs this once a day (see install_daily_update.sh), and it is safe to run
# by hand at any time: saving replaces rows that are already there, so the overlap
# between runs does no harm. Going back 7 days means a day the Mac was off gets
# filled in on the next run.
#
# If a dataset fails, the others still download; the run then ends with FAILED in the
# log, a notification on the Mac, and a non-zero exit code. data/status.json records
# when each dataset last downloaded successfully.
#
# After the download, `python -m isem_data.checks` looks at the archive itself: is every
# dataset up to date, is any day missing, are any farms stuck "held down"? Results go to
# data/checks.json; a failed check is reported the same way as a failed download.

set -uo pipefail  # treat unset variables as errors (no -e: a failed step is reported, not fatal)

cd "$(dirname "$0")/.."               # the project folder: this script's parent folder
start=$(date -v-7d +%Y-%m-%d)         # 7 days ago (macOS date syntax)

notify() {  # a notification on the Mac; harmless if nobody is logged in
    osascript -e "display notification \"$1\" with title \"isem-data\"" >/dev/null 2>&1 || true
}

echo "=== $(date '+%Y-%m-%d %H:%M:%S') daily update from $start ==="
.venv/bin/python -m isem_data --start "$start"
download=$?

.venv/bin/python -m isem_data.checks
checks=$?

scripts/backup_data.sh
backup=$?

if (( download != 0 )); then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') FAILED: some datasets did not download (see above) ==="
    notify "Daily download: some datasets failed. See logs/daily_update.log"
elif (( checks != 0 )); then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') FAILED: data checks (see above and data/checks.json) ==="
    notify "Daily download: data checks failed. See logs/daily_update.log"
elif (( backup != 0 )); then
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') done, but NOT BACKED UP ==="
    notify "Downloaded, but the data has no backup. See scripts/backup_data.sh"
else
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') done ==="
fi
exit $(( download != 0 ? download : checks != 0 ? checks : backup ))
