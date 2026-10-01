#!/usr/bin/env python3
"""Gapless, duplicate-free MP3 feeder: STREAM_URL -> stdout (into ffmpeg).

Why this exists (replaces `curl | ffmpeg`):

- On every connect the radio sends a ~5.5 s "burst" of audio it already
  sent before (byte-identical MP3 frames). With curl, each reconnect
  re-recorded that burst, so listeners heard ~5 s repeated. Here the
  first bytes of a new connection are looked up in the tail of what was
  already forwarded and the overlap is skipped exactly.
- One ffmpeg process stays alive across reconnects, so the archive keeps
  one continuous timeline: no PTS reset, no segment-name phase shift,
  no half-written segment per reconnect.
- While the source is down, silent MP3 frames are fed in real time after
  a short grace, so segments keep appearing every SEGMENT_SECONDS and the
  delayed playlist never runs dry.
- Only whole, validated MP3 frames are forwarded; HTTP errors / HTML
  bodies never reach ffmpeg.
"""
import os, sys, time, queue, threading, urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("STREAM_URL", "")
READ_TIMEOUT = float(os.environ.get("FEED_READ_TIMEOUT", "15"))
SILENCE_GRACE = float(os.environ.get("FEED_SILENCE_GRACE", "3"))
TAIL_BYTES = 512 * 1024    # ~55 s of forwarded stream kept for de-duplication
PROBE_BYTES = 4096         # first bytes of a new connection looked up in the tail

BITRATES = {  # kbps, index 1..14
    1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],   # MPEG-1 L3
    2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],      # MPEG-2/2.5 L3
}
SAMPLERATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def log(msg):
    print(f"[feeder] {msg}", file=sys.stderr, flush=True)


def frame_info(b, i):
    """(length, duration_s) of a Layer III frame header at b[i], or None."""
    if i + 4 > len(b) or b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
        return None
    ver = (b[i + 1] >> 3) & 3
    layer = (b[i + 1] >> 1) & 3
    bri = (b[i + 2] >> 4) & 15
    sri = (b[i + 2] >> 2) & 3
    if ver == 1 or layer != 1 or bri in (0, 15) or sri == 3:
        return None
    pad = (b[i + 2] >> 1) & 1
    sr = SAMPLERATES[ver][sri]
    br = BITRATES[1 if ver == 3 else 2][bri] * 1000
    if ver == 3:
        return 144 * br // sr + pad, 1152 / sr
    return 72 * br // sr + pad, 576 / sr


def silent_frame(header):
    """A silent frame in the stream's format: no CRC, no padding, zeroed
    side info / main data (main_data_begin=0, so no bit reservoir use)."""
    h = bytearray(header[:4])
    h[1] |= 0x01      # protection bit set = no CRC
    h[2] &= ~0x02     # no padding
    n, dur = frame_info(h, 0)
    return bytes(h) + bytes(n - 4), dur


def reader(q):
    """Network thread: puts ("connect", None) then ("data", bytes) chunks;
    reconnects forever with capped backoff."""
    backoff = 1
    while True:
        try:
            req = urllib.request.Request(URL, headers={
                "Icy-MetaData": "0", "User-Agent": "quran-recorder/2.0"})
            with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as r:
                ctype = r.headers.get("Content-Type", "")
                if r.status != 200 or not ("audio" in ctype or "mpeg" in ctype):
                    raise IOError(f"bad response {r.status} {ctype!r}")
                q.put(("connect", None))
                backoff = 1
                while True:
                    chunk = r.read(4096)
                    if not chunk:
                        raise IOError("stream ended")
                    q.put(("data", chunk))
        except Exception as e:
            log(f"connection lost: {e}; reconnecting in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 10)


def main():
    out = sys.stdout.buffer
    q = queue.Queue()
    threading.Thread(target=reader, args=(q,), daemon=True).start()

    tail = bytearray()      # raw stream bytes already accepted (for de-dup)
    buf = bytearray()       # accepted, not yet forwarded (partial frame)
    probe = None            # bytes of a fresh connection awaiting de-dup
    skip = 0                # bytes of the current connection still to drop
    header = None           # last good frame header (format for silence)
    last_real = time.time()
    silence_s = 0.0         # silence fed during the current outage
    connects = 0

    def accept(data):
        nonlocal tail
        buf.extend(data)
        tail.extend(data)
        if len(tail) > TAIL_BYTES:
            del tail[:len(tail) - TAIL_BYTES]

    def flush_frames():
        """Forward every complete, validated frame in buf."""
        nonlocal header
        i, sent = 0, []
        while True:
            fi = frame_info(buf, i)
            if fi is None:
                j = buf.find(b"\xff", i + 1)
                if j < 0:
                    i = len(buf)
                    break
                i = j
                continue
            n, _ = fi
            if i + n + 4 > len(buf):
                break                      # need the next header to confirm
            if frame_info(buf, i + n) is None:
                i += 1                     # false sync: keep scanning
                continue
            sent.append(bytes(buf[i:i + n]))
            header = bytes(buf[i:i + 4])
            i += n
        del buf[:i]
        if sent:
            out.write(b"".join(sent))
            out.flush()

    while True:
        try:
            kind, data = q.get(timeout=0.1)
        except queue.Empty:
            kind, data = None, None

        if kind == "connect":
            connects += 1
            probe, skip = bytearray(), 0
        elif kind == "data":
            if probe is not None:
                probe.extend(data)
                if len(probe) < PROBE_BYTES:
                    continue
                data, probe = bytes(probe), None
                p = tail.rfind(data[:PROBE_BYTES]) if tail else -1
                if p >= 0:
                    skip = len(tail) - p
                    log(f"reconnect #{connects}: dropped {skip} replayed bytes")
                else:
                    if connects > 1:
                        log(f"reconnect #{connects}: no overlap (content gap)")
                    buf.clear()            # drop the old partial frame
            if skip:
                d = min(skip, len(data))
                skip -= d
                data = data[d:]
            if data:
                accept(data)
                flush_frames()
                last_real = time.time()
                silence_s = 0.0

        # outage: keep the timeline moving with real-time silence
        if header is not None:
            due = time.time() - last_real - SILENCE_GRACE - silence_s
            if due > 0:
                frame, dur = silent_frame(header)
                n = int(due / dur) + 1
                if silence_s == 0.0:
                    log("source silent: feeding silence")
                    buf.clear()            # partial frame can never complete
                out.write(frame * n)
                out.flush()
                silence_s += n * dur


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
