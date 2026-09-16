/* Quran Radio admin — login + dashboard. Vanilla JS, no build step. */
"use strict";

const $ = id => document.getElementById(id);

async function api(path, opts = {}) {
  const r = await fetch(path, {
    credentials: "same-origin",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
  });
  if (r.status === 401) { showLogin(); throw new Error("unauthorized"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

const fmtClock = tz => new Intl.DateTimeFormat("en-IE", {
  timeZone: tz, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const clockDublin = fmtClock("Europe/Dublin");
const clockCairo = fmtClock("Africa/Cairo");
const fmtTime = new Intl.DateTimeFormat("en-IE", {
  timeZone: "Europe/Dublin", hour: "2-digit", minute: "2-digit", hour12: false,
});

function fmtDuration(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}

function fmtHM(totalSeconds) {
  const sign = totalSeconds < 0 ? "−" : "";
  const s = Math.abs(totalSeconds);
  return `${sign}${Math.floor(s / 3600)}h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")}m`;
}

function fmtEpoch(epoch) {
  return new Date(epoch * 1000).toISOString().slice(0, 19).replace("T", " ");
}

// ---------------- login ----------------

function showLogin() {
  $("login").hidden = false;
  $("app").hidden = true;
}

function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
}

$("login").addEventListener("submit", async e => {
  e.preventDefault();
  $("login-msg").textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ user: $("user").value, password: $("pass").value }),
    });
    $("pass").value = "";
    boot();
  } catch {
    $("login-msg").textContent = "Wrong username or password";
  }
});

$("logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

// ---------------- dashboard ----------------

let lastStatus = null;
let delayDraft = null;   // null = follow server value

function render(s) {
  lastStatus = s;
  $("who").textContent = s.clocks.dublin.slice(0, 10);

  const dot = $("health-dot");
  dot.classList.toggle("ok", s.health.recorder.ok);
  dot.title = s.health.recorder.ok
    ? `recorder healthy (newest segment ${s.health.recorder.newest_segment_age_s}s old)`
    : "recorder stale — check the watchdog!";

  // delay card
  if (delayDraft === null) delayDraft = s.delay.current_s;
  $("delay-val").textContent = fmtHM(s.delay.current_s);
  $("delay-sub").textContent =
    `env default ${fmtHM(s.delay.env_s)} · natural Cairo−Dublin now ${fmtHM(s.delay.natural_cairo_minus_dublin_s)}`;
  $("delay-input").value = Math.round(delayDraft / 60);
  $("delay-max").textContent = fmtHM(s.delay.bounds.max);
  $("method").textContent = s.config.method;
  $("margins").textContent = `${s.config.win_pre}s / ${s.config.win_post}s`;

  // prayer tables
  renderPrayerTable($("tbl-dublin"), s.prayers.dublin, null);
  renderPrayerTable($("tbl-cairo"), s.prayers.cairo, "Africa/Cairo");

  // next prayer banner
  const nxt = s.prayers.next;
  $("next-prayer").innerHTML = nxt
    ? `Next: <b>${cap(nxt.prayer)}</b> in <b>${fmtDuration(nxt.at - s.now_utc)}</b>`
    : "No upcoming prayer found";

  // recent splice runs (content-time epochs)
  const runs = $("runs");
  runs.innerHTML = "";
  if (!s.recent_runs.length) {
    runs.innerHTML = '<li class="empty">No adhan/filler splices yet</li>';
  } else {
    for (const [a, b] of [...s.recent_runs].reverse().slice(0, 8)) {
      const li = document.createElement("li");
      li.textContent = `slots ${fmtEpoch(a)} → ${fmtEpoch(b)} UTC`;
      runs.appendChild(li);
    }
  }
}

function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : "—"; }

function renderPrayerTable(table, times, tz) {
  const tbody = table.tBodies[0];
  tbody.innerHTML = "";
  if (!times) {
    tbody.innerHTML = '<tr><td class="empty">unavailable</td></tr>';
    return;
  }
  const now = lastStatus ? lastStatus.now_utc : Date.now() / 1000;
  const nextAt = lastStatus && lastStatus.prayers.next
    ? lastStatus.prayers.next.at : null;
  for (const p of ["fajr", "dhuhr", "asr", "maghrib", "isha"]) {
    const tr = document.createElement("tr");
    const tdName = document.createElement("td");
    const tdTime = document.createElement("td");
    tdName.textContent = cap(p);
    const t = times[p];
    if (t === undefined || t === null) {
      tdTime.textContent = "—";
    } else if (typeof t === "number") {          // Cairo: UTC epoch
      tdTime.textContent = new Intl.DateTimeFormat("en-IE", {
        timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false,
      }).format(new Date(t * 1000));
      tr.classList.toggle("next-prayer", Math.abs(t - nextAt) < 60);
    } else {                                      // Dublin: local "H:MM"
      tdTime.textContent = String(t).padStart(5, "0");
    }
    tr.append(tdName, tdTime);
    tbody.appendChild(tr);
  }
}

// ---------------- delay editing ----------------

document.querySelectorAll("[data-delta]").forEach(btn => {
  btn.addEventListener("click", () => {
    const s = lastStatus;
    if (!s) return;
    delayDraft = Math.min(s.delay.bounds.max,
      Math.max(s.delay.bounds.min, (delayDraft ?? s.delay.current_s) + Number(btn.dataset.delta)));
    $("delay-input").value = Math.round(delayDraft / 60);
  });
});

$("delay-input").addEventListener("change", () => {
  const s = lastStatus;
  const mins = Number($("delay-input").value) || 0;
  delayDraft = Math.min(s.delay.bounds.max, Math.max(s.delay.bounds.min, mins * 60));
});

$("delay-save").addEventListener("click", async () => {
  const msg = $("delay-msg");
  msg.className = "msg";
  msg.textContent = "Saving…";
  try {
    await api("/api/config", {
      method: "PUT",
      body: JSON.stringify({ delay_seconds: Math.round(delayDraft) }),
    });
    msg.className = "msg ok";
    msg.textContent = "Saved — takes effect within seconds.";
    delayDraft = null;
    refresh();
  } catch (err) {
    msg.className = "msg err";
    msg.textContent = err.message;
  }
});

// ---------------- boot / polling ----------------

let pollTimer = null;

async function refresh() {
  try {
    const s = await api("/api/status");
    render(s);
    $("clock-dublin").textContent = clockDublin.format(new Date());
    $("clock-cairo").textContent = clockCairo.format(new Date());
    $("clock-utc").textContent = new Date().toISOString().slice(11, 19);
  } catch (err) {
    if (err.message !== "unauthorized") console.warn("status refresh:", err.message);
  }
}

async function boot() {
  showApp();
  await refresh();
  clearInterval(pollTimer);
  pollTimer = setInterval(refresh, 5000);
}

(async () => {
  try {
    await api("/api/session");
    boot();
  } catch {
    showLogin();
  }
})();
