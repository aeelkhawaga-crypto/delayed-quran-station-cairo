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
import os, re, time, json, glob, shutil, subprocess, urllib.request, datetime

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
FILLERS = os.environ.get("FILLER_DIR", "/fillers")
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
    """From the user's manual timetable file."""
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
        m = re.fullmatch(r"(?:(\S+)\s+)?(\d{1,2}):(\d{2})", line)
        if not m:
            continue
        daypart, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
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
            events.append(local.timestamp() - dublin_offset(local))
    return events

def _irish_events_json(now):
    """From the Islamic Foundation of Ireland yearly timetable (MM-DD keys,
    Europe/Dublin local times, repeating annually)."""
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
            events.append(local.timestamp() - off)
    return events

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

# ---------------- chunksets (adhan / filler) ----------------

def folder_sig(folder):
    files = sorted(f for f in glob.glob(os.path.join(folder, "*"))
                   if os.path.splitext(f)[1].lower() in AUDIO_EXT)
    return files, "|".join(f"{os.path.basename(f)}:{os.path.getmtime(f)}" for f in files)

def ensure_chunksets():
    """(Re)chunk adhan/filler audio into static HLS segment sets on change."""
    os.makedirs(STATIC, exist_ok=True)
    sets = {}
    for kind, folder in (("adhan", ADHANS), ("filler", FILLERS)):
        try:
            files, sig = folder_sig(folder)
        except Exception:
            files, sig = [], ""
        tag = os.path.join(STATIC, f".{kind}.sig")
        try:
            old = open(tag).read()
        except Exception:
            old = None
        if sig == old and glob.glob(os.path.join(STATIC, f"{kind}-*")):
            sets[kind] = sorted(glob.glob(os.path.join(STATIC, f"{kind}-*")))
            continue
        for d in glob.glob(os.path.join(STATIC, f"{kind}-*")):
            shutil.rmtree(d, ignore_errors=True)
        made = []
        for i, f in enumerate(files):
            sdir = os.path.join(STATIC, f"{kind}-{i}")
            os.makedirs(sdir, exist_ok=True)
            r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", f,
                                "-vn", "-c:a", "aac", "-b:a", "96k",
                                "-f", "hls", "-hls_time", str(SEG),
                                "-hls_list_size", "0",
                                "-hls_segment_filename",
                                os.path.join(sdir, "seg_%04d.ts"),
                                os.path.join(sdir, "playlist.m3u8")],
                               capture_output=True, text=True)
            if r.returncode != 0:
                log(f"chunking failed for {f}: {r.stderr.strip()[:200]}")
                shutil.rmtree(sdir, ignore_errors=True)
                continue
            made.append(sdir)
        with open(tag, "w") as tf:
            tf.write(sig)
        sets[kind] = made
        log(f"{kind}: {len(made)} file(s) chunked")
    return sets

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

def delayed_entries(target_ts):
    """Archive segments whose filename timestamps fall near target_ts."""
    target = datetime.datetime.fromtimestamp(target_ts, UTC)
    high, low = fmt(target), fmt(target - datetime.timedelta(seconds=3 * SEG))
    try:
        lines = open(os.path.join(ARCHIVE, "index.m3u8")).read().splitlines()
    except Exception:
        return None
    if not lines or not lines[0].startswith("#EXTM3U"):
        return None
    entries, dur = [], None
    for line in lines:
        if line.startswith("#EXTINF:"):
            dur = line[len("#EXTINF:"):].rstrip().rstrip(",")
        elif line.startswith("#") or not line.strip():
            continue
        else:
            b = os.path.basename(line.strip())
            m = re.fullmatch(r"(\d{14})\.ts", b)
            if m and low <= m.group(1) <= high:
                entries.append((dur or str(SEG), f"/archive/{b}"))
            dur = None
    return entries

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

def tick():
    now = time.time()
    purge_old_state(now)
    ensure_chunksets()  # chunksets are the splicer's source material

    # Single path: the delayed window is always archive segments; the
    # splicer rewrites the segment *files* inside the window, so adhan /
    # filler content simply plays through the continuous archive sequence.
    entries = delayed_entries(now - DELAY)
    if not entries:
        emit_header_only()
        return
    runs = splice_runs()
    if runs:
        items, prev = [], None
        for e in entries:
            ts = int(entries_ts(e))
            sp = any(a <= ts <= b for a, b in runs)
            if prev is not None and sp != prev:
                items.append((None, None))
            items.append(e)
            prev = sp
        emit(int(entries_ts(entries[0])), items)
    else:
        emit(int(entries_ts(entries[0])), entries)

def main():
    log(f"starting: delay={DELAY}s seg={SEG}s")
    while True:
        try:
            tick()
        except Exception as e:
            log(f"tick error: {e}")
        time.sleep(LOOP)

if __name__ == "__main__":
    main()
