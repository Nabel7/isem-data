#!/bin/zsh
# Set up (or refresh) the daily update as a launchd job for your user.
#
# launchd is macOS's built-in scheduler. A job is described by a small settings
# file (a "plist") in ~/Library/LaunchAgents; this script writes that file with
# the right paths for this project and tells launchd to load it.
#
# Run now:     launchctl kickstart gui/$(id -u)/com.isem-data.daily-update
# Remove:      launchctl bootout gui/$(id -u)/com.isem-data.daily-update
#              rm ~/Library/LaunchAgents/com.isem-data.daily-update.plist

set -euo pipefail

project=$(cd "$(dirname "$0")/.." && pwd)    # the project folder, as a full path
label=com.isem-data.daily-update             # the job's name in launchd
plist=~/Library/LaunchAgents/$label.plist
log=$project/logs/daily_update.log

mkdir -p "$project/logs" ~/Library/LaunchAgents

# Run scripts/daily_update.sh every day at 14:00 local time, appending its output
# to the log. If the Mac is asleep at 14:00, launchd runs the job when it wakes.
cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>$project/scripts/daily_update.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>14</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$log</string>
    <key>StandardErrorPath</key>
    <string>$log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true   # unload an older copy, if any
launchctl bootstrap "gui/$(id -u)" "$plist"

echo "Installed $label: runs every day at 14:00."
echo "Settings: $plist"
echo "Log:      $log"
