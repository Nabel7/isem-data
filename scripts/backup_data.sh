#!/bin/zsh
# Copy the data archive (data/) to a backup folder.
#
# SEMO deletes most of its reports after 2-3 months, so data/ can't be downloaded
# again: it needs a second copy somewhere that isn't this disk (an external drive,
# or a folder a cloud service syncs).
#
# Say where, in either way:
#   echo "/Volumes/MyDrive/isem-data-backup" > .backup_dir     (in the project folder)
#   ISEM_BACKUP_DIR=/some/folder scripts/backup_data.sh
#
# What it keeps in that folder:
#   latest/            a copy of data/ as it is now (only changed files are copied)
#   weekly/2026-W46/   a copy of the main datasets, once a week, kept for 8 weeks, in
#                      case a bad file is copied over a good one in latest/
#
# daily_update.sh runs this after each download. It is safe to run by hand.

set -euo pipefail

cd "$(dirname "$0")/.."
dest=${ISEM_BACKUP_DIR:-}
if [[ -z "$dest" && -f .backup_dir ]]; then
    dest=$(head -n 1 .backup_dir)
fi
if [[ -z "$dest" ]]; then
    echo "WARNING: no backup folder is set, so data/ has NO backup. See scripts/backup_data.sh"
    exit 2
fi
dest=${dest/#\~/$HOME}
if [[ ! -d "$dest" ]]; then
    echo "WARNING: backup folder $dest isn't there (drive not plugged in?). Nothing backed up."
    exit 3
fi

mkdir -p "$dest/latest" "$dest/weekly"
rsync -a --exclude '*.tmp' data/ "$dest/latest/"

week=$(date +%G-W%V)
if [[ ! -d "$dest/weekly/$week" ]]; then
    mkdir "$dest/weekly/$week"
    cp data/*.parquet "$dest/weekly/$week/"
    # Keep the 8 newest weekly copies.
    weeks=("$dest"/weekly/*(/On))          # zsh: the weekly folders, newest name first
    for old in ${weeks[9,-1]}; do rm -rf "$old"; done
fi

files=$(find "$dest/latest" -name '*.parquet' | wc -l | tr -d ' ')
size=$(du -sh "$dest/latest" | cut -f1)
echo "backup: $files files ($size) in $dest/latest; weekly copy $week"
