#!/usr/bin/env bash
# Starts ONLY the web app (FastAPI, no Discord bot, no scheduler) on a throw-away COPY of the database, for the UI audit.
#   audit/serve-test.sh start|stop      (port 8010 — never the real app's 8000; the real news.db is never opened)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE="${TMPDIR:-/tmp}/news-bot-audit"
PORT=8010
case "${1:-}" in
  start)
    mkdir -p "$STATE"
    cp "$DIR/news.db" "$STATE/news.db"
    DB_PATH="$STATE/news.db" nohup "$DIR/bin/python" -c "import uvicorn, db; db.init_db(); from web import app; uvicorn.run(app, host='127.0.0.1', port=$PORT, log_level='warning')" >"$STATE/server.log" 2>&1 &
    echo $! > "$STATE/server.pid"
    for _ in $(seq 1 30); do curl -fs -o /dev/null "http://127.0.0.1:$PORT/api/settings" && break; sleep 0.3; done
    echo "audit server on http://127.0.0.1:$PORT (pid $(cat "$STATE/server.pid"))";;
  stop)
    pid="$(cat "$STATE/server.pid" 2>/dev/null || true)"
    if [ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q uvicorn; then kill "$pid"; echo stopped; fi
    rm -f "$STATE/server.pid";;
  *) echo "usage: $0 start|stop" >&2; exit 1;;
esac
