#!/bin/bash
# Weekly analyst snapshot on this Mac: Sundays 3:30 AM (after the 2:30 AM nightly backup).
# Runs etl/export_snapshot.py --zip --keep 3 --drive and logs to ~/Library/Logs/iog/export-snapshot.log.
#
#   etl/install_export_schedule.sh            # write ~/Library/LaunchAgents/org.iog.export-snapshot.plist and load it
#   etl/install_export_schedule.sh --print    # only print the plist
#   etl/install_export_schedule.sh --uninstall
#
# Test it once after installing: launchctl kickstart gui/$(id -u)/org.iog.export-snapshot
set -euo pipefail

LABEL=org.iog.export-snapshot
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv-analytics/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/iog/export-snapshot.log"

plist() {
cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string>
    <string>$REPO/etl/export_snapshot.py</string>
    <string>--zip</string>
    <string>--keep</string><string>3</string>
    <string>--drive</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StartCalendarInterval</key><dict>
    <key>Weekday</key><integer>0</integer>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>30</integer>
  </dict>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/opt/postgresql@17/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
}

case "${1:-}" in
  --print) plist ;;
  --uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $LABEL" ;;
  "")
    [ -x "$PY" ] || { echo "Missing $PY (analytics venv with duckdb)"; exit 1; }
    mkdir -p "$(dirname "$LOG")" "$(dirname "$PLIST")"
    plist > "$PLIST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Installed $PLIST: Sundays 3:30 AM. Log: $LOG"
    echo "Run once now: launchctl kickstart gui/$(id -u)/$LABEL" ;;
  *) echo "usage: $0 [--print|--uninstall]"; exit 2 ;;
esac
