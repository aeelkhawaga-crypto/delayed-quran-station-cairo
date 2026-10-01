/* Quran Radio monitor page. Vanilla JS + hls.js (vendored). */
"use strict";
const $ = id => document.getElementById(id);
const KIND_COLOR = { quran: "#3a6ea5", "dublin-adhan": "#2fbf71",
                     "fajr-adhan": "#9be15d", "cairo-suppression": "#e0a63c",
                     starter: "#5ec8d8", healed: "#e05a8c" };
const KIND_LABEL = { quran: "Quran", "dublin-adhan": "Dublin adhan",
                     "fajr-adhan": "Fajr adhan", "cairo-suppression": "Cairo suppressed",
                     starter: "Prayer starter", healed: "Healed gap (filler)" };

let TL = null;            // last timeline payload
let seekTs = null;        // where the player is pointed (content ts)
let playBase = null;      // content ts of the first segment in the listen playlist
let sel = null;           // {a, b} selected content range
let drag = null;          // in-progress pointer drag on the timeline
let stopAt = null;        // pause when the playhead passes this (play selection)
let playing = false;
let hls = null;

const fmtE = e => new Date(e * 1000).toISOString().slice(0, 19).replace("T", " ");
const fmtDub = e => new Date(e * 1000).toLocaleTimeString("en-GB", { timeZone: "Europe/Dublin" });
const fmtDubHM = e => new Date(e * 1000).toLocaleTimeString("en-GB", { timeZone: "Europe/Dublin", hour: "2-digit", minute: "2-digit" });
// All times on this page are Irish time. Archive segments are named by their
// recording time; they air in Ireland DELAY seconds later (air = t + delay).
const fmtDur = s => s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`
                  : s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`;

async function api(path) {
  const r = await fetch(path, { credentials: "same-origin" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

/* ---------------- health cards ---------------- */

function renderCards(o) {
  const rec = o.recorder, pl = o.playlist;
  const recCls = rec.newest_age_s == null ? "bad" : rec.newest_age_s < 30 ? "ok"
               : rec.newest_age_s < 90 ? "warn" : "bad";
  const plCls = pl.segments >= 5 ? "ok" : pl.segments >= 2 ? "warn" : "bad";
  const wd = o.watchdog;
  const wdTxt = wd ? `${Math.round(o.now_utc - wd.checked_at)}s ago` : "no state";
  const wdCls = wd ? (o.now_utc - wd.checked_at < 180 ? "ok" : "warn") : "warn";
  $("cards").innerHTML = `
    <div class="card"><div class="k">Recorder</div>
      <div class="v ${recCls}">${rec.newest_age_s == null ? "—" : Math.round(rec.newest_age_s) + "s old"}</div>
      <div class="s">${rec.segments} segments on disk</div></div>
    <div class="card"><div class="k">Playlist</div>
      <div class="v ${plCls}">${pl.segments} segments</div>
      <div class="s">seq ${pl.media_seq ?? "—"} · written ${pl.file_age_s ?? "—"}s ago</div></div>
    <div class="card"><div class="k">Archive span</div>
      <div class="v">${rec.oldest ? fmtDubHM(rec.oldest + o.delay) + " → " + fmtDubHM(rec.newest + o.delay) : "—"}</div>
      <div class="s">air times in Ireland, ${((rec.newest - rec.oldest) / 3600).toFixed(1)}h</div></div>
    <div class="card"><div class="k">Healed gaps</div>
      <div class="v ${o.healed_segments ? "warn" : "ok"}">${o.healed_segments}</div>
      <div class="s">synthesized filler segments</div></div>
    <div class="card"><div class="k">Watchdog</div>
      <div class="v ${wdCls}">${wdTxt}</div>
      <div class="s">${wd && wd.restarted ? "restarted recorder on last check" : "checks every minute"}</div></div>
    ${feederCard(o)}
    <div class="card"><div class="k">Splicer</div>
      <div class="v ${o.splicer_tick_age_s == null ? "bad" : o.splicer_tick_age_s < 30 ? "ok" : "bad"}">${o.splicer_tick_age_s == null ? "no state" : Math.round(o.splicer_tick_age_s) + "s ago"}</div>
      <div class="s">last tick · ${o.runs_active} active splice runs</div></div>
    <div class="card"><div class="k">Disk</div>
      <div class="v ${o.disk && o.disk.used_pct < 85 ? "ok" : "warn"}">${o.disk ? o.disk.free_gb + " GB free" : "—"}</div>
      <div class="s">${o.disk ? o.disk.used_pct + "% used" : ""}</div></div>
    <div class="card"><div class="k">Adhan pool</div>
      <div class="v">${o.pools.adhan.chunksets} shared</div>
      <div class="s">next: ${o.pools.adhan.next ?? "—"}</div></div>
    <div class="card"><div class="k">Fajr pool</div>
      <div class="v">${o.pools.fajr.chunksets} fajr-only</div>
      <div class="s">next: ${o.pools.fajr.next ?? "—"}</div></div>`;
}

function feederCard(o) {
  const f = o.feeder;
  if (!f) return `<div class="card"><div class="k">Source feed</div>
    <div class="v warn">no status</div><div class="s">feeder not reporting</div></div>`;
  const stale = o.now_utc - f.updated > 30;
  const cls = stale ? "bad" : f.silence_active ? "bad" : f.connected ? "ok" : "warn";
  const state = stale ? "not reporting" : f.silence_active ? "OUTAGE (silence)"
              : f.connected ? "connected" : "reconnecting";
  const bps = f.bytes_forwarded / Math.max(1, (f.updated - f.started));
  const replaySec = bps ? f.replay_dropped_bytes / bps : 0;
  return `<div class="card"><div class="k">Source feed</div>
    <div class="v ${cls}">${state}</div>
    <div class="s">${f.connects - 1} reconnects · ${f.replay_drops} replays removed (${replaySec.toFixed(0)}s)
      · ${f.outages} outages, ${fmtDur(f.silence_total_s)} silence${f.last_error ? ` · last error: ${f.last_error}` : ""}</div></div>`;
}

function renderComing() {
  const tb = document.querySelector("#tbl-coming tbody");
  if (!TL) return;
  // group consecutive segments from the airing point onwards into runs by kind
  const runs = [];
  for (const [t, k] of TL.segments) {
    if (t + TL.seg <= airingNow()) continue;
    const last = runs[runs.length - 1];
    if (last && last.kind === k && t - last.t1 < 2 * TL.seg) last.t1 = t + TL.seg;
    else runs.push({ kind: k, t0: t, t1: t + TL.seg });
  }
  // pending (not yet spliced) events whose content is already saved
  for (const m of TL.marks) {
    if (m.status !== "pending" || m.t0 + TL.delay < TL.now_utc) continue;
    runs.push({ kind: m.kind, t0: m.t0, t1: m.t1, pending: true, label: m.label });
  }
  runs.sort((a, b) => a.t0 - b.t0);
  tb.innerHTML = "";
  if (!runs.length) { tb.innerHTML = '<tr><td colspan="6">nothing saved ahead yet</td></tr>'; return; }
  for (const r of runs) {
    const air = r.t0 + TL.delay;
    const what = KIND_LABEL[r.kind] + (r.pending ? ` — ${r.label}, <span class="pill pending">preview — written into the stream ~10 min before air</span>` : "");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmtDub(Math.max(air, TL.now_utc))}</td>
      <td class="dim">${air <= TL.now_utc ? "airing now" : fmtDur(air - TL.now_utc)}</td>
      <td class="dim">${fmtDub(r.t1 + TL.delay)}</td>
      <td><span class="kind-dot" style="background:${KIND_COLOR[r.kind]}"></span>${what}</td>
      <td>${fmtDur(r.t1 - r.t0)}</td>
      <td><button data-t="${Math.max(r.pending ? r.t0 - 20 : r.t0, airingNow())}">▶ listen</button></td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    seekTo(Number(b.dataset.t), false);
    if (!playing) $("btn-play").click();
  }));
}

function renderEvents(o) {
  const tb = document.querySelector("#tbl-events tbody");
  tb.innerHTML = "";
  const rows = o.events.filter(r => r.wall > o.now_utc - 3600 && r.wall < o.now_utc + 6 * 3600);
  if (!rows.length) { tb.innerHTML = '<tr><td colspan="6">nothing scheduled</td></tr>'; return; }
  for (const r of rows) {
    const isFuture = r.wall > o.now_utc;
    const tr = document.createElement("tr");
    const file = r.kind === "dublin-adhan"
      ? (r.prayer === "fajr" ? o.pools.fajr.next : o.pools.adhan.next) ?? "—" : "";
    tr.innerHTML = `
      <td>${fmtDubHM(r.wall)}${isFuture ? ` <span class="dim">(in ${Math.round((r.wall - o.now_utc) / 60)}m)</span>` : ""}</td>
      <td>${r.wall_end ? fmtDubHM(r.wall_end) : "—"}</td>
      <td>${r.kind === "cairo-suppression" ? "Cairo suppression" : r.kind === "fajr-adhan" ? "Fajr adhan" : "Dublin adhan"}</td>
      <td>${r.prayer ? r.prayer[0].toUpperCase() + r.prayer.slice(1) : "—"}</td>
      <td><span class="pill ${r.status}">${r.status}</span></td>
      <td>${file}</td>`;
    tb.appendChild(tr);
  }
}

/* ---------------- timeline ---------------- */

const canvas = $("timeline"), ctx = canvas.getContext("2d");

function drawTimeline() {
  if (!TL) return;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  if (canvas.width !== W * dpr) { canvas.width = W * dpr; canvas.height = H * dpr; }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const t0 = TL.segments.length ? TL.segments[0][0] : TL.now_utc - 3600;
  const t1 = TL.segments.length ? TL.segments[TL.segments.length - 1][0] + TL.seg
                                : TL.now_utc;
  const span = Math.max(1, t1 - t0);
  const x = t => ((t - t0) / span) * W;
  // segment columns
  for (const [t, k] of TL.segments) {
    ctx.fillStyle = KIND_COLOR[k] || "#3a6ea5";
    ctx.fillRect(x(t), 12, Math.max(1, x(t + TL.seg) - x(t) - 0.5), H - 24);
  }
  // hour gridlines + labels
  ctx.fillStyle = "#7d93ad";
  ctx.strokeStyle = "#1e3350";
  ctx.font = "10px sans-serif";
  const hour0 = Math.ceil(t0 / 3600) * 3600;
  ctx.textAlign = "center";
  for (let h = hour0; h < t1; h += 3600) {
    ctx.beginPath(); ctx.moveTo(x(h), 8); ctx.lineTo(x(h), H - 8); ctx.stroke();
    ctx.fillText(fmtDubHM(h + TL.delay), x(h), H - 1);
  }
  // pending marks (outlined)
  for (const m of TL.marks) {
    if (m.status !== "pending") continue;
    ctx.strokeStyle = KIND_COLOR[m.kind] || "#fff";
    ctx.setLineDash([3, 3]);
    ctx.strokeRect(x(m.t0), 10, Math.max(2, x(m.t1) - x(m.t0)), H - 20);
    ctx.setLineDash([]);
  }
  // airing line: follows the live clock (TL is only refetched every 30 s)
  const airNow = airingNow();
  const ax = x(airNow);
  ctx.strokeStyle = "#ffffff";
  ctx.setLineDash([5, 4]);
  ctx.beginPath(); ctx.moveTo(ax, 4); ctx.lineTo(ax, H - 4); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#fff";
  ctx.textAlign = ax > W - 60 ? "right" : "left";
  ctx.fillText("on air " + fmtDub(airNow + TL.delay), ax + (ax > W - 60 ? -3 : 3), 9);
  // selection
  const S = drag && drag.moved ? { a: Math.min(drag.t0, drag.t1), b: Math.max(drag.t0, drag.t1) } : sel;
  if (S) {
    ctx.fillStyle = "rgba(255,255,255,0.18)";
    ctx.fillRect(x(S.a), 0, Math.max(1, x(S.b) - x(S.a)), H);
    ctx.strokeStyle = "#ffffff";
    ctx.beginPath();
    ctx.moveTo(Math.round(x(S.a)) + 0.5, 0); ctx.lineTo(Math.round(x(S.a)) + 0.5, H);
    ctx.moveTo(Math.round(x(S.b)) + 0.5, 0); ctx.lineTo(Math.round(x(S.b)) + 0.5, H);
    ctx.stroke();
  }
  // playhead: where the audio actually is
  const ph = playheadTs();
  if (ph != null && ph >= t0 && ph <= t1) {
    const px = x(ph);
    ctx.strokeStyle = "#ffd23f";        // thin vertical cursor
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(Math.round(px) + 0.5, 0); ctx.lineTo(Math.round(px) + 0.5, H); ctx.stroke();
    ctx.fillStyle = "#ffd23f";
    const lab = fmtDub(ph + TL.delay);
    ctx.font = "10px sans-serif";
    ctx.textAlign = px > W - 80 ? "right" : "left";
    ctx.fillText(lab, px + (px > W - 80 ? -8 : 8), H - 14);
  }
  TL._geom = { t0, t1, W };
}

function canvasTs(ev) {
  if (!TL || !TL._geom) return null;
  const r = canvas.getBoundingClientRect();
  const fx = (ev.clientX - r.left) / r.width;
  const { t0, t1 } = TL._geom;
  return t0 + fx * (t1 - t0);
}

canvas.addEventListener("pointerdown", ev => {
  const t = canvasTs(ev);
  if (t == null) return;
  drag = { x: ev.clientX, t0: t, t1: t, moved: false };
  canvas.setPointerCapture(ev.pointerId);
});
canvas.addEventListener("pointermove", ev => {
  if (!drag) return;
  drag.t1 = clampTs(canvasTs(ev));
  if (Math.abs(ev.clientX - drag.x) > 4) drag.moved = true;
  if (drag.moved) drawTimeline();
});
canvas.addEventListener("pointerup", ev => {
  if (!drag) return;
  const d = drag; drag = null;
  if (!d.moved) {                       // a tap/click: jump there and play
    stopAt = null;
    seekTo(d.t0, false);
    if (!playing) $("btn-play").click();
    return;
  }
  const a = Math.min(d.t0, d.t1), b = Math.max(d.t0, d.t1);
  sel = b - a >= TL.seg ? { a, b } : null;
  renderSel(); drawTimeline();
});
canvas.addEventListener("pointercancel", () => { drag = null; drawTimeline(); });

function clampTs(t) {
  if (!TL || !TL._geom || t == null) return t;
  return Math.max(TL._geom.t0, Math.min(TL._geom.t1, t));
}

function renderSel() {
  const bar = $("selbar");
  if (!sel) { bar.style.display = "none"; return; }
  bar.style.display = "flex";
  $("sel-txt").textContent =
    `Selected: ${fmtDub(sel.a + TL.delay)} → ${fmtDub(sel.b + TL.delay)} Irish time · ${fmtDur(sel.b - sel.a)}`;
  $("btn-sel-dl").href = `/monitor/api/download?start=${Math.floor(sel.a)}&end=${Math.ceil(sel.b)}`;
  $("sel-note").textContent = "";
}

$("btn-sel-play").addEventListener("click", () => {
  if (!sel) return;
  seekTo(sel.a, false);
  stopAt = sel.b;
  if (!playing) $("btn-play").click(); else $("audio").play().catch(() => {});
});
$("btn-sel-clear").addEventListener("click", () => { sel = null; stopAt = null; renderSel(); drawTimeline(); });
$("btn-sel-dl").addEventListener("click", () => {
  $("sel-note").textContent = "preparing file… the download starts when it is ready (longer ranges take a little while)";
});
canvas.addEventListener("mousemove", ev => {
  const t = canvasTs(ev);
  const h = $("hover");
  if (t == null || !TL) { h.style.display = "none"; return; }
  const r = canvas.getBoundingClientRect();
  h.style.display = "block";
  h.style.left = (ev.clientX - r.left + 12) + "px";
  h.style.top = (ev.clientY - r.top - 8) + "px";
  const seg = TL.segments.find(s => t >= s[0] && t < s[0] + TL.seg);
  const air = t + TL.delay;
  h.textContent = `${seg ? KIND_LABEL[seg[1]] : "no segment"} — ` +
    (air < TL.now_utc ? `aired ${fmtDub(air)}` : `airs ${fmtDub(air)} (in ${fmtDur(air - TL.now_utc)})`);
});

/* ---------------- player ---------------- */

function media() {
  const a = $("audio");
  return a;
}

function attach(src) {
  const a = $("audio") || document.createElement("audio");
  if (!a.parentNode) { a.id = "audio"; a.style.width = "260px"; $("playbar").appendChild(a); }
  if (hls) { hls.destroy(); hls = null; }
  if (window.Hls && Hls.isSupported()) {
    hls = new Hls({ maxBufferLength: 30 });
    hls.loadSource(src);
    hls.attachMedia(a);
  } else {
    a.src = src;   // Safari native HLS
  }
  return a;
}

// broadcast point now (content time), from the live clock; TL is only
// refetched every 30 s, so TL.airing_ts alone would lag and jump
function airingNow() {
  return TL.now_utc_live ? TL.now_utc_live() - TL.delay : TL.airing_ts;
}

function playheadTs() {
  const a = $("audio");
  if (!a || playBase == null) return null;
  return playBase + (a.currentTime || 0);
}

function kindAt(ts) {
  if (!TL) return "Quran";
  const m = TL.marks.find(m => m.status === "pending" && ts >= m.t0 && ts < m.t1);
  if (m) return `${KIND_LABEL[m.kind]} (preview)`;
  const seg = TL.segments.find(s => ts >= s[0] && ts < s[0] + TL.seg);
  return seg ? KIND_LABEL[seg[1]] : "no segment";
}

function seekTo(ts, live) {
  seekTs = ts;
  stopAt = null;                        // any jump cancels "play selection"
  // the listen playlist starts at the first segment named at/after ts
  const first = TL ? TL.segments.find(s => s[0] >= ts) : null;
  playBase = first ? first[0] : ts;
  $("btn-live").classList.toggle("on", !!live);
  const a = attach(`/monitor/api/listen.m3u8?start=${Math.round(ts)}&minutes=30`);
  if (playing) a.play().catch(() => {});
  updatePos();
}

function updatePos() {
  const ph = playheadTs() ?? seekTs;
  if (ph == null || !TL) return;
  const behind = Math.round(TL.now_utc_live() - TL.delay - ph);
  $("pos").textContent =
    `${playing ? "playing" : "paused at"} ${kindAt(ph)} — ${fmtDub(ph + TL.delay)} Irish time` +
    (behind > 15 ? ` (aired ${fmtDur(behind)} ago)`
     : behind < -15 ? ` (airs in ${fmtDur(-behind)})`
     : " (on air now)");
}

$("btn-play").addEventListener("click", () => {
  const a = $("audio");
  playing = !playing;
  $("btn-play").textContent = playing ? "⏸ Pause" : "▶ Play";
  if (playing) { if (!a) seekTo(TL ? airingNow() : Date.now() / 1000 - 7200, true);
                 else a.play().catch(() => {}); }
  else if (a) a.pause();
});
$("btn-live").addEventListener("click", () => {
  if (TL) seekTo(airingNow(), true);
});
$("btn-back").addEventListener("click", () => seekTo((seekTs ?? airingNow()) - 60, false));
$("btn-fwd").addEventListener("click", () => {
  const a = TL ? airingNow() : seekTs;
  const newest = TL ? TL.newest_ts - 60 : a;
  seekTo(Math.min((seekTs ?? a) + 60, newest), false);
});

/* ---------------- polling ---------------- */

async function refreshOverview() {
  try {
    const o = await api("/monitor/api/overview");
    renderCards(o);
    renderEvents(o);
    $("ck-dub").textContent = o.dublin.slice(11, 19);
    $("ck-cai").textContent = o.cairo.slice(11, 19);
    $("ck-utc").textContent = fmtE(o.now_utc).slice(11, 19);
  } catch (e) { console.warn("overview:", e.message); }
}

async function refreshTimeline() {
  try {
    TL = await api("/monitor/api/timeline?hours=4.5");
    const fetchedAt = Date.now() / 1000, serverNow = TL.now_utc;
    TL.now_utc_live = () => serverNow + (Date.now() / 1000 - fetchedAt);
    $("ck-air").textContent = new Date(airingNow() * 1000).toLocaleTimeString("en-GB",
      { timeZone: "Africa/Cairo", hour: "2-digit", minute: "2-digit" });
    drawTimeline();
    renderComing();
    $("tl-ahead").textContent = (TL.delay / 3600).toFixed(1).replace(/\.0$/, "");
    if ($("btn-live").classList.contains("on")) { seekTs = airingNow(); }
    updatePos();
  } catch (e) { console.warn("timeline:", e.message); }
}

window.addEventListener("resize", drawTimeline);
refreshOverview(); refreshTimeline();
setInterval(refreshOverview, 10000);
setInterval(refreshTimeline, 30000);
setInterval(() => {                  // move the playhead; stop at selection end
  const ph = playheadTs();
  if (stopAt != null && ph != null && ph >= stopAt && playing) {
    stopAt = null;
    $("btn-play").click();
  }
  drawTimeline(); updatePos();
}, 500);
