// Labelling page: review camera frames and draw boxes for fine-tuning.
//
// Two sources of frames, one editor:
//   dataset -- a frame already in dataset/ (auto-collected or saved earlier);
//              saved with POST /api/dataset/{name}/save
//   snap    -- a fresh capture: the detector's ring of the last frames, so the
//              user can step back to the right moment; saved with
//              POST /api/snapshot/{sid}/save, which adds it to dataset/
//
// Boxes travel as frame FRACTIONS (cx, cy, w, h in 0..1): that is a YOLO label
// line already, so no resolution constant exists on either end. Frames in the
// dataset come in several sizes, so the size is always read from the image.
//
// The class list comes from the server (labels.DATASET_CLASSES): the page
// never offers a class the detector would refuse to write.
"use strict";

const $ = (id) => document.getElementById(id);
const img = $("l-img"), cv = $("l-cv"), ctx = cv.getContext("2d");
const stageEl = $("l-stage"), msgEl = $("l-msg"), queueEl = $("l-queue");
const HANDLE = window.matchMedia("(pointer:coarse)").matches ? 16 : 9;   // CSS px

// Colours of the dashboard where it has them; a fallback palette for the rest.
const KNOWN = { dog: "#E0A063", bowl: "#63A8E0", person: "#A38FD6" };
const PALETTE = ["#7FBDA0", "#E0B054", "#DE7C63", "#8FC6D6", "#B7C46A", "#D68FA8"];

let CLS = [];              // class names, index order
const COLOR = {};
let items = [];            // dataset listing from the detector
let counts = null;
let order = "suspicious", filter = "";
let view = [];             // names after the filter, in display order

let cur = null;            // {src, name|sid, frames, n, size, boxes, saved}
let sel = -1, drag = null, drawCls = null, hide = false;
let undo = [];
let pendingConfirm = false;     // the next Save confirms a vanished class
let discardArmed = null;        // the next navigation discards unsaved edits
let dropArmed = false, dropTimer = null;
let busy = false;

// ---------- small helpers ----------

async function api(method, url, body) {
  const opt = { method, headers: { "X-Pawsight": "1" } };
  if (body !== undefined) {
    opt.headers["content-type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  try { return await r.json(); } catch (e) { return { ok: false, error: "HTTP " + r.status }; }
}

function msg(text, bad) {
  msgEl.textContent = text || "";
  msgEl.className = "bar-msg" + (bad ? " bad" : "");
}
function msgRich(strong, rest, bad) {
  msgEl.textContent = "";
  msgEl.className = "bar-msg" + (bad ? " bad" : "");
  const b = document.createElement("b");
  b.textContent = strong;
  msgEl.appendChild(b);
  msgEl.appendChild(document.createTextNode(rest));
}

let toastTimer = null;
function toast(text, bad) {
  let t = document.querySelector(".toast");
  if (!t) { t = document.createElement("div"); document.body.appendChild(t); }
  t.className = "toast" + (bad ? " bad" : "");
  t.textContent = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), 3500);
}

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const norm4 = (b) => [Math.min(b[0], b[2]), Math.min(b[1], b[3]),
                      Math.max(b[0], b[2]), Math.max(b[1], b[3])];
const short = (name) => name.replace(/^(dog|manual)_/, "");

function fromWire(b, src) {
  return { cls: b.cls, conf: b.conf || null, src,
           box: norm4([b.cx - b.w / 2, b.cy - b.h / 2, b.cx + b.w / 2, b.cy + b.h / 2]) };
}
function toWire(b) {
  const x = b.box;
  return { cls: b.cls, cx: (x[0] + x[2]) / 2, cy: (x[1] + x[3]) / 2,
           w: x[2] - x[0], h: x[3] - x[1] };
}
function boxesKey(boxes) {
  return JSON.stringify(boxes.map((b) => [b.cls, b.box.map((v) => v.toFixed(4))]));
}
function dirty() { return !!cur && boxesKey(cur.boxes) !== cur.saved; }

// ---------- classes ----------

async function loadClasses() {
  const d = await api("GET", "/api/label/classes");
  CLS = d.ok ? d.classes : ["dog", "bowl", "person"];
  let k = 0;
  CLS.forEach((c) => { COLOR[c] = KNOWN[c] || PALETTE[k++ % PALETTE.length]; });
  drawCls = CLS[0];
  const host = $("l-classes");
  host.textContent = "";
  CLS.forEach((c, i) => {
    const b = document.createElement("button");
    b.className = "seg-b" + (i === 0 ? " on" : "");
    b.dataset.cls = c;
    const sw = document.createElement("i");
    sw.style.background = COLOR[c];
    b.appendChild(sw);
    b.appendChild(document.createTextNode(c));
    if (i < 9) {
      const kb = document.createElement("kbd");
      kb.textContent = String(i + 1);
      b.appendChild(kb);
    }
    b.addEventListener("click", () => { setCls(c, true); cv.focus(); });
    host.appendChild(b);
  });
}

function setCls(c, applyToSel) {
  drawCls = c;
  document.querySelectorAll("#l-classes .seg-b").forEach((b) =>
    b.classList.toggle("on", b.dataset.cls === c));
  if (applyToSel && cur && sel >= 0 && cur.boxes[sel].cls !== c) {
    pushUndo();
    cur.boxes[sel].cls = c;
    cur.boxes[sel].src = "user";
    changed();
  }
}

// ---------- frame list ----------

async function loadList() {
  const d = await api("GET", "/api/dataset?limit=5000&order=" + order);
  if (!d.ok) {
    queueEl.textContent = "";
    queueEl.appendChild(li("pane-empty", "detector did not respond: " + (d.error || "")));
    return;
  }
  items = d.items || [];
  counts = d.counts || {};
  renderList();
}

function li(cls, text) {
  const e = document.createElement(cls === "pane-empty" ? "li" : "span");
  e.className = cls;
  e.textContent = text;
  return e;
}

function passes(it) {
  const k = it.kinds || [];
  if (filter === "todo") return !it.human;
  if (filter === "no_bowl") return k.indexOf("bowl") < 0;
  if (filter === "dog_only") return k.length === 1 && k[0] === "dog";
  if (filter === "empty") return !k.length;
  return true;
}

function renderList() {
  const vis = items.filter(passes);
  view = vis.map((it) => it.name);
  queueEl.textContent = "";
  if (!vis.length) {
    queueEl.appendChild(li("pane-empty", items.length ? "no frames match the filter"
                                                       : "no frames in the dataset yet"));
  }
  const frag = document.createDocumentFragment();
  vis.forEach((it) => {
    const e = document.createElement("li");
    e.dataset.name = it.name;
    if (it.human) e.classList.add("done");
    if (cur && cur.src === "dataset" && cur.name === it.name) e.classList.add("on");
    e.appendChild(li("sc", it.human ? "✓" : it.score.toFixed(1)));
    e.appendChild(li("nm", short(it.name)));
    e.appendChild(li("kd", (it.kinds || []).map((k) => k.slice(0, 3)).join("·") || "empty"));
    if ((it.why || []).length) e.appendChild(li("wy", it.why.join(", ")));
    e.addEventListener("click", () => openName(it.name));
    frag.appendChild(e);
  });
  queueEl.appendChild(frag);
  renderStat();
  renderPos();
}

function renderStat() {
  const c = counts || {}, host = $("l-stat");
  host.textContent = "";
  const add = (tag, text) => {
    const e = document.createElement(tag); e.textContent = text; host.appendChild(e);
  };
  add("b", String(c.all || 0));
  host.appendChild(document.createTextNode(" frames · reviewed "));
  add("b", String(c.human || 0));
  host.appendChild(document.createTextNode(" · no bowl "));
  add("s", String(c.no_bowl || 0));
  host.appendChild(document.createTextNode(" · empty "));
  add("b", String(c.empty || 0));
  if (filter) host.appendChild(document.createTextNode(" · shown " + view.length));
  $("l-pairs").textContent = (c.all || 0) + " frames in dataset";
}

function renderPos() {
  const i = cur && cur.src === "dataset" ? view.indexOf(cur.name) : -1;
  $("l-pos").textContent = i >= 0 ? (i + 1) + " / " + view.length : "";
  $("l-prev").disabled = !view.length;
  $("l-next").disabled = !view.length;
  queueEl.querySelectorAll("li[data-name]").forEach((e) =>
    e.classList.toggle("on", !!cur && cur.src === "dataset" && e.dataset.name === cur.name));
}

function item(name) { return items.find((it) => it.name === name) || null; }

// ---------- navigation with unsaved edits ----------

// Unsaved boxes are not thrown away silently: the first attempt to leave
// says so, the second (the same action again) discards. Same two-press rule
// the dashboard uses for empty labels and frame deletion.
function leaveOk(key) {
  if (!dirty() || discardArmed === key) { discardArmed = null; return true; }
  discardArmed = key;
  msgRich("Unsaved boxes on this frame. ",
          "Press Save (Enter) to keep them, or repeat the same action to discard them.", true);
  return false;
}

function dropSnap() {
  // A capture nobody saved: free the server's memory instead of waiting for eviction.
  if (cur && cur.src === "snap") {
    fetch("/api/snapshot/" + encodeURIComponent(cur.sid),
          { method: "DELETE", headers: { "X-Pawsight": "1" } }).catch(() => {});
  }
}

function resetFrameState() {
  sel = -1; drag = null; undo = []; pendingConfirm = false; discardArmed = null;
  dropDisarm();
}

async function openName(name, force) {
  if (busy) return;
  if (!force && cur && cur.src === "dataset" && cur.name === name) return;
  if (!force && !leaveOk("open:" + name)) return;
  busy = true;
  const d = await api("GET", "/api/dataset/" + encodeURIComponent(name) + "/meta")
    .catch(() => ({ ok: false, error: "server did not respond" }));
  if (!d.ok) { busy = false; toast("frame did not open: " + d.error, true); return; }
  dropSnap();
  resetFrameState();
  const boxes = (d.boxes || []).filter((b) => CLS.indexOf(b.cls) >= 0)
    .map((b) => fromWire(b, "auto"));
  cur = { src: "dataset", name, boxes, saved: boxesKey(boxes), size: [640, 360] };
  showImage("/api/dataset/" + encodeURIComponent(name) + ".jpg?t=" + Date.now(), () => {
    const it = item(name);
    $("l-src").textContent = short(name) + (it && it.human ? " · reviewed" : "");
    $("l-ring").hidden = true;
    renderPos();
    const e = queueEl.querySelector('li[data-name="' + CSS.escape(name) + '"]');
    if (e) e.scrollIntoView({ block: "nearest" });
  });
}

function step(d) {
  if (!view.length) return;
  let i = cur && cur.src === "dataset" ? view.indexOf(cur.name) : -1;
  i = i < 0 ? (d > 0 ? 0 : view.length - 1) : clamp(i + d, 0, view.length - 1);
  if (cur && cur.src === "dataset" && view[i] === cur.name) return;
  openName(view[i]);
}

function showImage(src, after) {
  img.onload = () => {
    busy = false;
    cur.size = [img.naturalWidth || 640, img.naturalHeight || 360];
    // The stage takes the frame's own proportions: no letterbox to miscount.
    stageEl.style.aspectRatio = cur.size[0] + " / " + cur.size[1];
    img.hidden = false;
    after && after();
    fit();
    changed(true);
    cv.focus();
  };
  img.onerror = () => { busy = false; toast("image failed to load", true); };
  img.src = src;
}

function badge(text) { $("l-badge").textContent = text; }

// ---------- capture from the camera ----------

async function take() {
  if (busy) return;
  if (!leaveOk("take")) return;
  busy = true;
  $("l-take").disabled = true;
  msg("waiting for frames from the detector…");
  try {
    const r = await api("POST", "/api/snapshot");
    if (!r.ok) throw new Error(r.error || "detector refused");
    const m = await api("GET", "/api/snapshot/" + encodeURIComponent(r.id));
    if (!m.ok || !(m.frames || []).length) throw new Error(m.error || "no frames");
    dropSnap();
    resetFrameState();
    cur = { src: "snap", sid: r.id, frames: m.frames, n: m.frames.length - 1,
            boxes: [], saved: "", size: [640, 360] };
    msg("");
    ringGo(cur.n, true);
  } catch (e) {
    busy = false;
    msg("capture failed: " + e.message, true);
  } finally {
    $("l-take").disabled = false;
  }
}

function ringGo(n, force) {
  if (!cur || cur.src !== "snap") return;
  n = clamp(n, 0, cur.frames.length - 1);
  if (!force && n === cur.n) return;
  if (!force && !leaveOk("ring:" + n)) return;
  resetFrameState();
  const f = cur.frames[n];
  cur.n = n;
  // The model's boxes on that very frame are the starting point: correcting
  // is faster than drawing, and they are shown dashed until touched.
  cur.boxes = (f.boxes || []).filter((b) => CLS.indexOf(b.cls) >= 0).map((b) => fromWire(b, "auto"));
  cur.saved = "";        // a capture is unsaved by definition
  busy = true;
  showImage(f.url, () => {
    $("l-src").textContent = "camera capture · " + new Date(f.ts * 1000).toLocaleTimeString();
    $("l-ring").hidden = false;
    $("l-ring-pos").textContent = "frame " + (n + 1) + " of " + cur.frames.length;
    $("l-ring-prev").disabled = n === 0;
    $("l-ring-next").disabled = n === cur.frames.length - 1;
    renderPos();
  });
}

// ---------- geometry ----------

function fit() {
  const r = cv.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(1, Math.round(r.width * dpr));
  cv.height = Math.max(1, Math.round(r.height * dpr));
  draw();
}
function px() {
  const r = cv.getBoundingClientRect();
  return r.width ? cv.width / r.width : (window.devicePixelRatio || 1);
}
function frameRect() {
  const sw = cur.size[0], sh = cur.size[1];
  const k = Math.min(cv.width / sw, cv.height / sh);
  return { x: (cv.width - sw * k) / 2, y: (cv.height - sh * k) / 2, k };
}
function toFrame(ev) {
  const r = cv.getBoundingClientRect(), fr = frameRect();
  const cx = (ev.clientX - r.left) * (cv.width / r.width);
  const cy = (ev.clientY - r.top) * (cv.height / r.height);
  return { x: clamp((cx - fr.x) / (fr.k * cur.size[0]), 0, 1),
           y: clamp((cy - fr.y) / (fr.k * cur.size[1]), 0, 1) };
}
function toCanvas(x, y) {
  const fr = frameRect();
  return { x: fr.x + x * fr.k * cur.size[0], y: fr.y + y * fr.k * cur.size[1] };
}

// ---------- drawing ----------

function draw() {
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (!cur || hide) return;
  const p = px();
  cur.boxes.forEach((b, i) => {
    const a = toCanvas(b.box[0], b.box[1]), c = toCanvas(b.box[2], b.box[3]);
    const col = COLOR[b.cls] || "#A2988A", on = i === sel;
    ctx.strokeStyle = b.src === "hint" && !on ? "#7FBDA0" : col;
    ctx.lineWidth = (on ? 2.5 : 1.75) * p;
    // Solid: touched by a human. Sparse dashes: the model's box.
    // Dense green dashes: a low-threshold hint, still to be decided.
    ctx.setLineDash(on ? [] : b.src === "hint" ? [3 * p, 3 * p] : b.src === "auto" ? [6 * p, 4 * p] : []);
    ctx.strokeRect(a.x, a.y, c.x - a.x, c.y - a.y);
    ctx.setLineDash([]);
    if (on) {
      ctx.fillStyle = col + "22";
      ctx.fillRect(a.x, a.y, c.x - a.x, c.y - a.y);
      [[a.x, a.y], [c.x, a.y], [c.x, c.y], [a.x, c.y]].forEach((h) => {
        ctx.fillStyle = col;
        ctx.fillRect(h[0] - HANDLE * p / 2, h[1] - HANDLE * p / 2, HANDLE * p, HANDLE * p);
      });
    }
    const tag = (b.src === "hint" ? "? " : "") + b.cls + (b.conf ? " " + Math.round(b.conf * 100) + "%" : "");
    ctx.font = "600 " + (11 * p).toFixed(1) + "px ui-monospace, Menlo, monospace";
    const tw = ctx.measureText(tag).width, th = 15 * p;
    const ty = a.y - th < 0 ? a.y : a.y - th;        // keep the tag inside the canvas
    ctx.fillStyle = "rgba(0,0,0,.72)";
    ctx.fillRect(a.x, ty, tw + 8 * p, th);
    ctx.fillStyle = col;
    ctx.fillText(tag, a.x + 4 * p, ty + th - 4 * p);
  });
  if (drag && drag.kind === "new") {
    const p1 = toCanvas(Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1));
    const p2 = toCanvas(Math.max(drag.x0, drag.x1), Math.max(drag.y0, drag.y1));
    ctx.strokeStyle = COLOR[drawCls];
    ctx.lineWidth = 2 * p;
    ctx.strokeRect(p1.x, p1.y, p2.x - p1.x, p2.y - p1.y);
  }
}

// Called after every edit: the tally, the buttons and the dirty mark follow.
function changed(fresh) {
  if (!fresh) { pendingConfirm = false; discardArmed = null; }
  const n = {};
  CLS.forEach((c) => { n[c] = 0; });
  (cur ? cur.boxes : []).forEach((b) => { n[b.cls]++; });
  const host = $("l-tally");
  host.textContent = "";
  CLS.forEach((c, i) => {
    if (i) host.appendChild(document.createTextNode(" · "));
    const e = document.createElement(n[c] ? "b" : "s");
    e.textContent = c + " " + n[c];
    host.appendChild(e);
  });
  const ds = !!cur && cur.src === "dataset";
  $("l-save").disabled = !cur;
  $("l-del").disabled = sel < 0;
  $("l-undo").disabled = !undo.length;
  $("l-clear").disabled = !cur || !cur.boxes.length;
  $("l-hint").disabled = !ds;
  $("l-drop").disabled = !ds;
  // Unsaved work is shown on the frame's badge, in the accent colour.
  const unsaved = dirty() || (!!cur && cur.src === "snap");
  const bd = $("l-badge");
  bd.classList.toggle("unsaved", unsaved);
  if (cur) bd.textContent = (cur.src === "snap" ? "CAMERA CAPTURE" : (item(cur.name) || {}).human ? "REVIEWED" : "DATASET FRAME") +
                            (unsaved ? " · not saved" : "");
  if (fresh && cur) {
    const miss = CLS.filter((c) => !n[c]);
    if (miss.length) {
      msgRich("no boxes: " + miss.join(", ") + ". ",
              "If they are in the frame, draw them: an unlabelled class is learned as background.");
    } else {
      msg("");
    }
  }
  draw();
}

// ---------- undo ----------

function pushUndo() {
  undo.push(JSON.stringify({ boxes: cur.boxes, sel }));
  if (undo.length > 100) undo.shift();
}
function popUndo() {
  if (!undo.length) return;
  const s = JSON.parse(undo.pop());
  cur.boxes = s.boxes;
  sel = s.sel < cur.boxes.length ? s.sel : -1;
  changed();
}

// ---------- hit-testing ----------

const area = (b) => (b[2] - b[0]) * (b[3] - b[1]);
function hitBox(p) {
  // Smallest first: otherwise a bowl inside a dog box could never be picked.
  const order = cur.boxes.map((b, i) => ({ i, a: area(b.box) })).sort((x, y) => x.a - y.a);
  for (const o of order) {
    const b = cur.boxes[o.i].box;
    if (p.x >= b[0] && p.x <= b[2] && p.y >= b[1] && p.y <= b[3]) return o.i;
  }
  return -1;
}
function hitHandle(p) {
  if (sel < 0) return -1;
  const b = cur.boxes[sel].box, fr = frameRect();
  const rx = HANDLE * px() / (fr.k * cur.size[0]), ry = HANDLE * px() / (fr.k * cur.size[1]);
  const pts = [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]];
  for (let i = 0; i < 4; i++)
    if (Math.abs(p.x - pts[i][0]) <= rx && Math.abs(p.y - pts[i][1]) <= ry) return i;
  return -1;
}

// ---------- mouse and touch ----------

cv.addEventListener("pointerdown", (e) => {
  if (!cur || busy || hide) return;
  cv.setPointerCapture(e.pointerId);
  cv.focus();
  const p = toFrame(e), h = hitHandle(p);
  if (h >= 0) {
    const b = cur.boxes[sel].box;
    pushUndo();
    drag = { kind: "size", ax: (h === 0 || h === 3) ? b[2] : b[0],
                           ay: (h === 0 || h === 1) ? b[3] : b[1], moved: false };
  } else {
    const i = hitBox(p);
    if (i >= 0) {
      sel = i;
      setCls(cur.boxes[i].cls, false);
      pushUndo();
      drag = { kind: "move", i, px: p.x, py: p.y, b0: cur.boxes[i].box.slice(), moved: false };
    } else {
      sel = -1;
      drag = { kind: "new", x0: p.x, y0: p.y, x1: p.x, y1: p.y };
    }
  }
  changed();
});

cv.addEventListener("pointermove", (e) => {
  if (!drag) return;
  const p = toFrame(e);
  if (drag.kind === "new") { drag.x1 = p.x; drag.y1 = p.y; }
  else if (drag.kind === "size") { cur.boxes[sel].box = norm4([drag.ax, drag.ay, p.x, p.y]); drag.moved = true; }
  else {
    const b = drag.b0;
    const dx = clamp(p.x - drag.px, -b[0], 1 - b[2]), dy = clamp(p.y - drag.py, -b[1], 1 - b[3]);
    if (Math.abs(dx) + Math.abs(dy) > 0.001) drag.moved = true;
    cur.boxes[drag.i].box = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy];
  }
  draw();
});

function endDrag() {
  if (!drag) return;
  if (drag.kind === "new") {
    const w = Math.abs(drag.x1 - drag.x0), h = Math.abs(drag.y1 - drag.y0);
    if (w >= 0.008 && h >= 0.008) {             // smaller is a misclick, not a box
      pushUndo();
      cur.boxes.push({ cls: drawCls, src: "user", conf: null,
                       box: norm4([drag.x0, drag.y0, drag.x1, drag.y1]) });
      sel = cur.boxes.length - 1;
    }
  } else if (drag.moved) {
    const i = drag.kind === "move" ? drag.i : sel;
    if (i >= 0 && cur.boxes[i]) cur.boxes[i].src = "user";
  } else {
    undo.pop();                                   // a plain click changed nothing
  }
  drag = null;
  changed();
}
cv.addEventListener("pointerup", endDrag);
cv.addEventListener("pointercancel", () => { if (drag && drag.kind !== "new") popUndo(); drag = null; draw(); });

// ---------- keyboard ----------

document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
  if (e.key === "c" || e.key === "C") {
    if (!e.metaKey && !e.ctrlKey) { take(); e.preventDefault(); }
    return;
  }
  if (!cur) {
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") { step(e.key === "ArrowRight" ? 1 : -1); e.preventDefault(); }
    return;
  }
  if ((e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z")) { popUndo(); e.preventDefault(); return; }
  if (e.key === "Escape") {
    if (drag) { if (drag.kind !== "new") popUndo(); drag = null; }
    sel = -1; changed(); e.preventDefault(); return;
  }
  if (e.key === "h" || e.key === "H") { toggleHide(); e.preventDefault(); return; }
  if (e.key === "Delete" || e.key === "Backspace") { delSel(); e.preventDefault(); return; }
  if (e.key === "Enter") { if (t && t.tagName === "BUTTON" && t !== $("l-save")) return; save(); e.preventDefault(); return; }
  if (e.key === "Tab" && t === cv && cur.boxes.length) {
    sel = (sel + (e.shiftKey ? cur.boxes.length - 1 : 1)) % cur.boxes.length;
    if (sel < 0) sel = cur.boxes.length - 1;
    setCls(cur.boxes[sel].cls, false);
    changed(); e.preventDefault(); return;
  }
  const ci = "123456789".indexOf(e.key);
  if (ci >= 0 && !e.metaKey && !e.ctrlKey && ci < CLS.length) { setCls(CLS[ci], true); changed(); e.preventDefault(); return; }
  if (sel < 0 && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
    if (cur.src === "snap") ringGo(cur.n + (e.key === "ArrowRight" ? 1 : -1));
    else step(e.key === "ArrowRight" ? 1 : -1);
    e.preventDefault(); return;
  }
  const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
  if (d && sel >= 0) {
    // In frame pixels, so a step looks the same at any window size.
    const st = e.shiftKey ? 10 : 1;
    const b = cur.boxes[sel].box;
    if (!e.repeat) pushUndo();
    const dx = clamp(d[0] * st / cur.size[0], -b[0], 1 - b[2]);
    const dy = clamp(d[1] * st / cur.size[1], -b[1], 1 - b[3]);
    cur.boxes[sel].box = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy];
    cur.boxes[sel].src = "user";
    changed(); e.preventDefault();
  }
});

function toggleHide() {
  hide = !hide;
  $("l-hide").setAttribute("aria-pressed", hide ? "true" : "false");
  draw();
}

function delSel() {
  if (!cur || sel < 0) return;
  pushUndo();
  cur.boxes.splice(sel, 1);
  sel = -1;
  changed();
}

// ---------- save ----------

async function save() {
  if (!cur || busy) return;
  busy = true;
  $("l-save").disabled = true;
  const boxes = cur.boxes.map(toWire), confirm = pendingConfirm;
  pendingConfirm = false;
  const was = cur;
  let r;
  try {
    r = cur.src === "dataset"
      ? await api("POST", "/api/dataset/" + encodeURIComponent(cur.name) + "/save",
                  { boxes, confirm_empty: confirm })
      : await api("POST", "/api/snapshot/" + encodeURIComponent(cur.sid) + "/save",
                  { frame: cur.n, boxes, confirm_empty: confirm });
  } catch (e) {
    r = null;
  }
  busy = false;
  $("l-save").disabled = false;
  if (cur !== was) return;
  if (!r) { msg("server did not respond: the boxes are kept, try again", true); return; }
  if (!r.ok) {
    if (r.need_confirm) {
      // Not silent: an unlabelled class is learned as background, and every
      // zone in the project hangs on the bowl class.
      pendingConfirm = true;
      msgRich((r.missing || []).join(", ") + ": ",
              (r.error || "") + " If they are really not in the frame, press Save again.", true);
      return;
    }
    msg("not saved: " + (r.error || "unknown error"), true);
    return;
  }
  const kinds = CLS.filter((c) => boxes.some((b) => b.cls === c));
  if (cur.src === "dataset") {
    const it = item(cur.name);
    if (it) {
      if (counts) {
        // Keep the header honest without reloading (a reload re-sorts the
        // list under the user's hands).
        if (!it.human) counts.human = (counts.human || 0) + 1;
        const had = it.kinds || [];
        counts.no_bowl = (counts.no_bowl || 0) - (had.indexOf("bowl") < 0) + (kinds.indexOf("bowl") < 0);
        counts.empty = (counts.empty || 0) - !had.length + !kinds.length;
      }
      it.human = true; it.n = r.labels; it.kinds = kinds;
    }
    cur.saved = boxesKey(cur.boxes);
    toast("saved " + short(cur.name) + ": " +
          (r.labels ? r.labels + " boxes" : "empty labels, a negative example"));
    renderList();
    const i = view.indexOf(cur.name);
    if (i >= 0 && i < view.length - 1) openName(view[i + 1], true);
    else { changed(true); msg("saved. That was the last frame in the list."); }
    return;
  }
  // A capture becomes a dataset frame: show it from there, so what is on
  // screen is exactly what went to disk.
  toast("added to the dataset: " + short(r.image) + " (" + r.labels + " boxes), dataset pairs: " + r.pairs);
  items.unshift({ name: r.image, ts: Date.now() / 1000, score: 0, why: [], n: r.labels,
                  kinds, human: true });
  if (counts) { counts.all = (counts.all || 0) + 1; counts.human = (counts.human || 0) + 1; }
  cur = null;
  renderList();
  openName(r.image, true);
}

// ---------- hint, clear, delete frame ----------

async function hint() {
  if (!cur || cur.src !== "dataset" || busy) return;
  const asked = cur.name, btn = $("l-hint");
  btn.disabled = true;
  msg("re-running the frame at a low threshold…");
  const r = await api("POST", "/api/dataset/" + encodeURIComponent(asked) + "/hint")
    .catch(() => ({ ok: false, error: "detector did not respond" }));
  btn.disabled = false;
  if (!cur || cur.name !== asked) return;     // the frame changed meanwhile
  if (!r.ok) { msg("hint failed: " + r.error, true); return; }
  let added = 0;
  pushUndo();
  (r.boxes || []).forEach((b) => {
    if (CLS.indexOf(b.cls) < 0) return;
    const nb = fromWire(b, "hint");
    const dup = cur.boxes.some((o) => o.cls === b.cls &&
      Math.abs(o.box[0] - nb.box[0]) < 0.05 && Math.abs(o.box[1] - nb.box[1]) < 0.05 &&
      Math.abs(o.box[2] - nb.box[2]) < 0.05);
    if (!dup) { cur.boxes.push(nb); added++; }
  });
  if (!added) undo.pop();
  changed();
  msg(added ? "suggested boxes: " + added + " (green dashes). Delete the wrong ones, then Save."
            : "the model found nothing new even at a low threshold");
}

function clearAll() {
  if (!cur || !cur.boxes.length) return;
  pushUndo();
  const n = cur.boxes.length;
  cur.boxes = [];
  sel = -1;
  changed();
  msgRich("cleared " + n + " boxes. ",
          "Save stores the frame as a negative example: right only if it really has no " +
          CLS.join(", ") + ". Ctrl+Z brings them back.");
}

function dropDisarm() {
  dropArmed = false;
  clearTimeout(dropTimer);
  const b = $("l-drop");
  b.classList.remove("armed");
  b.textContent = "Delete frame";
}

async function dropFrame() {
  if (!cur || cur.src !== "dataset" || busy) return;
  const name = cur.name;
  if (!dropArmed) {
    dropArmed = true;
    const b = $("l-drop");
    b.classList.add("armed");
    b.textContent = "Delete frame? Press again";
    const it = item(name) || {};
    msg("frame " + short(name) + " will be erased from disk with its labels (" +
        (typeof it.n === "number" ? it.n : cur.boxes.length) + " boxes" +
        (it.human ? ", reviewed by hand" : "") + "). This cannot be undone.", true);
    dropTimer = setTimeout(() => { dropDisarm(); msg(""); }, 15000);
    return;
  }
  dropDisarm();
  busy = true;
  const r = await fetch("/api/dataset/" + encodeURIComponent(name),
                        { method: "DELETE", headers: { "X-Pawsight": "1" } })
    .then((x) => x.json()).catch(() => ({ ok: false, error: "server did not respond" }));
  busy = false;
  if (!r.ok) { msg("not deleted: " + r.error, true); return; }
  toast("deleted " + short(name) + ", dataset pairs: " + r.pairs +
        (r.was_val ? " (it was in the validation set)" : ""));
  const i = view.indexOf(name);
  const it = item(name);
  items = items.filter((x) => x.name !== name);
  if (counts && it) {
    counts.all = Math.max(0, (counts.all || 0) - 1);
    if (it.human) counts.human = Math.max(0, (counts.human || 0) - 1);
    if ((it.kinds || []).indexOf("bowl") < 0) counts.no_bowl = Math.max(0, (counts.no_bowl || 0) - 1);
    if (!(it.kinds || []).length) counts.empty = Math.max(0, (counts.empty || 0) - 1);
  }
  cur = null;
  renderList();
  if (view.length) openName(view[clamp(i, 0, view.length - 1)], true);
  else { img.hidden = true; badge("no frame"); changed(true); msg("no frames left"); }
}

// ---------- wiring ----------

$("l-take").addEventListener("click", take);
$("l-reload").addEventListener("click", loadList);
$("l-filter").addEventListener("change", (e) => { filter = e.target.value; renderList(); });
document.querySelectorAll(".lside-ctrls .seg-b").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".lside-ctrls .seg-b").forEach((o) => o.classList.toggle("on", o === b));
  order = b.dataset.ord;
  loadList();
}));
$("l-prev").addEventListener("click", () => step(-1));
$("l-next").addEventListener("click", () => step(1));
$("l-ring-prev").addEventListener("click", () => cur && ringGo(cur.n - 1));
$("l-ring-next").addEventListener("click", () => cur && ringGo(cur.n + 1));
$("l-save").addEventListener("click", save);
$("l-undo").addEventListener("click", popUndo);
$("l-hide").addEventListener("click", toggleHide);
$("l-hint").addEventListener("click", hint);
$("l-del").addEventListener("click", delSel);
$("l-clear").addEventListener("click", clearAll);
$("l-drop").addEventListener("click", dropFrame);
window.addEventListener("resize", () => { if (cur) fit(); });
window.addEventListener("beforeunload", (e) => {
  if (dirty() || (cur && cur.src === "snap")) { e.preventDefault(); e.returnValue = ""; }
});

(async function init() {
  await loadClasses();
  changed(true);
  msg("");
  await loadList();
  const want = new URLSearchParams(location.search).get("frame");
  if (want && item(want)) openName(want, true);
})();
