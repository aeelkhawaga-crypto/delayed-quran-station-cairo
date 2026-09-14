#!/usr/bin/env python3
"""Content splicer for the delayed Quran stream.

Runs as a separate job alongside the scheduler. Instead of switching the
live playlist between archive/adhan/filler URIs (which forced players to
re-sync and could mix buffered chunks), this job OVERWRITES the archive
segment files themselves, inside the delayed window: about a minute
before an Irish Adhan airs, the archive slots it will occupy are replaced
with the adhan chunks, byte for byte, under the same filenames. The
playlist stays one continuous archive sequence; players simply play
through the filenames and hear adhan where adhan belongs.

- Irish Adhan: at each event one adhan file (round-robin) occupies the
  slots from the event start; afterwards the stream continues from where
  the adhan ended (the skipped gap is an accepted tradeoff).
- Cairo Adhan suppression: during each Cairo window the slots are filled
  with whole filler files, one at a time, round-robin per window. With no
  fillers the Cairo adhan simply stays audible.
- If an Irish adhan and a Cairo window overlap or are within MIN_GAP
  seconds, neither is spliced (normal delayed stream plays).

A slot is fetched by players only during the ~30s before it airs, so the
splice happens LEAD seconds before the event; that way no player holds
the old bytes for a spliced slot.
"""
import os, re, sys, json, glob, time, datetime

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
            out.append((int(dt.timestamp()), m.group(1)))
    return sorted(out)


def chunk_segments(sdir):
    """Absolute paths of a chunkset's segments, in order."""
    entries, _ = sch.load_chunkset(sdir)
    return [os.path.join(sdir, u) for _, u in entries]


def chunkset_dirs(kind):
    return sorted(glob.glob(os.path.join(STATIC, f"{kind}-*")))


def splice_slots(c_start, c_end, chunks, now):
    """Replace archive files whose content ts falls in (c_start-SEG, c_end)
    with successive chunk segments, 1:1 in order. Slots that already aired
    are dropped together with their chunk counterparts. Returns [t0, t1]
    content range of the replaced run, or None if nothing was replaced."""
    slots = [(ts, n) for ts, n in archive_slots() if c_start - SEG < ts < c_end]
    if not slots:
        return None
    dropped = 0
    while dropped < len(slots) and slots[dropped][0] + DELAY + EXPOSURE < now:
        dropped += 1
    slots, chunks = slots[dropped:], chunks[dropped:]
    n = 0
    for (ts, name), src in zip(slots, chunks):
        try:
            with open(src, "rb") as f:
                data = f.read()
        except Exception as e:
            log(f"chunk unreadable {src}: {e} — will retry")
            return None
        tmp = os.path.join(ARCHIVE, "." + name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(ARCHIVE, name))
        n += 1
    if not n:
        return None
    return [slots[0][0], slots[n - 1][0]]


def gap(a0, a1, b0, b1):
    if a1 < b0:
        return b0 - a1
    if b1 < a0:
        return a0 - b1
    return 0


def tick():
    now = time.time()
    adhan_sets = chunkset_dirs("adhan")
    filler_sets = chunkset_dirs("filler")
    events = sch.irish_events(now)
    wins = sch.cairo_windows(now)
    runs = state.setdefault("runs", [])
    done = state.setdefault("spliced", {})
    changed = False

    def span_of(start):
        idx = state.get("adhan_idx", 0)
        if not adhan_sets:
            return 0.0
        return len(chunk_segments(adhan_sets[idx % len(adhan_sets)])) * SEG

    # --- Irish Adhan events ---
    for start in events:
        if start > now + LEAD:
            continue
        key = f"irish-{int(start)}"
        if key in done:
            continue
        if not adhan_sets:
            continue
        idx = state.get("adhan_idx", 0) % len(adhan_sets)
        chunks = chunk_segments(adhan_sets[idx])
        total = len(chunks) * SEG
        if start + total + 30 < now:
            done[key] = None
            changed = True
            continue
        if any(gap(start, start + total, w["start"], w["end"]) <= MIN_GAP for w in wins):
            log(f"irish@{int(start)} within {MIN_GAP}s of a Cairo window — not splicing")
            done[key] = None
            changed = True
            continue
        run = splice_slots(start - DELAY, start - DELAY + total, chunks, now)
        if run:
            done[key] = run
            runs.append(run)
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
        if not filler_sets:
            continue
        idx = state.get("filler_idx", 0) % len(filler_sets)
        chain = flatten(filler_sets, idx)
        if any(gap(s, s + span_of(s), w["start"], w["end"]) <= MIN_GAP for s in events
               if s - 86400 < w["end"]):
            log(f"cairo@{int(w['start'])} within {MIN_GAP}s of an Irish adhan — not splicing")
            done[key] = None
            changed = True
            continue
        run = splice_slots(w["start"] - DELAY, w["end"] - DELAY, chain, now)
        if run:
            done[key] = run
            runs.append(run)
            state["filler_idx"] = (idx + 1) % len(filler_sets)
            changed = True
            log(f"cairo@{int(w['start'])}: filler-{idx}+ spliced into slots {run[0]}..{run[1]}")

    # prune
    runs[:] = [r for r in runs if r[1] + DELAY + 3600 > now]
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


def flatten(sets, start_idx):
    """Chain filler chunksets in order, starting at start_idx (round-robin)."""
    out = []
    for k in range(len(sets)):
        out.extend(chunk_segments(sets[(start_idx + k) % len(sets)]))
    return out


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
