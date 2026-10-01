#!/usr/bin/env python3
"""Delayed playlist builder — single continuous path.

Rebuilds /live/delayed.m3u8 every few seconds from the recorded archive
segments around (now - DELAY). All content changes (Irish Adhan inserts,
Cairo Adhan suppression with fillers) are done by the SEPARATE splicer
job (splice.py), which overwrites the archive segment *files* inside the
delayed window before they air. This service only marks the spliced
ranges with EXT-X-DISCONTINUITY so players flush cleanly at the audio
change; the playlist itself is always one continuous archive sequence.
"""
import os, re, time, json, glob, shutil, threading, subprocess, urllib.request, datetime

SEG = int(os.environ.get("SEGMENT_SECONDS", "10"))
DELAY = int(os.environ.get("DELAY_SECONDS", "7200"))
METHOD = os.environ.get("PRAYER_METHOD", "5")
LOOP = float(os.environ.get("LOOP_SECONDS", "2.5"))
WIN_PRE = int(os.environ.get("WINDOW_PRE_SECONDS", "90"))
WIN_POST = int(os.environ.get("WINDOW_POST_SECONDS", "360"))
MIN_GAP = int(os.environ.get("MIN_GAP_SECONDS", "240"))

ARCHIVE = os.environ.get("ARCHIVE_DIR", "/archive")
LIVE = os.environ.get("LIVE_DIR", "/live")
STATIC = os.environ.get("STATIC_DIR", "/live-static")
ADHANS = os.environ.get("ADHAN_DIR", "/adhans")
FAJR_ADHANS = os.environ.get("FAJR_ADHAN_DIR", "/adhans-fajr")
FILLERS = os.environ.get("FILLER_DIR", "/fillers")
STARTERS = os.environ.get("STARTER_DIR", "/starters")
SCHED = os.environ.get("SCHEDULE_DIR", "/schedule")
IRISH_FILE = os.path.join(SCHED, "irish-times.txt")
IRISH_JSON = os.path.join(SCHED, "dublin-prayer-times.json")
CAIRO_CACHE = os.path.join(SCHED, "cairo-times.json")
STATE_FILE = os.path.join(LIVE, ".scheduler-state.json")

UTC = datetime.timezone.utc
DOW = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}
PRAYERS = ("Fajr", "Dhuhr", "Asr", "Maghrib", "Isha")
CAIRO_LAT, CAIRO_LON = 30.0444, 31.2357
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac")


def log(msg):
    print(f"[scheduler] {msg}", flush=True)


def num(d, default=0.0):
    try:
        return float(d)
    except Exception:
        return default


def fmt(dt):
    return dt.strftime("%Y%m%d%H%M%S")


def dublin_offset(dt_utc):
    """Europe/Dublin offset via EU DST rules (no tzdata needed)."""
    def last_sunday(y, m):
        d = datetime.date(y, m, 31)
        while d.weekday() != 6:
            d -= datetime.timedelta(days=1)
        return d
    year = dt_utc.year
    start = datetime.datetime(year, 3, last_sunday(year, 3).day, 1, 0, tzinfo=UTC)
    end = datetime.datetime(year, 10, last_sunday(year, 10).day, 1, 0, tzinfo=UTC)
    return 3600 if start <= dt_utc < end else 0


def http_get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "quran-scheduler/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------------- state ----------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)

state = load_state()

def purge_old_state(now):
    for k in list(state.keys()):
        if k.startswith("irish-") or k.startswith("cairo-"):
            try:
                ts = float(k.split("-", 1)[1])
                if now - ts > 2 * 86400:
                    del state[k]
            except Exception:
                pass

# ---------------- irish timetable ----------------

def irish_events(now):
    """Event start epochs (UTC) near now: manual timetable + IFI yearly JSON."""
    return sorted(set(_irish_events_text(now) + _irish_events_json(now)))

def _irish_events_text(now):
    """From the user's manual timetable file: [(start, prayer|None)].
    A line may end with a prayer name (e.g. "13:23 dhuhr" or
    "2026-09-14 08:20 fajr") to attach a starter for one-off events."""
    events = []
    try:
        lines = open(IRISH_FILE).read().splitlines()
    except Exception:
        return events
    day = datetime.datetime.fromtimestamp(now, UTC).date()
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"(?:(\S+)\s+)?(\d{1,2}):(\d{2})(?:\s+([A-Za-z]+))?", line)
        if not m:
            continue
        daypart, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
        prayer = m.group(4).lower() if m.group(4) else None
        if prayer not in {p.lower() for p in PRAYERS}:
            prayer = None
        for delta in (-1, 0, 1):
            d = day + datetime.timedelta(days=delta)
            if daypart is None:
                ok = True
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", daypart):
                ok = (daypart == d.isoformat())
            else:
                ok = (DOW.get(daypart.title(), -1) == d.weekday())
            if not ok:
                continue
            local = datetime.datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC)
            events.append((local.timestamp() - dublin_offset(local), prayer))
    return events

def _irish_events_json(now):
    """From the Islamic Foundation of Ireland yearly timetable (MM-DD keys,
    Europe/Dublin local times, repeating annually): [(start, prayer)]."""
    try:
        days = json.load(open(IRISH_JSON)).get("days", {})
    except Exception:
        return []
    if not days:
        return []
    events = []
    off = dublin_offset(datetime.datetime.fromtimestamp(now, UTC))
    dublin_today = datetime.datetime.fromtimestamp(now + off, UTC).date()
    for delta in (-1, 0, 1):
        d = dublin_today + datetime.timedelta(days=delta)
        entry = days.get(f"{d.month:02d}-{d.day:02d}")
        if not entry:
            continue
        times = entry.get("times") or entry.get("standardTimes") or {}
        for p in ("fajr", "dhuhr", "asr", "maghrib", "isha"):
            t = times.get(p)
            if not t:
                continue
            hh, mm = str(t).split(":")[:2]
            local = datetime.datetime(d.year, d.month, d.day,
                                      int(hh), int(mm), tzinfo=UTC)
            events.append((local.timestamp() - off, p))
    return events

def irish_prayer_events(now):
    """[(start_epoch, prayer|None)] near now: manual timetable + IFI JSON."""
    tagged = {}
    for start, prayer in _irish_events_text(now):
        tagged[start] = prayer
    for start, prayer in _irish_events_json(now):
        tagged.setdefault(start, prayer)
    return sorted(tagged.items())

def irish_events(now):
    return [s for s, _ in irish_prayer_events(now)]

# ---------------- cairo timings ----------------

def cairo_prayers(day):
    """UTC epochs for the five prayers on the given UTC date (cached)."""
    key = day.isoformat()
    try:
        days = json.load(open(CAIRO_CACHE)).get("days", {})
    except Exception:
        days = {}
    if key in days:
        return days[key]
    ddmmyyyy = day.strftime("%d-%m-%Y")
    url = (f"https://api.aladhan.com/v1/timings/{ddmmyyyy}"
           f"?latitude={CAIRO_LAT}&longitude={CAIRO_LON}"
           f"&method={METHOD}&timezone=Africa/Cairo&iso8601=true")
    try:
        d = http_get_json(url)["data"]["timings"]
        utc = {}
        for p in PRAYERS:
            dt = datetime.datetime.fromisoformat(d[p])
            utc[p] = dt.astimezone(UTC).timestamp()
        os.makedirs(SCHED, exist_ok=True)
        days[key] = utc
        # keep only today/tomorrow to bound the file
        today = datetime.datetime.now(UTC).date()
        keep = {k: v for k, v in days.items()
                if today - datetime.timedelta(days=1) <= datetime.date.fromisoformat(k)
                <= today + datetime.timedelta(days=1)}
        tmp = CAIRO_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"days": keep}, f)
        os.replace(tmp, CAIRO_CACHE)
        log(f"Cairo timings cached for {key}")
        return utc
    except Exception as e:
        log(f"Cairo timings fetch failed for {key}: {e}")
        return None

def cairo_windows(now):
    """Suppression windows in served wall-time (UTC epochs)."""
    day = datetime.datetime.fromtimestamp(now, UTC).date()
    wins = []
    for d in (day, day + datetime.timedelta(days=1)):
        pr = cairo_prayers(d)
        if not pr:
            continue
        for p, pt in pr.items():
            start = pt + DELAY - WIN_PRE
            end = pt + DELAY + WIN_POST
            if end > now - 3600:
                wins.append({"name": p, "start": start, "end": end,
                             "content_end": pt + WIN_POST})
    return wins

# ---------------- chunksets (adhan / filler / starter) ----------------

def folder_sig(folder):
    files = sorted(f for f in glob.glob(os.path.join(folder, "*"))
                   if os.path.splitext(f)[1].lower() in AUDIO_EXT)
    return files, "|".join(f"{os.path.basename(f)}:{os.path.getmtime(f)}" for f in files)

def probe_duration(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip().rstrip(","))
    except Exception:
        return None

def retimed_chunk(f, sdir):
    """Chunk an audio file into whole 10s slots: retime with atempo
    (imperceptible) to an exact multiple of SEG, then pad/trim to exact,
    so every segment is a full slot and slots never carry short tails."""
    dur = probe_duration(f)
    n = 1
    if dur:
        cands = [k for k in (round(dur / SEG) + d for d in (-1, 0, 1)) if k >= 1]
        n = min(cands, key=lambda k: abs(dur / (k * SEG) - 1))
    target = n * SEG
    tempo = dur / target if dur else 1.0
    filters = []
    if 0.5 <= tempo <= 2.0:
        filters.append(f"atempo={tempo:.5f}")
    # trim a hair short: the AAC priming frame (~23ms) would otherwise spill
    # past the target and the muxer emits a tiny tail segment
    filters.append(f"apad,atrim=0:{target - 0.06:.3f}")
    return subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", f,
                           "-vn", "-af", ",".join(filters),
                           "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-ar", "44100",
                           "-f", "hls", "-hls_time", str(SEG),
                           "-hls_list_size", "0",
                           "-hls_segment_filename",
                           os.path.join(sdir, "seg_%04d.ts"),
                           os.path.join(sdir, "playlist.m3u8")],
                          capture_output=True, text=True)

CHUNK_STATUS = os.path.join(STATIC, ".chunk-status.json")

def chunk_status(kind, state):
    """Record 'preparing'/'ready' per kind for the monitor."""
    try:
        st = json.load(open(CHUNK_STATUS))
    except Exception:
        st = {}
    st[kind] = {"state": state, "at": time.time()}
    tmp = CHUNK_STATUS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, CHUNK_STATUS)

def ensure_chunksets():
    """(Re)chunk adhan/fajr-adhan/filler audio into static HLS segment sets on change.

    A rebuild is chunked into a hidden staging dir first and swapped in only
    when complete, so the splicer never sees a half-built or empty pool."""
    os.makedirs(STATIC, exist_ok=True)
    sets = {}
    for kind, folder in (("adhan", ADHANS), ("fajr-adhan", FAJR_ADHANS),
                         ("filler", FILLERS)):
        try:
            files, sig = folder_sig(folder)
        except Exception:
            files, sig = [], ""
        tag = os.path.join(STATIC, f".{kind}-v3.sig")
        try:
            old = open(tag).read()
        except Exception:
            old = None
        if sig == old and (not files or glob.glob(os.path.join(STATIC, f"{kind}-*"))):
            sets[kind] = sorted(glob.glob(os.path.join(STATIC, f"{kind}-*"))) if files else []
            continue
        chunk_status(kind, "preparing")
        stage = os.path.join(STATIC, f".stage-{kind}")
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage)
        staged = []
        for f in files:
            sdir = os.path.join(stage, f"{kind}-{len(staged)}")
            os.makedirs(sdir, exist_ok=True)
            r = retimed_chunk(f, sdir)
            if r.returncode != 0:
                log(f"chunking failed for {f}: {r.stderr.strip()[:200]}")
                shutil.rmtree(sdir, ignore_errors=True)
                continue
            staged.append(sdir)
        # swap: drop the old sets, move the staged ones into place
        for d in glob.glob(os.path.join(STATIC, f"{kind}-*")):
            shutil.rmtree(d, ignore_errors=True)
        made = []
        for sdir in staged:
            dst = os.path.join(STATIC, os.path.basename(sdir))
            os.rename(sdir, dst)
            made.append(dst)
        shutil.rmtree(stage, ignore_errors=True)
        with open(tag, "w") as tf:
            tf.write(sig)
        sets[kind] = made
        chunk_status(kind, "ready")
        log(f"{kind}: {len(made)} file(s) chunked")
    return sets

def ensure_starter_chunksets():
    """Per-prayer starters (adhan-prefixes), each retimed to whole slots."""
    os.makedirs(STATIC, exist_ok=True)
    out = {}
    did = False
    for f in sorted(glob.glob(os.path.join(STARTERS, "*"))):
        if os.path.splitext(f)[1].lower() not in AUDIO_EXT:
            continue
        prayer = os.path.splitext(os.path.basename(f))[0].lower()
        if prayer not in {p.lower() for p in PRAYERS}:
            continue
        sdir = os.path.join(STATIC, f"starter-{prayer}")
        tag = os.path.join(STATIC, f".starter-{prayer}-v2.sig")
        sig = f"{os.path.basename(f)}:{os.path.getmtime(f)}"
        try:
            old = open(tag).read()
        except Exception:
            old = None
        if sig == old and glob.glob(os.path.join(sdir, "seg_*.ts")):
            out[prayer] = sdir
            continue
        chunk_status("starter", "preparing")
        did = True
        stage = os.path.join(STATIC, f".stage-starter-{prayer}")
        shutil.rmtree(stage, ignore_errors=True)
        os.makedirs(stage)
        r = retimed_chunk(f, stage)
        if r.returncode != 0:
            log(f"chunking failed for starter {f}: {r.stderr.strip()[:200]}")
            shutil.rmtree(stage, ignore_errors=True)
            continue
        shutil.rmtree(sdir, ignore_errors=True)
        os.rename(stage, sdir)
        with open(tag, "w") as tf:
            tf.write(sig)
        out[prayer] = sdir
        log(f"starter: {prayer} chunked")
    # a starter whose file was removed must stop airing
    for d in glob.glob(os.path.join(STATIC, "starter-*")):
        prayer = os.path.basename(d)[len("starter-"):]
        if prayer not in out:
            shutil.rmtree(d, ignore_errors=True)
            try:
                os.remove(os.path.join(STATIC, f".starter-{prayer}-v2.sig"))
            except OSError:
                pass
            log(f"starter: {prayer} removed")
    if did:
        chunk_status("starter", "ready")
    return out

def chunk_loop():
    """Background chunker: re-chunking takes ~a minute for a full pool and
    must never block the playlist loop (players would stall)."""
    while True:
        try:
            ensure_chunksets()
            ensure_starter_chunksets()
        except Exception as e:
            log(f"chunking error: {e}")
        time.sleep(5)

def load_chunkset(sdir):
    entries, dur = [], None
    try:
        lines = open(os.path.join(sdir, "playlist.m3u8")).read().splitlines()
    except Exception:
        return [], 0.0
    for line in lines:
        if line.startswith("#EXTINF:"):
            dur = line[len("#EXTINF:"):].rstrip().rstrip(",")
        elif line and not line.startswith("#"):
            entries.append((dur or str(SEG), line.strip()))
            dur = None
    total = sum(num(d, SEG) for d, _ in entries)
    return entries, total

# ---------------- archive (delayed mode) ----------------

PLAYLIST_MAX = 8   # segments per emitted playlist (~80s of buffer)


def archive_names():
    """[(ts, name)] of well-formed archive segments, sorted by timestamp.

    Reads the archive directory (not the recorder's index.m3u8): the index
    only lists the recorder's own files and truncates when the recorder
    restarts, while healed gap-fill segments exist only on disk."""
    out = []
    for f in glob.glob(os.path.join(ARCHIVE, "*.ts")):
        b = os.path.basename(f)
        m = re.fullmatch(r"(\d{14})\.ts", b)
        if m:
            dt = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            out.append((int(dt.timestamp()), b))
    return sorted(out)


def index_durations():
    """{segment_name: dur_str} from the recorder index (recent window)."""
    d = {}
    try:
        lines = open(os.path.join(ARCHIVE, "index.m3u8")).read().splitlines()
    except Exception:
        return d
    dur = None
    for line in lines:
        if line.startswith("#EXTINF:"):
            dur = line[len("#EXTINF:"):].rstrip().rstrip(",")
        elif line.startswith("#") or not line.strip():
            continue
        else:
            b = os.path.basename(line.strip())
            if re.fullmatch(r"\d{14}\.ts", b) and dur is not None:
                d[b] = dur
            dur = None
    return d


def delayed_entries(target_ts):
    """Sliding window over archive names: the last PLAYLIST_MAX segments
    with name-ts <= target_ts, oldest first.

    The window's front edge anchors on the newest AVAILABLE name, not on
    target_ts, and reaches back PLAYLIST_MAX segments — so sparse names
    (upstream bursts/stalls, healed fills) shrink the playlist only when
    fewer than PLAYLIST_MAX segments exist at all, never to one fresh
    segment per tick. Returns [(ts, name)] (may be shorter than
    PLAYLIST_MAX near the start of an archive)."""
    avail = [(t, n) for t, n in archive_names() if t <= target_ts]
    return avail[-PLAYLIST_MAX:]

def entries_ts(entry):
    m = re.search(r"(\d{14})", entry[1])
    if m:
        dt = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        return dt.timestamp()
    return 0

# ---------------- emit ----------------

RUNS_FILE = os.path.join(SCHED, ".splice-runs.json")

def splice_runs():
    """Active spliced content ranges [(t0, t1)] in content time, from the splicer."""
    try:
        return [tuple(r) for r in json.load(open(RUNS_FILE)).get("runs", [])]
    except Exception:
        return []

def emit(seq, entries):
    """entries: (dur, uri) pairs; (None, None) writes an EXT-X-DISCONTINUITY."""
    os.makedirs(LIVE, exist_ok=True)
    tmp = os.path.join(LIVE, ".delayed.m3u8.tmp")
    with open(tmp, "w") as f:
        f.write("#EXTM3U\n#EXT-X-VERSION:3\n")
        f.write(f"#EXT-X-TARGETDURATION:{SEG}\n#EXT-X-MEDIA-SEQUENCE:{seq}\n")
        for dur, uri in entries:
            if dur is None:
                f.write("#EXT-X-DISCONTINUITY\n")
            else:
                f.write(f"#EXTINF:{dur},\n{uri}\n")
    os.replace(tmp, os.path.join(LIVE, "delayed.m3u8"))

def emit_header_only():
    if not os.path.exists(os.path.join(LIVE, "delayed.m3u8")):
        emit(0, [])

# ---------------- main loop ----------------

DURS_FILE = os.path.join(SCHED, ".splice-durs.json")
SEQ_FILE = os.path.join(LIVE, ".media-seq.json")
_emitted_key = {"k": None}   # (first_ts, last_ts) of the last emitted playlist
_seq = {"map": None}         # segment name -> media sequence number

def media_sequence(names):
    """EXT-X-MEDIA-SEQUENCE for a playlist of `names` (oldest first).

    HLS requires each segment to keep one sequence number and consecutive
    segments to differ by exactly 1; native players (iOS AVPlayer,
    ExoPlayer) track their position by it. Each name gets the next number
    the first time it enters the playlist; the map is persisted so a
    scheduler restart continues the count. A fresh map is seeded from the
    first segment's epoch, which stays above the old epoch-based numbering."""
    m = _seq["map"]
    if m is None:
        try:
            m = {k: int(v) for k, v in json.load(open(SEQ_FILE)).get("map", {}).items()}
        except Exception:
            m = {}
    nxt = max(m.values()) + 1 if m else None
    if nxt is None:
        ts = re.search(r"(\d{14})", names[0]).group(1)
        nxt = int(datetime.datetime.strptime(ts, "%Y%m%d%H%M%S")
                  .replace(tzinfo=UTC).timestamp())
    for n in names:
        if n not in m:
            m[n] = nxt
            nxt += 1
    first = m[names[0]]
    if any(m[n] != first + i for i, n in enumerate(names)):
        # a name appeared between already-numbered ones (should not happen:
        # names are final long before they air). Renumber forward so no
        # number is ever reused for a different segment.
        log(f"media sequence: out-of-order segment near {names[0]} — renumbering")
        base = max(m.values()) + 1
        for i, n in enumerate(names):
            m[n] = base + i
        first = base
    _seq["map"] = m = {n: s for n, s in m.items() if s >= first}
    try:
        tmp = SEQ_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"map": m}, f)
        os.replace(tmp, SEQ_FILE)
    except Exception as e:
        log(f"media sequence save failed: {e}")
    return first

def splice_durs():
    """True durations {segment_name: dur} for spliced slots, from the splicer."""
    try:
        return json.load(open(DURS_FILE)).get("durs", {})
    except Exception:
        return {}

def tick():
    now = time.time()
    purge_old_state(now)

    # Single path: the delayed window is always archive segments; the
    # splicer rewrites the segment *files* inside the window, so adhan /
    # filler content simply plays through the continuous archive sequence.
    # No EXT-X-DISCONTINUITY: audio-only players (hls.js especially) are
    # prone to stalling on discontinuities; codec params are identical
    # across spliced/recorder segments, so a plain content change is safe.
    entries = delayed_entries(now - DELAY)
    if not entries:
        emit_header_only()   # nothing available yet: keep previous playlist
        return
    key = (entries[0][0], entries[-1][0])
    if key == _emitted_key.get("k"):   # unchanged window: don't rewrite
        return
    _emitted_key["k"] = key
    durs = splice_durs()
    idurs = index_durations()
    items = [(durs.get(n, idurs.get(n, f"{SEG}.000000")), f"/archive/{n}")
             for _, n in entries]
    emit(media_sequence([n for _, n in entries]), items)

def main():
    log(f"starting: delay={DELAY}s seg={SEG}s")
    threading.Thread(target=chunk_loop, daemon=True).start()
    try:
        import monitor
        monitor.start()
    except Exception as e:
        log(f"monitor API failed to start: {e}")
    while True:
        try:
            tick()
        except Exception as e:
            log(f"tick error: {e}")
        time.sleep(LOOP)

if __name__ == "__main__":
    main()
