#!/usr/bin/env python3
"""Content splicer for the delayed Quran stream.

Runs as a separate job alongside the scheduler. Instead of switching the
live playlist between archive/adhan/filler URIs (which forced players to
re-sync and could mix buffered chunks), this job OVERWRITES the archive
segment files themselves, inside the delayed window: about a minute
before content airs, the archive slots it will occupy are replaced with
the adhan/filler/starter chunks, byte for byte, under the same filenames.
The playlist stays one continuous archive sequence; players simply play
through the filenames and hear the inserted content where it belongs.

- Irish Adhan: at each event one adhan file (round-robin) occupies the
  slots from the prayer's grid anchor; the stream continues afterwards
  from where the adhan ended (the skipped gap is an accepted tradeoff).
- Prayer starters: a short starter (adhan-prefixes/<Prayer>.mp3) occupies
  the slots ending exactly at the adhan's anchor, so the adhan itself
  stays on time.
- Cairo Adhan suppression: during each Cairo window the slots are filled
  with whole filler files, one at a time, round-robin per window. With no
  fillers the Cairo adhan simply stays audible.
- If an Irish adhan and a Cairo window overlap or are within MIN_GAP
  seconds, neither is spliced (normal delayed stream plays).

A slot is fetched by players only during the ~30s before it airs, so the
splice happens LEAD seconds before the event; that way no player holds
the old bytes for a spliced slot. Chunksets are pre-retimed to whole
10s slots by the scheduler, so every replaced slot carries exactly one
full segment; the true durations are handed to the scheduler via
.splice-durs.json so the playlist timeline stays exact.
"""
import os, re, sys, json, glob, time, datetime, subprocess, shutil

sys.path.insert(0, "/")
import scheduler as sch

SEG = sch.SEG
DELAY = sch.DELAY
MIN_GAP = sch.MIN_GAP
ARCHIVE = sch.ARCHIVE
STATIC = sch.STATIC
UTC = sch.UTC

LEAD = 60.0        # splice this long before the first slot starts airing
EXPOSURE = 3 * SEG  # a slot keeps being fetched this long after it starts
LOOP = 5.0

STATE_FILE = os.path.join(sch.SCHED, ".splicer-state.json")
RUNS_FILE = os.path.join(sch.SCHED, ".splice-runs.json")
DURS_FILE = os.path.join(sch.SCHED, ".splice-durs.json")


def log(msg):
    print(f"[splicer] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


state = load_json(STATE_FILE, {})


def archive_slots():
    """[(content_ts_int, filename)] sorted, from the recorder index."""
    out = []
    try:
        lines = open(os.path.join(ARCHIVE, "index.m3u8")).read().splitlines()
    except Exception:
        return out
    for line in lines:
        m = re.fullmatch(r"(\d{14})\.ts", os.path.basename(line.strip()))
        if m:
            dt = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            out.append((int(dt.timestamp()), m.group(0)))
    return sorted(out)


def chunk_segments(sdir):
    """[(dur, abspath)] of a chunkset's segments, in order."""
    entries, _ = sch.load_chunkset(sdir)
    return [(d, os.path.join(sdir, u)) for d, u in entries]


def chunkset_dirs(kind):
    return sorted(glob.glob(os.path.join(STATIC, f"{kind}-*")))


def starter_dirs():
    return {os.path.basename(d)[len("starter-"):]: d
            for d in glob.glob(os.path.join(STATIC, "starter-*"))}


FRAME_TICKS = int(1024 * 90000 / 44100)  # one AAC frame in 90kHz ticks

def _pts_list(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "packet=pts", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=30)
        vals = []
        for tok in r.stdout.split():
            try:
                vals.append(int(tok))
            except ValueError:
                pass
        return vals
    except Exception:
        return []

def _write_aligned(src, dst, start_pts):
    """Remux src so its first audio PTS == start_pts (90kHz ticks), keeping
    the delayed stream's timestamp continuity across spliced content.
    Players stall on mid-stream timestamp resets, so this matters more
    than the audio payload itself."""
    vals = _pts_list(src)
    if not vals:
        shutil.copyfile(src, dst)
        return
    off = (start_pts - vals[0]) / 90000.0
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-c", "copy",
                        "-muxdelay", "0", "-muxpreload", "0",
                        "-output_ts_offset", f"{off:.6f}", dst],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        log(f"pts-align remux failed for {src}: {r.stderr.strip()[:150]} — plain copy")
        shutil.copyfile(src, dst)

def replace_slots(slots, chunks, now, prev_path):
    """Overwrite the given archive slots with chunk segments 1:1 in order,
    skipping pairs already fully past. slots: [(ts, name)];
    chunks: [(dur, path)]. prev_path: archive segment airing right before
    slots[0]; spliced chunks continue its timestamps. Returns (run, durs)
    or (None, {})."""
    dropped = 0
    while dropped < len(slots) and slots[dropped][0] + DELAY + EXPOSURE < now:
        dropped += 1
    slots, chunks = slots[dropped:], chunks[dropped:]
    if not slots:
        return None, {}
    prev_vals = _pts_list(prev_path) if prev_path else []
    base = (max(prev_vals) + FRAME_TICKS) if prev_vals else None
    durs, n = {}, 0
    for (ts, name), (dur, src) in zip(slots, chunks):
        tmp = os.path.join(ARCHIVE, "." + name + ".tmp")
        try:
            if base is not None:
                _write_aligned(src, tmp, base + int(n * SEG * 90000))
            else:
                shutil.copyfile(src, tmp)
        except Exception as e:
            log(f"chunk unreadable {src}: {e} — will retry")
            if os.path.exists(tmp):
                os.remove(tmp)
            return None, {}
        os.replace(tmp, os.path.join(ARCHIVE, name))
        durs[name] = dur
        n += 1
    if not n:
        return None, {}
    return [slots[0][0], slots[n - 1][0]], durs


def gap(a0, a1, b0, b1):
    if a1 < b0:
        return b0 - a1
    if b1 < a0:
        return a0 - b1
    return 0


def flatten(sets, start_idx):
    """Chain filler chunksets in order, starting at start_idx (round-robin)."""
    out = []
    for k in range(len(sets)):
        out.extend(chunk_segments(sets[(start_idx + k) % len(sets)]))
    return out


def tick():
    now = time.time()
    adhan_sets = chunkset_dirs("adhan")
    filler_sets = chunkset_dirs("filler")
    starters = starter_dirs()
    events = sch.irish_prayer_events(now)
    wins = sch.cairo_windows(now)
    runs = state.setdefault("runs", [])
    done = state.setdefault("spliced", {})
    durs_all = state.setdefault("durs", {})
    changed = False

    slots = archive_slots()

    def grid_anchor(c):
        """Archive grid point at or just before content time c."""
        below = [ts for ts, _ in slots if ts <= c]
        if below and c - below[-1] < SEG:
            return below[-1]
        return None

    def prev_slot_path(ts):
        """Archive segment file airing right before grid time ts."""
        earlier = [(t, n) for t, n in slots if t < ts]
        return os.path.join(ARCHIVE, earlier[-1][1]) if earlier else None

    def event_span(prayer):
        if not adhan_sets:
            return 0.0
        i = state.get("adhan_idx", 0) % len(adhan_sets)
        sp = len(chunk_segments(starters[prayer])) * SEG if prayer in starters else 0
        return sp + len(chunk_segments(adhan_sets[i])) * SEG

    # --- Irish Adhan events (with prayer starters) ---
    for start, prayer in events:
        if start > now + LEAD:
            continue
        key = f"irish-{int(start)}"
        if key in done:
            continue
        if not adhan_sets or not slots:
            continue
        idx = state.get("adhan_idx", 0) % len(adhan_sets)
        achunks = chunk_segments(adhan_sets[idx])
        if not achunks:
            continue
        schunks = chunk_segments(starters[prayer]) if prayer in starters else []
        spre, total = len(schunks) * SEG, len(achunks) * SEG
        if start + total + 30 < now:
            done[key] = None
            changed = True
            continue
        if any(gap(start - spre, start + total, w["start"], w["end"]) <= MIN_GAP for w in wins):
            log(f"irish@{int(start)} within {MIN_GAP}s of a Cairo window — not splicing")
            done[key] = None
            changed = True
            continue
        anchor = grid_anchor(start - DELAY)
        if anchor is None:
            continue
        adhan_slots = [(ts, n) for ts, n in slots if anchor <= ts < anchor + total]
        if not adhan_slots:
            continue
        if schunks:
            s_slots = [(ts, n) for ts, n in slots if anchor - spre <= ts < anchor]
            if s_slots:
                run, d = replace_slots(s_slots, schunks, now,
                                       prev_slot_path(s_slots[0][0]))
                if run is None:
                    continue
                runs.append(run)
                durs_all.update(d)
                log(f"irish@{int(start)}: starter-{prayer} spliced into slots "
                    f"{run[0]}..{run[1]}")
        run, d = replace_slots(adhan_slots, achunks, now, prev_slot_path(anchor))
        if run is None:
            continue
        runs.append(run)
        durs_all.update(d)
        done[key] = run
        state["adhan_idx"] = (idx + 1) % len(adhan_sets)
        changed = True
        log(f"irish@{int(start)}: adhan-{idx} spliced into slots {run[0]}..{run[1]}")

    # --- Cairo Adhan suppression windows ---
    for w in wins:
        if w["start"] > now + LEAD or w["end"] + 300 < now:
            continue
        key = f"cairo-{int(w['start'])}"
        if key in done:
            continue
        if not filler_sets or not slots:
            continue
        idx = state.get("filler_idx", 0) % len(filler_sets)
        chain = flatten(filler_sets, idx)
        if any(gap(s, s + event_span(p), w["start"], w["end"]) <= MIN_GAP
               for s, p in events):
            log(f"cairo@{int(w['start'])} within {MIN_GAP}s of an Irish adhan — not splicing")
            done[key] = None
            changed = True
            continue
        c_slots = [(ts, n) for ts, n in slots
                   if w["start"] - DELAY - SEG < ts < w["end"] - DELAY]
        if not c_slots:
            continue
        run, d = replace_slots(c_slots, chain, now, prev_slot_path(c_slots[0][0]))
        if run is None:
            continue
        runs.append(run)
        durs_all.update(d)
        done[key] = run
        state["filler_idx"] = (idx + 1) % len(filler_sets)
        changed = True
        log(f"cairo@{int(w['start'])}: filler-{idx}+ spliced into slots {run[0]}..{run[1]}")

    # prune
    runs[:] = [r for r in runs if r[1] + DELAY + 3600 > now]
    durs_all = state["durs"] = {
        k: v for k, v in durs_all.items()
        if any(a <= int(datetime.datetime.strptime(k.split(".")[0], "%Y%m%d%H%M%S")
                        .replace(tzinfo=UTC).timestamp()) <= b
               for a, b in runs)}
    for k in list(done):
        try:
            if float(k.split("-", 1)[1]) < now - 2 * 86400:
                del done[k]
                changed = True
        except Exception:
            pass

    if changed:
        save_json(STATE_FILE, state)
    save_json(RUNS_FILE, {"runs": runs})
    save_json(DURS_FILE, {"durs": durs_all})


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
