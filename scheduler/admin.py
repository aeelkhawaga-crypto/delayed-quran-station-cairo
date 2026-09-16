#!/usr/bin/env python3
"""Admin HTTP API for the radio station — stdlib only.

Runs as a daemon thread inside the scheduler container; nginx proxies
/api/ here. Auth is a username/password from .env (ADMIN_USER /
ADMIN_PASSWORD) exchanged for an HMAC-signed session cookie. All station
state stays file-based: this API only reads/writes schedule/*.json with
tmp+replace, and the scheduler/splicer pick changes up on their next tick.
"""
import glob, hmac, hashlib, json, os, re, secrets, threading, time, datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import scheduler as sch

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# Fixed secret keeps sessions valid across restarts; a random one is fine
# otherwise (everyone just logs in again after a deploy).
SESSION_SECRET = os.environ.get("ADMIN_SECRET", "").encode() or secrets.token_hex(16).encode()
SESSION_MAX_AGE = 7 * 86400
MAX_BODY = 150 * 1024 * 1024
COOKIE = "quran_admin"

CONFIG_KEYS = {
    "delay_seconds": int,
    "window_pre_seconds": int,
    "window_post_seconds": int,
    "min_gap_seconds": int,
    "method": str,
    "cairo_enabled": bool,
}
DELAY_BOUNDS = {"min": sch.SEG, "max": int(os.environ.get("ARCHIVE_HOURS", "4")) * 3600 - 600}
RANGE_BOUNDS = {"window_pre_seconds": (0, 3600), "window_post_seconds": (0, 7200),
                "min_gap_seconds": (0, 3600)}


def log(msg):
    print(f"[admin] {msg}", flush=True)


# ---------------- auth ----------------

def _sign(msg):
    return hmac.new(SESSION_SECRET, msg.encode(), hashlib.sha256).hexdigest()


def make_session():
    expiry = int(time.time()) + SESSION_MAX_AGE
    return f"{expiry}.{_sign(f'{ADMIN_USER}:{expiry}')}"


def check_session(value):
    try:
        expiry_s, sig = value.rsplit(".", 1)
        expiry = int(expiry_s)
    except ValueError:
        return False
    if expiry < time.time():
        return False
    return hmac.compare_digest(sig, _sign(f"{ADMIN_USER}:{expiry}"))


# ---------------- status helpers ----------------

def cairo_offset(dt_utc):
    """Africa/Cairo offset via Egypt DST rules (last Fri Apr -> last Thu Oct)."""
    def last_friday(y, m):
        d = datetime.date(y, m, 30 if m == 4 else 31)
        while d.weekday() != 4:
            d -= datetime.timedelta(days=1)
        return d
    def last_thursday(y, m):
        d = datetime.date(y, m, 31)
        while d.weekday() != 3:
            d -= datetime.timedelta(days=1)
        return d
    y = dt_utc.year
    start = datetime.datetime(y, 4, last_friday(y, 4).day, 21, 0, tzinfo=sch.UTC)
    end = datetime.datetime(y, 10, last_thursday(y, 10).day, 21, 0, tzinfo=sch.UTC)
    return 3 * 3600 if start <= dt_utc < end else 2 * 3600


def iso_local(dt_utc, offset_s):
    return (dt_utc + datetime.timedelta(seconds=offset_s)).strftime("%Y-%m-%d %H:%M")


def recorder_health():
    try:
        newest = max((os.path.getmtime(p) for p in
                      glob.glob(os.path.join(sch.ARCHIVE, "*.ts"))),
                     default=None)
    except Exception:
        newest = None
    if newest is None:
        return {"ok": False, "newest_segment_age_s": None}
    age = time.time() - newest
    return {"ok": age < 90, "newest_segment_age_s": round(age, 1)}


def prayer_status(now):
    cfg = sch.get_config()
    today = datetime.datetime.fromtimestamp(now, sch.UTC).date()
    cairo = sch.cairo_prayers(today)
    dublin = None
    try:
        days = json.load(open(sch.IRISH_JSON)).get("days", {})
        entry = days.get(f"{today.month:02d}-{today.day:02d}")
        if entry:
            dublin = entry.get("times") or entry.get("standardTimes")
    except Exception:
        pass
    # next prayer across both cities (IFI/manual events near now + Cairo)
    candidates = []
    for start, prayer in sch._irish_events_json(now) + [(s, p) for p, s in (cairo or {}).items()]:
        if start > now:
            candidates.append((start, prayer))
    nxt = min(candidates) if candidates else None
    return {
        "cairo": cairo,
        "dublin": dublin,
        "next": {"at": nxt[0], "prayer": nxt[1]} if nxt else None,
    }, cfg


def build_status():
    now = time.time()
    dt = datetime.datetime.fromtimestamp(now, sch.UTC)
    prayers, cfg = prayer_status(now)
    try:
        runs = json.load(open(os.path.join(sch.SCHED, ".splice-runs.json"))).get("runs", [])
    except Exception:
        runs = []
    return {
        "now_utc": now,
        "clocks": {
            "utc": dt.strftime("%H:%M:%S"),
            "dublin": iso_local(dt, sch.dublin_offset(dt)),
            "cairo": iso_local(dt, cairo_offset(dt)),
        },
        "delay": {
            "current_s": cfg["delay"],
            "env_s": sch.DELAY,
            "natural_cairo_minus_dublin_s": cairo_offset(dt) - sch.dublin_offset(dt),
            "bounds": DELAY_BOUNDS,
        },
        "prayers": prayers,
        "config": {k: cfg[k] for k in ("win_pre", "win_post", "min_gap", "method",
                                       "cairo_enabled")},
        "health": {"recorder": recorder_health()},
        "recent_runs": runs[-10:],
    }


# ---------------- config read/write ----------------

def read_config_file():
    try:
        with open(sch.CONFIG_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_config_file(d):
    tmp = sch.CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, sch.CONFIG_FILE)


def validate_config(patch):
    """Return (clean_dict, error). None values delete a key (back to env)."""
    clean, errors = {}, []
    for k, v in patch.items():
        if k not in CONFIG_KEYS:
            errors.append(f"unknown key: {k}")
            continue
        if v is None:
            clean[k] = None
            continue
        t = CONFIG_KEYS[k]
        if t is bool:
            if not isinstance(v, bool):
                errors.append(f"{k} must be true/false")
                continue
        elif t is int:
            try:
                v = int(v)
            except (TypeError, ValueError):
                errors.append(f"{k} must be an integer")
                continue
            if k == "delay_seconds":
                lo, hi = DELAY_BOUNDS["min"], DELAY_BOUNDS["max"]
            else:
                lo, hi = RANGE_BOUNDS[k]
            if not lo <= v <= hi:
                errors.append(f"{k} must be {lo}..{hi}")
                continue
        else:
            if not re.fullmatch(r"\d{1,2}", str(v)):
                errors.append(f"{k} must be a number")
                continue
            v = str(v)
        clean[k] = v
    if errors:
        return None, "; ".join(errors)
    return clean, None


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "quran-admin/1.0"

    def log_message(self, fmt, *args):
        log(fmt % args)

    # -- helpers --
    def _json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n > MAX_BODY:
            return None
        return self.rfile.read(n) if n else b""

    def _read_json(self):
        raw = self._read_body()
        if raw is None:
            self._json({"error": "body too large"}, 413)
            return None
        try:
            obj = json.loads(raw or b"{}")
            return obj if isinstance(obj, dict) else {}
        except Exception:
            self._json({"error": "invalid JSON"}, 400)
            return None

    def _cookie_value(self):
        c = cookies.SimpleCookie(self.headers.get("Cookie"))
        m = c.get(COOKIE)
        return m.value if m else None

    def _authorized(self):
        v = self._cookie_value()
        return v is not None and check_session(v)

    def _require_auth(self):
        if self._authorized():
            return True
        self._json({"error": "unauthorized"}, 401)
        return False

    # -- routes --
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/session":
            if self._authorized():
                self._json({"user": ADMIN_USER})
            else:
                self._json({"error": "unauthorized"}, 401)
        elif path == "/api/status":
            if self._require_auth():
                try:
                    self._json(build_status())
                except Exception as e:
                    self._json({"error": str(e)}, 500)
        elif path == "/api/config":
            if self._require_auth():
                self._json({"file": read_config_file(),
                            "env": {"delay_seconds": sch.DELAY,
                                    "window_pre_seconds": sch.WIN_PRE,
                                    "window_post_seconds": sch.WIN_POST,
                                    "min_gap_seconds": sch.MIN_GAP,
                                    "method": sch.METHOD,
                                    "cairo_enabled": True},
                            "delay_bounds": DELAY_BOUNDS})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/login":
            body = self._read_json()
            if body is None:
                return
            ok = (hmac.compare_digest(str(body.get("user", "")), ADMIN_USER)
                  and ADMIN_PASSWORD
                  and hmac.compare_digest(str(body.get("password", "")), ADMIN_PASSWORD))
            if not ok:
                self._json({"error": "bad credentials"}, 401)
                return
            self._json({"ok": True}, 200, {
                "Set-Cookie": f"{COOKIE}={make_session()}; Path=/; HttpOnly; "
                              f"SameSite=Lax; Max-Age={SESSION_MAX_AGE}"})
        elif path == "/api/logout":
            self._json({"ok": True}, 200, {
                "Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; Max-Age=0"})
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/config":
            if not self._require_auth():
                return
            body = self._read_json()
            if body is None:
                return
            clean, err = validate_config(body)
            if err:
                self._json({"error": err}, 400)
                return
            merged = read_config_file()
            for k, v in clean.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            try:
                write_config_file(merged)
            except Exception as e:
                self._json({"error": str(e)}, 500)
                return
            sch._cfg_cache["mtime"] = None  # force re-read on next tick
            self._json({"ok": True, "file": merged})
        else:
            self._json({"error": "not found"}, 404)


def start_admin_thread():
    if not ADMIN_PASSWORD:
        log("ADMIN_PASSWORD not set — admin API disabled")
        return
    def run():
        server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
        log(f"API listening on :8000 (user {ADMIN_USER!r})")
        while True:
            try:
                server.serve_forever()
            except Exception as e:
                log(f"server error: {e} — restarting in 2s")
                time.sleep(2)
    threading.Thread(target=run, daemon=True, name="admin").start()
