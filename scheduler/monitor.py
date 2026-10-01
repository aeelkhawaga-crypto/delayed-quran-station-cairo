#!/usr/bin/env python3
"""Read-only monitoring API + static page for the delayed Quran stream.

Runs as a daemon thread inside the scheduler container (port 8001).
nginx proxies /monitor/ here and password-protects the whole prefix with
auth_basic (same style as the family-hub app). Everything is derived from
state that already exists: the archive directory, the recorder index, the
splicer state files, and the scheduler's own timetable functions.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import datetime
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import scheduler as sch

PORT = int(os.environ.get("MONITOR_PORT", "8001"))
UTC = datetime.timezone.utc
ARCHIVE = sch.ARCHIVE
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "monitor-static")
if not os.path.isdir(STATIC_DIR):            # running from / in the container
    STATIC_DIR = "/monitor-static"
LISTEN_MAX_MIN = 120
DOWNLOAD_MAX_S = 5 * 3600
_download_lock = threading.Semaphore(1)   # one export at a time (CPU/disk)
HEALED_MIN_LAG = 90   # a file written this long after its name time was synthesized


def log(msg):
    print(f"[monitor] {msg}", flush=True)


# ---------------- shared helpers ----------------

def archive_names():
    """[(ts, name)] of well-formed archive segments, sorted."""
    out = []
    for f in glob.glob(os.path.join(ARCHIVE, "*.ts")):
        b = os.path.basename(f)
        m = re.fullmatch(r"(\d{14})\.ts", b)
        if m:
            dt = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            out.append((int(dt.timestamp()), b))
    return sorted(out)


def index_durs_names():
    """({name: dur_str}, {name}) from the recorder index (its own files)."""
    durs, names = {}, set()
    try:
        lines = open(os.path.join(ARCHIVE, "index.m3u8")).read().splitlines()
    except Exception:
        return durs, names
    dur = None
    for line in lines:
        if line.startswith("#EXTINF:"):
            dur = line[len("#EXTINF:"):].rstrip().rstrip(",")
        elif line.startswith("#") or not line.strip():
            continue
        else:
            b = os.path.basename(line.strip())
            if re.fullmatch(r"\d{14}\.ts", b):
                names.add(b)
                if dur is not None:
                    durs[b] = dur
            dur = None
    return durs, names


def load_splicer_state():
    try:
        with open(os.path.join(sch.SCHED, ".splicer-state.json")) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def splice_durs():
    try:
        return json.load(open(sch.DURS_FILE)).get("durs", {})
    except Exception:
        return {}


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def file_age(path, now):
    try:
        return round(now - os.path.getmtime(path), 1)
    except OSError:
        return None


def healed_names(names, spliced):
    """Names the splicer synthesized to fill gaps: written long after their
    name time and not part of a splice. (The recorder index can't tell: it
    restarts empty with the recorder.)"""
    out = set()
    for t, n in names:
        if n in spliced:
            continue
        try:
            if os.path.getmtime(os.path.join(ARCHIVE, n)) - t > HEALED_MIN_LAG:
                out.add(n)
        except OSError:
            pass
    return out


def pool_files():
    """Adhan pool file lists + the round-robin heads (next file per pool)."""
    st = load_splicer_state()

    def listing(kind, folder):
        try:
            files = sorted(os.path.basename(f) for f in glob.glob(os.path.join(folder, "*"))
                           if os.path.splitext(f)[1].lower() in sch.AUDIO_EXT)
        except Exception:
            files = []
        sets = sorted(glob.glob(os.path.join(sch.STATIC, f"{kind}-*")))
        return files, sets

    out = {}
    for key, kind, folder, idxkey in (
            ("adhan", "adhan", sch.ADHANS, "adhan_idx"),
            ("fajr", "fajr-adhan", sch.FAJR_ADHANS, "fajr_adhan_idx")):
        files, sets = listing(kind, folder)
        n = len(sets)
        nxt = None
        if files and n:
            nxt = files[st.get(idxkey, 0) % n] if n == len(files) else f"({st.get(idxkey, 0) % n} of {n} chunksets)"
        out[key] = {"files": files, "chunksets": n, "next": nxt}
    return out


def event_rows(now, delay):
    """Upcoming/past splice activity: Irish adhan events + Cairo windows."""
    st = load_splicer_state()
    done = st.get("spliced", {}) if isinstance(st.get("spliced"), dict) else {}
    rows = []
    for start, prayer in sch.irish_prayer_events(now):
        if start < now - 6 * 3600 or start > now + 8 * 3600:
            continue
        key = f"irish-{int(start)}"
        status = "pending"
        if key in done:
            status = "spliced" if done[key] else "skipped"
        elif start + 60 < now:
            status = "missed"           # aired without the splicer acting on it
        rows.append({"kind": "dublin-adhan", "prayer": prayer, "wall": start,
                     "content": start - delay, "status": status})
    for w in sch.cairo_windows(now):
        if w["end"] < now - 6 * 3600 or w["start"] > now + 8 * 3600:
            continue
        key = f"cairo-{int(w['start'])}"
        status = "pending"
        if key in done:
            status = "spliced" if done[key] else "skipped"
        elif w["start"] + 60 < now:
            status = "missed"
        rows.append({"kind": "cairo-suppression", "prayer": w["name"],
                     "wall": w["start"], "wall_end": w["end"],
                     "content": w["start"] - delay,
                     "content_end": w["end"] - delay, "status": status})
    rows.sort(key=lambda r: r["wall"])
    return rows


def chunk_count(kind, idx):
    sets = sorted(glob.glob(os.path.join(sch.STATIC, f"{kind}-*")))
    if not sets:
        return 0
    entries, _ = sch.load_chunkset(sets[idx % len(sets)])
    return len(entries)


# ---------------- planned (not yet applied) splices ----------------
# The splicer overwrites archive slots only ~60 s before they air, so until
# then the files still hold the original Cairo audio. To let the monitor
# preview what WILL air, this mirrors splice.tick()'s plan (same slots,
# same round-robin order, same MIN_GAP skip rule) without touching files.

def _chunks(sdir):
    entries, _ = sch.load_chunkset(sdir)
    return [(d, os.path.join(sdir, u)) for d, u in entries]


def _gap(a0, a1, b0, b1):
    if a1 < b0:
        return b0 - a1
    if b1 < a0:
        return a0 - b1
    return 0


def _static_uri(path):
    return "/live-static/" + os.path.relpath(path, sch.STATIC)


def planned_splices(now, names):
    """{slot_name: (dur, uri)} for splices the splicer has not applied yet."""
    st = load_splicer_state()
    done = st.get("spliced", {}) if isinstance(st.get("spliced"), dict) else {}
    seg, delay, min_gap = sch.SEG, sch.DELAY, sch.MIN_GAP
    sets = {k: sorted(glob.glob(os.path.join(sch.STATIC, f"{k}-*")))
            for k in ("adhan", "fajr-adhan", "filler")}
    starters = {os.path.basename(d)[len("starter-"):]: d
                for d in glob.glob(os.path.join(sch.STATIC, "starter-*"))}
    idx = {"adhan": st.get("adhan_idx", 0), "fajr-adhan": st.get("fajr_adhan_idx", 0),
           "filler": st.get("filler_idx", 0)}
    events = sch.irish_prayer_events(now)
    wins = sch.cairo_windows(now)

    def pool(prayer):
        return "fajr-adhan" if prayer == "fajr" and sets["fajr-adhan"] else "adhan"

    def span(prayer):
        k = pool(prayer)
        if not sets[k]:
            return 0
        sp = len(_chunks(starters[prayer])) * seg if prayer in starters else 0
        return sp + len(_chunks(sets[k][idx[k] % len(sets[k])])) * seg

    plan = {}
    jobs = [(s, "irish", p) for s, p in events] + [(w["start"], "cairo", w) for w in wins]
    for start, kind, x in sorted(jobs, key=lambda j: j[0]):
        if kind == "irish":
            key = f"irish-{int(start)}"
            k = pool(x)
            if key in done or not sets[k]:
                continue
            achunks = _chunks(sets[k][idx[k] % len(sets[k])])
            schunks = _chunks(starters[x]) if x in starters else []
            spre, total = len(schunks) * seg, len(achunks) * seg
            if start + total < now:
                continue
            if any(_gap(start - spre, start + total, w["start"], w["end"]) <= min_gap
                   for w in wins):
                continue                      # splicer skips it: Cairo stays
            c = start - delay
            below = [t for t, _ in names if t <= c]
            if not below or c - below[-1] >= seg:
                continue
            anchor = below[-1]
            pre = [n for t, n in names if anchor - spre <= t < anchor]
            body = [n for t, n in names if anchor <= t < anchor + total]
            for n, (d, src) in list(zip(pre, schunks)) + list(zip(body, achunks)):
                plan[n] = (d, _static_uri(src))
            idx[k] += 1
        else:
            w = x
            key = f"cairo-{int(w['start'])}"
            if key in done or not sets["filler"] or w["end"] < now:
                continue
            if any(_gap(s, s + span(p), w["start"], w["end"]) <= min_gap for s, p in events):
                continue
            chain = []
            fs = sets["filler"]
            for j in range(len(fs)):
                chain.extend(_chunks(fs[(idx["filler"] + j) % len(fs)]))
            slots = [n for t, n in names
                     if w["start"] - delay - seg < t < w["end"] - delay]
            for n, (d, src) in zip(slots, chain):
                plan[n] = (d, _static_uri(src))
            idx["filler"] += 1
    return plan


# ---------------- endpoints ----------------

def api_overview(now):
    names = archive_names()
    idurs, index_names = index_durs_names()
    live_path = os.path.join(sch.LIVE, "delayed.m3u8")
    pl_seq, pl_n, pl_age = None, 0, None
    try:
        txt = open(live_path).read()
        m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", txt)
        pl_seq = int(m.group(1)) if m else None
        pl_n = txt.count("#EXTINF:")
        pl_age = round(now - os.path.getmtime(live_path), 1)
    except Exception:
        pass
    st = load_splicer_state()
    runs = st.get("runs", [])
    last_splice = None
    for v in (st.get("spliced") or {}).values():
        if isinstance(v, list) and len(v) == 2:
            cand = v[1]
            last_splice = cand if last_splice is None else max(last_splice, cand)
    wd = None
    try:
        wd = json.load(open(os.path.join(sch.SCHED, ".watchdog.json")))
    except Exception:
        pass
    healed = len(healed_names(names, set(splice_durs())))
    try:
        du = shutil.disk_usage(ARCHIVE)
        disk = {"free_gb": round(du.free / 1e9, 1), "used_pct": round(100 * du.used / du.total, 1)}
    except OSError:
        disk = None
    return {
        "now_utc": now, "delay": sch.DELAY, "seg": sch.SEG,
        "dublin": iso_local(now, sch.dublin_offset(datetime.datetime.fromtimestamp(now, UTC))),
        "cairo": iso_local(now, cairo_offset(datetime.datetime.fromtimestamp(now, UTC))),
        "recorder": {"newest_age_s": round(now - names[-1][0], 1) if names else None,
                     "segments": len(names),
                     "oldest": names[0][0] if names else None,
                     "newest": names[-1][0] if names else None},
        "playlist": {"media_seq": pl_seq, "segments": pl_n, "file_age_s": pl_age},
        "feeder": read_json(os.path.join(ARCHIVE, ".feeder.json")),
        "splicer_tick_age_s": file_age(os.path.join(sch.SCHED, ".splice-runs.json"), now),
        "disk": disk,
        "healed_segments": healed,
        "splice_last_end": last_splice,
        "runs_active": len(runs),
        "watchdog": wd,
        "pools": pool_files(),
        "events": event_rows(now, sch.DELAY),
    }


def iso_local(epoch, offset_s):
    return (datetime.datetime.fromtimestamp(epoch, UTC)
            + datetime.timedelta(seconds=offset_s)).strftime("%Y-%m-%d %H:%M:%S")


def cairo_offset(dt_utc):
    """Africa/Cairo: UTC+3 from the last Friday of April 00:00 local to the
    end of the last Thursday of October (24:00 local), else UTC+2."""
    def last_weekday(y, m, wd):
        d = datetime.date(y, m, 30 if m == 4 else 31)
        while d.weekday() != wd:
            d -= datetime.timedelta(days=1)
        return d
    y = dt_utc.year
    fri = last_weekday(y, 4, 4)
    thu = last_weekday(y, 10, 3)
    start = datetime.datetime(fri.year, fri.month, fri.day, tzinfo=UTC) - datetime.timedelta(hours=2)
    end = datetime.datetime(thu.year, thu.month, thu.day, tzinfo=UTC) + datetime.timedelta(hours=21)
    return 3 * 3600 if start <= dt_utc < end else 2 * 3600


_timeline_cache = {"t": 0.0, "data": None}


def api_timeline(now, hours):
    if now - _timeline_cache["t"] < 2.0 and _timeline_cache["data"]:
        return _timeline_cache["data"]
    delay = sch.DELAY
    lo = now - hours * 3600
    names = [(t, n) for t, n in archive_names() if t >= lo]
    st = load_splicer_state()
    done = st.get("spliced", {}) if isinstance(st.get("spliced"), dict) else {}
    adhan_idx = st.get("adhan_idx", 0)
    fajr_idx = st.get("fajr_adhan_idx", 0)

    # classification ranges in content time
    marks = []   # (t0, t1, kind, label, status)
    for start, prayer in sch.irish_prayer_events(now):
        cs = start - delay
        if cs + 3600 < lo or cs > now + 600:
            continue
        is_fajr = prayer == "fajr"
        n_adhan = chunk_count("fajr-adhan" if is_fajr else "adhan",
                              fajr_idx if is_fajr else adhan_idx)
        span = n_adhan * sch.SEG
        key = f"irish-{int(start)}"
        status = "pending"
        if key in done:
            status = "spliced" if done[key] else "skipped"
        run = done.get(key)
        if isinstance(run, list) and len(run) == 2:   # actual spliced slots
            cs, span = run[0], run[1] + sch.SEG - run[0]
        marks.append((cs, cs + span,
                      "fajr-adhan" if is_fajr else "dublin-adhan",
                      f"{prayer} adhan", status))
    for w in sch.cairo_windows(now):
        c0, c1 = w["start"] - delay, w["end"] - delay
        if c1 < lo or c0 > now + 600:
            continue
        key = f"cairo-{int(w['start'])}"
        status = "pending"
        if key in done:
            status = "spliced" if done[key] else "skipped"
        run = done.get(key)
        if isinstance(run, list) and len(run) == 2:
            c0, c1 = run[0], run[1] + sch.SEG
        marks.append((c0, c1, "cairo-suppression", f"{w['name']} window", status))
    marks.sort()

    spliced = set(splice_durs())
    healed = healed_names(names, spliced)
    segs = []
    for t, n in names:
        kind = "quran"
        for a, b, k, lab, status in marks:
            if a <= t < b and status == "spliced" and n in spliced:
                kind = k
                break
        if kind == "quran" and n in spliced:
            kind = "starter"            # prayer starter: slots just before an adhan
        elif kind == "quran" and n in healed:
            kind = "healed"
        segs.append((t, kind))
    data = {"now_utc": now, "delay": delay, "seg": sch.SEG, "airing_ts": now - delay,
            "newest_ts": names[-1][0] + sch.SEG if names else now,
            "segments": segs, "marks": [
                {"t0": a, "t1": b, "kind": k, "label": l, "status": s}
                for a, b, k, l, s in marks]}
    _timeline_cache.update(t=now, data=data)
    return data


def range_items(now, start, end):
    """[(dur_str, uri, path)] covering content time [start, end]: the archive
    segments, with not-yet-applied splices substituted (what will air)."""
    names = archive_names()
    if not names:
        return []
    later = [i for i, (t, _) in enumerate(names) if t >= start]
    first_i = later[0] if later else len(names) - 1
    picked = [(t, n) for t, n in names[first_i:] if t < end] or [names[first_i]]
    idurs, _ = index_durs_names()
    sdurs = splice_durs()
    plan = planned_splices(now, names)
    out = []
    for t, n in picked:
        if n in plan:
            d, uri = plan[n]
            path = os.path.join(sch.STATIC, uri[len("/live-static/"):])
        else:
            d = sdurs.get(n) or idurs.get(n) or f"{sch.SEG}.000000"
            uri, path = f"/archive/{n}", os.path.join(ARCHIVE, n)
        out.append((d, uri, path))
    return out


def api_listen(now, start, minutes):
    minutes = max(1, min(LISTEN_MAX_MIN, minutes or 30))
    items = range_items(now, start, start + minutes * 60)
    if not items:
        return None
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{sch.SEG}",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
    prev_planned = None
    for d, uri, _ in items:
        planned = uri.startswith("/live-static/")
        if prev_planned is not None and planned != prev_planned:
            lines.append("#EXT-X-DISCONTINUITY")   # preview chunks carry own PTS
        prev_planned = planned
        lines.append(f"#EXTINF:{d},")
        lines.append(uri)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def api_download(now, start, end):
    """Export content [start, end] as one .m4a (AAC stream copy, no
    re-encode). Returns (path, filename) of a temp file the caller deletes."""
    end = min(end, start + DOWNLOAD_MAX_S)
    items = range_items(now, start, end)
    if not items:
        return None
    tmpdir = tempfile.mkdtemp(prefix="quran-dl-")
    lst = os.path.join(tmpdir, "list.txt")
    with open(lst, "w") as f:
        for _, _, path in items:
            f.write("file '%s'\n" % path.replace("'", "'\\''"))
    out = os.path.join(tmpdir, "out.m4a")
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error",
                        "-f", "concat", "-safe", "0", "-i", lst,
                        "-vn", "-c:a", "copy", "-bsf:a", "aac_adtstoasc",
                        "-movflags", "+faststart", out],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0 or not os.path.exists(out):
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise RuntimeError("export failed: " + r.stderr.strip()[:200])
    off = sch.dublin_offset(datetime.datetime.fromtimestamp(start + sch.DELAY, UTC))
    a = datetime.datetime.fromtimestamp(start + sch.DELAY + off, UTC)
    b = datetime.datetime.fromtimestamp(end + sch.DELAY + off, UTC)
    name = f"quran-radio-{a:%Y-%m-%d_%H%M}-{b:%H%M}-dublin.m4a"
    return out, name


# ---------------- HTTP ----------------

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
        ".css": "text/css", ".map": "application/json"}


class Handler(BaseHTTPRequestHandler):
    server_version = "quran-monitor"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, ctype="application/json; charset=utf-8", status=200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        now = time.time()
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path == "/monitor.js":
                return self._static("monitor.js")
            if path == "/hls.min.js":
                return self._static("hls.min.js")
            if path == "/api/overview":
                return self._send(json.dumps(api_overview(now)))
            if path == "/api/timeline":
                hours = float(q.get("hours", ["4.5"])[0])
                hours = max(0.5, min(8.0, hours))
                return self._send(json.dumps(api_timeline(now, hours)))
            if path == "/api/listen.m3u8":
                start = float(q.get("start", [str(now - sch.DELAY)])[0])
                minutes = int(float(q.get("minutes", ["30"])[0]))
                pl = api_listen(now, start, minutes)
                if pl is None:
                    return self._send('{"error": "archive empty"}', status=404)
                return self._send(pl, ctype="application/vnd.apple.mpegurl")
            if path == "/api/download":
                start = float(q["start"][0])
                end = float(q["end"][0])
                if end <= start:
                    return self._send('{"error": "empty range"}', status=400)
                if not _download_lock.acquire(blocking=False):
                    return self._send('{"error": "another export is running"}', status=429)
                try:
                    res = api_download(now, start, end)
                finally:
                    _download_lock.release()
                if res is None:
                    return self._send('{"error": "archive empty"}', status=404)
                return self._send_file(*res)
            self._send('{"error": "not found"}', status=404)
        except Exception as e:
            self._send(json.dumps({"error": str(e)}), status=500)

    def _send_file(self, path, filename):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mp4")
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 256 * 1024)
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def _static(self, name):
        safe = os.path.basename(name)
        path = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(path):
            return self._send('{"error": "missing %s"}' % safe, status=404)
        ext = os.path.splitext(safe)[1]
        with open(path, "rb") as f:
            self._send(f.read(), ctype=MIME.get(ext, "application/octet-stream"))


def start():
    def run():
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
            log(f"listening on :{PORT}")
            srv.serve_forever()
        except Exception as e:
            log(f"failed to start: {e}")

    t = threading.Thread(target=run, daemon=True)
    t.start()


if __name__ == "__main__":
    start()
    while True:
        time.sleep(3600)
