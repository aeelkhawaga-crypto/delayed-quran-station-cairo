#!/bin/sh
# Recorder hang watchdog: if the newest archived segment is older than
# 90 seconds, the recorder (curl|ffmpeg) has hung — restart it.
# Intended for host cron: * * * * * root /opt/quran-radio/scripts/watchdog.sh
set -u
ARCHIVE=${ARCHIVE:-/opt/quran-radio/data/archive}
COMPOSE=${COMPOSE:-/opt/quran-radio/docker-compose.yml}
DIR=${DIR:-/opt/quran-radio}

newest=$(find "$ARCHIVE" -maxdepth 1 -name '*.ts' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
[ -n "$newest" ] || exit 0
now=$(date +%s)
age=$(( now - ${newest%.*} ))
if [ "$age" -gt 90 ]; then
    echo "$(date -Is) watchdog: newest segment ${age}s old, restarting recorder" >&2
    (cd "$DIR" && docker compose -f "$COMPOSE" restart recorder) >&2 2>&1
fi
