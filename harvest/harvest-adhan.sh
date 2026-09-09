#!/bin/sh
# Adhan harvester: cuts the five daily Adhans out of the rolling archive.
#
# Runs every few minutes via cron. For each Cairo prayer time (AlAdhan API,
# method=5) that has passed by at least PRAYER+7 minutes, extracts
# [PRAYER-3min, PRAYER+7min] from the archive segments into:
#   adhans-raw/YYYY-MM-DD_<Prayer>.mp3
# Raw cuts keep generous margins for manual cleaning/trimming.
set -u

BASE=/opt/quran-radio
ARCHIVE=$BASE/data/archive
OUT=$BASE/adhans-raw
SCHED=$BASE/schedule
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$OUT"

export ARCHIVE OUT TMP
python3 <<'PYEOF'
import os, json, glob, re, subprocess, urllib.request, datetime

ARCHIVE = os.environ["ARCHIVE"]; OUT = os.environ["OUT"]; TMP = os.environ["TMP"]
UTC = datetime.timezone.utc
PRE, POST = 180, 420
now = datetime.datetime.now(UTC).timestamp()

# --- today's + yesterday's Cairo prayer times (UTC epochs), cached ---
def fetch(day):
    key = day.isoformat()
    cache = f"/opt/quran-radio/schedule/cairo-times.json"
    try:
        c = json.load(open(cache))
        if c.get("date") == key:
            return c["utc"]
    except Exception:
        pass
    url = (f"https://api.aladhan.com/v1/timings/{day.strftime('%d-%m-%Y')}"
           f"?latitude=30.0444&longitude=31.2357&method=5"
           f"&timezone=Africa/Cairo&iso8601=true")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "adhan-harvest/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)["data"]["timings"]
        utc = {p: datetime.datetime.fromisoformat(d[p]).astimezone(UTC).timestamp()
               for p in ("Fajr", "Dhuhr", "Asr", "Maghrib", "Isha")}
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        tmp = cache + ".tmp"
        json.dump({"date": key, "utc": utc}, open(tmp, "w"))
        os.replace(tmp, cache)
        return utc
    except Exception as e:
        print(f"[harvest] timings fetch failed for {key}: {e}")
        return None

today = datetime.date.fromtimestamp(now, UTC)
targets = {}
for d in (today - datetime.timedelta(days=1), today):
    pr = fetch(d)
    if pr:
        targets.update(pr)

fmt = lambda ts: datetime.datetime.fromtimestamp(ts, UTC).strftime("%Y%m%d%H%M%S")
datef = lambda ts: datetime.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")

for prayer, pt in sorted(targets.items(), key=lambda kv: kv[1]):
    if not (now > pt + POST + 60):
        continue                                  # hasn't happened yet
    if now - pt > 4 * 3600:
        continue                                  # rotated out of the archive
    out = os.path.join(OUT, f"{datef(pt)}_{prayer}.mp3")
    if os.path.exists(out):
        continue                                  # already harvested
    low, high = fmt(pt - PRE), fmt(pt + POST)
    segs = sorted(os.path.basename(p) for p in glob.glob(f"{ARCHIVE}/*.ts")
                  if low <= os.path.basename(p)[:-3] <= high)
    if not segs:
        print(f"[harvest] {prayer}: no segments in window yet")
        continue
    first_ts = datetime.datetime.strptime(segs[0][:-3], "%Y%m%d%H%M%S").replace(tzinfo=UTC).timestamp()
    offset = max(0.0, (pt - PRE) - first_ts)
    lst = os.path.join(TMP, "list.txt")
    with open(lst, "w") as f:
        for s in segs:
            f.write(f"file '/archive/{s}'\n")
    dur = PRE + POST
    cmd = ("ffmpeg -y -v error -f concat -safe 0 -i /tmp/work/list.txt -c copy /tmp/work/raw.ts && "
           f"ffmpeg -y -v error -ss {offset:.3f} -t {dur} -i /tmp/work/raw.ts "
           f"-vn -c:a libmp3lame -q:a 3 /out/{datef(pt)}_{prayer}.mp3")
    r = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh",
                        "-v", f"{ARCHIVE}:/archive:ro",
                        "-v", f"{TMP}:/tmp/work",
                        "-v", f"{OUT}:/out",
                        "quran-radio-recorder", "-c", cmd],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print(f"[harvest] saved {out} (+{offset:.1f}s offset, {len(segs)} segments)")
    else:
        print(f"[harvest] FAILED {prayer}: {r.stderr.strip()[:300]}")
PYEOF
