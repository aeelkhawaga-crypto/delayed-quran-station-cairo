/* Media library panel: list / preview / upload / trash / restore. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const HINT = {
    adhan: "Played at Irish prayer times, one file per prayer in rotation.",
    fajr: "Used for Fajr only, in their own rotation (falls back to Adhans if empty).",
    filler: "Played over the Cairo adhan (and to cover long recorder gaps), in rotation.",
    starter: "A short lead-in played just before that prayer's adhan, so the adhan starts on time.",
  };
  let kind = "adhan", M = null, playingKey = null, notice = null;

  const fmtDur = s => s == null ? "—" : s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`;
  const fmtSize = b => b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB";
  const fmtDub = e => new Date(e * 1000).toLocaleString("en-GB", { timeZone: "Europe/Dublin",
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  const esc = t => String(t).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const qs = o => Object.entries(o).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

  async function load() {
    try {
      const r = await fetch("/monitor/api/media", { credentials: "same-origin" });
      M = await r.json();
      render();
    } catch (e) { console.warn("media:", e); }
  }

  async function action(path, params) {
    const r = await fetch(`/monitor/api/media/${path}?${qs(params)}`, {
      method: "POST", credentials: "same-origin", headers: { "X-Monitor-Action": "1" } });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }

  function upload(file, extra) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open("POST", `/monitor/api/media/upload?${qs({ kind, name: file.name, ...extra })}`);
      x.setRequestHeader("X-Monitor-Action", "1");
      x.setRequestHeader("Content-Type", "application/octet-stream");
      const pr = $("media-progress");
      pr.hidden = false; pr.value = 0;
      x.upload.onprogress = e => { if (e.lengthComputable) pr.value = e.loaded / e.total; };
      x.onload = () => {
        pr.hidden = true;
        let j = {};
        try { j = JSON.parse(x.responseText); } catch (e) {}
        x.status < 300 ? resolve(j) : reject(new Error(j.error || `HTTP ${x.status}`));
      };
      x.onerror = () => { pr.hidden = true; reject(new Error("network error")); };
      x.send(file);
    });
  }

  function say(type, text) { notice = { type, text }; renderNotices(); }

  function renderNotices() {
    const d = M && M[kind];
    let h = "";
    if (d && d.busy) h += `<div class="notice warn">Changes paused: ${esc(d.busy)}.</div>`;
    if (d && d.preparing) h += `<div class="notice info">Preparing audio for broadcast… (about a minute; the previous set stays in use until it's ready)</div>`;
    if (notice) h += `<div class="notice ${notice.type}">${esc(notice.text)}</div>`;
    $("media-notices").innerHTML = h;
  }

  function playBtn(k, name, trash) {
    const key = `${k}|${name}|${trash ? 1 : 0}`;
    return `<button data-play="${esc(key)}">${playingKey === key ? "⏸" : "▶"}</button>`;
  }

  function render() {
    if (!M) return;
    const d = M[kind];
    document.querySelectorAll("#media-tabs button").forEach(b => {
      const n = M[b.dataset.k].files.length;
      b.classList.toggle("on", b.dataset.k === kind);
      b.textContent = b.textContent.replace(/ \(\d+\)$/, "") + ` (${n})`;
    });
    $("media-hint").textContent = HINT[kind] + ` Allowed length ${fmtDur(d.limits[0])}–${fmtDur(d.limits[1])}.`;
    $("media-upload-wrap").style.display = kind === "starter" ? "none" : "";
    renderNotices();
    const thead = document.querySelector("#tbl-media thead"), tb = document.querySelector("#tbl-media tbody");
    tb.innerHTML = "";
    if (kind === "starter") {
      thead.innerHTML = "<tr><th>Prayer</th><th></th><th>File</th><th>Length</th><th></th></tr>";
      for (const p of M.prayers) {
        const f = d.files.find(x => x.prayer === p);
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${p[0].toUpperCase() + p.slice(1)}</td>
          <td>${f ? playBtn(kind, f.name) : ""}</td>
          <td>${f ? esc(f.name) : '<span class="dim">none — adhan starts directly</span>'}</td>
          <td>${f ? fmtDur(f.duration) : ""}</td>
          <td><label class="upload"><button type="button" data-pick="${p}">${f ? "Replace…" : "Upload…"}</button>
              <input type="file" data-prayer="${p}" accept="audio/*,.mp3,.m4a,.aac,.wav,.ogg,.flac" hidden></label>
              ${f ? `<button class="danger" data-trash="${esc(f.name)}">🗑</button>` : ""}</td>`;
        tb.appendChild(tr);
      }
    } else {
      thead.innerHTML = "<tr><th></th><th>File</th><th>Length</th><th>Size</th><th></th><th></th></tr>";
      if (!d.files.length) tb.innerHTML = `<tr><td colspan="6" class="dim">no files${kind === "fajr" ? " — Fajr uses the Adhans pool" : ""}</td></tr>`;
      for (const f of d.files) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${playBtn(kind, f.name)}</td><td>${esc(f.name)}</td>
          <td>${fmtDur(f.duration)}</td><td class="dim">${fmtSize(f.size)}</td>
          <td>${f.next ? '<span class="pill spliced">plays next</span>' : ""}</td>
          <td><button class="danger" data-trash="${esc(f.name)}">🗑</button></td>`;
        tb.appendChild(tr);
      }
    }
    $("trash-n").textContent = d.trash.length;
    const tt = document.querySelector("#tbl-trash tbody");
    tt.innerHTML = d.trash.length ? "" : '<tr><td colspan="3" class="dim">empty</td></tr>';
    for (const t of d.trash) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${esc(t.orig)}</td><td class="dim">${fmtDub(t.deleted)}</td>
        <td>${playBtn(kind, t.name, true)} <button data-restore="${esc(t.name)}">↩ Restore</button></td>`;
      tt.appendChild(tr);
    }
  }

  // ---- events ----
  $("media-tabs").addEventListener("click", e => {
    const b = e.target.closest("button"); if (!b) return;
    kind = b.dataset.k; notice = null; render();
  });
  $("media-upload-btn").addEventListener("click", () => $("media-file").click());
  $("media-file").addEventListener("change", async e => {
    const files = [...e.target.files]; e.target.value = "";
    for (const [i, f] of files.entries()) {
      say("info", `Uploading ${f.name} (${i + 1}/${files.length})…`);
      try { await upload(f, {}); say("ok", `Added ${f.name}. It goes on air once prepared.`); }
      catch (err) { say("err", `${f.name}: ${err.message}`); break; }
    }
    load();
  });
  document.addEventListener("click", async e => {
    const t = e.target;
    if (t.dataset.pick) { t.parentNode.querySelector("input").click(); return; }
    if (t.dataset.play) {
      const a = $("media-audio"), [k, name, tr] = t.dataset.play.split("|");
      if (playingKey === t.dataset.play) { a.pause(); playingKey = null; render(); return; }
      a.src = `/monitor/api/media/file?${qs({ kind: k, name, trash: tr })}`;
      a.play().catch(() => {}); playingKey = t.dataset.play; render();
      a.onended = () => { playingKey = null; render(); };
      return;
    }
    if (t.dataset.trash) {
      if (!confirm(`Move "${t.dataset.trash}" to the trash? It stops being used right away (you can restore it).`)) return;
      try { await action("trash", { kind, name: t.dataset.trash }); say("ok", `Moved ${t.dataset.trash} to trash.`); }
      catch (err) { say("err", err.message); }
      load(); return;
    }
    if (t.dataset.restore) {
      try { const j = await action("restore", { kind, name: t.dataset.restore }); say("ok", `Restored ${j.name}.`); }
      catch (err) { say("err", err.message); }
      load();
    }
  });
  document.addEventListener("change", async e => {
    const inp = e.target;
    if (!inp.dataset || !inp.dataset.prayer || !inp.files.length) return;
    const f = inp.files[0]; inp.value = "";
    say("info", `Uploading ${f.name} as the ${inp.dataset.prayer} starter…`);
    try { await upload(f, { prayer: inp.dataset.prayer }); say("ok", `${inp.dataset.prayer} starter updated.`); }
    catch (err) { say("err", err.message); }
    load();
  });

  load();
  setInterval(load, 10000);
})();
