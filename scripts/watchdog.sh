#!/bin/sh
# Recorder hang watchdog: if the newest archived segment is older than
# 90 seconds, the recorder (curl|ffmpeg) has hung — restart it.
# Intended for host cron: * * * * * root /opt/quran-radio/scripts/watchdog.sh
# Also records its verdict to schedule/.watchdog.json for the monitor page.
set -u
ARCHIVE=${ARCHIVE:-/opt/quran-radio/data/archive}
COMPOSE=${COMPOSE:-/opt/quran-radio/docker-compose.yml}
DIR=${DIR:-/opt/quran-radio}
STATE=${STATE:-/opt/quran-radio/schedule/.watchdog.json}

newest=$(find "$ARCHIVE" -maxdepth 1 -name '*.ts' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
now=$(date +%s)
[ -n "$newest" ] || exit 0
age=$(( now - ${newest%.*} ))
restarted=false
if [ "$age" -gt 90 ]; then
    echo "$(date -Is) watchdog: newest segment ${age}s old, restarting recorder" >&2
    (cd "$DIR" && docker compose -f "$COMPOSE" restart recorder) >&2 2>&1
    restarted=true
fi
tmp="$STATE.tmp"
printf '{"checked_at": %s, "newest_age_s": %s, "restarted": %s}\n' \
    "$now" "$age" "$restarted" > "$tmp" && mv "$tmp" "$STATE"
