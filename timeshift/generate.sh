#!/bin/sh
# Rebuilds /live/delayed.m3u8 every few seconds so it always lists the
# segments whose UTC filename timestamps fall in the window
# [now - DELAY_SECONDS - WINDOW, now - DELAY_SECONDS]. The recorder's
# own index.m3u8 is the source of truth for segment durations, so the
# delayed playlist carries exact #EXTINF values (no player drift).
# Players see a normal live HLS stream lagging the source by DELAY_SECONDS.
set -u

DELAY="${DELAY_SECONDS:-7200}"
SEG="${SEGMENT_SECONDS:-10}"
WINDOW=$(( SEG * 3 ))
LOOP=$SEG
[ "$LOOP" -gt 3 ] && LOOP=3

SRC=/archive/index.m3u8
mkdir -p /live

while true; do
    now=$(date +%s)
    target=$(( now - DELAY ))
    low=$(date -u -d "@$(( target - WINDOW ))" +%Y%m%d%H%M%S)
    high=$(date -u -d "@$target" +%Y%m%d%H%M%S)

    tmp=/live/.delayed.m3u8.tmp
    body=/live/.body.tmp
    first=""
    count=0
    : > "$body"

    # Skip a torn/partial read of the source playlist: it must at least
    # start with #EXTM3U; anything else keeps the last good playlist.
    head -1 "$SRC" 2>/dev/null | grep -q '#EXTM3U' && {
        dur=""
        while IFS= read -r line; do
            case "$line" in
                \#EXTINF:*)
                    dur=${line#\#EXTINF:}
                    dur=${dur%,}
                    ;;
                \#*|"")
                    ;;
                *)
                    b=${line##*/}
                    ts=${b%.ts}
                    case "$ts" in
                        *[!0-9]*) dur="" ; continue ;;
                    esac
                    if [ "$ts" -ge "$low" ] && [ "$ts" -le "$high" ]; then
                        [ -z "$first" ] && first=$ts
                        if [ -n "$dur" ]; then inf=$dur; else inf="$SEG.000"; fi
                        {
                            echo "#EXTINF:$inf,"
                            echo "/archive/$b"
                        } >> "$body"
                        count=$(( count + 1 ))
                    fi
                    dur=""
                    ;;
            esac
        done < "$SRC"
    }

    if [ "$count" -gt 0 ]; then
        {
            echo "#EXTM3U"
            echo "#EXT-X-VERSION:3"
            echo "#EXT-X-TARGETDURATION:$SEG"
            echo "#EXT-X-MEDIA-SEQUENCE:$first"
            cat "$body"
        } > "$tmp"
        mv "$tmp" /live/delayed.m3u8
    elif [ ! -f /live/delayed.m3u8 ]; then
        # Startup (or recorder down) and no playlist yet: emit a
        # header-only live playlist so players keep retrying.
        {
            echo "#EXTM3U"
            echo "#EXT-X-VERSION:3"
            echo "#EXT-X-TARGETDURATION:$SEG"
            echo "#EXT-X-MEDIA-SEQUENCE:0"
        } > /live/delayed.m3u8
    fi
    rm -f "$body"

    sleep "$LOOP"
done
