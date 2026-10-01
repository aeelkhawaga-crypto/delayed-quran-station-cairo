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
  Fajr uses its own pool (adhans-fajr/ -> fajr-adhan-* chunksets) with a
  separate round-robin when that folder has files; otherwise the shared
  pool is used.
- Prayer starters: a short starter (adhan-prefixes/<Prayer>.mp3) occupies
  the slots ending exactly at the adhan's anchor, so the adhan itself
  stays on time.
- Cairo Adhan suppression: during each Cairo window the slots are filled
  with whole filler files, one at a time, round-robin per window. With no
  fillers the Cairo adhan simply stays audible.
- If an Irish adhan and a Cairo window overlap or are within MIN_GAP
  seconds, neither is spliced (normal delayed stream plays).
- Archive gap healing: when the upstream stalls or the recorder restarts,
  missing segment slots (and the stall tail) are synthesized from filler
  chunksets — or silence if no fillers exist — PTS-chained to neighbours,
  so the delayed playlist never runs dry (see heal_archive_gaps).

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
    """[(content_ts_int, filename)] sorted, from the archive directory.

    Not from the recorder's index.m3u8: the index restarts empty whenever
    the recorder restarts, which hid the whole delayed window from the
    splicer for DELAY seconds afterwards (events silently skipped)."""
    return dir_slots()


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
        # ffprobe csv output can carry trailing commas on some lines; a strict
        # int() per token would silently drop those packets (first and/or last),
        # which corrupts both the alignment base and the offset math.
        for tok in r.stdout.replace(",", " ").split():
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
    than the audio payload itself.

    -output_ts_offset does not shift by the given value on all ffmpeg
    builds (some rebase the output to zero first), so the offset is applied
    and the result verified, correcting until the first PTS lands on
    start_pts within half an AAC frame."""
    vals = _pts_list(src)
    if not vals:
        shutil.copyfile(src, dst)
        return
    off = (start_pts - vals[0]) / 90000.0
    for _ in range(3):
        r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", src, "-c", "copy",
                            "-muxdelay", "0", "-muxpreload", "0",
                            "-output_ts_offset", f"{off:.6f}", "-f", "mpegts", dst],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            break
        out = _pts_list(dst)
        if not out:
            break
        residual = start_pts - out[0]
        if abs(residual) <= FRAME_TICKS // 2:
            return
        off += residual / 90000.0
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


# ---------------- archive gap healing ----------------
# The recorder feeds real-time silence through upstream outages, so holes
# in the archive now only appear when the recorder itself restarts or dies.
# The delayed playlist would freeze on such a hole, so this healer
# synthesizes filler segments there (filler audio if available, silence
# otherwise). Rules that keep it from fighting the rest of the system:
# - only slots that have not aired yet (newer than now - DELAY + LEAD) are
#   filled; older holes are history, and refilling them is what kept
#   resurrecting deleted segments in a loop;
# - a fill never overlaps the next real segment (g + SEG <= next name);
# - holes shorter than HEAL_MIN_GAP (30 s) are skipped, not filled;
# - one hole per tick, PTS-chained from the segment right before it;
# - names are final once HEAL_MIN_AGE old, so only slots that old are filled.

HEAL_MIN_AGE = 120.0
# Holes shorter than this are left alone: the delayed stream just skips a
# few seconds (absorbed by the players' ~30 s buffer), which sounds better
# than splicing in 10 s of a different reciter.
HEAL_MIN_GAP = float(os.environ.get("HEAL_MIN_GAP_SECONDS", "30"))
FILL_MAX = 12   # max slots healed per tick (bounds CPU after a long outage)
KEEP_SECONDS = int(float(os.environ.get("ARCHIVE_HOURS", "4")) * 3600) + 1800
PRUNE_EVERY = 300.0


def dir_slots():
    """[(ts, name)] of well-formed segments in the archive dir, sorted."""
    out = []
    for f in glob.glob(os.path.join(ARCHIVE, "*.ts")):
        b = os.path.basename(f)
        m = re.fullmatch(r"(\d{14})\.ts", b)
        if m:
            dt = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            out.append((int(dt.timestamp()), b))
    return sorted(out)


def silence_chunk():
    """A cached 10s silent segment (fallback fill material)."""
    path = os.path.join(STATIC, "silence-chunk.ts")
    if not os.path.exists(path):
        r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error",
                            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                            "-t", str(SEG), "-c:a", "aac", "-b:a", "96k",
                            "-ac", "2", "-ar", "44100", "-f", "mpegts", path],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            log(f"silence chunk generation failed: {r.stderr.strip()[:150]}")
            return None
    return path


def fill_pool():
    """Cyclic list of fill material paths: filler chunk segments, else silence."""
    pool = []
    for d in chunkset_dirs("filler"):
        pool.extend(p for _, p in chunk_segments(d))
    if pool:
        return pool
    sil = silence_chunk()
    return [sil] if sil else []


def heal_archive_gaps(now):
    """Fill missing not-yet-aired archive slots (one hole per tick, and
    forward past the newest name while the recorder is down) with
    synthesized filler segments, PTS-chained from the previous segment."""
    slots = dir_slots()
    if not slots:
        return
    have = {t for t, _ in slots}
    limit = now - HEAL_MIN_AGE
    floor = now - DELAY + LEAD          # slots before this have (nearly) aired
    missing = []
    prev = None
    for ts, _ in slots:
        if prev is not None and ts - prev - SEG >= max(HEAL_MIN_GAP, SEG) and ts > floor:
            g = prev + SEG
            while g + SEG <= ts and g <= limit:
                if g > floor and g not in have:
                    missing.append(g)
                g += SEG
            if missing:
                break                   # one hole per tick: one PTS chain
        prev = ts
    if not missing and slots[-1][0] < limit:   # recorder stalled/dead
        g = slots[-1][0] + SEG
        while g <= limit:
            if g > floor and g not in have:
                missing.append(g)
            g += SEG
    if not missing:
        return
    missing = missing[:FILL_MAX]   # pace catch-up; the rest lands next ticks
    pool = fill_pool()
    if not pool:
        if now - state.get("heal_warned", 0) > 600:
            state["heal_warned"] = now
            save_json(STATE_FILE, state)
            log(f"heal: {len(missing)} slot(s) missing but no filler material")
        return
    earlier = [(t, n) for t, n in slots if t < missing[0]]
    prev_path = os.path.join(ARCHIVE, earlier[-1][1]) if earlier else None
    prev_vals = _pts_list(prev_path) if prev_path else []
    base = (max(prev_vals) + FRAME_TICKS) if prev_vals else None
    start_i = state.get("heal_idx", 0)
    made = 0
    for i, g in enumerate(missing):
        name = datetime.datetime.fromtimestamp(g, UTC).strftime("%Y%m%d%H%M%S") + ".ts"
        dst = os.path.join(ARCHIVE, name)
        if os.path.exists(dst):
            continue
        tmp = os.path.join(ARCHIVE, "." + name + ".tmp")
        src = pool[(start_i + i) % len(pool)]
        try:
            if base is not None:
                _write_aligned(src, tmp, base + int(i * SEG * 90000))
            else:
                shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
            made += 1
        except Exception as e:
            log(f"heal: {name} failed: {e}")
            if os.path.exists(tmp):
                os.remove(tmp)
            break
    if made:
        state["heal_idx"] = (start_i + made) % len(pool)
        save_json(STATE_FILE, state)
        t0 = datetime.datetime.fromtimestamp(missing[0], UTC)
        t1 = datetime.datetime.fromtimestamp(missing[-1], UTC)
        log(f"heal: filled {made} slot(s) {t0:%H:%M:%S}..{t1:%H:%M:%S} UTC")


def prune_archive(now):
    """Delete archive files whose NAME is older than the retention window.

    The recorder's own cleanup goes by mtime, which never catches files the
    splicer rewrote (spliced/healed slots get a fresh mtime)."""
    if now - state.get("pruned_at", 0) < PRUNE_EVERY:
        return
    state["pruned_at"] = now
    cutoff = now - KEEP_SECONDS
    n = 0
    for b in os.listdir(ARCHIVE):
        m = re.fullmatch(r"\.?(\d{14})(\.ts)?(\.tmp)?", b)
        if not m:
            continue
        ts = datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=UTC).timestamp()
        if ts < cutoff:
            try:
                os.remove(os.path.join(ARCHIVE, b))
                n += 1
            except OSError:
                pass
    if n:
        log(f"prune: removed {n} archive file(s) older than {KEEP_SECONDS // 3600}h")


def tick():
    now = time.time()
    prune_archive(now)
    heal_archive_gaps(now)
    adhan_sets = chunkset_dirs("adhan")
    fajr_adhan_sets = chunkset_dirs("fajr-adhan")
    filler_sets = chunkset_dirs("filler")
    starters = starter_dirs()
    events = sch.irish_prayer_events(now)
    wins = sch.cairo_windows(now)
    runs = state.setdefault("runs", [])
    done = state.setdefault("spliced", {})
    durs_all = state.setdefault("durs", {})
    changed = False

    slots = archive_slots()

    def adhan_pool(prayer):
        """(pool, state_index_key): Fajr uses its own pool when available."""
        if prayer == "fajr" and fajr_adhan_sets:
            return fajr_adhan_sets, "fajr_adhan_idx"
        return adhan_sets, "adhan_idx"

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
        pool, idxkey = adhan_pool(prayer)
        if not pool:
            return 0.0
        i = state.get(idxkey, 0) % len(pool)
        sp = len(chunk_segments(starters[prayer])) * SEG if prayer in starters else 0
        return sp + len(chunk_segments(pool[i])) * SEG

    # --- Irish Adhan events (with prayer starters) ---
    for start, prayer in events:
        if start > now + LEAD:
            continue
        key = f"irish-{int(start)}"
        if key in done:
            continue
        if not adhan_sets or not slots:
            continue
        pool, idxkey = adhan_pool(prayer)
        idx = state.get(idxkey, 0) % len(pool)
        achunks = chunk_segments(pool[idx])
        if not achunks:
            continue
        schunks = chunk_segments(starters[prayer]) if prayer in starters else []
        spre, total = len(schunks) * SEG, len(achunks) * SEG
        if start + total + 30 < now:
            log(f"irish@{int(start)}: event past before splice — marked done (total={total})")
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
        state[idxkey] = (idx + 1) % len(pool)
        changed = True
        src = "fajr-" if idxkey == "fajr_adhan_idx" else ""
        log(f"irish@{int(start)}: {src}adhan-{idx} spliced into slots {run[0]}..{run[1]}")

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
