/* a7v Tether – web UI (no dependencies, works offline) */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const h = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name) => `<svg><use href="#i-${name}"/></svg>`;

const state = {
  status: null,
  local: true,
  instance: null,
  recent: [],           // newest first
  history: { offset: 0, limit: 50, total: 0, items: [] },
  settings: null,
  meta: null,
  original: "",
  logs: [],
  logLevel: "",
  lb: { list: [], index: 0 },
  seen: new Set(),
};

/* ---------------------------------------------------------- prefs ---- */
const prefs = (() => {
  let data = {};
  try { data = JSON.parse(localStorage.getItem("a7v-prefs") || "{}"); } catch (_) { /* storage blocked */ }
  return {
    get: (k, d) => (k in data ? data[k] : d),
    set: (k, v) => { data[k] = v; try { localStorage.setItem("a7v-prefs", JSON.stringify(data)); } catch (_) { /* ignore */ } },
  };
})();
function applyTheme() {
  const t = prefs.get("theme", "");
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
}
applyTheme();

/* ------------------------------------------------------------ api ---- */
class ApiError extends Error { constructor(msg, status, data) { super(msg); this.status = status; this.data = data; } }
async function api(method, path, body) {
  const opts = { method, headers: { "X-A7V": "1" }, credentials: "same-origin" };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (res.status === 401) { showLogin(); throw new ApiError("PIN erforderlich", 401, data); }
  if (!res.ok && res.status !== 422) throw new ApiError((data && data.error) || res.statusText, res.status, data);
  return data;
}

/* -------------------------------------------------------- format ---- */
const nf1 = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });
const nf0 = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 0 });
function fmtBytes(b) {
  if (b == null) return "–";
  if (b < 1e6) return `${nf0.format(b / 1e3)} KB`;
  if (b < 1e9) return `${nf1.format(b / 1e6)} MB`;
  return `${nf1.format(b / 1e9)} GB`;
}
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" });
function fmtAgo(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 10) return "gerade eben";
  if (s < 60) return `vor ${Math.round(s)} s`;
  if (s < 3600) return `vor ${Math.round(s / 60)} min`;
  if (s < 86400) return `vor ${Math.round(s / 3600)} h`;
  return fmtDate(ts);
}
const fmtSec = (v) => (v == null ? "–" : v < 0.1 ? "< 0,1 s" : `${nf1.format(v)} s`);
function exifSummary(e) {
  if (!e) return "";
  const p = [];
  if (e.exposure) p.push(`${e.exposure} s`);
  if (e.fnumber) p.push(`f/${String(e.fnumber).replace(".", ",")}`);
  if (e.iso) p.push(`ISO ${e.iso}`);
  if (e.focal_mm) p.push(`${nf0.format(e.focal_mm)} mm`);
  return p.join(" · ");
}
const STATUS = {
  imported: ["Übergeben", "imported"], held: ["Wartet", "held"], duplicate: ["Duplikat", "duplicate"],
  ignored: ["Nicht übergeben", "ignored"], failed: ["Fehler", "failed"], processing: ["Verarbeite …", ""],
};
function badge(t) {
  if (t.status === "imported" && t.lr_picked_up) return `<span class="badge lr" title="Von Lightroom übernommen um ${fmtTime(t.lr_picked_up)}">${icon("check")}In Lightroom</span>`;
  const [label, cls] = STATUS[t.status] || [t.status, ""];
  return `<span class="badge ${cls}" title="${h(t.message || "")}">${label}</span>`;
}
const imgUrl = (t, kind) => `/api/image/${t.id}/${kind}`;
function orientClass(t) {
  if (state.status && state.status.features.thumbs_oriented) return "";
  return { 3: "rot180", 6: "rot90", 8: "rot270" }[(t.exif || {}).orientation] || "";
}

/* ---------------------------------------------------------- toast ---- */
function toast(text, kind = "info", ms = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `${icon(kind === "ok" ? "check" : kind === "info" ? "info" : "warn")}<div>${h(text)}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 300); }, ms);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove();
  }
  toast(`Kopiert: ${text}`, "ok", 1800);
}

/* -------------------------------------------------------- routing ---- */
const VIEWS = ["live", "history", "camera", "settings", "log"];
function currentView() {
  const v = location.hash.slice(1);
  if (!VIEWS.includes(v)) return "live";
  if (!state.local && (v === "settings" || v === "camera")) return "live";
  return v;
}
function route() {
  const v = currentView();
  VIEWS.forEach((name) => { $(`#view-${name}`).hidden = name !== v; });
  $$(".sidebar a").forEach((a) => a.classList.toggle("active", a.dataset.view === v));
  if (v === "history") loadHistory();
  if (v === "settings" && !state.settings) loadSettings();
  if (v === "camera") { loadSettings().then(renderCamera); }
  if (v === "log") loadLogs();
  window.scrollTo({ top: 0 });
}
window.addEventListener("hashchange", route);

/* --------------------------------------------------------- status ---- */
async function refreshStatus() {
  try {
    const st = await api("GET", "/api/status");
    renderStatus(st);
  } catch (e) { if (e.status !== 401) console.warn(e); }
}

function setPill(id, cls, text, title = "") {
  const el = $(id);
  el.className = `pill ${cls}`;
  el.lastElementChild.textContent = text;
  el.title = title;
}

function renderStatus(st) {
  const first = !state.status;
  state.status = st;
  if (state.local !== st.client.local) {
    state.local = st.client.local;
    $$("[data-local]").forEach((el) => { el.hidden = !state.local; });
    if (!state.local) route();
  }
  $("#version").textContent = `Version ${st.version}`;
  const si = $("#session-name");
  if (document.activeElement !== si) si.value = st.session;

  const f = st.ftp;
  if (f.error) setPill("#pill-ftp", "err", "FTP Fehler", f.error);
  else if (f.running) setPill("#pill-ftp", "ok", `FTP${f.tls ? "S" : ""} · Port ${f.port}`, "FTP-Server läuft");
  else setPill("#pill-ftp", "warn", "FTP gestoppt");

  const cams = f.cameras.filter((c) => c.authenticated);
  const lastTs = state.recent.length ? state.recent[0].received_at : null;
  if (f.uploads.length) setPill("#pill-camera", "busy", "Kamera sendet …", f.uploads.map((u) => u.ip).join(", "));
  else if (cams.length) setPill("#pill-camera", "ok", "Kamera verbunden", cams.map((c) => c.ip).join(", "));
  else if (lastTs && Date.now() / 1000 - lastTs < 600) setPill("#pill-camera", "", `Kamera · ${fmtAgo(lastTs)}`, "Letzte Übertragung");
  else setPill("#pill-camera", "", "Keine Kamera");

  const lr = st.lightroom;
  const lrMap = {
    ok: ["ok", "Lightroom übernimmt", lr.last_delay_s != null ? `Zuletzt nach ${nf1.format(lr.last_delay_s)} s übernommen` : ""],
    waiting: ["busy", "Lightroom …", `${lr.pending} Bild(er) warten auf Lightroom`],
    stalled: ["warn", "Lightroom reagiert nicht", "Auto-Import in Lightroom aktivieren"],
    unknown: ["", "Lightroom", "Noch keine Übergabe"],
  };
  setPill("#pill-lr", ...lrMap[lr.status]);

  const pb = $("#btn-pause");
  pb.classList.toggle("paused", st.paused);
  pb.innerHTML = st.paused ? `${icon("play")}<span>Fortsetzen</span>` : `${icon("pause")}<span>Pausieren</span>`;
  pb.title = st.paused ? "Gesammelte Bilder an Lightroom übergeben" : "Bilder sammeln statt sofort an Lightroom zu geben";

  const s = st.stats;
  $("#k-session").textContent = nf0.format(s.session_count);
  const extra = [];
  if (s.by_status.held) extra.push(`${s.by_status.held} wartend`);
  if (s.by_status.duplicate) extra.push(`${s.by_status.duplicate} Duplikate`);
  if (s.by_status.failed) extra.push(`${s.by_status.failed} Fehler`);
  $("#k-session-sub").textContent = extra.join(" · ") || (st.queue ? `${st.queue} in Verarbeitung` : "an Lightroom übergeben");
  $("#k-today").textContent = nf0.format(s.today_count);
  $("#k-total").textContent = `${nf0.format(s.total_count)} insgesamt`;
  $("#k-bytes").textContent = fmtBytes(s.session_bytes);
  $("#k-avg").textContent = s.session_count ? `Ø ${fmtBytes(s.session_bytes / s.session_count)} pro Bild` : "";
  $("#k-speed").textContent = s.avg_mbps ? `${nf1.format(s.avg_mbps)} Mbit/s` : "–";
  $("#k-upload").textContent = s.avg_upload_s != null ? `Ø ${fmtSec(s.avg_upload_s)} pro Bild` : "";
  const d = st.disks.watch;
  const kd = $("#k-disk").parentElement;
  if (d) {
    $("#k-disk").textContent = fmtBytes(d.free);
    const perImg = s.session_count ? s.session_bytes / s.session_count : 50e6;
    $("#k-disk-sub").textContent = `≈ ${nf0.format(d.free / perImg)} weitere Bilder`;
    kd.classList.toggle("alert", st.warnings.some((w) => w.text.startsWith("Wenig Speicher")));
  }

  const wc = $("#card-warnings");
  wc.hidden = !st.warnings.length;
  $("#warnings").innerHTML = st.warnings.map((w) => `<li class="${w.level}">${icon(w.level === "info" ? "info" : "warn")}<span>${h(w.text)}</span></li>`).join("");

  $("#uploads").innerHTML = f.uploads.length
    ? f.uploads.map((u) => `<div class="upload"><div class="upload-head"><b>${h(u.name)}</b><span>${fmtBytes(u.bytes)} · ${nf1.format(u.mbps)} Mbit/s</span></div><div class="bar"><i></i></div></div>`).join("")
    : st.queue ? `<p class="muted">${st.queue} Datei(en) in Verarbeitung …</p>` : `<p class="muted">Keine laufende Übertragung</p>`;

  const ips = f.ips.length ? f.ips : ["–"];
  const rows = [
    ["Adresse", `${h(ips[0])} <button class="btn ghost icon small" data-copy="${h(ips[0])}" title="Kopieren">${icon("copy")}</button>`],
    ["Port", f.port + (f.tls ? " (FTPS)" : "")],
    ["Empfangen", `${f.files_received} seit Start`],
  ];
  if (ips.length > 1) rows.splice(1, 0, ["Weitere", h(ips.slice(1).join(", "))]);
  if (state.local && st.folders.watch) rows.push(["Ordner", `<button class="btn ghost small" data-open="watch">${icon("folder")}<span>Lightroom-Ordner</span></button>`]);
  if (lr.files_in_watch_dir) rows.push(["Im Ordner", `${lr.files_in_watch_dir} nicht abgeholt`]);
  $("#conn-info").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

  if (state.instance && st.instance !== state.instance) { state.instance = st.instance; loadRecent(); }
  state.instance = st.instance;

  const sel = $("#h-session");
  const cur = sel.value;
  const opts = ['<option value="">Alle Sessions</option>'].concat(s.sessions.map((x) => `<option${x === cur ? " selected" : ""}>${h(x)}</option>`));
  if (sel.options.length !== opts.length) sel.innerHTML = opts.join("");

  if (currentView() === "camera") renderCamera();
  if (first) route();
}

/* ------------------------------------------------------- live feed ---- */
async function loadRecent() {
  const data = await api("GET", "/api/transfers?limit=48");
  state.recent = data.items;
  data.items.forEach((t) => state.seen.add(t.id));
  renderStrip();
  renderHero();
}

function upsertRecent(t) {
  const i = state.recent.findIndex((x) => x.id === t.id);
  if (i >= 0) state.recent[i] = t; else { state.recent.unshift(t); state.recent.length = Math.min(state.recent.length, 48); }
  state.recent.sort((a, b) => b.id - a.id);
}

function tileHtml(t, isNew) {
  const img = t.has_thumb ? `<img src="${imgUrl(t, "thumb")}" alt="" loading="lazy" class="${orientClass(t)}">` : `.${h((t.ext || "").toUpperCase())}`;
  return `<button class="tile${isNew ? " new" : ""}" data-id="${t.id}" title="${h(t.original_name)}">
    <div class="ph">${img}</div>${badge(t)}
    <div class="cap"><b>${h(t.final_name || t.original_name)}</b><span>${fmtTime(t.received_at)}</span></div></button>`;
}

function renderStrip(newId) {
  const el = $("#strip");
  el.innerHTML = state.recent.length
    ? state.recent.map((t) => tileHtml(t, t.id === newId)).join("")
    : `<p class="muted">Noch keine Bilder in dieser Ansicht.</p>`;
}

function renderHero() {
  const t = state.recent.find((x) => x.has_thumb) || state.recent[0];
  const box = $("#hero-img");
  $("#hero-empty").hidden = !!t;
  $("#hero-meta").hidden = !t;
  box.querySelectorAll("img, .badge").forEach((n) => n.remove());
  if (!t) return;
  if (t.has_thumb) {
    const img = new Image();
    img.alt = t.original_name;
    img.className = orientClass(t);
    img.src = imgUrl(t, "preview");
    box.appendChild(img);
  }
  box.insertAdjacentHTML("beforeend", badge(t));
  box.dataset.id = t.id;
  const renamed = t.final_name && t.final_name !== t.original_name;
  $("#hero-name").textContent = t.final_name || t.original_name;
  $("#hero-sub").textContent = `${renamed ? `${t.original_name} · ` : ""}${fmtTime(t.received_at)} · ${fmtBytes(t.size)}${t.upload_s != null ? ` · Upload ${fmtSec(t.upload_s)}` : ""}`;
  const e = t.exif || {};
  const items = [
    ["Belichtung", e.exposure ? `${e.exposure} s` : ""],
    ["Blende", e.fnumber ? `f/${String(e.fnumber).replace(".", ",")}` : ""],
    ["ISO", e.iso || ""],
    ["Brennweite", e.focal_mm ? `${nf0.format(e.focal_mm)} mm` : ""],
    ["Korrektur", e.bias_ev ? `${e.bias_ev > 0 ? "+" : ""}${nf1.format(e.bias_ev)} EV` : ""],
    ["Objektiv", e.lens || ""],
    ["Kamera", e.model || ""],
  ].filter(([, v]) => v !== "");
  $("#hero-exif").innerHTML = items.map(([k, v]) => `<div><dt>${k}</dt><dd>${h(v)}</dd></div>`).join("");
}

let sparkTimer = null;
async function loadTimeline() {
  try {
    const { buckets } = await api("GET", "/api/timeline?minutes=60");
    const max = Math.max(1, ...buckets);
    const w = 300 / buckets.length;
    $("#spark").innerHTML = buckets.map((v, i) => {
      const bh = v ? Math.max(4, (v / max) * 60) : 2;
      return `<rect class="${v ? "" : "zero"}" x="${i * w + 0.6}" y="${64 - bh}" width="${w - 1.2}" height="${bh}" rx="1.2"><title>${v} Bild(er)</title></rect>`;
    }).join("");
    $("#spark-max").textContent = max > 1 ? `max. ${max}/min` : "";
  } catch (_) { /* offline */ }
}
function scheduleTimeline() { clearTimeout(sparkTimer); sparkTimer = setTimeout(loadTimeline, 800); }

/* -------------------------------------------------- notifications ---- */
let audioCtx = null;
function beep() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(); const g = audioCtx.createGain();
    o.type = "sine"; o.frequency.value = 880;
    g.gain.setValueAtTime(0.0001, audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.12, audioCtx.currentTime + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.18);
    o.connect(g).connect(audioCtx.destination); o.start(); o.stop(audioCtx.currentTime + 0.2);
  } catch (_) { /* no audio */ }
}
function notifyNew(t) {
  if (prefs.get("sound", false)) beep();
  if (prefs.get("notify", false) && document.hidden && "Notification" in window && Notification.permission === "granted") {
    const n = new Notification(t.final_name || t.original_name, { body: exifSummary(t.exif) || "Neues Bild empfangen", icon: t.has_thumb ? imgUrl(t, "thumb") : "/icon.svg", tag: "a7v" });
    setTimeout(() => n.close(), 5000);
  }
}

/* ------------------------------------------------------------ SSE ---- */
function connectEvents() {
  const es = new EventSource("/api/events");
  es.addEventListener("hello", (ev) => {
    $("#conn-banner").hidden = true;
    const { instance } = JSON.parse(ev.data);
    if (state.instance && instance !== state.instance) { loadRecent(); }
    refreshStatus();
  });
  es.addEventListener("transfer", (ev) => {
    const t = JSON.parse(ev.data);
    const isNew = !state.seen.has(t.id);
    state.seen.add(t.id);
    upsertRecent(t);
    renderStrip(isNew ? t.id : null);
    renderHero();
    if (isNew && t.status !== "processing") {
      if (t.status === "imported" || t.status === "held") notifyNew(t);
      if (t.status === "failed") toast(`Fehler bei ${t.original_name}: ${t.message}`, "error", 8000);
    }
    if (currentView() === "history" && state.history.offset === 0) loadHistory();
    scheduleTimeline();
    refreshStatus();
  });
  es.addEventListener("lightroom", (ev) => {
    const d = JSON.parse(ev.data);
    const t = state.recent.find((x) => x.id === d.id);
    if (t && d.picked_up) { t.lr_picked_up = Date.now() / 1000; renderStrip(); renderHero(); }
    if (!d.picked_up) toast("Lightroom übernimmt keine Bilder – ist „Automatischen Import aktivieren“ eingeschaltet?", "warn", 9000);
    refreshStatus();
  });
  es.addEventListener("aborted", (ev) => { const d = JSON.parse(ev.data); toast(`Übertragung abgebrochen: ${d.name} – die Kamera sendet erneut`, "warn"); });
  es.addEventListener("log", (ev) => { addLog(JSON.parse(ev.data)); });
  ["camera", "ftp", "paused", "session"].forEach((k) => es.addEventListener(k, () => refreshStatus()));
  es.addEventListener("session", () => loadRecent());
  es.addEventListener("settings", () => { if (!state.original || !isDirty()) { state.settings = null; if (["settings", "camera"].includes(currentView())) loadSettings(); } });
  es.onerror = () => { $("#conn-banner").hidden = false; };
}

/* -------------------------------------------------------- history ---- */
let historyTimer = null;
function historyQuery() {
  const p = new URLSearchParams();
  const q = $("#h-q").value.trim(); const st = $("#h-status").value; const se = $("#h-session").value;
  if (q) p.set("q", q); if (st) p.set("status", st); if (se) p.set("session", se);
  return p;
}
async function loadHistory() {
  const p = historyQuery();
  $("#h-export").href = `/api/transfers.csv?${p}`;
  p.set("limit", state.history.limit); p.set("offset", state.history.offset);
  const data = await api("GET", `/api/transfers?${p}`);
  Object.assign(state.history, data);
  $("#h-empty").hidden = data.items.length > 0;
  $("#h-rows").innerHTML = data.items.map((t) => {
    const renamed = t.final_name && t.final_name !== t.original_name;
    const e = t.exif || {};
    return `<tr data-id="${t.id}">
      <td>${t.has_thumb ? `<img class="thumb ${orientClass(t)}" src="${imgUrl(t, "thumb")}" alt="" loading="lazy">` : `<div class="thumb"></div>`}</td>
      <td class="time">${fmtTime(t.received_at)}<br><span class="muted small">${fmtDate(t.received_at)}</span></td>
      <td class="file"><b>${h(t.final_name || t.original_name)}</b><span>${renamed ? `aus ${h(t.original_name)} · ` : ""}${h(t.session)}</span></td>
      <td class="shot hide-sm">${h(exifSummary(e)) || "–"}<br><span>${h(e.lens || e.model || "")}</span></td>
      <td class="num hide-sm">${fmtBytes(t.size)}</td>
      <td class="num hide-sm">${fmtSec(t.upload_s)}</td>
      <td>${badge(t)}</td></tr>`;
  }).join("");
  const page = Math.floor(state.history.offset / state.history.limit) + 1;
  const pages = Math.max(1, Math.ceil(data.total / state.history.limit));
  $("#h-page").textContent = `Seite ${page} von ${pages} · ${nf0.format(data.total)} Einträge`;
  $("#h-prev").disabled = page <= 1; $("#h-next").disabled = page >= pages;
}
["#h-status", "#h-session"].forEach((s) => $(s).addEventListener("change", () => { state.history.offset = 0; loadHistory(); }));
$("#h-q").addEventListener("input", () => { clearTimeout(historyTimer); historyTimer = setTimeout(() => { state.history.offset = 0; loadHistory(); }, 250); });
$("#h-prev").addEventListener("click", () => { state.history.offset = Math.max(0, state.history.offset - state.history.limit); loadHistory(); });
$("#h-next").addEventListener("click", () => { state.history.offset += state.history.limit; loadHistory(); });
$("#h-rows").addEventListener("click", (ev) => {
  const tr = ev.target.closest("tr[data-id]");
  if (tr) openLightbox(state.history.items, Number(tr.dataset.id));
});

/* ------------------------------------------------------- lightbox ---- */
function openLightbox(list, id) {
  const items = list.filter((t) => t.has_thumb);
  const index = items.findIndex((t) => t.id === id);
  if (index < 0) { toast("Für diese Datei gibt es keine Vorschau", "info", 2500); return; }
  state.lb = { list: items, index };
  $("#lightbox").hidden = false;
  showLightbox();
}
function showLightbox() {
  const t = state.lb.list[state.lb.index];
  const img = $("#lb-img");
  img.className = orientClass(t);
  img.src = imgUrl(t, "preview");
  img.alt = t.original_name;
  const e = t.exif || {};
  const parts = [exifSummary(e), e.lens, `${fmtDate(t.received_at)} ${fmtTime(t.received_at)}`, fmtBytes(t.size)].filter(Boolean);
  $("#lb-cap").innerHTML = `<b>${h(t.final_name || t.original_name)}</b> ${badge(t)}<br>${h(parts.join(" · "))}
    ${state.local ? `<br><button class="btn ghost small" data-open-transfer="${t.id}">${icon("folder")}<span>Im Ordner zeigen</span></button>` : ""}`;
  $("#lb-prev").disabled = state.lb.index >= state.lb.list.length - 1;
  $("#lb-next").disabled = state.lb.index <= 0;
}
function closeLightbox() { $("#lightbox").hidden = true; $("#lb-img").removeAttribute("src"); }
$("#lb-close").addEventListener("click", closeLightbox);
$("#lb-prev").addEventListener("click", () => { if (state.lb.index < state.lb.list.length - 1) { state.lb.index++; showLightbox(); } });
$("#lb-next").addEventListener("click", () => { if (state.lb.index > 0) { state.lb.index--; showLightbox(); } });
$("#lightbox").addEventListener("click", (ev) => { if (ev.target.id === "lightbox" || ev.target.tagName === "FIGURE") closeLightbox(); });
document.addEventListener("keydown", (ev) => {
  if ($("#lightbox").hidden) return;
  if (ev.key === "Escape") closeLightbox();
  if (ev.key === "ArrowLeft") $("#lb-prev").click();
  if (ev.key === "ArrowRight") $("#lb-next").click();
});
$("#strip").addEventListener("click", (ev) => { const b = ev.target.closest(".tile"); if (b) openLightbox(state.recent, Number(b.dataset.id)); });
$("#hero-img").addEventListener("click", () => { const id = Number($("#hero-img").dataset.id); if (id) openLightbox(state.recent, id); });

/* --------------------------------------------------------- camera ---- */
function renderCamera() {
  const st = state.status; const s = state.settings;
  if (!st || !s) return;
  const ip = st.ftp.ips[0] || "–";
  const row = (k, v, copy = true) => `<dt>${k}</dt><dd><span>${h(v)}</span>${copy ? `<button class="btn ghost icon small" data-copy="${h(v)}" title="Kopieren">${icon("copy")}</button>` : ""}</dd>`;
  $("#cam-values").innerHTML = [
    row("Hostname", ip), row("Sicheres Protokoll", s.tls_enabled ? "Ein" : "Aus", false), row("Port", String(s.ftp_port), false),
    row("Passiver Modus", "Ein", false), row("Verzeichnis", "(leer lassen)", false),
    row("Benutzername", s.ftp_user), row("Passwort", s.ftp_password),
  ].join("") + (st.ftp.ips.length > 1 ? `<dt>Andere IPs</dt><dd class="muted">${h(st.ftp.ips.slice(1).join(", "))}</dd>` : "");
  $("#cam-tls").hidden = !s.tls_enabled;
  $("#lr-watch").textContent = st.folders.watch || "";
}
$("#btn-selftest").addEventListener("click", async (ev) => {
  const btn = ev.currentTarget; btn.disabled = true;
  try {
    const { results } = await api("POST", "/api/selftest", {});
    $("#selftest-card").hidden = false;
    const ic = { ok: "check", warn: "warn", error: "x", info: "info" };
    $("#selftest").innerHTML = results.map((r) => `<li><span class="${r.status}">${icon(ic[r.status])}</span><b>${h(r.name)}</b><span>${h(r.detail)}</span></li>`).join("");
    const bad = results.filter((r) => r.status === "error").length;
    const hints = results.filter((r) => r.status === "warn").length;
    toast(bad ? `Selbsttest: ${bad} Problem(e) gefunden` : hints ? `Selbsttest bestanden – ${hints} Hinweis(e)` : "Selbsttest bestanden",
      bad ? "error" : hints ? "warn" : "ok");
  } catch (e) { toast(e.message, "error"); }
  btn.disabled = false;
});

/* ------------------------------------------------------- settings ---- */
const form = $("#settings-form");
async function loadSettings() {
  const data = await api("GET", "/api/settings");
  state.settings = data.settings; state.meta = data.meta; state.derived = data.derived;
  fillForm(data.settings);
  return data;
}
function fillForm(s) {
  for (const el of form.elements) {
    if (!el.name || !(el.name in s)) continue;
    const v = s[el.name];
    if (el.type === "checkbox") el.checked = !!v;
    else if (Array.isArray(v)) el.value = v.join(", ");
    else el.value = v ?? "";
  }
  $("#staging-input").placeholder = `automatisch: ${state.derived.staging_path}`;
  $("#ext-chips").innerHTML = state.meta.extensions.map((e) => `<label class="chip"><input type="checkbox" value="${e}" ${s.extensions.includes(e) ? "checked" : ""}><span>${e.toUpperCase()}</span></label>`).join("");
  $("#tokens").innerHTML = state.meta.tokens.map((t) => `<button type="button" data-token="{${t}}">{${t}}</button>`).join("");
  $("#data-dir").textContent = `Daten & Verlauf: ${state.meta.data_dir}`;
  $("#pref-theme").value = prefs.get("theme", "");
  $("#pref-sound").checked = prefs.get("sound", false);
  $("#pref-notify").checked = prefs.get("notify", false);
  renderTls(state.meta.tls);
  clearErrors();
  state.original = JSON.stringify(readForm());
  onFormChange();
}
function readForm() {
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") out[el.name] = el.checked;
    else if (el.type === "number") out[el.name] = el.value === "" ? null : Number(el.value);
    else out[el.name] = el.value;
  }
  out.extensions = $$("#ext-chips input:checked").map((i) => i.value);
  return out;
}
const isDirty = () => state.original && JSON.stringify(readForm()) !== state.original;
function clearErrors() { $$("[data-err]", form).forEach((e) => { e.textContent = ""; }); $$("[aria-invalid]", form).forEach((e) => e.removeAttribute("aria-invalid")); }
function renderTls(tls) {
  let text;
  if (!tls.available) text = "Für FTPS bitte die Pakete „pyopenssl“ und „cryptography“ installieren (in requirements.txt enthalten).";
  else if (!tls.exists) text = "Noch kein Zertifikat. Erzeugen, cacert.pem auf die Speicherkarte kopieren und in der Kamera importieren.";
  else text = `Zertifikat gültig für ${tls.ips.join(", ")} bis ${new Date(tls.valid_until).toLocaleDateString("de-DE")}.`;
  $("#tls-info").textContent = text;
  $("#btn-tls-gen").disabled = !tls.available;
  $("#btn-tls-gen").textContent = tls.exists ? "Zertifikat erneuern" : "Zertifikat erzeugen";
  $("#btn-tls-dl").hidden = !tls.exists;
}
function renderPattern(p, tokens) {
  return p.replace(/\{(\w+)\}/g, (m, k) => (k in tokens ? tokens[k] : m));
}
function onFormChange() {
  const v = readForm();
  $("#savebar").hidden = !isDirty();
  $("#rename-box").classList.toggle("off", !v.rename_enabled);
  const last = state.recent[0];
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const tok = {
    orig: last ? last.original_name.replace(/\.[^.]+$/, "") : "DSC01234",
    seq: "0001", session: v.session_name || (state.status && state.status.session) || "Shooting",
    date: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
    time: `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`,
    camera: (last && last.exif && last.exif.model ? last.exif.model : "Kamera").replace(/\s/g, ""),
  };
  if (state.status) tok.session = state.status.session;
  $("#rename-preview").textContent = `Beispiel: ${renderPattern(v.rename_pattern || "", tok).replace(/[<>:"/\\|?*]/g, "_")}.ARW`;
  const sep = (v.backup_dir || "").includes("\\") ? "\\" : "/";
  $("#backup-preview").textContent = v.backup_dir
    ? `→ ${v.backup_dir.replace(/[\\/]+$/, "")}${sep}${renderPattern(v.backup_structure || "", tok).split(/[\\/]+/).filter(Boolean).join(sep)}${sep}${tok.orig}.ARW`
    : "Keine Sicherung eingestellt";
  const ip = state.status && state.status.ftp.ips[0];
  $("#remote-url").textContent = v.web_remote && ip ? `Am Handy öffnen: http://${ip}:${v.web_port}/` : "";
}
form.addEventListener("input", onFormChange);
form.addEventListener("change", onFormChange);
$("#ext-chips").addEventListener("change", onFormChange);
$("#tokens").addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-token]"); if (!b) return;
  const inp = form.elements.rename_pattern;
  const [a, z] = [inp.selectionStart ?? inp.value.length, inp.selectionEnd ?? inp.value.length];
  inp.value = inp.value.slice(0, a) + b.dataset.token + inp.value.slice(z);
  inp.focus(); inp.setSelectionRange(a + b.dataset.token.length, a + b.dataset.token.length);
  onFormChange();
});
form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#btn-save"); btn.disabled = true;
  try {
    const res = await api("PUT", "/api/settings", readForm());
    clearErrors();
    if (!res.ok) {
      let firstEl = null;
      for (const [k, msg] of Object.entries(res.errors)) {
        const em = $(`[data-err="${k}"]`, form);
        if (em) em.textContent = msg;
        const inp = form.elements[k];
        if (inp && inp.setAttribute) { inp.setAttribute("aria-invalid", "true"); firstEl = firstEl || inp; }
        if (!em) toast(msg, "error");
      }
      if (firstEl && firstEl.focus) firstEl.focus();
      toast("Bitte die markierten Felder prüfen", "error");
    } else {
      state.settings = res.settings;
      await loadSettings();
      toast(res.ftp_restarted ? "Gespeichert – FTP-Server neu gestartet" : "Einstellungen gespeichert", "ok");
      if (res.web_restart_required) toast("Port/Fernzugriff der Weboberfläche gilt nach einem Neustart der App", "warn", 8000);
      refreshStatus();
    }
  } catch (e) { toast(e.message, "error"); }
  btn.disabled = false;
});
$("#btn-discard").addEventListener("click", () => fillForm(state.settings));
window.addEventListener("beforeunload", (ev) => { if (isDirty()) { ev.preventDefault(); ev.returnValue = ""; } });

$("#btn-gen-pw").addEventListener("click", () => {
  const chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const arr = new Uint32Array(10); crypto.getRandomValues(arr);
  const inp = form.elements.ftp_password;
  inp.value = Array.from(arr, (n) => chars[n % chars.length]).join("");
  inp.type = "text"; onFormChange();
  toast("Neues Passwort – nach dem Speichern auch in der Kamera eintragen", "info");
});
form.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button"); if (!t) return;
  if (t.dataset.togglePw) { const i = form.elements[t.dataset.togglePw]; i.type = i.type === "password" ? "text" : "password"; }
  if (t.dataset.pick) {
    const inp = form.elements[t.dataset.pick];
    t.disabled = true;
    try {
      const r = await api("POST", "/api/pick-folder", { initial: inp.value });
      if (r.ok) { inp.value = r.path; onFormChange(); } else if (r.error) toast(r.error, "warn");
    } catch (e) { toast(e.message, "error"); }
    t.disabled = false;
  }
  if (t.dataset.ftp) {
    try {
      const r = await api("POST", "/api/ftp", { action: t.dataset.ftp });
      toast(r.ok ? (r.ftp.running ? "FTP-Server läuft" : "FTP-Server gestoppt") : (r.ftp.error || "Fehler"), r.ok ? "ok" : "error");
      refreshStatus();
    } catch (e) { toast(e.message, "error"); }
  }
});
$("#btn-tls-gen").addEventListener("click", async () => {
  try {
    const r = await api("POST", "/api/tls/generate", {});
    renderTls(r.tls);
    toast("Zertifikat erzeugt – cacert.pem auf die Speicherkarte kopieren und in der Kamera importieren", "ok", 8000);
  } catch (e) { toast(e.message, "error"); }
});
$("#pref-theme").addEventListener("change", (ev) => { prefs.set("theme", ev.target.value); applyTheme(); });
$("#pref-sound").addEventListener("change", (ev) => { prefs.set("sound", ev.target.checked); if (ev.target.checked) beep(); });
$("#pref-notify").addEventListener("change", async (ev) => {
  if (ev.target.checked && "Notification" in window && Notification.permission !== "granted") {
    const p = await Notification.requestPermission();
    if (p !== "granted") { ev.target.checked = false; toast("Benachrichtigungen wurden im Browser blockiert", "warn"); }
  }
  prefs.set("notify", ev.target.checked);
});

/* ------------------------------------------------------------ log ---- */
const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
function logVisible(l) {
  const min = state.logLevel === "" ? 20 : LEVELS[state.logLevel];
  if (state.logLevel === "DEBUG") { /* all */ } else if ((LEVELS[l.level] || 0) < min) return false;
  const q = $("#log-q").value.trim().toLowerCase();
  return !q || l.message.toLowerCase().includes(q);
}
const logLine = (l) => `<div class="${l.level}"><time>${fmtTime(l.ts)}</time><span class="lvl">${l.level === "WARNING" ? "WARN" : l.level}</span><span>${h(l.message)}</span></div>`;
function renderLogs() {
  const el = $("#log");
  el.innerHTML = state.logs.filter(logVisible).map(logLine).join("") || `<p class="muted" style="padding:0 16px">Keine Einträge</p>`;
  if ($("#log-follow").checked) el.scrollTop = el.scrollHeight;
}
async function loadLogs() { const { items } = await api("GET", "/api/logs?limit=1000"); state.logs = items; renderLogs(); }
function addLog(l) {
  state.logs.push(l); if (state.logs.length > 1500) state.logs.splice(0, 500);
  if (currentView() !== "log" || !logVisible(l)) return;
  const el = $("#log");
  if (el.querySelector("p")) el.innerHTML = "";
  el.insertAdjacentHTML("beforeend", logLine(l));
  if ($("#log-follow").checked) el.scrollTop = el.scrollHeight;
}
$("#log-level").addEventListener("click", (ev) => {
  const b = ev.target.closest("button"); if (!b) return;
  state.logLevel = b.dataset.level;
  $$("#log-level button").forEach((x) => x.classList.toggle("on", x === b));
  renderLogs();
});
$("#log-q").addEventListener("input", renderLogs);

/* ------------------------------------------------------- top bar ---- */
$("#btn-pause").addEventListener("click", async () => {
  const paused = !(state.status && state.status.paused);
  try {
    const r = await api("POST", "/api/pause", { paused });
    toast(paused ? "Pausiert – neue Bilder werden gesammelt" : `Fortgesetzt – ${r.released} Bild(er) an Lightroom übergeben`, "info");
    refreshStatus();
  } catch (e) { toast(e.message, "error"); }
});
async function setSession(name) {
  try { await api("POST", "/api/session", { name }); toast(`Session: ${name || "neu"}`, "ok", 2200); refreshStatus(); loadRecent(); }
  catch (e) { toast(e.message, "error"); }
}
$("#session-name").addEventListener("keydown", (ev) => { if (ev.key === "Enter") ev.target.blur(); if (ev.key === "Escape") { ev.target.value = state.status.session; ev.target.blur(); } });
$("#session-name").addEventListener("change", (ev) => { const v = ev.target.value.trim(); if (v && v !== state.status.session) setSession(v); });
$("#btn-new-session").addEventListener("click", () => {
  const d = new Date();
  const def = `Shooting ${d.toLocaleDateString("de-DE")} ${d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}`;
  const name = prompt("Name der neuen Session:", def);
  if (name !== null) setSession(name.trim() || def);
});
$("#btn-shutdown").addEventListener("click", async () => {
  if (!confirm("a7v Tether beenden? Die Kamera kann dann keine Bilder mehr senden.")) return;
  try { await api("POST", "/api/shutdown", {}); } catch (_) { /* server gone */ }
  document.body.innerHTML = `<div class="login"><div class="card" style="text-align:center"><h1>Beendet</h1><p class="muted">a7v Tether wurde beendet. Dieses Fenster kann geschlossen werden.</p></div></div>`;
});

/* global click helpers */
document.addEventListener("click", async (ev) => {
  const c = ev.target.closest("[data-copy]"); if (c) { copyText(c.dataset.copy); return; }
  const cf = ev.target.closest("[data-copy-from]"); if (cf) { copyText($(`#${cf.dataset.copyFrom}`).textContent); return; }
  const o = ev.target.closest("[data-open]");
  const ot = ev.target.closest("[data-open-transfer]");
  if (o || ot) {
    try {
      const r = await api("POST", "/api/open", o ? { which: o.dataset.open } : { transfer: Number(ot.dataset.openTransfer) });
      if (!r.ok) toast(r.error || "Konnte Ordner nicht öffnen", "warn");
    } catch (e) { toast(e.message, "error"); }
  }
});

/* ---------------------------------------------------------- login ---- */
function showLogin() { $("#login").hidden = false; setTimeout(() => $("#login-pin").focus(), 50); }
$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const res = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin: $("#login-pin").value.trim() }) });
  if (res.ok) location.reload();
  else { const d = await res.json().catch(() => ({})); $("#login-err").textContent = d.error || "Falsche PIN"; }
});

/* ---------------------------------------------------------- start ---- */
(async function init() {
  try {
    await refreshStatus();
    if (!state.status) return;
    await loadRecent();
    loadTimeline();
    connectEvents();
    setInterval(refreshStatus, 2000);
    setInterval(loadTimeline, 30000);
    setInterval(() => { if (currentView() === "live") renderStrip(); }, 60000);
  } catch (e) { console.error(e); }
})();
