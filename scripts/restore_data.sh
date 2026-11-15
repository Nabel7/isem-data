#!/bin/zsh
# Fill data/ from the backup on GitHub: the latest main datasets and every month of
# per-farm wind forecasts. The opposite of backup_data.sh's GitHub mode.
#
#   scripts/restore_data.sh github:Nabel7/isem-data-archive
#   scripts/restore_data.sh                    (uses the place in .backup_to)
#
# ISEM_RESTORE_PREFIX reads releases with that prefix (ci-latest, ...) instead.
# Needs the gh command, logged in or given a token in GH_TOKEN. Files already in data/
# are replaced.

set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

source_place=${1:-}
if [[ -z "$source_place" && -f .backup_to ]]; then
    source_place=$(head -n 1 .backup_to)
fi
if [[ "$source_place" != github:* ]]; then
    echo "Say which GitHub repository to restore from, e.g. github:Nabel7/isem-data-archive"
    exit 2
fi
repo=${source_place#github:}
prefix=${ISEM_RESTORE_PREFIX:-}

# Downloads sometimes drop part-way, so each release is tried up to 3 times.
fetch() {  # tag, folder
    for attempt in 1 2 3; do
        gh release download "$1" -R "$repo" -D "$2" --clobber && return 0
        echo "download of $1 failed (try $attempt of 3)"
        sleep $(( attempt * 10 ))
    done
    echo "ERROR: couldn't download $1 from github.com/$repo"
    exit 3
}

mkdir -p data/wind_unit_forecasts
fetch "${prefix}latest" data
months=($(gh release list -R "$repo" --limit 200 --json tagName -q '.[].tagName' | grep "^${prefix}forecasts-" | sort))
for tag in $months; do
    fetch "$tag" data/wind_unit_forecasts
done
echo "restored: $(ls data/*.parquet | wc -l | tr -d ' ') datasets and $(ls data/wind_unit_forecasts | wc -l | tr -d ' ') days of forecasts from github.com/$repo (${prefix}latest, ${#months} month(s) of forecasts)"
