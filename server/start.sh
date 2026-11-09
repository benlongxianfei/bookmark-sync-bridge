#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[ERROR] Python 3 not found in PATH." >&2
  exit 1
fi

echo "Bookmark Sync Bridge  http://127.0.0.1:8765"
echo "Press Ctrl+C to stop."
exec "$PY" bookmark_sync_server.py
