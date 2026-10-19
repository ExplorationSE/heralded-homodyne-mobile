// Harp ADQ32 HHD Mobil (ExplorationSE Harp ADQ32 HHD Mobil) – Web-App des HHD-Mess-Servers. Spricht nur mit dem Server (WebSocket /ws und /api/*).
"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const KC = { vacuum: "--kv", electronic: "--el", herald_blocked: "--kh", heralded: "--a", free_running: "--b", random: "--b", state: "--warn" };
const fmt = (x, d = 3) => (x === null || x === undefined || Number.isNaN(x)) ? "–" : Number(x).toFixed(d);
const fmtE = (x, e, d = 4) => (x === null || x === undefined) ? "–" : `${fmt(x, d)}${e != null ? " ± " + fmt(e, d) : ""}`;

let ws = null, me = null, local = false, S = null, nextId = 1, pending = {}, selSession = null, lastSessions = [];

// ------------------------------------------------------------------ Verbindung
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => $("conn").className = "dot ok";
  ws.onclose = () => { $("conn").className = "dot bad"; setTimeout(connect, 1500); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "hello") { me = m.client; local = m.local; }
    else if (m.type === "status") { S = m; if (m.you_local !== undefined) local = m.you_local; render(); }
    else if (m.type === "ack") {
      const cb = pending[m.id]; delete pending[m.id];
      if (cb) cb(m); else if (!m.ok) toast(m.msg, true);
    }
  };
}
function send(cmd, params = {}, cb = null) {
  if (!ws || ws.readyState !== 1) { toast("keine Verbindung", true); return; }
  const id = nextId++;
  pending[id] = cb || ((m) => toast(m.msg, !m.ok));
  ws.send(JSON.stringify({ type: "command", cmd, params, id, name: navigator.platform || "Browser" }));
}
let toastT = null;
function toast(msg, err = false) {
  const t = $("toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toastT); toastT = setTimeout(() => t.className = "toast hidden", 3500);
}

// ------------------------------------------------------------------ Zeichnen
function canvas(id) {
  const c = $(id), r = window.devicePixelRatio || 1, w = c.clientWidth;
  if (!c.dataset.h) c.dataset.h = c.getAttribute("height");
  const h = c.dataset.h * 1;
  if (c.width !== w * r || c.height !== h * r) { c.width = w * r; c.height = h * r; c.style.height = h + "px"; }
  const g = c.getContext("2d"); g.setTransform(r, 0, 0, r, 0, 0); g.clearRect(0, 0, w, h);
  return { g, w, h };
}
function axes(g, w, h, x0, x1, y0, y1, opt = {}) {
  const L = 40, R = 8, T = 8, B = 22;
  const X = (x) => L + (x - x0) / (x1 - x0) * (w - L - R), Y = (y) => h - B - (y - y0) / (y1 - y0) * (h - T - B);
  g.strokeStyle = css("--line"); g.fillStyle = css("--mut"); g.font = "11px system-ui"; g.lineWidth = 1;
  g.beginPath(); g.moveTo(L, T); g.lineTo(L, h - B); g.lineTo(w - R, h - B); g.stroke();
  const ticks = (a, b, n) => { const s = Math.pow(10, Math.floor(Math.log10((b - a) / n))); const st = [1, 2, 5, 10].map(k => k * s).find(k => (b - a) / k <= n) || s; const o = []; for (let v = Math.ceil(a / st) * st; v <= b + 1e-12; v += st) o.push(+v.toFixed(10)); return o; };
  g.textAlign = "center"; for (const v of ticks(x0, x1, opt.nx || 6)) g.fillText(opt.fx ? opt.fx(v) : v, X(v), h - 6);
  g.textAlign = "right"; for (const v of ticks(y0, y1, opt.ny || 4)) { g.fillText(opt.fy ? opt.fy(v) : v, L - 4, Y(v) + 3); g.strokeStyle = "rgba(148,163,184,.08)"; g.beginPath(); g.moveTo(L, Y(v)); g.lineTo(w - R, Y(v)); g.stroke(); }
  return { X, Y };
}
const gauss = (x, v) => Math.exp(-x * x / (2 * v)) / Math.sqrt(2 * Math.PI * v);

function drawHist(cur, edges) {
  const { g, w, h } = canvas("c-hist");
  const hist = cur?.hist || [];
  const n = hist.reduce((a, b) => a + b, 0);
  const dx = edges.length > 1 ? edges[1] - edges[0] : 1;
  const dens = hist.map((c) => n ? c / (n * dx) : 0);
  const ymax = Math.max(0.6, ...dens) * 1.1;
  const { X, Y } = axes(g, w, h, edges[0] ?? -4, edges[edges.length - 1] ?? 4, 0, ymax);
  g.fillStyle = css(KC[cur?.kind] || "--acc"); g.globalAlpha = .75;
  dens.forEach((d, i) => { const x0 = X(edges[i]), x1 = X(edges[i + 1]); g.fillRect(x0, Y(d), Math.max(x1 - x0 - 1, 1), Y(0) - Y(d)); });
  g.globalAlpha = 1; g.strokeStyle = "#94a3b8"; g.lineWidth = 1.5; g.beginPath();
  for (let i = 0; i <= 200; i++) { const x = edges[0] + (edges[edges.length - 1] - edges[0]) * i / 200; const y = Y(gauss(x, 0.5)); i ? g.lineTo(X(x), y) : g.moveTo(X(x), y); }
  g.stroke();
  if (!n) { g.fillStyle = css("--mut"); g.textAlign = "center"; g.fillText(S?.live?.normalized === false && cur ? "Normierung nach dem ersten K-V-Lauf" : "noch keine Daten", w / 2, h / 2); }
  $("histlbl").textContent = cur ? `${cur.label} ${cur.block_id} · N = ${n}` : "";
}
function drawVar(runs) {
  const { g, w, h } = canvas("c-var");
  const ys = runs.filter(r => r.var != null).map(r => r.var + (r.var_err || 0));
  const ymax = Math.max(1.3, ...ys) * 1.05;
  const { X, Y } = axes(g, w, h, -0.5, Math.max(runs.length, 6) - 0.5, 0, ymax, { fx: (v) => Number.isInteger(v) ? v + 1 : "" });
  g.strokeStyle = "#475569"; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(X(-0.5), Y(0.5)); g.lineTo(X(Math.max(runs.length, 6) - 0.5), Y(0.5)); g.stroke(); g.setLineDash([]);
  runs.forEach((r, i) => {
    if (r.var == null) return;
    const c = css(KC[r.kind] || "--acc"); g.strokeStyle = c; g.fillStyle = c;
    const e = r.var_err || 0; g.beginPath(); g.moveTo(X(i), Y(r.var - e)); g.lineTo(X(i), Y(r.var + e)); g.stroke();
    g.beginPath(); g.arc(X(i), Y(r.var), r.done ? 4 : 5, 0, 7); r.done ? g.fill() : g.stroke();
  });
}
function drawTrace(id, t, y, opt) {
  const { g, w, h } = canvas(id);
  const pts = t.map((tt, i) => [tt, y[i]]).filter(p => p[1] != null);
  if (pts.length < 2) { g.fillStyle = css("--mut"); g.textAlign = "center"; g.font = "12px system-ui"; g.fillText("noch keine Daten", w / 2, h / 2); return; }
  const tx0 = pts[0][0], tx1 = Math.max(pts[pts.length - 1][0], tx0 + 1);
  let y0 = Math.min(...pts.map(p => p[1])), y1 = Math.max(...pts.map(p => p[1]));
  if (opt.band) { y0 = Math.min(y0, opt.band[0]); y1 = Math.max(y1, opt.band[1]); }
  const pad = (y1 - y0) * 0.15 || 1; y0 -= pad; y1 += pad; if (opt.zero) y0 = 0;
  const { X, Y } = axes(g, w, h, tx0, tx1, y0, y1, { fy: opt.fy, nx: 5, ny: 3 });
  if (opt.band) { g.fillStyle = "rgba(34,197,94,.08)"; g.fillRect(X(tx0), Y(opt.band[1]), X(tx1) - X(tx0), Y(opt.band[0]) - Y(opt.band[1])); }
  g.strokeStyle = css(opt.color || "--acc"); g.lineWidth = 1.5; g.beginPath();
  pts.forEach((p, i) => i ? g.lineTo(X(p[0]), Y(p[1])) : g.moveTo(X(p[0]), Y(p[1]))); g.stroke();
}

// ------------------------------------------------------------------ Darstellung Status
function render() {
  if (!S) return;
  const ML = { live: "LIVE", sim: "SIM", replay: "REPLAY" }, MC = { live: "live", sim: "sim", replay: "replay" };
  const modes = S.modes || { harp: S.mode, digitizer: S.mode };
  $("modebadges").innerHTML = [["harp", "HydraHarp"], ["digitizer", "ADQ32"]].map(([k, n]) =>
    `<span class="mbadge ${MC[modes[k]] || ""}" title="${esc(n)}: ${esc(modes[k])}">${esc(n)} <b>${esc(ML[modes[k]] || modes[k])}</b></span>`).join("");
  const sb = $("simbadge"); const sm = S.mode || "sim";
  sb.textContent = ((S.mode_labels && S.mode_labels.session) || (S.simulated ? "SIMULIERT" : "LIVE"))
    + (S.sim_engine === "stream" ? " · DATENSTROM" : "");
  sb.className = "badge " + (sm === "live" ? "live" : sm === "mixed" ? "mixed" : "warn");
  const mb = $("modebanner");
  if (S.live_any || S.fallback) {
    const live = Object.entries(modes).filter(([, m]) => m === "live").map(([k]) => k === "harp" ? "HydraHarp 500" : "ADQ32");
    mb.innerHTML = (S.live_any ? `<b>${sm === "mixed" ? "GEMISCHTER BETRIEB" : "LIVE-MESSUNG"}</b> – echte Hardware: ${esc(live.join(" + "))}` +
      (sm === "mixed" ? ` · simuliert/wiedergegeben: ${esc(Object.entries(modes).filter(([, m]) => m !== "live").map(([k, m]) => (k === "harp" ? "HydraHarp" : "ADQ32") + " " + m).join(", "))}` : "") +
      " · kritische Parameter nur am Mess-PC oder mit Bestätigung" : "") +
      (S.fallback ? `<br><b>Rückfall:</b> ${esc(S.fallback)}` : "");
    mb.className = "modebanner " + (sm === "live" ? "live" : "mixed");
  } else mb.className = "modebanner hidden";
  $("sub").textContent = `${S.devices.harp.model} · ${S.devices.digitizer.model} · hhd ${S.version}`;
  const mine = S.lock.holder === me, other = S.lock.holder && !mine;
  const lb = $("lockbar"); lb.className = "lockbar" + (mine ? " mine" : other ? " other" : "");
  $("locktext").textContent = mine ? "Sie steuern" + (local ? " (Mess-PC)" : " (entfernt)") : other ? `gesteuert von ${S.lock.name || "anderem Client"}` : "Nur Ansicht – niemand steuert";
  $("lockbtn").textContent = mine ? "Steuerung abgeben" : "Steuerung übernehmen";
  document.querySelectorAll("[data-ctl]").forEach(b => b.disabled = !mine);
  const running = ["vorbereitung", "messung", "schreiben", "pause"].includes(S.state);
  const paused = S.state === "pause";
  $("start").disabled = !mine || running || paused; $("stop").disabled = !mine || !(running || paused);
  $("cont").classList.toggle("hidden", !S.waiting_continue); $("cont").disabled = !mine;
  const st = $("state"); st.textContent = S.state_text; st.className = "pill" + (running ? " run" : S.state === "fehler" ? " err" : "");
  const plan = $("plan");
  if (!plan.options.length) {
    const groups = S.plan_groups || [{ label: "", plans: Object.keys(S.plans) }], seen = new Set();
    for (const gr of groups) {
      const og = document.createElement("optgroup"); og.label = gr.label;
      for (const k of gr.plans) if (S.plans[k]) { const o = new Option(S.plans[k], k); o.title = (S.plan_doc || {})[k] || ""; og.appendChild(o); seen.add(k); }
      plan.appendChild(og);
    }
    for (const [k, v] of Object.entries(S.plans)) if (!seen.has(k)) plan.add(new Option(v, k));
  }
  if (document.activeElement !== plan) plan.value = S.display.plan;
  plan.title = (S.plan_doc || {})[plan.value] || "";
  if (document.activeElement !== $("speed")) $("speed").value = S.display.speed;
  $("autoana").checked = !!S.display.auto_analyze;
  $("simeng_l").classList.toggle("hidden", !S.sim_engine);
  if (S.sim_engine && document.activeElement !== $("simeng")) $("simeng").value = S.sim_engine;
  $("simeng").disabled = !mine || running || paused;
  $("sessinfo").textContent = [(S.session_id && !(S.msg || "").includes(S.session_id)) ? `Sitzung ${S.session_id}` : "", S.msg || ""].filter(Boolean).join(" · ") + (S.device_error ? " · " + S.device_error : "");
  const L = S.live || {}, runs = L.runs || [];
  $("planbar").innerHTML = (S.plan || []).map((r, i) => {
    const cls = i < runs.length && runs[i]?.done ? "done" : (i === S.run_index ? "cur" : "");
    return `<span class="chip ${cls}" title="${esc(r.block_id)}">${esc(r.label)}</span>`;
  }).join("");
  const cur = L.current;
  $("prog").style.width = (running && cur?.progress != null ? cur.progress * 100 : (S.state === "fertig" ? 100 : 0)) + "%";
  drawHist(cur, L.edges || []);
  drawVar(runs);
  $("norminfo").textContent = L.normalized ? "normiert auf K-V (Vakuum = ½)" : "noch nicht normiert";
  const tr = L.trace || { t: [] };
  drawTrace("c-lo", tr.t, (tr.lo || []).map(v => v == null ? null : (v - 1) * 100), { band: [-1, 1], fy: (v) => v + " %", color: "--warn" });
  drawTrace("c-rate", tr.t, tr.rate_h || [], { zero: true, color: "--a" });
  $("k-her").textContent = cur ? Math.round(cur.rate_herald) : "–";
  $("k-rec").textContent = cur ? Math.round(cur.rate_records) : "–";
  $("k-var").textContent = cur?.var != null ? fmt(cur.var, 3) : "–";
  $("k-varerr").textContent = cur?.var_err != null ? "± " + fmt(cur.var_err, 3) : "";
  $("k-sync").textContent = cur ? (cur.locked ? "✓" : "…") : "–";
  $("k-sync").className = cur?.locked ? "ok-t" : "";
  const mf = runs.filter(r => r.sync?.matched_frac != null).slice(-1)[0];
  $("k-match").textContent = mf ? `${(mf.sync.matched_frac * 100).toFixed(1)} % zugeordnet` : "eingerastet";
  if (S.mode === "mixed") { $("k-sync").textContent = "–"; $("k-sync").className = ""; $("k-match").textContent = "gemischt: ungekoppelt"; }
  const W = L.warnings || [];
  $("warns").innerHTML = W.length ? W.slice().reverse().map(x => `<li><b>${esc(x.kind)}</b>${esc(x.msg)}</li>`).join("") : '<li class="mut">keine</li>';
  renderJobs(); renderDevices();
}

function renderJobs() {
  const J = S?.jobs || [];
  $("jobs").innerHTML = J.length ? "Aufträge: " + J.slice(0, 3).map(j => `${esc(j.session)} – <b>${esc(j.state)}</b>${j.dauer_s != null ? ` (${j.dauer_s} s)` : ""}${j.error ? " " + esc(j.error) : ""}`).join(" · ") : "";
  const sig = J.map(j => j.id + j.state).join();
  if (sig !== renderJobs.sig) { renderJobs.sig = sig; loadSessions(); }
}

// ------------------------------------------------------------------ Geräte
let devSig = "";
function renderDevices() {
  const sig = JSON.stringify([S.devices, S.lock.holder === me, local]);
  if (sig === devSig) return; devSig = sig;
  $("whereami").textContent = local ? "Sie sind am Mess-PC." : "Sie sind entfernt verbunden.";
  const mine = S.lock.holder === me;
  $("devcards").innerHTML = ["harp", "digitizer"].map((k) => {
    const d = S.devices[k];
    const rows = d.param_specs.map((p) => {
      const v = d.params[p.name];
      const inp = p.choices ? `<select data-dev="${k}" data-p="${esc(p.name)}">${p.choices.map(c => `<option ${String(c) === String(v) ? "selected" : ""} value="${esc(c)}">${esc(c)}</option>`).join("")}</select>`
        : typeof p.default === "boolean" ? `<select data-dev="${k}" data-p="${esc(p.name)}"><option value="false" ${!v ? "selected" : ""}>aus</option><option value="true" ${v ? "selected" : ""}>ein</option></select>`
        : `<input type="number" data-dev="${k}" data-p="${esc(p.name)}" value="${esc(v)}" step="any">`;
      const tags = (p.critical ? '<span class="tag crit">kritisch</span>' : "") + (p.local_only ? '<span class="tag loc">nur Mess-PC</span>' : "");
      const dis = !mine || (p.local_only && !local) ? "disabled" : "";
      return `<div class="param"><div class="nm">${esc(p.name)} ${tags}<small>${esc(p.doc)}${p.unit ? " [" + esc(p.unit) + "]" : ""}</small></div>${inp.replace("<select", `<select ${dis}`).replace("<input", `<input ${dis}`)}<button class="small" data-set="${k}" data-p="${esc(p.name)}" ${dis}>setzen</button></div>`;
    }).join("");
    return `<div class="card"><div class="row between"><h2>${esc(d.model)}</h2><span class="pill mode-${esc(d.mode)}">${esc(({ live: "LIVE", sim: d.sim_engine === "stream" ? "SIMULIERT · DATENSTROM" : "SIMULIERT", replay: "WIEDERGABE" })[d.mode] || d.mode)}${d.open ? "" : " · nicht geöffnet"}</span></div>${rows}</div>`;
  }).join("");
  document.querySelectorAll("[data-set]").forEach((b) => b.onclick = () => {
    const dev = b.dataset.set, name = b.dataset.p;
    const el = document.querySelector(`[data-dev="${dev}"][data-p="${name}"]`);
    const spec = S.devices[dev].param_specs.find(p => p.name === name);
    let val = el.value;
    if (typeof spec.default === "boolean") val = val === "true"; else if (typeof spec.default === "number") val = Number(val);
    setDevice(dev, { [name]: val });
  });
}
function setDevice(dev, params, confirm) {
  send("set_device", { device: dev, params, ...(confirm ? { confirm } : {}) }, (m) => {
    if (m.needs_confirm) {
      $("confirmtitle").textContent = "Kritischen Parameter ändern?";
      $("cyes").textContent = "Ja, ändern";
      $("confirmtext").textContent = `${dev}: ${Object.entries(params).map(([k, v]) => `${k} = ${v}`).join(", ")}`;
      $("confirm").classList.remove("hidden");
      $("cyes").onclick = () => { $("confirm").classList.add("hidden"); setDevice(dev, params, m.confirm); };
      $("cno").onclick = () => { $("confirm").classList.add("hidden"); toast("abgebrochen"); };
    } else toast(m.msg, !m.ok);
  });
}

// ------------------------------------------------------------------ Ergebnisse
async function api(path) { const r = await fetch(path, { credentials: "same-origin" }); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.status); return r; }
async function loadSessions() {
  try {
    lastSessions = await (await api("/api/sessions")).json();
    $("sesslist").innerHTML = lastSessions.length ? lastSessions.map(s => `<div class="sess ${s.id === selSession ? "sel" : ""}" data-s="${esc(s.id)}"><div><b>${esc(s.id)}</b><br><small>${esc(s.plan || "")}${s.state && s.state !== "fock1" ? " · " + esc(s.state_label || s.state) + (s.phase_sensitive ? " (phasengem.)" : "") : ""} · ${s.runs ?? "?"} Läufe · ${esc(s.mtime)}${s.mode && s.mode !== "sim" ? " · " + esc(({ live: "LIVE", mixed: "gemischt", replay: "Wiedergabe" })[s.mode] || s.mode) : (s.simulated ? " · simuliert" : "")}</small></div><span class="${s.analyzed ? "ok-t" : "mut"}">${s.analyzed ? "ausgewertet" : (s.job ? esc(s.job) : "–")}</span></div>`).join("") : '<div class="mut">noch keine Sitzungen</div>';
    document.querySelectorAll(".sess").forEach(e => e.onclick = () => openSession(e.dataset.s));
    if (selSession && lastSessions.find(s => s.id === selSession)?.analyzed && !openSession.loaded) openSession(selSession);
    if (!selSession && lastSessions.length && lastSessions[0].analyzed) openSession(lastSessions[0].id);
  } catch (e) { /* ignore */ }
}
async function openSession(id) {
  selSession = id; openSession.loaded = false;
  document.querySelectorAll(".sess").forEach(e => e.classList.toggle("sel", e.dataset.s === id));
  $("resbox").classList.remove("hidden"); $("restitle").textContent = `Ergebnis ${id}`;
  let R;
  try { R = await (await api(`/api/sessions/${encodeURIComponent(id)}/summary`)).json(); }
  catch (e) { $("restbl").innerHTML = `<tr><td class="mut">Noch keine Auswertung – „neu auswerten“ starten.</td></tr>`; return; }
  openSession.loaded = true;
  renderResult(R);
  try { $("report").textContent = await (await api(`/api/sessions/${encodeURIComponent(id)}/report`)).text(); } catch (e) { $("report").textContent = ""; }
}
function ci(g, q) { const c = g[q + "_ci68"]; return c ? `[${fmt(c[0], 4)}, ${fmt(c[1], 4)}]` : ""; }
function renderResult(R) {
  const A = R.groups.A || {}, B = R.groups.B || {}, C = R.comparison || {};
  const row = (lbl, a, b) => `<tr><th>${lbl}</th><td>${a}</td><td>${b}</td></tr>`;
  const W = (g) => `${fmtE(g.W00, g.W00_err)}<br><small class="mut">68 %: ${ci(g, "W00")} ${esc(g.W00_ci_method || "")}</small>`;
  const E = (g) => `${fmtE(g.eta_fit, g.eta_fit_err)}<br><small class="mut">68 %: ${ci(g, "eta_fit")}</small>`;
  const st = R.state || { preset: "fock1", label: "Einzelphoton |1⟩" }, f1 = (st.preset || "fock1") === "fock1";
  const ps = !!st.phase_sensitive;
  $("resstate").innerHTML = `Zustand A: <b>${esc(st.label || st.preset)}</b>` + (st.mean_n_ideal != null ? ` · ⟨n⟩<sub>ideal</sub> = ${fmt(st.mean_n_ideal, 3)}` : "") +
    (ps ? ` · <span class="warn-t">phasenempfindlich – nur phasengemittelt dargestellt und ausgewertet (Sollwerte ebenso)</span>` : "");
  $("wiglbl").innerHTML = (ps ? "<b>phasengemittelt</b> (Phasenstruktur des Zustands nicht sichtbar)" : "phasengemittelt") + ", W(x,p) aus W(r); blau &lt; 0 (nichtklassisch)";
  const En = (g) => g.eta_n != null ? fmtE(g.eta_n, g.eta_n_err) : "–";
  $("restbl").innerHTML = `<tr><th></th><th>A (herald-bedingt)</th><th>B (frei laufend)</th></tr>` +
    row("N", A.n ?? "–", B.n ?? "–") + row("Var x", fmtE(A.var_x, A.var_err), fmtE(B.var_x, B.var_err)) +
    row("W(0,0)", W(A), W(B)) + (f1 ? row("η (Fit)", E(A), E(B)) : row("η (⟨n⟩)", En(A), "–")) + row("⟨n⟩", fmt(A.mean_n), fmt(B.mean_n)) +
    row("g²(0)", fmt(A.g2, 3), fmt(B.g2, 2)) + (f1 ? row("F (verlustbeh. |1⟩)", fmt(A.fidelity_lossy_fock1, 4), fmt(B.fidelity_lossy_fock1, 4)) : "") +
    row("A − B", `ΔVar = ${fmtE(C.diff_var_x?.value, C.diff_var_x?.err)}`, `ΔW(0,0) = ${fmtE(C.diff_W00?.value, C.diff_W00?.err)} (z = ${fmt(C.diff_W00?.z, 1)})`);
  const neg = A.W00 != null && A.W00_err ? (-A.W00 / A.W00_err) : null;
  $("reswarn").innerHTML = (neg != null && neg > 0 ? `<span class="ok-t">W_A(0,0) &lt; 0 mit ${neg.toFixed(1)} σ (nichtklassisch)</span><br>` : "") +
    (R.warnings || []).map(w => "⚠ " + esc(typeof w === "string" ? w : JSON.stringify(w))).join("<br>");
  drawWigner(A); drawWr(A, B); drawPn(A, B); drawAB(C);
  const eb = R.efficiency_budget || {};
  const brow = (k, v) => `<tr><th>${k}</th><td>${v}</td></tr>`;
  const ev = (o) => o && typeof o === "object" ? fmtE(o.value, o.err) : fmt(o, 4);
  $("budget").innerHTML = brow("Herald-Rate", fmt(eb.herald_rate_per_s, 0) + " /s") + brow("ξ (Herald-Reinheit)", fmt(eb.xi, 4)) +
    brow("η_el (elektronisch)", fmtE(eb.eta_el, eb.eta_el_err)) + brow("η_Mode (PCA)", fmt(eb.eta_mode_pca, 4)) +
    (f1 ? brow("η_eff (Fit)", ev(eb.eta_eff_fit)) : brow("η_eff (⟨n⟩)", ev(eb.eta_eff_n)) + brow("η_eff (|1⟩-Fit, nicht gültig)", ev(eb.eta_eff_fit))) + brow(f1 ? "η_eff (Varianz)" : "Var x − ½ (= η_eff·⟨n⟩)", ev(eb.eta_eff_var)) + brow("η_rest", ev(eb.eta_rest));
}
function wr(g, r) { const R = g.wigner_r || [], V = g.wigner_radial || []; if (r >= R[R.length - 1]) return V[V.length - 1] || 0; let i = 1; while (i < R.length && R[i] < r) i++; const t = (r - R[i - 1]) / (R[i] - R[i - 1]); return V[i - 1] * (1 - t) + V[i] * t; }
function drawWigner(A) {
  const { g, w, h } = canvas("c-wig");
  if (!A.wigner_r) return;
  const s = Math.min(w, h) - 20, ox = (w - s) / 2, oy = 10, n = 90, xm = 3.2, vmax = 1 / Math.PI;
  const cell = s / n;
  for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) {
    const x = -xm + 2 * xm * (i + .5) / n, p = -xm + 2 * xm * (j + .5) / n, v = wr(A, Math.hypot(x, p)) / vmax;
    const c = v >= 0 ? [Math.round(15 + 234 * v), Math.round(23 + 92 * v), Math.round(42 - 20 * v)] : [Math.round(15 + 20 * -v), Math.round(23 + 140 * -v), Math.round(42 + 213 * -v)];
    g.fillStyle = `rgb(${c})`; g.fillRect(ox + i * cell, oy + (n - 1 - j) * cell, cell + .5, cell + .5);
  }
  g.strokeStyle = "rgba(226,232,240,.25)"; g.beginPath(); g.moveTo(ox, oy + s / 2); g.lineTo(ox + s, oy + s / 2); g.moveTo(ox + s / 2, oy); g.lineTo(ox + s / 2, oy + s); g.stroke();
  g.fillStyle = css("--mut"); g.font = "11px system-ui"; g.fillText("x", ox + s - 10, oy + s / 2 - 4); g.fillText("p", ox + s / 2 + 4, oy + 12);
  g.fillText(`W(0,0) = ${fmt(A.W00, 3)}`, ox + 4, oy + s - 6);
}
function drawWr(A, B) {
  const { g, w, h } = canvas("c-wr");
  if (!A.wigner_r) return;
  const R = A.wigner_r, all = [...(A.wigner_radial || []), ...(B.wigner_radial || []), 1 / Math.PI];
  const { X, Y } = axes(g, w, h, 0, R[R.length - 1], Math.min(...all, 0) * 1.1, Math.max(...all) * 1.1);
  g.strokeStyle = "#475569"; g.beginPath(); g.moveTo(X(0), Y(0)); g.lineTo(X(R[R.length - 1]), Y(0)); g.stroke();
  g.setLineDash([4, 4]); g.beginPath(); R.forEach((r, i) => { const y = Y(Math.exp(-r * r) / Math.PI); i ? g.lineTo(X(r), y) : g.moveTo(X(r), y); }); g.stroke(); g.setLineDash([]);
  for (const [G, c] of [[B, "--b"], [A, "--a"]]) { if (!G.wigner_radial) continue; g.strokeStyle = css(c); g.lineWidth = 2; g.beginPath(); G.wigner_r.forEach((r, i) => i ? g.lineTo(X(r), Y(G.wigner_radial[i])) : g.moveTo(X(r), Y(G.wigner_radial[i]))); g.stroke(); g.lineWidth = 1; }
}
function drawPn(A, B) {
  const { g, w, h } = canvas("c-pn");
  const pa = A.p_n || [], pb = B.p_n || [], n = Math.min(Math.max(pa.length, pb.length), 6);
  const { X, Y } = axes(g, w, h, -0.5, n - 0.5, 0, 1.05, { nx: n });
  const bw = (X(1) - X(0)) * 0.38;
  for (let k = 0; k < n; k++) {
    g.fillStyle = css("--a"); g.fillRect(X(k) - bw, Y(pa[k] || 0), bw, Y(0) - Y(pa[k] || 0));
    g.fillStyle = css("--b"); g.fillRect(X(k), Y(pb[k] || 0), bw, Y(0) - Y(pb[k] || 0));
  }
}
function drawAB(C) {
  const { g, w, h } = canvas("c-ab"); const H = C.histogram; if (!H) return;
  const e = H.edges, dx = e[1] - e[0];
  const sum = (a) => a.reduce((s, v) => s + v, 0);
  const norm = (a) => { const s = sum(a); return s > 1.5 ? a.map(v => v / (s * dx)) : a; };   // Zählungen oder Dichte
  const A = norm(H.A), B = norm(H.B), ymax = Math.max(...A, ...B, 0.6) * 1.1;
  const { X, Y } = axes(g, w, h, e[0], e[e.length - 1], 0, ymax);
  for (const [D, c] of [[B, "--b"], [A, "--a"]]) { g.strokeStyle = css(c); g.lineWidth = 2; g.beginPath(); D.forEach((v, i) => { const x0 = X(e[i]), x1 = X(e[i + 1]); i ? g.lineTo(x0, Y(v)) : g.moveTo(x0, Y(v)); g.lineTo(x1, Y(v)); }); g.stroke(); }
  g.lineWidth = 1;
  $("ablbl").innerHTML = `<span class="lg a"></span>A <span class="lg b"></span>B · KS p = ${C.ks?.p != null ? Number(C.ks.p).toExponential(2) : "–"} · χ² = ${fmt(H.chi2, 1)}/${H.ndof}`;
}

// ------------------------------------------------------------------ Protokoll
async function loadLog() {
  try {
    const L = await (await api("/api/log?n=200")).json();
    $("logtbl").innerHTML = L.reverse().map(e => `<div class="logrow"><div><b>${esc(e.cmd)}</b> <span class="${e.ok ? "ok-t" : "bad-t"}">${e.ok ? "ok" : "abgelehnt"}</span><small class="mut"> · ${esc((e.ts || "").replace("T", " ").slice(0, 19))} · ${e.local ? "Mess-PC" : "entfernt"} · ${esc(e.client)}</small></div><small class="mut">${esc(JSON.stringify(e.params))}${e.msg ? " → " + esc(e.msg) : ""}</small></div>`).join("") || '<div class="mut">leer</div>';
  } catch (e) { /* ignore */ }
  if (S) $("about").innerHTML = `ExplorationSE Harp ADQ32 HHD Mobil · Server hhd ${esc(S.version)} · Betriebsart: HydraHarp ${esc((S.modes || {}).harp || S.mode)}, ADQ32 ${esc((S.modes || {}).digitizer || S.mode)}${S.simulated ? " (SIMULIERT)" : ""} · Serverzeit ${esc(S.server_time)}<br>Steuersperre verfällt nach ${S.lock.timeout_s} s Inaktivität oder bei Trennung. Alle Befehle werden protokolliert (logs/commands.jsonl).`;
}

// ------------------------------------------------------------------ Bedienung
document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("active", x === b));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.id === b.dataset.v));
  if (b.dataset.v === "v-log") loadLog();
  if (b.dataset.v === "v-erg") loadSessions();
  history.replaceState(null, "", "#" + b.dataset.v.slice(2));
  setTimeout(render, 30);
  if (b.dataset.v === "v-erg" && selSession) setTimeout(() => openSession(selSession), 60);
});
$("lockbtn").onclick = () => send(S && S.lock.holder === me ? "release_control" : "acquire_control");
function startSession(confirm) {
  const p = { plan: $("plan").value, speed: Number($("speed").value), ...(confirm ? { confirm } : {}) };
  send("start_session", p, (m) => {
    if (m.needs_confirm) {
      $("confirmtitle").textContent = "Messung mit echten Geräten starten?";
      $("cyes").textContent = "Ja, starten";
      $("confirmtext").textContent = `Sitzung „${p.plan}“ mit LIVE-Geräten starten (${Object.entries(S.modes || {}).map(([k, v]) => k + ": " + v).join(", ")})`;
      $("confirm").classList.remove("hidden");
      $("cyes").onclick = () => { $("confirm").classList.add("hidden"); startSession(m.confirm); };
      $("cno").onclick = () => { $("confirm").classList.add("hidden"); toast("abgebrochen"); };
    } else toast(m.msg, !m.ok);
  });
}
$("start").onclick = () => startSession();
$("cont").onclick = () => send("continue_run");
$("stop").onclick = () => { if (confirm("Sitzung stoppen? Abgeschlossene Läufe werden gespeichert.")) send("stop_session"); };
$("plan").onchange = () => send("set_display", { plan: $("plan").value });
$("simeng").onchange = () => send("set_display", { sim_engine: $("simeng").value });
$("autoana").onchange = () => send("set_display", { auto_analyze: $("autoana").checked });
$("clrwarn").onclick = () => send("clear_warnings");
$("refresh").onclick = loadSessions;
$("logref").onclick = loadLog;
$("reana").onclick = () => selSession && send("analyze", { session: selSession });
$("togrep").onclick = () => { const r = $("report"); r.classList.toggle("hidden"); $("togrep").textContent = r.classList.contains("hidden") ? "anzeigen" : "ausblenden"; };
window.addEventListener("resize", () => { render(); if (selSession) openSession(selSession); });
const h0 = location.hash.slice(1); if (h0) { const b = document.querySelector(`.tabs button[data-v="v-${h0}"]`); if (b) setTimeout(() => b.click(), 50); }
connect();
