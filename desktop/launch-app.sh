#!/usr/bin/env bash
# Launches News Bot as a standalone app window on this machine.
#
# On-demand only: starts the server (main.py — the same single process
# that serves the web UI *and* runs the Discord bot, see main.py's own
# docstring) if it isn't already running, then opens the web UI in a
# browser "app mode" window. Same launcher pattern as Achievement Vault's
# bin/launch-app.sh and Compta's webapp/run.sh (Firefox first, Chrome/
# Chromium --app= fallback, xdg-open last resort) — kept in this project's
# own desktop/ directory rather than bin/, because bin/ here is the
# project's Python venv (bin/python, bin/uvicorn, ...), not a scripts
# folder, unlike the other two apps.
#
# News Bot also has a GNOME autostart entry
# (~/.config/autostart/news-bot-server.desktop) that starts main.py
# headlessly on login, since the Discord bot and the 8am daily digest need
# to keep running even when no browser window is open — so the common case
# here is "already up, just open a window onto it." This script still
# starts it itself if that hasn't happened yet (autostart disabled, login
# script hasn't run, or the process died), using the exact same command
# the autostart entry uses.
#
# Nothing about closing the browser window stops the server — see
# stop-app.sh for that.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$DIR/bin/python"
MAIN="$DIR/main.py"

if [ ! -x "$PYTHON" ]; then
  echo "$PYTHON not found — the project's venv looks missing. See CLAUDE.md's 'Running it' section." >&2
  exit 1
fi

# PORT/HOST default the same way main.py itself does (os.environ.get, see
# main.py) -- read from .env if set there, since that's what the running
# server would actually be bound to.
PORT="${PORT:-8000}"
if [ -f "$DIR/.env" ]; then
  env_port="$(grep -E '^PORT=' "$DIR/.env" | tail -n1 | cut -d= -f2- || true)"
  [ -n "$env_port" ] && PORT="$env_port"
fi
URL="http://127.0.0.1:$PORT/"

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/news-bot"
LOG_FILE="$STATE_DIR/server.log"
PID_FILE="$STATE_DIR/server.pid"
mkdir -p "$STATE_DIR"

is_up() {
  curl -fs -o /dev/null -m 1 "http://127.0.0.1:$PORT/api/settings"
}

if ! is_up; then
  echo "Starting News Bot server on port $PORT…"
  # Absolute paths (not `cd "$DIR" && ...`) so the pgrep match below — and
  # stop-app.sh's — can find this exact process by its own main.py path,
  # not some other project's.
  nohup "$PYTHON" "$MAIN" >>"$LOG_FILE" 2>&1 &
  disown

  for _ in $(seq 1 40); do
    is_up && break
    sleep 0.3
  done
  if ! is_up; then
    echo "Server didn't come up — check $LOG_FILE" >&2
    exit 1
  fi
fi

# Record the PID of the actual python process running main.py, not the
# subshell above — nohup's own $! is the backgrounded job, not reliably
# the final python PID once bash's own job control has done its
# bookkeeping. Matched on this repo's absolute main.py path so it can't
# pick up an unrelated project's own main.py. A no-op (harmless empty
# write skipped) if the server was already running and started by
# autostart instead — that process still matches the same pgrep pattern.
pgrep -f "${PYTHON}.*${MAIN}" | head -n1 > "$PID_FILE" || true

# Firefox first by default. Firefox has no real equivalent of Chrome's
# --app= (a frameless window with no tabs/address bar) — the closest is a
# plain new window, so this opens with normal browser chrome rather than
# looking like a standalone app the way the Chrome fallback below does.
# $BROWSER still overrides everything if set.
if [ -n "${BROWSER:-}" ]; then
  exec "$BROWSER" --app="$URL" --new-window >/dev/null 2>&1
elif command -v firefox >/dev/null 2>&1; then
  exec firefox --new-window "$URL" >/dev/null 2>&1
elif command -v google-chrome >/dev/null 2>&1; then
  exec google-chrome --app="$URL" --new-window >/dev/null 2>&1
elif command -v chromium-browser >/dev/null 2>&1; then
  exec chromium-browser --app="$URL" --new-window >/dev/null 2>&1
elif command -v chromium >/dev/null 2>&1; then
  exec chromium --app="$URL" --new-window >/dev/null 2>&1
else
  exec xdg-open "$URL" >/dev/null 2>&1
fi
