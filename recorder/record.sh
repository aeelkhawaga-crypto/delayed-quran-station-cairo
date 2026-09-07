#!/bin/sh
# Records STREAM_URL into a rolling HLS archive in /archive, forever.
#
# - Network sources are pulled with curl through a pipe: a byte stream
#   cannot seek back, which avoids the mp3 demuxer's resync behaviour
#   that can briefly duplicate audio when reading http directly.
# - Segments are named by UTC timestamp (%Y%m%d%H%M%S); the timeshift
#   service uses those names to serve segments from DELAY_SECONDS ago.
# - The HLS muxer writes index.m3u8 with exact segment durations and
#   deletes segments older than the configured archive window.
# - A flock guard makes sure only one recorder ever writes to /archive,
#   even if a duplicate container is accidentally started.
set -u

SEG="${SEGMENT_SECONDS:-10}"
BR="${BITRATE:-96k}"
KEEP_MIN=$(( (${ARCHIVE_HOURS:-4} * 60) + 30 ))
LIST_SIZE=$(( (KEEP_MIN * 60) / SEG ))

exec 9>/archive/.recorder.lock
if ! flock -n 9; then
    echo "[recorder] another recorder already holds /archive — exiting"
    exit 1
fi

cleanup() {
    while true; do
        find /archive -type f -name '*.ts' -mmin "+$KEEP_MIN" -delete 2>/dev/null
        sleep 300
    done
}
cleanup &

run_ffmpeg() {
    ffmpeg -hide_banner -loglevel warning -stats \
        "$@" \
        -vn -c:a aac -b:a "$BR" -ac 2 -ar 44100 \
        -f hls -hls_time "$SEG" -hls_list_size "$LIST_SIZE" \
        -hls_flags delete_segments \
        -strftime 1 -hls_segment_filename "/archive/%Y%m%d%H%M%S.ts" \
        /archive/index.m3u8
}

case "$STREAM_URL" in
    lavfi:*)
        echo "[recorder] test source: $STREAM_URL"
        while true; do
            run_ffmpeg -re -f lavfi -i "${STREAM_URL#lavfi:}"
            echo "[recorder] ffmpeg exited ($?), retrying in 3s"
            sleep 3
        done
        ;;
    *)
        echo "[recorder] connecting: $STREAM_URL"
        while true; do
            curl -sL --retry 5 --retry-delay 5 --retry-all-errors \
                 -H "Icy-MetaData: 0" "$STREAM_URL" |
            run_ffmpeg -i pipe:0
            echo "[recorder] stream ended ($?), reconnecting in 3s"
            sleep 3
        done
        ;;
esac
