#!/usr/bin/env python3
"""Event-aware delayed playlist builder.

Replaces the old timeshift shell script. Rebuilds /live/delayed.m3u8 every
few seconds. Normal mode serves archive segments from DELAY_SECONDS ago.
Two event types override it:

- Irish Adhan: at each time from /schedule/irish-times.txt, play one file
  from /adhans (round-robin), then resume the delayed stream.
- Cairo Adhan suppression: when the delayed stream would air the Adhan that
  the Cairo station broadcasts at prayer times (AlAdhan API, method=5),
  serve filler files from /fillers for the whole window instead; if there
  are no fillers, skip the Adhan content and jump ahead.

Sequence numbers are derived from wall-clock time in "content space"
(wall time minus DELAY) so they are stable per URI and monotonic.
"""
import os, re, time, json, glob, shutil, subprocess, urllib.request, datetime

SEG = int(os.environ.get("SEGMENT_SECONDS", "10"))
DELAY = int(os.environ.get("DELAY_SECONDS", "7200"))
METHOD = os.environ.get("PRAYER_METHOD", "5")
LOOP = float(os.environ.get("LOOP_SECONDS", "2.5"))
WIN_PRE = int(os.environ.get("WINDOW_PRE_SECONDS", "90"))
WIN_POST = int(os.environ.get("WINDOW_POST_SECONDS", "360"))

ARCHIVE = os.environ.get("ARCHIVE_DIR", "/archive")
LIVE = os.environ.get("LIVE_DIR", "/live")
STATIC = os.environ.get("STATIC_DIR", "/live-static")
ADHANS = os.environ.get("ADHAN_DIR", "/adhans")
FILLERS = os.environ.get("FILLER_DIR", "/fillers")
SCHED = os.environ.get("SCHEDULE_DIR", "/schedule")
IRISH_FILE = os.path.join(SCHED, "irish-times.txt")
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
        if k.startswith("irish-"):
            try:
                ts = float(k.split("-", 1)[1])
                if now - ts > 2 * 86400:
                    del state[k]
            except Exception:
                pass

# ---------------- irish timetable ----------------

def irish_events(now):
    """Event start epochs (UTC) near now, from the user's timetable file."""
    events = []
    try:
        lines = open(IRISH_FILE).read().splitlines()
    except Exception:
        return events
    day = datetime.date.fromtimestamp(now, UTC)
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
    return sorted(set(e for e in events if abs(e - now) < 3 * 86400))

# ---------------- cairo timings ----------------

def cairo_prayers(day):
    """UTC epochs for the five prayers on the given UTC date (cached)."""
    key = day.isoformat()
    try:
        c = json.load(open(CAIRO_CACHE))
        if c.get("date") == key:
            return c["utc"]
    except Exception:
        pass
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
        tmp = CAIRO_CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"date": key, "utc": utc}, f)
        os.replace(tmp, CAIRO_CACHE)
        log(f"Cairo timings cached for {key}")
        return utc
    except Exception as e:
        log(f"Cairo timings fetch failed for {key}: {e}")
        return None

def cairo_windows(now):
    """Suppression windows in served wall-time (UTC epochs)."""
    day = datetime.date.fromtimestamp(now, UTC)
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

def emit(seq, entries):
    os.makedirs(LIVE, exist_ok=True)
    tmp = os.path.join(LIVE, ".delayed.m3u8.tmp")
    with open(tmp, "w") as f:
        f.write("#EXTM3U\n#EXT-X-VERSION:3\n")
        f.write(f"#EXT-X-TARGETDURATION:{SEG}\n#EXT-X-MEDIA-SEQUENCE:{seq}\n")
        for dur, uri in entries:
            f.write(f"#EXTINF:{dur},\n{uri}\n")
    os.replace(tmp, os.path.join(LIVE, "delayed.m3u8"))

def emit_header_only():
    if not os.path.exists(os.path.join(LIVE, "delayed.m3u8")):
        emit(0, [])

# ---------------- main loop ----------------

def tick():
    now = time.time()
    purge_old_state(now)
    sets = ensure_chunksets()
    adhan_sets = sets.get("adhan", [])
    filler_sets = sets.get("filler", [])

    # --- 1) Irish Adhan event? ---
    for start in irish_events(now):
        if not (start <= now) or not adhan_sets:
            continue
        eid = f"irish-{int(start)}"
        if eid not in state:
            # first sighting: assign rotation slot, then bump for next time
            state[eid] = state.get("adhan_idx", 0)
            state["adhan_idx"] = (state.get("adhan_idx", 0) + 1) % len(adhan_sets)
            save_state()
        idx = state[eid] % len(adhan_sets)
        entries, total = load_chunkset(adhan_sets[idx])
        if entries and now < start + total:
            name = os.path.basename(adhan_sets[idx])
            emit(int(start) - DELAY,
                 [(d, f"/live-static/{name}/{u}") for d, u in entries])
            return

    # --- 2) Cairo Adhan suppression window? ---
    for w in cairo_windows(now):
        if not (w["start"] <= now <= w["end"]):
            continue
        elapsed = now - w["start"]
        if filler_sets:
            # flat cycle plan across all filler sets
            plan, total = [], 0.0
            for sdir in filler_sets:
                name = os.path.basename(sdir)
                for d, u in load_chunkset(sdir)[0]:
                    dur = num(d, SEG)
                    plan.append((total, dur, f"/live-static/{name}/{u}"))
                    total += dur
            if plan:
                pos = elapsed % total
                picked = [p for p in plan if p[0] + p[1] > pos][:6] or plan[:1]
                seq = int(w["start"]) - DELAY + int(picked[0][0])
                emit(seq, [(f"{p[1]:.6f}", p[2]) for p in picked])
                return
        else:
            # no fillers: skip the Adhan content entirely and jump ahead
            eff_delay = now - (w["content_end"] + elapsed)
            entries = delayed_entries(now - eff_delay)
            if entries:
                emit(int(entries_ts(entries[0])), entries)
                return

    # --- 3) normal delayed mode ---
    entries = delayed_entries(now - DELAY)
    if entries:
        emit(int(entries_ts(entries[0])), entries)
    else:
        emit_header_only()

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
