#!/bin/sh
# Sources cc.env and execs the daemon. Used directly and by the launchd job.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/cc.env"
exec "$DIR/venv/bin/python" "$DIR/cc-daemon.py"
