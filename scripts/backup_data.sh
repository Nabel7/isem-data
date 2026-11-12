#!/bin/zsh
# Copy the data archive (data/) somewhere that isn't this disk.
#
# SEMO deletes most of its reports after 2-3 months, so data/ can't be downloaded
# again. Say where the copy goes, in either way:
#
#   echo "github:Nabel7/isem-data-archive" > .backup_to     (in the project folder)
#   ISEM_BACKUP_TO=/Volumes/MyDrive/isem-backup scripts/backup_data.sh
#
# Two kinds of place work:
#
#   github:owner/repo   a private GitHub repository. Files are attached to releases,
#                       which keeps them out of the repository's history. Needs the
#                       gh command, logged in (gh auth login).
#   /some/folder        a folder: an external drive, or one a cloud service syncs.
#
# Either way it keeps:
#   latest         every main dataset as it is now, replaced on each run
#   forecasts      the per-farm wind forecasts, one file per day (only new or changed
#                  files are sent); on GitHub, one release per month: forecasts-2026-11
#   weekly         the main datasets once a week, kept for 8 weeks, in case a bad file
#                  replaces a good one in latest; on GitHub: weekly-2026-W46
#
# To restore from GitHub, see "Backing up the archive" in the README.
#
# daily_update.sh runs this after each download. It is safe to run by hand.

set -euo pipefail

cd "$(dirname "$0")/.."
# launchd gives scripts a bare PATH; gh lives in Homebrew's folder.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

dest=${ISEM_BACKUP_TO:-}
if [[ -z "$dest" && -f .backup_to ]]; then
    dest=$(head -n 1 .backup_to)
fi
if [[ -z "$dest" ]]; then
    echo "WARNING: no backup place is set, so data/ has NO backup. See scripts/backup_data.sh"
    exit 2
fi

week=$(date +%G-W%V)
forecasts=data/wind_unit_forecasts
main_files=(data/*.parquet(N) data/status.json(N))   # (N): no error if there are none

# ---- a folder ------------------------------------------------------------------------

backup_to_folder() {
    local dest=${1/#\~/$HOME}
    if [[ ! -d "$dest" ]]; then
        echo "WARNING: backup folder $dest isn't there (drive not plugged in?). Nothing backed up."
        exit 3
    fi
    mkdir -p "$dest/latest" "$dest/weekly"
    rsync -a --exclude '*.tmp' data/ "$dest/latest/"
    if [[ ! -d "$dest/weekly/$week" ]]; then
        mkdir "$dest/weekly/$week"
        cp $main_files "$dest/weekly/$week/"
        local weeks=("$dest"/weekly/*(/On))          # the weekly folders, newest name first
        for old in ${weeks[9,-1]}; do rm -rf "$old"; done
    fi
    local files=$(find "$dest/latest" -name '*.parquet' | wc -l | tr -d ' ')
    echo "backup: $files files ($(du -sh "$dest/latest" | cut -f1)) in $dest/latest; weekly copy $week"
}

# ---- a GitHub repository ----------------------------------------------------------------

# Make the release if it isn't there yet.
ensure_release() {  # tag, title
    gh release view "$1" -R "$repo" >/dev/null 2>&1 ||
        gh release create "$1" -R "$repo" --title "$2" --notes "Made by isem-data's scripts/backup_data.sh" >/dev/null
}

# Send the given files to a release, skipping any whose size already matches.
# (A dataset's file only ever grows or changes size when its content changes.)
upload_changed() {  # tag, files...
    local tag=$1; shift
    typeset -A remote
    local name size
    while read -r name size; do remote[$name]=$size; done < <(
        gh release view "$tag" -R "$repo" --json assets -q '.assets[] | "\(.name) \(.size)"'
    )
    local changed=()
    for file in "$@"; do
        [[ "${remote[${file:t}]:-}" == "$(stat -f %z "$file")" ]] || changed+=("$file")
    done
    if (( ${#changed} )); then
        gh release upload "$tag" $changed -R "$repo" --clobber >/dev/null
    fi
    sent=$(( sent + ${#changed} ))
}

# Check every file is on GitHub at its full size; count the ones that aren't.
check_uploaded() {  # tag, files...
    local tag=$1; shift
    typeset -A remote
    local name size
    while read -r name size; do remote[$name]=$size; done < <(
        gh release view "$tag" -R "$repo" --json assets -q '.assets[] | "\(.name) \(.size)"'
    )
    for file in "$@"; do
        checked=$(( checked + 1 ))
        if [[ "${remote[${file:t}]:-}" != "$(stat -f %z "$file")" ]]; then
            echo "WARNING: ${file:t} in $tag doesn't match the local file"
            wrong=$(( wrong + 1 ))
        fi
    done
}

backup_to_github() {
    repo=$1
    sent=0 checked=0 wrong=0
    if ! command -v gh >/dev/null; then
        echo "WARNING: the gh command isn't installed, so nothing was backed up."
        exit 4
    fi

    # The main datasets: always replaced, since they change every day.
    ensure_release latest "Latest copy of the main datasets"
    gh release upload latest $main_files -R "$repo" --clobber >/dev/null
    sent=$(( sent + ${#main_files} ))

    # The forecasts: one release per month, sending only new or changed days.
    local months=($(ls $forecasts 2>/dev/null | cut -c1-7 | sort -u))
    for month in $months; do
        ensure_release "forecasts-$month" "Per-farm wind forecasts, $month"
        upload_changed "forecasts-$month" $forecasts/$month-*.parquet
    done

    # Once a week, a second copy of the main datasets; keep the 8 newest.
    if ! gh release view "weekly-$week" -R "$repo" >/dev/null 2>&1; then
        ensure_release "weekly-$week" "Main datasets, week $week"
        gh release upload "weekly-$week" $main_files -R "$repo" --clobber >/dev/null
        local weeks=($(gh release list -R "$repo" --limit 200 --json tagName -q '.[].tagName' | grep '^weekly-' | sort -r))
        for old in ${weeks[9,-1]}; do gh release delete "$old" -R "$repo" --cleanup-tag --yes >/dev/null; done
    fi

    # Make sure what's on GitHub is what's here.
    check_uploaded latest $main_files
    for month in $months; do check_uploaded "forecasts-$month" $forecasts/$month-*.parquet; done
    if (( wrong )); then
        echo "WARNING: $wrong of $checked files on GitHub don't match. The backup is incomplete."
        exit 5
    fi
    echo "backup: $checked files match on github.com/$repo ($sent sent this run); weekly copy $week"
}

case "$dest" in
    github:*) backup_to_github "${dest#github:}" ;;
    *)        backup_to_folder "$dest" ;;
esac
