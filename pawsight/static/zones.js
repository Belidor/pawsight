// Zone editor: draw zones on a detector frame and choose which send Telegram.
//
// All coordinates here are DETECTOR frame pixels (the SD substream, 640x360 by
// default), the same space zones.yaml uses. The canvas only scales them.
//
// Two kinds of drawn zones:
//   bowls -- stored as offsets from the floor point of the bowl pair, so the
//            zone follows the bowls when the camera pans. Needs the pair on
//            this frame.
//   fixed -- plain frame coordinates; right only while the camera stays put.
// Built-in zones (bowls, water, food) and hand-written custom ones keep their
// geometry: only the name and the Telegram switch can change here.
"use strict";

const COLORS = ["#E0A063", "#63A8E0", "#7FBDA0", "#C98FD6", "#E0B054",
                "#DE7C63", "#8FC6D6", "#B7C46A", "#D68FA8", "#A2988A"];
const LOCKED = { builtin: "built-in", custom: "edited in zones.yaml" };

const $ = (id) => document.getElementById(id);
const img = $("frame"), cv = $("canvas"), ctx = cv.getContext("2d");

let S = null;          // server state: size, pair, bowls
let zones = [];        // [{name?, label, notify, kind, shape?, polygon?, abs, color}]
let notifyOutside = true;
let sel = -1;          // selected zone index
let draw = null;       // {kind, pts, target (index or -1 for new)}
let hover = null;      // mouse point in frame px while drawing
let dirty = false;

async function api(method, body) {
  const r = await fetch("/api/zones", {
    method,
    headers: method === "GET" ? {} : { "content-type": "application/json", "X-Pawsight": "1" },
    body: body ? JSON.stringify(body) : undefined,
  });
  let d = {};
  try { d = await r.json(); } catch (e) { d = { ok: false, error: "HTTP " + r.status }; }
  return d;
}

function status(text, cls) {
  const s = $("status");
  s.textContent = text || "";
  s.className = "bar-hint " + (cls || "");
}

function setDirty(v) {
  dirty = v;
  $("save").disabled = !v || !!draw;
  if (v) status("unsaved changes");
}

async function load() {
  const d = await api("GET");
  if (!d.ok) {
    $("badge").textContent = d.error || "the detector did not answer";
    $("badge").className = "badge stale";
    return;
  }
  S = d;
  img.src = "data:image/jpeg;base64," + d.frame_b64;
  notifyOutside = d.notify_outside;
  $("notify-outside").checked = notifyOutside;
  zones = d.zones.map((z, i) => ({
    ...z, abs: (d.drawn || {})[z.name] || null, color: COLORS[i % COLORS.length],
  }));
  const age = d.age > 3 ? ` · frame ${Math.round(d.age)} s old` : "";
  $("badge").textContent = (d.pair ? "bowl pair found" : "bowl pair NOT in view") + age;
  $("badge").className = "badge" + (d.pair ? "" : " stale");
  $("add-bowls").disabled = !d.pair;
  $("add-note").textContent = d.pair ? "" :
    "Zones that move with the bowls need both bowls in this frame. " +
    "Point the camera at them and reload the page.";
  sel = -1;
  setDirty(false);
  status("");
  renderList();
}

// ---------- canvas ----------

function scale() { return cv.clientWidth / (S ? S.size[0] : 640); }

function fit() {
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(cv.clientWidth * dpr);
  cv.height = Math.round(cv.clientHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  paint();
}

function toFrame(ev) {
  const r = cv.getBoundingClientRect(), k = scale();
  const x = Math.round((ev.clientX - r.left) / k), y = Math.round((ev.clientY - r.top) / k);
  return [Math.max(0, Math.min(S.size[0], x)), Math.max(0, Math.min(S.size[1], y))];
}

function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
}

function poly(pts, color, { fill = 0.18, width = 2, closed = true, label = null } = {}) {
  if (!pts || !pts.length) return;
  const k = scale();
  ctx.beginPath();
  pts.forEach(([x, y], i) => (i ? ctx.lineTo(x * k, y * k) : ctx.moveTo(x * k, y * k)));
  if (closed) ctx.closePath();
  if (closed && fill) { ctx.fillStyle = hexA(color, fill); ctx.fill(); }
  ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke();
  if (label) {
    const cx = pts.reduce((a, p) => a + p[0], 0) / pts.length * k;
    const cy = pts.reduce((a, p) => a + p[1], 0) / pts.length * k;
    ctx.font = "600 12px -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif";
    const w = ctx.measureText(label).width + 10;
    ctx.fillStyle = "rgba(0,0,0,.65)";
    ctx.fillRect(cx - w / 2, cy - 9, w, 18);
    ctx.fillStyle = color; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(label, cx, cy);
  }
}

function paint() {
  ctx.clearRect(0, 0, cv.clientWidth, cv.clientHeight);
  if (!S) return;
  const k = scale();
  // the bowls the detector found, and the pair's floor point
  ctx.setLineDash([4, 3]);
  for (const b of S.bowls || []) {
    ctx.strokeStyle = "rgba(237,230,217,.55)"; ctx.lineWidth = 1;
    ctx.strokeRect(b[0] * k, b[1] * k, (b[2] - b[0]) * k, (b[3] - b[1]) * k);
  }
  ctx.setLineDash([]);
  if (S.pair) {
    ctx.fillStyle = "#EDE6D9";
    ctx.beginPath(); ctx.arc(S.pair[0] * k, S.pair[1] * k, 3.5, 0, 7); ctx.fill();
  }
  zones.forEach((z, i) => {
    if (draw && draw.target === i) return;
    poly(z.abs, z.color, { fill: i === sel ? 0.3 : 0.14, width: i === sel ? 3 : 1.6,
                          label: z.label });
  });
  if (draw) {
    const pts = hover ? [...draw.pts, hover] : draw.pts;
    poly(pts, "#EDE6D9", { fill: 0.12, width: 2, closed: draw.pts.length >= 2 });
    ctx.fillStyle = "#EDE6D9";
    for (const [x, y] of draw.pts) {
      ctx.beginPath(); ctx.arc(x * k, y * k, 3.5, 0, 7); ctx.fill();
    }
  }
}

// ---------- drawing ----------

function startDraw(kind, target) {
  if (kind === "bowls" && !S.pair) return;
  draw = { kind, pts: [], target };
  hover = null;
  $("drawbar").hidden = false;
  $("save").disabled = true;
  hint();
  cv.focus();
  paint();
}

function hint() {
  const n = draw.pts.length;
  $("draw-hint").textContent = (draw.kind === "bowls" ? "Moves with the bowls. " : "Fixed in the frame. ")
    + (n < 3 ? `Click to place corners (${n} of at least 3).` : `${n} corners. Enter or double-click to finish.`);
  $("draw-done").disabled = n < 3;
  $("draw-undo").disabled = n === 0;
}

function endDraw(commit) {
  if (commit && draw.pts.length >= 3) {
    const pts = draw.pts;
    let z;
    if (draw.target >= 0) {
      z = zones[draw.target];
    } else {
      const n = zones.filter((q) => !q.name || !(q.name in LOCKED)).length + 1;
      z = { label: "zone " + n, notify: true, kind: draw.kind,
            color: COLORS[zones.length % COLORS.length] };
      zones.push(z);
      sel = zones.length - 1;
    }
    z.abs = pts;
    if (z.kind === "bowls") {
      z.shape = pts.map(([x, y]) => [x - S.pair[0], y - S.pair[1]]);
      delete z.polygon;
    } else {
      z.polygon = pts;
      delete z.shape;
    }
    setDirty(true);
  }
  draw = null; hover = null;
  $("drawbar").hidden = true;
  $("save").disabled = !dirty;
  renderList();
  if (commit && sel >= 0) {
    const inp = document.querySelector(`#list li[data-i="${sel}"] input[type=text]`);
    if (inp && draw === null && /^zone \d+$/.test(zones[sel].label)) { inp.focus(); inp.select(); }
  }
}

cv.addEventListener("click", (ev) => {
  if (!S) return;
  if (!draw) { pick(toFrame(ev)); return; }
  if (draw.pts.length >= 40) return;
  const p = toFrame(ev);
  const last = draw.pts[draw.pts.length - 1];
  if (last && last[0] === p[0] && last[1] === p[1]) return;   // the double-click's second click
  draw.pts.push(p);
  hint(); paint();
});
cv.addEventListener("dblclick", (ev) => { if (draw) { ev.preventDefault(); endDraw(true); } });
cv.addEventListener("mousemove", (ev) => { if (draw) { hover = toFrame(ev); paint(); } });
cv.addEventListener("mouseleave", () => { if (draw) { hover = null; paint(); } });
document.addEventListener("keydown", (ev) => {
  if (!draw) return;
  if (ev.key === "Escape") endDraw(false);
  else if (ev.key === "Enter" && draw.pts.length >= 3) endDraw(true);
  else if (ev.key === "Backspace" && ev.target === cv) { draw.pts.pop(); hint(); paint(); ev.preventDefault(); }
});
$("draw-done").onclick = () => endDraw(true);
$("draw-cancel").onclick = () => endDraw(false);
$("draw-undo").onclick = () => { draw.pts.pop(); hint(); paint(); };
$("add-bowls").onclick = () => startDraw("bowls", -1);
$("add-fixed").onclick = () => startDraw("fixed", -1);

function inside(p, pts) {
  let c = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i], [xj, yj] = pts[j];
    if ((yi > p[1]) !== (yj > p[1]) && p[0] < (xj - xi) * (p[1] - yi) / (yj - yi) + xi) c = !c;
  }
  return c;
}

function pick(p) {
  // the topmost (last drawn) zone under the click
  for (let i = zones.length - 1; i >= 0; i--) {
    if (zones[i].abs && inside(p, zones[i].abs)) { sel = i; renderList(); return; }
  }
  sel = -1; renderList();
}

// ---------- list ----------

function renderList() {
  const ul = $("list");
  ul.textContent = "";
  zones.forEach((z, i) => {
    const li = document.createElement("li");
    li.dataset.i = i;
    if (i === sel) li.className = "sel";
    const row = document.createElement("div"); row.className = "zrow";
    const sw = document.createElement("span"); sw.className = "zsw"; sw.style.background = z.color;
    const inp = document.createElement("input");
    inp.type = "text"; inp.value = z.label; inp.maxLength = 40;
    inp.setAttribute("aria-label", "zone name");
    inp.oninput = () => { z.label = inp.value; setDirty(true); paint(); };
    inp.onfocus = () => { if (sel !== i) { sel = i; markSel(); paint(); } };
    row.append(sw, inp);

    const meta = document.createElement("div"); meta.className = "zmeta";
    const lab = document.createElement("label"); lab.className = "zcheck small";
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = z.notify;
    cb.onchange = () => { z.notify = cb.checked; setDirty(true); };
    lab.append(cb, " Telegram");
    meta.append(lab);
    const kind = document.createElement("span");
    kind.textContent = LOCKED[z.kind] || (z.kind === "bowls" ? "moves with bowls" : "fixed");
    if (!z.abs) kind.textContent += " · not on this frame";
    meta.append(kind);
    if (!(z.kind in LOCKED)) {
      const re = document.createElement("button");
      re.className = "btn sm"; re.textContent = "Redraw";
      re.disabled = z.kind === "bowls" && !S.pair;
      re.onclick = () => { sel = i; startDraw(z.kind, i); };
      const del = document.createElement("button");
      del.className = "btn sm danger"; del.textContent = "Delete";
      del.onclick = () => { zones.splice(i, 1); sel = -1; setDirty(true); renderList(); };
      meta.append(re, del);
    }
    li.append(row, meta);
    ul.append(li);
  });
  paint();
}

function markSel() {
  document.querySelectorAll("#list li").forEach((li) => {
    li.className = Number(li.dataset.i) === sel ? "sel" : "";
  });
}

$("notify-outside").onchange = (ev) => { notifyOutside = ev.target.checked; setDirty(true); };

$("save").onclick = async () => {
  $("save").disabled = true;
  status("saving…");
  const body = {
    notify_outside: notifyOutside,
    zones: zones.map((z) => {
      const o = { label: z.label.trim(), notify: z.notify, kind: z.kind };
      if (z.name) o.name = z.name;
      if (z.kind === "bowls" && z.shape) o.shape = z.shape;
      if (z.kind === "fixed" && z.polygon) o.polygon = z.polygon;
      return o;
    }),
  };
  const r = await api("POST", body);
  if (!r.ok) {
    status(r.error || "could not save", "err");
    $("save").disabled = false;
    return;
  }
  await load();
  status("saved; the detector uses the new zones now", "ok");
};

window.addEventListener("beforeunload", (ev) => {
  if (dirty) { ev.preventDefault(); ev.returnValue = ""; }
});
img.addEventListener("load", fit);
window.addEventListener("resize", fit);
load();
