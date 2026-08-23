#!/usr/bin/env bash
# Stops the server launch-app.sh (or the login autostart entry) started, if
# it's running. Closing the app window itself doesn't do this — the
# browser's app-mode window is just a frameless tab, it has no idea the
# server exists. Note: if the GNOME autostart entry
# (~/.config/autostart/news-bot-server.desktop) is still enabled, the
# server comes back on next login — this only stops the currently running
# instance, it doesn't disable autostart.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$DIR/bin/python"
MAIN="$DIR/main.py"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/news-bot"
PID_FILE="$STATE_DIR/server.pid"

pid=""
[ -f "$PID_FILE" ] && pid="$(cat "$PID_FILE")"
if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
  pid="$(pgrep -f "${PYTHON}.*${MAIN}" | head -n1)"
fi

if [ -z "$pid" ]; then
  echo "News Bot server isn't running."
  exit 0
fi

kill "$pid"
echo "Stopped (pid $pid)."
rm -f "$PID_FILE"
