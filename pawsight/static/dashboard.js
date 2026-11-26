// Dashboard logic (ES module). Moved out of dashboard.html (code review,
// 2026-11-21); loaded after /player/video-stream.js, as before.
(function () {
  "use strict";
  var WS_ORIGIN = location.origin.replace(/^http/, "ws");

  // All mutating requests go ONLY through here: the server requires the
  // X-Pawsight header. It is non-standard, so a foreign page cannot send it
  // without a preflight, and the server does not allow that preflight.
  // 127.0.0.1 is reachable from any site open in the browser, and without
  // this anyone could "pan the camera" or "delete runs/".
  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json", "X-Pawsight": "1" },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json(); });
  }
  var cv = document.getElementById("overlay");
  var ctx = cv.getContext("2d");
  var badge = document.getElementById("badge");
  var last = null;

  // ---- video ----
  // Wait for the custom element to register, or setting src goes nowhere.
  customElements.whenDefined("video-stream").then(function () {
    var el = document.createElement("video-stream");
    el.mode = "webrtc,mse";
    el.style.width = "100%";
    el.style.height = "100%";
    el.src = WS_ORIGIN + "/api/stream?src=dog_cam";
    var slot = document.getElementById("player-slot");
    slot.replaceWith(el);
    el.id = "player";
  }).catch(function (e) {
    badge.textContent = "player failed to load: " + e;
  });

  // ---- overlay ----
  // Observations arrive in detection-frame coordinates (1280x720). The video
  // is shown at a different size, so everything is scaled by one factor: the
  // aspect ratios match (both 16:9).
  function fit() {
    var dpr = window.devicePixelRatio || 1;
    [cv, shotEl].forEach(function (c) {
      var r = c.getBoundingClientRect();
      if (!r.width) return;                 // a hidden canvas has no size
      c.width = Math.round(r.width * dpr);
      c.height = Math.round(r.height * dpr);
    });
    draw();
    if (shot) drawShot();
  }
  window.addEventListener("resize", fit);
  // The tab strip and the labelling bar change .stage height WITHOUT a window
  // resize, and all coordinate math depends on the canvas size.
  if (window.ResizeObserver) new ResizeObserver(fit).observe(document.getElementById("stage"));

  function roundRect(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function label(text, x, y, color) {
    // Scale from the DETECTOR FRAME width (640), not the old 1280: otherwise
    // labels are half the size of the boxes they belong to.
    ctx.font = "600 " + (13 * (cv.width / 640)).toFixed(1) + "px ui-monospace, Menlo, monospace";
    var w = ctx.measureText(text).width;
    var h = 17 * (cv.width / 640);
    ctx.fillStyle = "rgba(0,0,0,.72)";
    roundRect(x, y - h, w + 10, h, 3);
    ctx.fill();
    ctx.fillStyle = color;
    ctx.fillText(text, x + 5, y - h * 0.28);
  }

  // Room zone colours match the image the owner approved them on
  // (zones.yaml, the "ROOM ZONES" block): the dashboard and that image must
  // read as the same place, or there is nothing to check by eye.
  // Without an entry here, a new zone would get the trigger's grey and blend
  // into it and the others.
  var ZONE_COLOR = {
    water:   "#63A8E0",
    food:    "#7FBDA0",
    bowls:   "#8A8175",
    door:    "#E84848",
    carpet:  "#4285F4",
    kitchen: "#F0B414",
    desk:    "#3CBE78"
  };

  function draw() {
    // In still-frame mode live boxes are not drawn: they are invisible under
    // the opaque image but would be confusing on exit.
    if (frozen()) return;
    ctx.clearRect(0, 0, cv.width, cv.height);
    if (!last) return;
    // The fallback size is the DETECTOR frame size. It has long been 640x360,
    // but this said 1280x720: an observation without a size field was drawn at half size.
    var src = last.size || [640, 360];
    var k = cv.width / src[0];

    // zones
    var zones = last.zones || {};
    // The "at the bowls" trigger is wider than the strips: it covers the gap
    // between them plus a margin for detector noise. A dog inside counts as
    // "at the bowls", so its outline is shown as a thin dashed line, with no
    // fill or label, so it does not compete with the water/food hints.
    Object.keys(zones).forEach(function (name) {
      var z = zones[name], p = z.poly, c = ZONE_COLOR[name] || "#8A8175";
      var trigger = name === "bowls";
      ctx.beginPath();
      p.forEach(function (pt, i) {
        var x = pt[0] * k, y = pt[1] * k;
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.closePath();
      ctx.strokeStyle = c;
      ctx.lineWidth = trigger ? Math.max(1, 1 * k) : Math.max(1.5, 2 * k);
      ctx.setLineDash(trigger ? [3 * k, 4 * k] : [6 * k, 5 * k]);
      ctx.stroke();
      ctx.setLineDash([]);
      if (trigger) return;
      ctx.fillStyle = c + "18";
      ctx.fill();
      // The label goes at the leftmost vertex. The strips start at the bowls and
      // extend left, toward the carpet: at the first vertex both labels met
      // above the bowls and covered them and each other. For a rectangle the
      // leftmost is the top-left, i.e. the same spot as before.
      var at = p.reduce(function (a, b) {
        return (b[0] < a[0] || (b[0] === a[0] && b[1] < a[1])) ? b : a;
      }, p[0]);
      label(z.label, at[0] * k, at[1] * k - 3, c);
    });

    // detected bowls
    (last.anchors || []).forEach(function (a) {
      var b = a.box;
      ctx.strokeStyle = "#8A8175";
      ctx.lineWidth = Math.max(1, 1.5 * k);
      ctx.strokeRect(b[0] * k, b[1] * k, (b[2] - b[0]) * k, (b[3] - b[1]) * k);
    });

    // people: shown only, not followed
    (last.persons || []).forEach(function (p) {
      var b = p.box, c = "#6E86A8";
      ctx.strokeStyle = c;
      ctx.lineWidth = Math.max(1, 1.5 * k);
      ctx.setLineDash([4 * k, 4 * k]);
      ctx.strokeRect(b[0] * k, b[1] * k, (b[2] - b[0]) * k, (b[3] - b[1]) * k);
      ctx.setLineDash([]);
      label("person", b[0] * k, b[1] * k - 3, c);
    });

    // dog
    (last.dogs || []).forEach(function (d) {
      var b = d.box, c = "#E0A063";
      ctx.strokeStyle = c;
      ctx.lineWidth = Math.max(2, 3 * k);
      roundRect(b[0] * k, b[1] * k, (b[2] - b[0]) * k, (b[3] - b[1]) * k, 4 * k);
      ctx.stroke();
      // floor contact point: zone membership is computed from it
      ctx.beginPath();
      ctx.arc(((b[0] + b[2]) / 2) * k, b[3] * k, Math.max(3, 4.5 * k), 0, Math.PI * 2);
      ctx.fillStyle = c;
      ctx.fill();
      var tag = "dog " + Math.round(d.conf * 100) + "%";
      if (d.zones && d.zones.length) {
        var z = d.zones.filter(function (n) { return n !== "bowls"; });
        if (z.length) tag += " · " + (zones[z[0]] ? zones[z[0]].label : z[0]);
      }
      label(tag, b[0] * k, b[1] * k - 3, c);
    });
  }

  // ---- live observation stream ----
  var wsRetry = null;
  function connectLive() {
    wsRetry = null;
    var ws = new WebSocket(location.origin.replace(/^http/, "ws") + "/api/live");
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
    ws.onmessage = function (e) {
      var msg;
      try { msg = JSON.parse(e.data); } catch (err) { return; }
      // An observation arrives WITHOUT a type field, for historical reasons. So
      // dispatch by type first, and only then "no type means observation".
      // Unknown types are silently skipped: otherwise a new backend message
      // would become an empty observation and wipe the overlay off the video.
      if (msg && msg.type) {
        if (msg.type === "camera_log") addCamlog(msg, true);
        else if (msg.type === "logs") { (msg.items || []).forEach(logPush); logCount(); }
        else if (msg.type === "model") { model = msg.status; if (modelLoaded) renderModel(); }
        else if (msg.type === "snapshot") onSnapshot(msg);
        return;
      }
      last = msg;
      var n = (last.dogs || []).length;
      document.getElementById("d-det").className = "dot on";
      if (frozen()) return;                 // badge and boxes belong to the still frame
      if (last.camera_moving) {
        badge.textContent = "camera moving: events paused";
        badge.className = "badge stale";
      } else {
        badge.textContent = n ? (n === 1 ? "dog in frame" : n + " dogs in frame") : "no dog";
        badge.className = "badge";
      }
      var ts = document.getElementById("tilt-status");
      if (last.tilt && last.tilt.status) {
        var s = last.tilt.status;
        ts.textContent = s + (last.tilt.dy_px != null && s !== "on target" ? " (" + (last.tilt.dy_px > 0 ? "+" : "") + last.tilt.dy_px + " px)" : "");
        ts.style.color = s === "on target" ? "var(--ok)" : (s.indexOf("corrected") === 0 ? "var(--accent)" : "var(--warn)");
      }
      var ps = document.getElementById("patrol-status");
      var pt = last.follow && last.follow.patrol;
      if (pt) {
        // The offset from home is always shown: it tells whether the camera has
        // really returned, not just believes it has.
        ps.textContent = pt.status + " (" + (pt.home > 0 ? "+" : "") + pt.home + " widths)";
        ps.style.color = pt.state === "home"
          ? (pt.home_confirmed ? "var(--ok)" : "var(--warn)")
          : "var(--accent)";
      }
      draw();
    };
    ws.onclose = function () {
      document.getElementById("d-det").className = "dot off";
      if (!frozen()) badge.textContent = "no connection to detector";
      // Without the flag, each failed handshake spawned another retry timer.
      if (!wsRetry) wsRetry = setTimeout(connectLive, 3000);
    };
  }

  // ---- still frame and manual labelling ----
  //
  // WHY FREEZE. The video arrives over MSE about a second late, while the
  // detector works on a frame about 0.2 s old. So at the moment of the click
  // the user is looking at an image OLDER than what the detector returns, and
  // a box drawn on the live video would not match the saved frame: a silent
  // miss. So we label exactly the frame that goes to disk, and a ring of six
  // frames lets the user step back to the right moment.
  // The base three; the real list (PAWSIGHT_CLASSES may add furniture) comes
  // from /api/label/classes below. Without it, boxes of an added class would
  // be filtered out of the editor and lost on the next "Save".
  var CLS = ["dog", "bowl", "person"];
  var CLS_NAME = { dog: "dog", bowl: "bowl", person: "person" };
  var CLS_COLOR = { dog: "#E0A063", bowl: "#8A8175", person: "#6E86A8" };
  var CLS_EXTRA_COLOR = ["#7FBDA0", "#E0B054", "#DE7C63", "#8FC6D6", "#B7C46A", "#D68FA8"];
  var shotMode = "off";          // off | wait | edit | saving
  // One editor serves two jobs: a fresh still frame ("live") and review of
  // what is already in the dataset ("dataset"). Coordinate math, drawing and
  // box hit-testing are shared; only the image source, the toolbar and the
  // save URL differ.
  var shot = null;               // {source, id|name, frames, n, boxes, sel, drawCls}
  var drag = null;
  var stageEl = document.getElementById("stage");
  var stillEl = document.getElementById("still");
  var shotEl = document.getElementById("shot");
  var sctx = shotEl.getContext("2d");
  var barMsgEl = document.getElementById("bar-msg");
  var HANDLE = window.matchMedia("(pointer:coarse)").matches ? 16 : 9;   // CSS pixels

  function frozen() { return shotMode !== "off"; }
  function barMsg(t, bad) {
    barMsgEl.textContent = t || "";
    barMsgEl.className = "bar-msg" + (bad ? " bad" : "");
  }
  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function norm4(b) {
    return [Math.min(b[0], b[2]), Math.min(b[1], b[3]),
            Math.max(b[0], b[2]), Math.max(b[1], b[3])];
  }

  // ---- geometry ----
  // Three coordinate systems: event CSS pixels -> canvas pixels -> frame FRACTIONS.
  // The latter are exactly a YOLO label line, so they are what goes over the
  // wire: no resolution constant on either end.
  //
  // Event -> canvas uses the SIZE RATIO, not devicePixelRatio: fit() rounds
  // width/height, so dpr and the true ratio differ by fractions of a pixel,
  // and this way page zoom is handled too.
  function frameRect() {
    // object-fit:contain. Right now .stage is 16:9 and the frame is 16:9, so
    // there are no margins and one factor, but as soon as the proportions
    // differ, boxes would drift, silently. Compute it properly.
    var sw = shot.size[0], sh = shot.size[1];
    var k = Math.min(shotEl.width / sw, shotEl.height / sh);
    return { x: (shotEl.width - sw * k) / 2, y: (shotEl.height - sh * k) / 2, k: k };
  }
  function cssPx() {
    var r = shotEl.getBoundingClientRect();
    return r.width ? shotEl.width / r.width : (window.devicePixelRatio || 1);
  }
  function toFrame(ev) {                    // event -> frame FRACTIONS [0..1]
    var r = shotEl.getBoundingClientRect(), fr = frameRect();
    var cx = (ev.clientX - r.left) * (shotEl.width / r.width);
    var cy = (ev.clientY - r.top) * (shotEl.height / r.height);
    return { x: clamp((cx - fr.x) / (fr.k * shot.size[0]), 0, 1),
             y: clamp((cy - fr.y) / (fr.k * shot.size[1]), 0, 1) };
  }
  function toCanvas(x, y) {                 // frame fractions -> canvas pixels
    var fr = frameRect();
    return { x: fr.x + x * fr.k * shot.size[0], y: fr.y + y * fr.k * shot.size[1] };
  }

  // ---- drawing ----
  function drawShot() {
    if (!shot) return;
    var px = cssPx();
    sctx.clearRect(0, 0, shotEl.width, shotEl.height);
    shot.boxes.forEach(function (b, i) {
      var a = toCanvas(b.box[0], b.box[1]), c = toCanvas(b.box[2], b.box[3]);
      var col = CLS_COLOR[b.cls] || "#8A8175", sel = i === shot.sel;
      sctx.strokeStyle = b.src === "hint" && !sel ? "#7FBDA0" : col;
      sctx.lineWidth = (sel ? 2.5 : 1.75) * px;
      // Solid: touched by a human; sparse dashes: model box;
      // dense green dashes: low-threshold hint, still to be decided.
      sctx.setLineDash(sel ? [] :
        b.src === "hint" ? [3 * px, 3 * px] :
        b.src === "auto" ? [5 * px, 4 * px] : []);
      sctx.strokeRect(a.x, a.y, c.x - a.x, c.y - a.y);
      sctx.setLineDash([]);
      if (sel) {
        sctx.fillStyle = col + "1F";
        sctx.fillRect(a.x, a.y, c.x - a.x, c.y - a.y);
        [[a.x, a.y], [c.x, a.y], [c.x, c.y], [a.x, c.y]].forEach(function (h) {
          sctx.fillStyle = col;
          sctx.fillRect(h[0] - HANDLE * px / 2, h[1] - HANDLE * px / 2, HANDLE * px, HANDLE * px);
        });
      }
      var tag = (b.src === "hint" ? "? " : "") + CLS_NAME[b.cls] +
                (b.conf ? " " + Math.round(b.conf * 100) + "%" : "");
      sctx.font = "600 " + (11 * px).toFixed(1) + "px ui-monospace, Menlo, monospace";
      var tw = sctx.measureText(tag).width, th = 15 * px;
      sctx.fillStyle = "rgba(0,0,0,.72)";
      sctx.fillRect(a.x, a.y - th, tw + 8 * px, th);
      sctx.fillStyle = col;
      sctx.fillText(tag, a.x + 4 * px, a.y - 4 * px);
    });
    if (drag && drag.kind === "new") {
      var p1 = toCanvas(Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1));
      var p2 = toCanvas(Math.max(drag.x0, drag.x1), Math.max(drag.y0, drag.y1));
      sctx.strokeStyle = CLS_COLOR[shot.drawCls];
      sctx.lineWidth = 2 * px;
      sctx.strokeRect(p1.x, p1.y, p2.x - p1.x, p2.y - p1.y);
    }
  }

  // ---- hit-testing ----
  function area(b) { return (b[2] - b[0]) * (b[3] - b[1]); }
  function hitBox(p) {
    // Check small boxes first: otherwise a box inside a large one cannot be selected.
    var order = shot.boxes.map(function (b, i) { return { i: i, a: area(b.box) }; })
                          .sort(function (x, y) { return x.a - y.a; });
    for (var n = 0; n < order.length; n++) {
      var b = shot.boxes[order[n].i].box;
      if (p.x >= b[0] && p.x <= b[2] && p.y >= b[1] && p.y <= b[3]) return order[n].i;
    }
    return -1;
  }
  function hitHandle(p) {
    if (shot.sel < 0) return -1;
    var b = shot.boxes[shot.sel].box, fr = frameRect();
    var rx = HANDLE * cssPx() / (fr.k * shot.size[0]);
    var ry = HANDLE * cssPx() / (fr.k * shot.size[1]);
    var pts = [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]];
    for (var i = 0; i < 4; i++)
      if (Math.abs(p.x - pts[i][0]) <= rx && Math.abs(p.y - pts[i][1]) <= ry) return i;
    return -1;
  }

  // ---- class tally ----
  // Shown ALWAYS, before "save" is pressed, not as a popup afterwards.
  // Every dataset class must be labelled in EVERY frame, or it is learned as
  // background: the one mistake that breaks the project silently.
  function shotTally() {
    var ds = shot.source === "dataset";
    var host = document.getElementById(ds ? "rv-tally" : "shot-tally"), n = {};
    CLS.forEach(function (c) { n[c] = 0; });
    shot.boxes.forEach(function (b) { n[b.cls]++; });
    host.textContent = "";
    CLS.forEach(function (c, i) {
      if (i) host.appendChild(document.createTextNode(" · "));
      var el = document.createElement(n[c] ? "b" : "s");
      el.textContent = CLS_NAME[c] + " " + n[c];
      host.appendChild(el);
    });
    var miss = CLS.filter(function (c) { return !n[c]; })
                  .map(function (c) { return CLS_NAME[c]; });
    if (miss.length) {
      barMsgEl.textContent = "";
      barMsgEl.className = "bar-msg";
      var b = document.createElement("b");
      b.textContent = "no boxes: " + miss.join(", ") + ". ";
      barMsgEl.appendChild(b);
      barMsgEl.appendChild(document.createTextNode(
        "If they are in the frame, draw them: an unlabelled class is learned as background."));
    } else {
      barMsg("");
    }
    document.getElementById(ds ? "rv-del" : "shot-del").disabled = shot.sel < 0;
    if (ds) {
      var i = qIndex(shot.name);
      document.getElementById("rv-pos").textContent =
        (i >= 0 ? (i + 1) + " of " + qList.length : shot.name);
    } else {
      document.getElementById("shot-when").textContent =
        "frame " + (shot.n + 1) + " of " + shot.frames.length;
    }
  }

  // ---- mode transitions ----
  function shotTake() {
    if (frozen()) return;
    shotMode = "wait";                 // set BEFORE the request: the broadcast
    shot = null;                       // snapshot can arrive before the response
    barMsg("waiting for a frame from the detector…");
    document.getElementById("shot-take").disabled = true;
    var t = setTimeout(function () {
      if (shotMode === "wait") { shotExit(); toast("detector sent no frame", true); }
    }, 9000);
    post("/api/snapshot").then(function (r) {
      if (!r.ok) { clearTimeout(t); shotExit(); toast("failed: " + r.error, true); }
    }).catch(function () { clearTimeout(t); shotExit(); toast("detector did not respond", true); });
  }

  function onSnapshot(m) {
    if (shotMode !== "wait" || !(m.frames || []).length) return;
    shot = { id: m.id, frames: m.frames, n: m.frames.length - 1,
             size: m.frames[m.frames.length - 1].size || [640, 360],
             boxes: [], sel: -1, drawCls: "dog" };
    loadFrame(shot.n, true);
  }

  function loadFrame(n, enter) {
    var f = shot.frames[n];
    shot.n = n;
    shot.size = f.size || [640, 360];
    shot.boxes = (f.boxes || []).filter(function (b) { return CLS.indexOf(b.cls) >= 0; })
      .map(function (b) {
        return { cls: b.cls, conf: b.conf, src: "auto",
                 box: norm4([b.cx - b.w / 2, b.cy - b.h / 2, b.cx + b.w / 2, b.cy + b.h / 2]) };
      });
    shot.sel = -1;
    stillEl.onload = function () {
      if (enter) shotEnter();
      else { fit(); shotTally(); drawShot(); }
    };
    stillEl.onerror = function () { shotExit(); toast("frame failed to load", true); };
    stillEl.src = f.url;
  }

  function shotEnter() {
    var ds = shot.source === "dataset";
    shotMode = "edit";
    stageEl.classList.add("frozen");
    stillEl.hidden = false;
    shotEl.hidden = false;
    document.querySelector(".bar-live").hidden = true;
    document.querySelector(".bar-edit").hidden = ds;
    document.querySelector(".bar-review").hidden = !ds;
    badge.textContent = ds ? "LABEL REVIEW" : "STILL FRAME · labelling";
    badge.className = "badge shot";
    ctx.clearRect(0, 0, cv.width, cv.height);   // live boxes are not needed under the frame
    fit();                                       // AFTER hidden=false: a hidden element has no size
    shotTally();
    drawShot();
    shotEl.focus();
  }

  function shotExit(keepMsg) {
    if (shot && shot.source === "dataset") {
      var was = shot.name;
      dropDisarm();
      shotMode = "off"; shot = null; drag = null;
      stageEl.classList.remove("frozen");
      stillEl.hidden = true; stillEl.removeAttribute("src");
      shotEl.hidden = true;
      document.querySelector(".bar-live").hidden = false;
      document.querySelector(".bar-review").hidden = true;
      document.getElementById("shot-take").disabled = false;
      if (!keepMsg) barMsg("");
      badge.className = "badge";
      badge.textContent = last ? "" : "waiting for detector…";
      qMark(was);
      draw();
      return;
    }
    if (shot && shot.id) {
      fetch("/api/snapshot/" + shot.id, { method: "DELETE", headers: { "X-Pawsight": "1" } })
        .catch(function () {});
    }
    shotMode = "off"; shot = null; drag = null;
    stageEl.classList.remove("frozen");
    stillEl.hidden = true; stillEl.removeAttribute("src");
    shotEl.hidden = true;
    document.querySelector(".bar-live").hidden = false;
    document.querySelector(".bar-edit").hidden = true;
    document.querySelector(".bar-review").hidden = true;
    document.getElementById("shot-take").disabled = false;
    if (!keepMsg) barMsg("");
    badge.className = "badge";
    badge.textContent = last ? "" : "waiting for detector…";
    draw();
  }

  // ---- mouse and touch ----
  shotEl.addEventListener("pointerdown", function (e) {
    if (shotMode !== "edit") return;
    shotEl.setPointerCapture(e.pointerId);
    shotEl.focus();
    var p = toFrame(e), h = hitHandle(p);
    if (h >= 0) {
      var b = shot.boxes[shot.sel].box;       // anchor is the opposite corner
      drag = { kind: "size", ax: (h === 0 || h === 3) ? b[2] : b[0],
                             ay: (h === 0 || h === 1) ? b[3] : b[1] };
    } else {
      var i = hitBox(p);
      if (i >= 0) {
        shot.sel = i;
        drag = { kind: "move", i: i, px: p.x, py: p.y, b0: shot.boxes[i].box.slice() };
      } else {
        shot.sel = -1;
        drag = { kind: "new", x0: p.x, y0: p.y, x1: p.x, y1: p.y };
      }
    }
    shotTally(); drawShot();
  });
  shotEl.addEventListener("pointermove", function (e) {
    if (!drag) return;
    var p = toFrame(e);
    if (drag.kind === "new") { drag.x1 = p.x; drag.y1 = p.y; }
    else if (drag.kind === "size") shot.boxes[shot.sel].box = norm4([drag.ax, drag.ay, p.x, p.y]);
    else {
      var b = drag.b0;
      var dx = clamp(p.x - drag.px, -b[0], 1 - b[2]);
      var dy = clamp(p.y - drag.py, -b[1], 1 - b[3]);
      shot.boxes[drag.i].box = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy];
    }
    drawShot();
  });
  function endDrag() {
    if (!drag) return;
    if (drag.kind === "new") {
      var w = Math.abs(drag.x1 - drag.x0), h = Math.abs(drag.y1 - drag.y0);
      if (w >= 0.008 && h >= 0.008) {         // smaller is a misclick, not a box
        shot.boxes.push({ cls: shot.drawCls, src: "user", conf: null,
                          box: norm4([drag.x0, drag.y0, drag.x1, drag.y1]) });
        shot.sel = shot.boxes.length - 1;
      }
    } else {
      var idx = drag.kind === "move" ? drag.i : shot.sel;
      if (idx >= 0 && shot.boxes[idx]) shot.boxes[idx].src = "user";
    }
    drag = null; shotTally(); drawShot();
  }
  shotEl.addEventListener("pointerup", endDrag);
  shotEl.addEventListener("pointercancel", function () { drag = null; drawShot(); });

  // ---- still-frame keyboard ----
  shotEl.addEventListener("keydown", function (e) {
    if (shotMode !== "edit") return;
    if (e.key === "Escape") { if (drag) { drag = null; drawShot(); } else shotExit(); e.preventDefault(); return; }
    if (e.key === "Delete" || e.key === "Backspace") {
      if (shot.sel >= 0) { shot.boxes.splice(shot.sel, 1); shot.sel = -1; shotTally(); drawShot(); }
      e.preventDefault(); return;
    }
    var ci = "123456789".indexOf(e.key);
    if (ci >= 0 && ci < CLS.length && !e.metaKey && !e.ctrlKey) {
      setCls(CLS[ci]);
      if (shot.sel >= 0) { shot.boxes[shot.sel].cls = CLS[ci]; shot.boxes[shot.sel].src = "user"; }
      shotTally(); drawShot(); e.preventDefault(); return;
    }
    if (e.key === "Enter" && shot.source === "dataset") { qSaveClick(); e.preventDefault(); return; }
    // Arrows: if a box is SELECTED they move it, otherwise they page the queue.
    // Clicking empty space clears the selection, so switching is easy.
    if (shot.source === "dataset" && shot.sel < 0 &&
        (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
      qStep(e.key === "ArrowRight" ? 1 : -1);
      e.preventDefault();
      return;
    }
    var d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
    if (d && shot.sel >= 0) {
      // Step in frame pixels, not fractions: that way it is the same at any window size.
      var st = (e.shiftKey ? 10 : 1);
      var dx = d[0] * st / shot.size[0], dy = d[1] * st / shot.size[1];
      var b = shot.boxes[shot.sel].box;
      dx = clamp(dx, -b[0], 1 - b[2]); dy = clamp(dy, -b[1], 1 - b[3]);
      shot.boxes[shot.sel].box = [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy];
      shot.boxes[shot.sel].src = "user";
      drawShot(); e.preventDefault();
    }
  });

  function setCls(c) {
    shot.drawCls = c;
    document.querySelectorAll('.bar-edit .seg-b, .bar-review .seg-b').forEach(function (b) {
      b.classList.toggle("on", b.dataset.cls === c);
    });
  }

  // ---- labelling bar buttons ----
  document.getElementById("shot-take").addEventListener("click", shotTake);
  document.getElementById("shot-cancel").addEventListener("click", function () { shotExit(); });
  document.getElementById("shot-del").addEventListener("click", function () {
    if (shot.sel >= 0) { shot.boxes.splice(shot.sel, 1); shot.sel = -1; shotTally(); drawShot(); }
  });
  function bindClassButtons(bar) {
    document.querySelectorAll(bar + ' .seg-b').forEach(function (b) {
      b.addEventListener("click", function () {
        if (!shot) return;
        setCls(b.dataset.cls);
        if (shot.sel >= 0) { shot.boxes[shot.sel].cls = b.dataset.cls; shot.boxes[shot.sel].src = "user"; }
        shotTally(); drawShot();
      });
    });
  }
  bindClassButtons(".bar-edit");
  // Rebuild both class selectors from the server's list.
  fetch("/api/label/classes").then(function (r) { return r.json(); }).then(function (d) {
    if (!d.ok || !d.classes || d.classes.join() === CLS.join()) return;
    CLS = d.classes;
    var k = 0;
    CLS.forEach(function (c) {
      if (!CLS_NAME[c]) CLS_NAME[c] = c;
      if (!CLS_COLOR[c]) CLS_COLOR[c] = CLS_EXTRA_COLOR[k++ % CLS_EXTRA_COLOR.length];
    });
    [".bar-edit", ".bar-review"].forEach(function (bar) {
      var seg = document.querySelector(bar + " .seg");
      seg.textContent = "";
      CLS.forEach(function (c, i) {
        var b = document.createElement("button");
        b.className = "seg-b" + (i === 0 ? " on" : "");
        b.dataset.cls = c;
        b.appendChild(document.createTextNode(CLS_NAME[c] + " "));
        if (i < 9) { var kb = document.createElement("kbd"); kb.textContent = String(i + 1); b.appendChild(kb); }
        seg.appendChild(b);
      });
      bindClassButtons(bar);
    });
  }).catch(function () {});
  document.getElementById("shot-prev").addEventListener("click", function () {
    if (shot && shot.n > 0) loadFrame(shot.n - 1, false);
  });
  document.getElementById("shot-next").addEventListener("click", function () {
    if (shot && shot.n < shot.frames.length - 1) loadFrame(shot.n + 1, false);
  });

  function shotSave(confirmEmpty) {
    if (shotMode !== "edit") return;
    shotMode = "saving";
    document.getElementById("shot-save").disabled = true;
    var body = {
      frame: shot.n, confirm_empty: !!confirmEmpty,
      boxes: shot.boxes.map(function (b) {
        var x = b.box;
        return { cls: b.cls, cx: (x[0] + x[2]) / 2, cy: (x[1] + x[3]) / 2,
                 w: x[2] - x[0], h: x[3] - x[1] };
      })
    };
    post("/api/snapshot/" + shot.id + "/save", body).then(function (r) {
      if (r.ok) {
        toast("frame saved: " + r.image + ", dataset pairs: " + r.pairs);
        shotExit();
        if (modelLoaded) loadModel();
        return;
      }
      shotMode = "edit";
      document.getElementById("shot-save").disabled = false;
      if (r.need_confirm) {
        // Not silently: the cost of a mistake is silently broken zones.
        barMsg(r.error + " Press \"Save\" again if they are not in the frame.", true);
        pendingEmpty = true;
        return;
      }
      barMsg("not saved: " + r.error, true);
    }).catch(function () {
      shotMode = "edit";
      document.getElementById("shot-save").disabled = false;
      // Labels are NOT lost: a network failure is no reason to discard the user's work.
      barMsg("server did not respond: labels are kept, try again", true);
    });
  }
  var pendingEmpty = false;
  document.getElementById("shot-save").addEventListener("click", function () {
    var again = pendingEmpty; pendingEmpty = false;
    shotSave(again);
  });

  // ---- label review queue ----
  // There is deliberately no "reviewed" button: suspiciousness is computed from
  // the label CONTENT, so a fixed frame leaves the top of the list by itself.
  // A frame where "dog only" is true (no bowls in frame) is demoted once
  // saved: the detector records it in human_labeled.json.
  var qList = [], qOrder = "suspicious", qLoaded = false, qDone = {};
  // Header counters come from the server. Keep a copy so that after a frame is
  // deleted the line can be fixed without reloading the whole list: a reload
  // would re-sort the queue under the user's hands.
  var qCounts = null;
  var queueEl = document.getElementById("queue");

  function qIndex(name) {
    for (var i = 0; i < qList.length; i++) if (qList[i].name === name) return i;
    return -1;
  }
  function qMark(name) {
    var li = queueEl.querySelector('[data-name="' + name + '"]');
    if (li) li.classList.remove("on");
  }

  function qRender() {
    queueEl.textContent = "";
    if (!qList.length) {
      queueEl.appendChild(cell("pane-empty", "no frames in the dataset"));
      return;
    }
    var frag = document.createDocumentFragment();
    qList.forEach(function (it) {
      var li = document.createElement("li");
      li.dataset.name = it.name;
      if (it.human || qDone[it.name]) li.className = "done";
      li.appendChild(cell("sc", it.human || qDone[it.name] ? "✓" : it.score.toFixed(1)));
      li.appendChild(cell("nm", it.name.replace(/^(dog|manual)_/, "")));
      li.appendChild(cell("wy", (it.why || []).join(", ")));
      li.appendChild(cell("kd", (it.kinds || []).map(function (k) {
        return (CLS_NAME[k] || k).slice(0, 3); }).join("·") || "empty"));
      li.addEventListener("click", function () { qOpen(it.name); });
      frag.appendChild(li);
    });
    queueEl.appendChild(frag);
  }

  function qStat(d) {
    var c = d.counts || {}, host = document.getElementById("q-stat");
    host.textContent = "";
    var b = document.createElement("b"); b.textContent = c.all || 0;
    host.appendChild(b);
    host.appendChild(document.createTextNode(" frames · human-edited "));
    var h = document.createElement("b"); h.textContent = c.human || 0;
    host.appendChild(h);
    host.appendChild(document.createTextNode(" · no bowls "));
    var nb = document.createElement("s"); nb.textContent = c.no_bowl || 0;
    host.appendChild(nb);
    host.appendChild(document.createTextNode(" · dog only "));
    var od = document.createElement("s"); od.textContent = c.dog_only || 0;
    host.appendChild(od);

    var note = document.getElementById("q-note");
    note.textContent = "";
    note.appendChild(document.createTextNode(
      "Labels are produced by the model itself, so a frame enters the dataset with its " +
      "mistakes, and they go into training as ground truth. "));
    var bb = document.createElement("b");
    bb.textContent = "Check \"bowl\" boxes first";
    note.appendChild(bb);
    note.appendChild(document.createTextNode(
      ": the current model's mAP50 on them is 0.597, it often misses them, and all " +
      "zones depend on this class. The \"Hint\" button re-runs the frame at a low " +
      "threshold and shows candidates as dashed boxes."));
  }

  function qLoad() {
    fetch("/api/dataset?order=" + qOrder).then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) { queueEl.textContent = ""; queueEl.appendChild(
          cell("pane-empty", "detector did not respond: " + (d.error || ""))); return; }
        qList = d.items || [];
        qCounts = d.counts || {};
        qStat(d); qRender();
      }).catch(function () {});
  }

  function qOpen(name) {
    if (frozen() && shot && shot.source === "dataset") {
      // already in the editor: just switch the frame
      qShow(name);
      return;
    }
    if (frozen()) return;
    shotMode = "wait";
    qShow(name, true);
  }

  function qShow(name, enter) {
    dropDisarm();                       // the armed state does not carry over to another frame
    fetch("/api/dataset/" + encodeURIComponent(name) + "/meta", {
      method: "GET", headers: { "X-Pawsight": "1" }
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok) { toast("frame failed to open: " + d.error, true); if (enter) shotExit(); return; }
      shot = { source: "dataset", name: name, size: [640, 360], sel: -1,
               drawCls: "dog", frames: [], n: 0,
               boxes: (d.boxes || []).filter(function (b) { return CLS.indexOf(b.cls) >= 0; })
                 .map(function (b) {
                   return { cls: b.cls, conf: null, src: "auto",
                            box: norm4([b.cx - b.w / 2, b.cy - b.h / 2,
                                        b.cx + b.w / 2, b.cy + b.h / 2]) };
                 }) };
      stillEl.onload = function () {
        // Take the size from the image ITSELF: the dataset holds frames from
        // several generations (1280x720, 640x360, 2304x1296), and assuming a
        // constant 640x360 would misplace boxes on all the older ones.
        shot.size = [stillEl.naturalWidth || 640, stillEl.naturalHeight || 360];
        if (enter) shotEnter(); else { fit(); shotTally(); drawShot(); }
        queueEl.querySelectorAll("li").forEach(function (li) {
          li.classList.toggle("on", li.dataset.name === name); });
        var li = queueEl.querySelector('[data-name="' + name + '"]');
        if (li) li.scrollIntoView({ block: "nearest" });
      };
      stillEl.onerror = function () { toast("image failed to load", true); if (enter) shotExit(); };
      stillEl.src = "/api/dataset/" + encodeURIComponent(name) + ".jpg";
    }).catch(function () { toast("server did not respond", true); if (enter) shotExit(); });
  }

  function qStep(d) {
    var i = qIndex(shot.name);
    if (i < 0) return;
    var j = Math.min(qList.length - 1, Math.max(0, i + d));
    if (j !== i) qShow(qList[j].name);
  }

  // A second "Save" press confirms that the vanished class really is not in
  // the frame. The flag lives only until the frame changes: otherwise a
  // confirmation given for one frame would silently apply to the next.
  var pendingDsEmpty = false;

  function qSave(advance, confirmEmpty) {
    if (shotMode !== "edit") return;
    shotMode = "saving";
    var name = shot.name, sent = shot.boxes.map(function (b) {
      var x = b.box;
      return { cls: b.cls, cx: (x[0] + x[2]) / 2, cy: (x[1] + x[3]) / 2,
               w: x[2] - x[0], h: x[3] - x[1] };
    });
    post("/api/dataset/" + encodeURIComponent(name) + "/save", {
      boxes: sent, confirm_empty: !!confirmEmpty
    }).then(function (r) {
      shotMode = "edit";
      if (!r.ok) {
        if (r.need_confirm) {
          // Not silently: the cost of a mistake is silently broken zones. Class
          // names are mapped here: on the wire they are the data.yaml strings,
          // and this is where the UI could rename them for display.
          var gone = (r.missing || []).map(function (c) { return CLS_NAME[c] || c; });
          barMsg(gone.length
            ? "the labels had boxes for: " + gone.join(", ") + ", and now they are gone. " +
              "An unlabelled class is learned as background, and all zones depend on the bowl. " +
              "If they are not in the frame, press \"Save\" again."
            : r.error + " Press \"Save\" again.", true);
          pendingDsEmpty = true;
          return;
        }
        barMsg("not saved: " + r.error, true);
        return;
      }
      pendingDsEmpty = false;
      qDone[name] = true;
      var li = queueEl.querySelector('[data-name="' + name + '"]');
      if (li) { li.className = "done"; li.firstChild.textContent = "✓"; }
      var it = qList[qIndex(name)];
      // Update kinds together with n: the header counters are computed from
      // them when a frame leaves the queue, and a stale list would corrupt them silently.
      if (it) {
        it.human = true;
        it.n = r.labels;
        it.kinds = CLS.filter(function (c) {
          return sent.some(function (b) { return b.cls === c; }); });
      }
      // A toast, not the bar under the frame: moving to the next frame
      // overwrites the bar with the class tally, and the save would look like
      // nothing happened ("removed a box, pressed save, and it is still there",
      // when it is actually a different frame).
      toast("saved: " + name.replace(/^(dog|manual)_/, "") + ": " +
            (r.labels ? r.labels + " boxes" : "empty labels, negative example"));
      if (advance) qStep(1); else barMsg("saved: " + r.labels + " boxes");
    }).catch(function () {
      shotMode = "edit";
      // Labels are NOT lost: a network failure is no reason to discard the user's work.
      barMsg("server did not respond: labels are kept, try again", true);
    });
  }

  function qHint() {
    if (shotMode !== "edit" || !shot) return;
    var btn = document.getElementById("rv-hint"), asked = shot.name;
    btn.disabled = true; barMsg("re-running the frame at a low threshold…");
    post("/api/dataset/" + encodeURIComponent(asked) + "/hint").then(function (r) {
      btn.disabled = false;
      // A hint takes seconds, and meanwhile the frame can be switched with an
      // arrow or deleted. Boxes of another frame put into the open one would go
      // to disk on the next "Save".
      if (!shot || shot.name !== asked) return;
      if (!r.ok) { barMsg("hint failed: " + r.error, true); return; }
      var had = shot.boxes.length, added = 0;
      (r.boxes || []).forEach(function (b) {
        if (CLS.indexOf(b.cls) < 0) return;
        var nb = norm4([b.cx - b.w / 2, b.cy - b.h / 2, b.cx + b.w / 2, b.cy + b.h / 2]);
        // A similar box of the same class already exists: do not duplicate.
        var dup = shot.boxes.some(function (o) {
          return o.cls === b.cls && Math.abs(o.box[0] - nb[0]) < 0.05 &&
                 Math.abs(o.box[1] - nb[1]) < 0.05 && Math.abs(o.box[2] - nb[2]) < 0.05;
        });
        if (dup) return;
        shot.boxes.push({ cls: b.cls, conf: b.conf, src: "hint", box: nb });
        added++;
      });
      shotTally(); drawShot();
      barMsg(added ? "boxes suggested: " + added + " (dashed). Remove extras with Delete."
                   : "the model found nothing new even at threshold 0.05");
    }).catch(function () { btn.disabled = false; barMsg("detector did not respond", true); });
  }

  function qSaveClick() {
    var again = pendingDsEmpty;
    pendingDsEmpty = false;
    qSave(true, again);
  }

  // ---- clear all labels ----
  // A separate button, not "select and delete one by one": a frame with a
  // bogus box on a chair base usually needs clearing entirely, and a tiny box
  // is hard to even select with a click. It only reaches disk on "Save":
  // until the user presses it, the old labels stay on disk.
  function qClear() {
    if (shotMode !== "edit" || !shot) return;
    var n = shot.boxes.length;
    if (!n) { barMsg("there are no boxes"); return; }
    shot.boxes = [];
    shot.sel = -1;
    shotTally(); drawShot();
    barMsgEl.textContent = "";
    barMsgEl.className = "bar-msg";
    var b = document.createElement("b");
    b.textContent = "boxes cleared: " + n + ". ";
    barMsgEl.appendChild(b);
    barMsgEl.appendChild(document.createTextNode(
      "\"Save\" will store the frame as a negative example, which is valid if the " +
      "frame really has no dog, bowls or people. If they are there, draw them: " +
      "an unlabelled class is learned as background."));
  }

  // ---- remove a frame from the dataset ----
  // Irreversible, so it takes two presses, the same trick as empty still-frame
  // labels: the cost of the action is stated, not asked as "are you sure?".
  // The armed state expires after 15 seconds and on switching to another
  // frame: otherwise it would outlive a user who got distracted for a minute.
  var dropArmed = 0, dropTimer = null;
  function dropDisarm() {
    pendingDsEmpty = false;      // called on frame change and exit: see qShow
    dropArmed = 0;
    if (dropTimer) { clearTimeout(dropTimer); dropTimer = null; }
    var btn = document.getElementById("rv-drop");
    btn.classList.remove("armed");
    btn.textContent = "Delete frame";
  }
  function qDrop() {
    if (shotMode !== "edit" || !shot) return;
    var btn = document.getElementById("rv-drop"), name = shot.name;
    if (!dropArmed) {
      dropArmed = 1;
      btn.classList.add("armed");
      btn.textContent = "Delete frame? Press again";
      var it = qList[qIndex(name)] || {};
      // Count boxes FROM DISK (it.n from the queue), not from the canvas: the
      // user may have just cleared them all without saving, and "0 boxes"
      // would misstate what is about to be lost.
      var n = typeof it.n === "number" ? it.n : shot.boxes.length;
      barMsg("frame " + name + " will be erased from disk with its labels (" +
             n + " boxes" + (it.human || qDone[name] ? ", human-edited" : "") +
             "), and cannot be restored. Press \"Delete frame\" again.", true);
      // 15 seconds: enough to read the message above. The armed state does not
      // last forever, so a stray click a minute later does not erase the frame.
      dropTimer = setTimeout(function () { dropDisarm(); barMsg(""); }, 15000);
      return;
    }
    dropDisarm();
    shotMode = "saving";
    btn.disabled = true;
    barMsg("deleting frame…");
    fetch("/api/dataset/" + encodeURIComponent(name),
          { method: "DELETE", headers: { "X-Pawsight": "1" } })
      .then(function (r) { return r.json(); })
      .then(function (r) {
        btn.disabled = false;
        shotMode = "edit";
        if (!r.ok) { barMsg("not deleted: " + (r.error || "detector refused"), true); return; }
        toast("frame deleted: " + name + ", dataset pairs: " + r.pairs);
        if (modelLoaded) loadModel();      // the "Model" tab shows the same counters
        qForget(name, r.was_val);
      }).catch(function () {
        btn.disabled = false;
        shotMode = "edit";
        barMsg("server did not respond: the frame is most likely still there", true);
      });
  }

  // Drop the deleted frame from the queue and move to the next one. The list
  // is NOT reloaded: qLoad would re-sort the queue under the user's hands and
  // they would lose their place.
  function qForget(name, wasVal) {
    var i = qIndex(name);
    if (i >= 0) {
      var it = qList[i];
      qList.splice(i, 1);
      if (qCounts) {
        var kinds = it.kinds || [];
        qCounts.all = Math.max(0, (qCounts.all || 0) - 1);
        if (it.human) qCounts.human = Math.max(0, (qCounts.human || 0) - 1);
        if (kinds.indexOf("bowl") < 0) qCounts.no_bowl = Math.max(0, (qCounts.no_bowl || 0) - 1);
        if (kinds.length === 1 && kinds[0] === "dog")
          qCounts.dog_only = Math.max(0, (qCounts.dog_only || 0) - 1);
        if (!kinds.length) qCounts.empty = Math.max(0, (qCounts.empty || 0) - 1);
        qStat({ counts: qCounts });
      }
    }
    delete qDone[name];
    qRender();
    var tail = wasVal ? " The frame was in the validation set, which is now smaller." : "";
    if (!qList.length) {
      shotExit(true);
      barMsg("no frames left in the dataset." + tail);
      return;
    }
    if (tail) toast(tail.trim());
    qShow(qList[Math.min(Math.max(i, 0), qList.length - 1)].name);
  }

  // ---- back to home ----
  // "Exit" leaves the user where they were, in the queue on the "Dataset"
  // tab. From there, getting to live video means scrolling up and switching
  // tabs, and the review bar has already scrolled off screen.
  function qHome() {
    shotExit();
    setTab("cam");
    window.scrollTo(0, 0);
  }

  document.getElementById("q-reload").addEventListener("click", qLoad);
  document.querySelectorAll('#tp-data .seg-b').forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll('#tp-data .seg-b').forEach(function (o) { o.classList.remove("on"); });
      b.classList.add("on"); qOrder = b.dataset.ord; qLoad();
    });
  });
  document.getElementById("rv-prev").addEventListener("click", function () { qStep(-1); });
  document.getElementById("rv-next").addEventListener("click", function () { qStep(1); });
  document.getElementById("rv-save").addEventListener("click", function () { qSaveClick(); });
  document.getElementById("rv-hint").addEventListener("click", qHint);
  document.getElementById("rv-cancel").addEventListener("click", function () { shotExit(); });
  document.getElementById("rv-del").addEventListener("click", function () {
    if (shot.sel >= 0) { shot.boxes.splice(shot.sel, 1); shot.sel = -1; shotTally(); drawShot(); }
  });
  document.getElementById("rv-clear").addEventListener("click", qClear);
  document.getElementById("rv-drop").addEventListener("click", qDrop);
  document.getElementById("rv-home").addEventListener("click", qHome);
  bindClassButtons(".bar-review");

  // ---- tabs ----
  // All tab contents always stay in the DOM: logs must accumulate on a hidden
  // tab too, or switching to it would show nothing.
  var TABS = ["cam", "log", "model", "data"];
  var tab = "cam";
  function setTab(name) {
    if (TABS.indexOf(name) < 0) name = "cam";
    tab = name;
    TABS.forEach(function (n) {
      var b = document.getElementById("tb-" + n), pn = document.getElementById("tp-" + n);
      var on = n === name;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
      pn.hidden = !on;
    });
    try { localStorage.setItem("pawsight.tab", name); } catch (e) {}
    if (name === "log") {
      if (!logLoaded) { logLoaded = true; loadLogs(); }
      // A hidden element has no scrollHeight: finish scrolling to the bottom here.
      if (logFollow) listEl.scrollTop = listEl.scrollHeight;
    }
    if (name === "model" && !modelLoaded) { modelLoaded = true; loadModel(); }
    if (name === "data" && !qLoaded) { qLoaded = true; qLoad(); }
  }
  document.querySelectorAll('.tabs [role=tab]').forEach(function (b) {
    b.addEventListener("click", function () { setTab(b.dataset.tab); });
  });
  document.querySelector(".tabs").addEventListener("keydown", function (e) {
    var i = TABS.indexOf(tab), n = null;
    if (e.key === "ArrowRight") n = TABS[(i + 1) % TABS.length];
    else if (e.key === "ArrowLeft") n = TABS[(i + TABS.length - 1) % TABS.length];
    else if (e.key === "Home") n = TABS[0];
    else if (e.key === "End") n = TABS[TABS.length - 1];
    if (!n) return;
    e.preventDefault();
    setTab(n);
    document.getElementById("tb-" + n).focus();
  });

  // ---- camera feed ----
  var KIND_NAME = { search: "search", follow: "follow", edge: "edge of travel", tilt: "tilt",
                    stream: "stream", manual: "manual", ptz: "pan",
                    dataset: "dataset", train: "training", brain: "brain", error: "error" };
  var camlogEl = document.getElementById("camlog");
  var camlogCount = 0;
  // A buffer separate from the markup: the kind filter must also show what
  // has already left the DOM.
  var camBuf = [], camOff = {};
  var CAM_BUF_MAX = 300, CAM_DOM_MAX = 150;
  function hhmmss(ts) {
    var d = new Date(ts * 1000);
    return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2) + ":" + ("0" + d.getSeconds()).slice(-2);
  }
  // Rows are built from NODES, not concatenated innerHTML. Text here comes
  // from logs and from the VLM, and the MQTT broker is anonymous: any local
  // process could publish a string with markup, it would land in SQLite and
  // execute on every page reload.
  function cell(cls, text, title) {
    var el = document.createElement("span");
    el.className = cls;
    el.textContent = text;
    if (title) el.title = title;
    return el;
  }
  function camRow(item, fresh) {
    var li = document.createElement("li");
    var lvl = item.level === "error" || item.level === "critical" ? "error"
            : item.level === "warning" ? "warning" : "";
    li.className = lvl + (fresh ? " new" : "");
    // strip prefixes like "search: ": the kind is already in the tag
    var text = String(item.text || "").replace(/^(search|follow|tilt)\s*[:：]\s*/i, "");
    var kind = KIND_NAME[item.kind] ? item.kind : "manual";
    li.appendChild(cell("t", hhmmss(item.ts)));
    li.appendChild(cell("k k-" + kind, KIND_NAME[kind]));
    li.appendChild(cell("x", text, text));
    return li;
  }
  function addCamlog(item, fresh) {
    camBuf.push(item);
    if (camBuf.length > CAM_BUF_MAX) camBuf.splice(0, camBuf.length - CAM_BUF_MAX);
    var kind = KIND_NAME[item.kind] ? item.kind : "manual";
    if (!camOff[kind]) {
      camlogEl.insertBefore(camRow(item, fresh), camlogEl.firstChild);
      while (camlogEl.children.length > CAM_DOM_MAX) camlogEl.removeChild(camlogEl.lastChild);
    }
    camlogCount++;
    document.getElementById("camlog-n").textContent = "latest " + Math.min(camlogCount, CAM_DOM_MAX);
    if (fresh) camKinds();
  }
  function camEmpty(what) {
    var li = document.createElement("li");
    li.appendChild(cell("t", "—"));
    li.appendChild(cell("k k-manual", "empty"));
    li.appendChild(cell("x", what));
    camlogEl.appendChild(li);
  }
  function camRender() {
    camlogEl.textContent = "";
    var vis = camBuf.filter(function (it) {
      return !camOff[KIND_NAME[it.kind] ? it.kind : "manual"];
    }).slice(-CAM_DOM_MAX);
    if (!vis.length) { camEmpty(camBuf.length ? "all kinds hidden by the filter" : "no actions yet"); return; }
    vis.forEach(function (it) { camlogEl.insertBefore(camRow(it, false), camlogEl.firstChild); });
  }
  // Filter tags are the same .k as in the rows: they double as the legend,
  // so there is no separate legend of kinds.
  function camKinds() {
    var host = document.getElementById("cam-kinds"), seen = {}, order = [];
    camBuf.forEach(function (it) {
      var k = KIND_NAME[it.kind] ? it.kind : "manual";
      if (!seen[k]) { seen[k] = 1; order.push(k); }
    });
    if (order.join() === host.dataset.shown) return;   // do not redraw needlessly
    host.dataset.shown = order.join();
    host.textContent = "";
    order.forEach(function (k) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "k k-" + k + (camOff[k] ? " off" : "");
      b.textContent = KIND_NAME[k];
      b.setAttribute("aria-pressed", camOff[k] ? "false" : "true");
      b.title = camOff[k] ? "show" : "hide (⌘-click: only this kind)";
      b.addEventListener("click", function (e) {
        if (e.metaKey || e.ctrlKey) order.forEach(function (o) { camOff[o] = o !== k; });
        else camOff[k] = !camOff[k];
        host.dataset.shown = "";
        camKinds(); camRender();
      });
      host.appendChild(b);
    });
  }
  function loadCamlog() {
    fetch("/api/camera_log?limit=120").then(function (r) { return r.json(); }).then(function (d) {
      camBuf = []; camlogCount = 0;
      d.items.slice().reverse().forEach(function (it) { camBuf.push(it); camlogCount++; });
      camKinds(); camRender();
      document.getElementById("camlog-n").textContent = camBuf.length
        ? "latest " + Math.min(camlogCount, CAM_DOM_MAX) : "";
    }).catch(function () {});
  }

  // ---- raw logs ----
  // Order is BOTTOM-UP, like tail -f. The reason is concrete: the detector
  // calls log.exception, so multi-line tracebacks enter the stream, and
  // reverse order would print them upside down, with the cause below the effect.
  //
  // Autoscroll and pause are DIFFERENT things. Autoscroll is derived from the
  // scroll position: scroll up and it turns off. Pause is pressed by hand and
  // stops insertion itself, so a line can be selected and copied.
  var LVL = { debug: 0, info: 1, warning: 2, error: 3, critical: 4 };
  var LOG_BUF_MAX = 2000, LOG_DOM_MAX = 600;
  var logBuf = [], logPaused = false, logFollow = true, logQ = "", logLvl = 0, logMissed = 0;
  var listEl = document.getElementById("loglist");
  var jumpEl = document.getElementById("log-jump");
  var logLoaded = false;

  function lvlNo(name) { var v = LVL[String(name || "info").toLowerCase()]; return v == null ? 1 : v; }
  function logPass(r) {
    if (lvlNo(r.level) < logLvl) return false;
    if (!logQ) return true;
    return ((r.text || "") + " " + (r.logger || "")).toLowerCase().indexOf(logQ) >= 0;
  }

  // Match highlighting WITHOUT innerHTML: only text nodes and <mark>.
  function hilite(host, text, q) {
    if (!q) { host.textContent = text; return; }
    var low = text.toLowerCase(), i = 0, j;
    while ((j = low.indexOf(q, i)) >= 0) {
      if (j > i) host.appendChild(document.createTextNode(text.slice(i, j)));
      var mk = document.createElement("mark");
      mk.textContent = text.slice(j, j + q.length);
      host.appendChild(mk);
      i = j + q.length;
    }
    host.appendChild(document.createTextNode(text.slice(i)));
  }

  function logRow(r) {
    var d = document.createElement("div");
    d.className = "lg s-" + (r.src || "detect") + " l-" + String(r.level || "info").toLowerCase();
    d.appendChild(cell("t", hhmmss(r.ts)));
    d.appendChild(cell("v", String(r.level || "info").slice(0, 3).toUpperCase()));
    d.appendChild(cell("n", String(r.logger || "").replace(/^pawsight\./, ""), r.logger || ""));
    var m = document.createElement("span");
    m.className = "m";
    hilite(m, String(r.text || ""), logQ);
    d.appendChild(m);
    return d;
  }

  function logAppend(r) {
    listEl.appendChild(logRow(r));
    // Trimming from the top shifts the content: if the user is reading above,
    // compensate the scroll by the removed height, or the text "jumps".
    while (listEl.childElementCount - 1 > LOG_DOM_MAX) {
      var first = listEl.querySelector(".lg");
      if (!first) break;
      var h = first.offsetHeight;
      listEl.removeChild(first);
      if (!logFollow) listEl.scrollTop -= h;
    }
    if (logFollow) listEl.scrollTop = listEl.scrollHeight;
    else { logMissed++; jumpEl.hidden = false; jumpEl.textContent = "▼ " + logMissed + " new"; }
  }

  function logCount() {
    document.getElementById("log-n").textContent = logBuf.length + " lines";
  }

  function logPush(r) {
    logBuf.push(r);
    if (logBuf.length > LOG_BUF_MAX) logBuf.splice(0, logBuf.length - LOG_BUF_MAX);
    if (!logPaused && logPass(r)) logAppend(r);
  }

  function logRender() {
    // jump lives inside the list (sticky), so only the rows are cleared
    [].slice.call(listEl.querySelectorAll(".lg")).forEach(function (n) { listEl.removeChild(n); });
    var vis = logBuf.filter(logPass), from = Math.max(0, vis.length - LOG_DOM_MAX);
    var frag = document.createDocumentFragment();
    for (var i = from; i < vis.length; i++) frag.appendChild(logRow(vis[i]));
    listEl.insertBefore(frag, jumpEl);
    listEl.scrollTop = listEl.scrollHeight;
    logFollow = true; logMissed = 0; jumpEl.hidden = true;
    logCount();
  }

  listEl.addEventListener("scroll", function () {
    var atEnd = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight < 24;
    logFollow = atEnd;
    if (atEnd) { logMissed = 0; jumpEl.hidden = true; }
  });
  jumpEl.addEventListener("click", function () {
    logFollow = true; logMissed = 0; jumpEl.hidden = true;
    listEl.scrollTop = listEl.scrollHeight;
  });

  document.querySelectorAll('#tp-log .seg-b').forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll('#tp-log .seg-b').forEach(function (o) { o.classList.remove("on"); });
      b.classList.add("on");
      logLvl = b.dataset.lvl ? lvlNo(b.dataset.lvl) : 0;
      logRender();
    });
  });

  var qTimer = null;
  document.getElementById("log-q").addEventListener("input", function (e) {
    clearTimeout(qTimer);
    var v = e.target.value;
    qTimer = setTimeout(function () { logQ = v.trim().toLowerCase(); logRender(); }, 120);
  });

  var pauseBtn = document.getElementById("log-pause");
  pauseBtn.addEventListener("click", function () {
    logPaused = !logPaused;
    pauseBtn.setAttribute("aria-pressed", logPaused ? "true" : "false");
    pauseBtn.textContent = logPaused ? "Resume" : "Pause";
    if (!logPaused) logRender();
  });

  function loadLogs() {
    fetch("/api/logs?limit=400").then(function (r) { return r.json(); }).then(function (d) {
      logBuf = (d.items || []).slice();
      logRender();
      if (!logBuf.length) logCount();
    }).catch(function () {});
  }

  // ---- "Model" tab ----
  var model = null, modelLoaded = false;

  function mb(n) {
    if (n == null) return "—";           // unknown
    if (!n) return "0 B";                // zero is zero, not a dash

    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(0) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  }
  function ymd(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" +
           ("0" + d.getDate()).slice(-2) + " " + hhmm(ts);
  }
  function hms(sec) {
    sec = Math.max(0, Math.round(sec));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h ? h + " h " + m + " min" : (m ? m + " min " + (sec % 60) + " s" : sec + " s");
  }
  function kv(host, label, value, cls) {
    var dt = document.createElement("dt"); dt.textContent = label;
    var dd = document.createElement("dd"); dd.textContent = value;
    if (cls) dd.className = cls;
    host.appendChild(dt); host.appendChild(dd);
  }

  function renderModel() {
    if (!model) return;
    document.getElementById("m-wait").hidden = true;
    document.getElementById("m-body").hidden = false;
    var m = model.model || {}, det = model.detector || {}, ds = model.dataset || {};

    var mh = document.getElementById("m-model"); mh.textContent = "";
    kv(mh, "weights", m.name || "—", m.finetuned ? "ok" : "");
    kv(mh, "size", mb(m.bytes) + (m.sha256_12 ? "  " + m.sha256_12 : ""), "dim");
    kv(mh, "updated", ymd(m.mtime), "dim");
    kv(mh, "device", det.device || "—");
    kv(mh, "frame", (det.infer_size || []).join("×") + "  imgsz " + (det.imgsz || "—"), "dim");
    kv(mh, "threshold", det.conf == null ? "—" : String(det.conf));
    kv(mh, "tracker", det.tracker || "—", "dim");
    var fps = det.fps_actual;
    kv(mh, "fps", (fps == null ? "—" : fps.toFixed(1)) + " of " + (det.fps_target || "—"),
       fps != null && det.fps_target && fps < det.fps_target / 2 ? "bad" : "dim");
    kv(mh, "classes", Object.keys(model.classes || {}).map(function (k) {
      return model.classes[k]; }).join(", ") || "—", "dim");
    if (det.uptime_sec != null) kv(mh, "uptime", hms(det.uptime_sec), "dim");

    var dh = document.getElementById("m-data"); dh.textContent = "";
    kv(dh, "labelled frames", String(ds.pairs == null ? "—" : ds.pairs));
    if (ds.unlabeled) kv(dh, "unlabelled", String(ds.unlabeled), "warn");
    kv(dh, "hand-labelled", String(ds.manual || 0), ds.manual ? "ok" : "dim");
    if (ds.empty_labels) kv(dh, "empty labels", String(ds.empty_labels), "dim");
    kv(dh, "validation", (ds.val || 0) + " frames" +
       (ds.pairs ? " (" + Math.round(100 * (ds.val || 0) / ds.pairs) + "%)" : ""),
       ds.val_ok ? "dim" : "bad");
    kv(dh, "oldest", ds.oldest || "—", "dim");
    kv(dh, "on disk", mb((model.disk || {}).dataset_bytes), "dim");

    var need = ds.need || 0, pairs = ds.pairs || 0, min = ds.min_images || 40;
    var bar = document.getElementById("m-prog");
    bar.style.width = Math.min(100, Math.round(100 * pairs / min)) + "%";
    bar.className = pairs >= min ? "full" : "";
    var t = document.getElementById("m-prog-t"); t.textContent = "";
    if (need) {
      t.appendChild(document.createTextNode(
        "training needs " + need + " more frames (" + pairs + " of " + min + ")"));
    } else if (!ds.val_ok) {
      // Not cosmetic: the acceptance gate is decided by the validation set, and
      // if it is frozen on a handful of old frames, anything passes the bowl
      // threshold and zones break silently. It fixes itself at the start of a
      // run, but the user must know the cost up front, not find it in a log.
      var b = document.createElement("b");
      b.textContent = "validation set is small: " + (ds.val || 0) + " frames of " + pairs + ". ";
      t.appendChild(b);
      t.appendChild(document.createTextNode(
        "It will be rebuilt at start: the acceptance gate will mean something " +
        "again, but metrics of past runs will not be comparable to future ones."));
    } else {
      t.textContent = "enough frames: " + pairs + " (minimum " + min + ")";
    }

    renderTrain();
    gateTrain();
  }

  function renderTrain() {
    var host = document.getElementById("m-train"); host.textContent = "";
    var tr = (model && model.train) || {}, last = tr.last;
    if (tr.state === "running") {
      var p = document.createElement("div");
      p.textContent = "running since " + hhmm(tr.started) +
        (tr.epoch ? ", epoch " + tr.epoch + " of " + tr.epochs : "");
      host.appendChild(p);
      return;
    }
    if (!last) {
      var n = cell("prog-t", "fine-tuning has not been run yet");
      host.appendChild(n);
      return;
    }
    var head = document.createElement("div");
    var v = document.createElement("span");
    v.className = "verdict " + (last.accepted ? "yes" : "no");
    v.textContent = last.accepted ? "ACCEPTED" : "REJECTED";
    head.appendChild(v);
    head.appendChild(document.createTextNode(
      "  " + ymd(last.finished) +
      (last.started && last.finished ? ", " + hms(last.finished - last.started) : "")));
    host.appendChild(head);

    if (last.per_class) {
      var dl = document.createElement("dl"); dl.className = "kv";
      dl.style.marginTop = "9px";
      Object.keys(last.per_class).forEach(function (c) {
        var v2 = last.per_class[c];
        kv(dl, c, "mAP50 " + Number(v2.map50).toFixed(3) +
           "  recall " + Number(v2.recall).toFixed(3));
      });
      host.appendChild(dl);
    }
    if (last.reason) host.appendChild(cell("note", last.reason));
    if (last.val_refrozen) {
      host.appendChild(cell("note", "validation set rebuilt before the run: " +
        last.val_refrozen.was + " → " + last.val_refrozen.now + " frames of " +
        last.val_refrozen.pairs + ". Metrics are not comparable with runs before this date."));
    }
    if (last.accepted && last.weights) {
      var n2 = document.createElement("div");
      n2.className = "note";
      n2.appendChild(document.createTextNode("Weights are not switched on automatically. Set "));
      var c2 = document.createElement("code");
      c2.textContent = "PAWSIGHT_MODEL=" + String(last.weights).split("/").pop();
      n2.appendChild(c2);
      n2.appendChild(document.createTextNode(" in .env and restart the detector."));
      host.appendChild(n2);
    }
  }

  // ---- actions: fine-tuning and cleanup ----
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function mkBtn(label, cls, fn) {
    var b = el("button", cls, label);
    b.type = "button";
    b.addEventListener("click", fn);
    return b;
  }

  function gateTrain() {
    var b = document.getElementById("train-go"), why = "";
    var ds = (model && model.dataset) || {}, tr = (model && model.train) || {};
    if (tr.state === "running") why = "running since " + hhmm(tr.started);
    else if (!model) why = "detector is not responding";
    else if ((ds.pairs || 0) < (ds.min_images || 50))
      why = "needs " + (ds.need || 0) + " more labelled frames";
    // A small validation set does NOT disable the button: it is rebuilt at
    // start (train.py::_split). Disabling was unfair: the set shrinks
    // relatively as the dataset grows, so the button died in response to
    // labelling and could only be revived from the terminal.
    b.disabled = !!why;
    b.textContent = tr.state === "running" ? "Stop fine-tuning" : "Start fine-tuning";
    if (tr.state === "running") b.disabled = false;
    document.getElementById("train-why").textContent = why;

    var chip = document.getElementById("h-train");
    chip.hidden = tr.state !== "running";
    if (tr.state === "running") {
      document.getElementById("h-train-t").textContent =
        (tr.epoch ? tr.epoch + "/" + tr.epochs : hms(Math.max(0, nowSec() - (tr.started || 0)))) +
        (((model.detector || {}).paused) ? " · detection paused" : "");
    }
    renderRun();
  }
  function nowSec() { return Date.now() / 1000; }

  function renderRun() {
    var host = document.getElementById("train-run");
    var tr = (model && model.train) || {};
    if (tr.state !== "running") { host.hidden = true; host.textContent = ""; return; }
    host.hidden = false; host.textContent = "";
    host.appendChild(el("div", "el", hms(Math.max(0, nowSec() - (tr.started || 0)))));
    var line = "run " + (tr.run || "—");
    if (tr.epoch) line += " · epoch " + tr.epoch + " of " + tr.epochs;
    // The estimate is MEASURED, from the ultralytics callback: the 2.5 hours in
    // the docs refer to a different dataset size.
    if (tr.eta_sec != null) line += " · about " + hms(tr.eta_sec) + " left";
    host.appendChild(el("div", "prog-t", line));
    if (tr.epochs) {
      var pr = el("div", "prog"); var i = el("i");
      i.style.width = Math.round(100 * (tr.epoch || 0) / tr.epochs) + "%";
      pr.appendChild(i); host.appendChild(pr);
    }
    var tail = el("div", "tail");
    logBuf.filter(function (r) { return r.src === "train"; }).slice(-14)
      .forEach(function (r) { tail.appendChild(logRow(r)); });
    host.appendChild(tail);
    host.appendChild(mkBtn("Stop", "btn sm", trainStop));
  }

  function trainConfirm() {
    var c = document.getElementById("train-confirm");
    if (!c.hidden) { c.hidden = true; return; }
    c.hidden = false; c.textContent = "";
    var ds = model.dataset || {};
    var eta = ((model.train || {}).last && model.train.last.finished && model.train.last.started)
      ? hms(model.train.last.finished - model.train.last.started) : "about an hour";
    c.appendChild(el("h4", null, "Fine-tuning takes about " + eta + " and occupies Metal"));
    c.appendChild(el("p", null,
      "It runs on the same Metal GPU as the detector: detection will be slower, " +
      "and search and following less responsive. If the frame rate halves, gaps " +
      "in the stream will eat events; this tab will show it."));
    c.appendChild(el("p", null,
      "New weights are NOT switched on automatically. If the model passes the acceptance gate " +
      "(bowl mAP50 of at least 0.70: all zones depend on this class), set it " +
      "in PAWSIGHT_MODEL and restart the detector."));
    if (ds.val_ok === false) {
      var about = Math.max(1, Math.round((ds.pairs || 0) * 0.2));
      c.appendChild(el("p", null, "Labelled frames: " + (ds.pairs || 0) +
        ", validation " + (ds.val || 0) + ": too few for the acceptance gate " +
        "to mean anything. The set will be rebuilt: " + (ds.val || 0) + " → " + about +
        " frames. After that, metrics of past runs are not comparable to future ones."));
    } else {
      c.appendChild(el("p", null, "Labelled frames: " + (ds.pairs || 0) +
        ", validation " + (ds.val || 0) + ". The set is frozen and will not change."));
    }
    var lab = el("label");
    var cb = document.createElement("input"); cb.type = "checkbox"; cb.id = "train-pause";
    lab.appendChild(cb);
    lab.appendChild(document.createTextNode("pause detection during training"));
    c.appendChild(lab);
    var f = el("div", "foot");
    f.appendChild(mkBtn("Yes, start (" + eta + ")", "btn go", trainStart));
    f.appendChild(mkBtn("Cancel", "btn ghost", function () { c.hidden = true; }));
    c.appendChild(f);
  }

  function trainStart() {
    var pause = (document.getElementById("train-pause") || {}).checked;
    document.getElementById("train-confirm").hidden = true;
    post("/api/train", { pause_detection: !!pause }).then(function (r) {
      if (!r.ok) { toast("did not start: " + r.error, true); return; }
      toast("fine-tuning started, run " + r.run);
      loadModel();
    }).catch(function () { toast("detector did not respond", true); });
  }
  function trainStop() {
    post("/api/train/stop").then(function (r) {
      toast(r.ok ? "fine-tuning stopped" : "failed: " + r.error, !r.ok);
      loadModel();
    }).catch(function () {});
  }

  // ---- cleanup ----
  var cleanPrev = null;
  function cleanPreview() {
    var c = document.getElementById("clean-confirm");
    post("/api/cleanup/preview", { days: cleanDays }).then(function (r) {
      if (!r.ok) { toast("could not compute: " + r.error, true); return; }
      cleanPrev = r; renderClean();
      c.hidden = false;
    }).catch(function () { toast("server did not respond", true); });
  }
  var cleanDays = 14;

  function renderClean() {
    var c = document.getElementById("clean-confirm");
    c.textContent = "";
    c.appendChild(el("h4", null, "What will be deleted"));
    c.appendChild(el("p", null, "This is a preview. Nothing has been deleted yet."));

    var days = el("label");
    days.appendChild(document.createTextNode("treat as old anything older than"));
    var inp = document.createElement("input");
    inp.type = "number"; inp.min = "1"; inp.max = "365"; inp.value = cleanDays;
    inp.style.cssText = "width:64px;background:var(--panel);border:1px solid var(--edge);" +
      "border-radius:4px;color:var(--ink);font-family:var(--m);font-size:12.5px;padding:3px 6px";
    inp.addEventListener("change", function () {
      cleanDays = Math.max(1, Math.min(365, parseInt(inp.value, 10) || 14));
      cleanPreview();                     // changes the PREVIEW, deletes nothing
    });
    days.appendChild(inp);
    days.appendChild(document.createTextNode("days"));
    c.appendChild(days);

    var tg = el("div", "tg");
    (cleanPrev.groups || []).forEach(function (g) {
      var cb = document.createElement("input");
      cb.type = "checkbox"; cb.dataset.gid = g.id;
      cb.disabled = !!g.blocked || !g.items;
      cb.addEventListener("change", cleanTotal);       // NOT checked by default
      tg.appendChild(cb);
      tg.appendChild(el("span", "lbl", g.label));
      tg.appendChild(el("span", "num", (g.items || 0) + " items"));
      tg.appendChild(el("span", "num", mb(g.bytes)));
      if (g.blocked) tg.appendChild(el("span", "blocked", g.blocked));
      else if ((g.sample || []).length) tg.appendChild(el("span", "sub", g.sample.join(" · ")));
      (g.kept || []).forEach(function (k) { tg.appendChild(el("span", "sub", "kept: " + k)); });
    });
    c.appendChild(tg);

    // What will NOT be touched is always visible, not hidden behind a toggle.
    var k = el("div", "keeps");
    k.appendChild(el("b", null, "Will not be touched:"));
    k.appendChild(el("div", null,
      "dataset frames and labels · the frozen validation set and dedup memory · " +
      "model weights · event, visit and drinking history · camera tilt reference · " +
      "the live log and the previous session's log · the latest training run"));
    c.appendChild(k);

    var f = el("div", "foot");
    cleanBtn = mkBtn("Select what to delete", "btn danger", cleanRun);
    cleanBtn.disabled = true;
    f.appendChild(cleanBtn);                       // button at the BOTTOM, far from the one
    f.appendChild(mkBtn("Cancel", "btn ghost", function () {   // that opened the panel
      document.getElementById("clean-confirm").hidden = true; }));
    c.appendChild(f);
    cleanTotal();
  }
  var cleanBtn = null;

  function picked() {
    return [].slice.call(document.querySelectorAll("#clean-confirm input[type=checkbox]:checked"))
      .map(function (x) { return x.dataset.gid; });
  }
  function cleanTotal() {
    var ids = picked();
    var b = (cleanPrev.groups || []).filter(function (g) { return ids.indexOf(g.id) >= 0; })
      .reduce(function (a, g) { return a + (g.bytes || 0); }, 0);
    cleanBtn.disabled = !ids.length;
    // The label repeats the total: the click and its consequence are one phrase.
    cleanBtn.textContent = ids.length
      ? "Delete permanently: " + mb(b) + " (" + ids.length + ")"
      : "Select what to delete";
  }
  function cleanRun() {
    var ids = picked();
    cleanBtn.disabled = true;
    post("/api/cleanup/apply", { token: cleanPrev.token, groups: ids }).then(function (r) {
      if (!r.ok) { toast("failed: " + r.error, true); cleanBtn.disabled = false; return; }
      toast("freed " + mb(r.freed_bytes));
      document.getElementById("clean-confirm").hidden = true;
      loadModel();
    }).catch(function () { toast("server did not respond", true); cleanBtn.disabled = false; });
  }

  document.getElementById("train-go").addEventListener("click", function () {
    if (((model || {}).train || {}).state === "running") trainStop();
    else trainConfirm();
  });
  document.getElementById("clean-go").addEventListener("click", cleanPreview);
  // The timer ticks only while training is running.
  setInterval(function () {
    if (((model || {}).train || {}).state === "running") { gateTrain(); }
  }, 1000);

  function loadModel() {
    fetch("/api/model").then(function (r) { return r.json(); }).then(function (d) {
      if (d.ok) { model = d.status; renderModel(); }
    }).catch(function () {});
  }

  // ---- state and counters ----
  function fmt(sec) {
    sec = Math.round(sec);
    var m = Math.floor(sec / 60);
    return m ? m + " min " + (sec % 60) + " s" : sec + " s";
  }
  function hhmm(ts) {
    var d = new Date(ts * 1000);
    return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }

  var sw = document.getElementById("sw-track");
  var busy = false;
  var homeBtn = document.getElementById("camera-home");
  var homeBusy = false;

  function refresh() {
    fetch("/api/state").then(function (r) { return r.json(); }).then(function (s) {
      document.getElementById("d-cam").className = "dot " + (s.camera_ok ? "on" : "off");
      document.getElementById("d-det").className = "dot " + (s.detector_ok ? "on" : "off");
      // Observations are stale (stream gap or silent detector): old boxes over
      // live video are misleading, so remove them and say so plainly.
      // Do NOT touch this under a still frame: clearing last would wipe the
      // labels from under the user's hand three seconds into the work.
      if (!frozen() && s.detector_paused) {
        last = null; draw();
        badge.textContent = "detection paused: fine-tuning in progress";
        badge.className = "badge moving";
      } else if (!frozen() && s.obs_age != null && s.obs_age > 3) {
        last = null; draw();
        badge.textContent = "detector silent for " + Math.round(s.obs_age) + " s: no frames";
        badge.className = "badge moving";
      }

      // A database failure no longer takes the whole dashboard down with a 500,
      // but it must not stay silent either: the user has to see "records are
      // not being saved" immediately, not three hours later (docs/architecture.md,
      // "The database has one owner").
      var dbw = document.getElementById("db-warn");
      var dbmsg = s.db_error ? "database unavailable: " + s.db_error
                : s.db_orphaned ? "database was replaced under the brain: records are not being saved"
                : "";
      dbw.textContent = dbmsg;
      dbw.hidden = !dbmsg;

      document.getElementById("n-drinks").textContent = s.drinks.length;
      document.getElementById("n-total").textContent =
        s.drinks.length ? "visits · " + fmt(s.total_sec) + " total" : "visits to the water bowl";

      var bt = document.getElementById("bouts");
      bt.textContent = "";
      s.drinks.slice().reverse().forEach(function (b) {
        var el = document.createElement("div");
        el.className = "bout" + (b.conflict ? " conflict" : "");
        el.appendChild(document.createElement("i"));
        el.appendChild(cell("t", hhmm(b.ts)));
        el.appendChild(cell("", fmt(b.dur)));
        if (b.conflict) el.appendChild(cell("", "⚠", "geometry and model disagree"));
        bt.appendChild(el);
      });

      var kinds = document.getElementById("kinds");
      var names = { drink: "drank", eat: "ate", unknown: "unrecognised" };
      kinds.textContent = "";
      var kk = Object.keys(s.by_kind);
      if (!kk.length) {
        var none = cell("", "no visits to the bowls today");
        none.style.color = "var(--faint)";
        kinds.appendChild(none);
      }
      kk.forEach(function (k) {
        var sp = document.createElement("span");
        sp.appendChild(document.createTextNode((names[k] || k) + " "));
        var b = document.createElement("b");
        b.textContent = s.by_kind[k].count;
        sp.appendChild(b);
        kinds.appendChild(sp);
      });

      var log = document.getElementById("log");
      log.textContent = "";
      if (!s.events.length) {
        log.appendChild(cell("ev txt", "no events yet")).style.color = "var(--faint)";
      }
      s.events.forEach(function (e) {
        var el = document.createElement("div");
        el.className = "ev";
        // e.vlm is VLM output. It has no business in innerHTML.
        el.appendChild(cell("meta", hhmm(e.ts)));
        el.appendChild(document.createTextNode(" "));
        el.appendChild(cell("zone", e.zone || "no zone"));
        el.appendChild(document.createTextNode(" "));
        el.appendChild(cell("txt", e.state + (e.vlm ? " · " + e.vlm : "")));
        log.appendChild(el);
      });

      if (!busy) {
        sw.disabled = !s.detector_ok;
        sw.setAttribute("aria-checked", s.follow ? "true" : "false");
      }
      if (!homeBusy) homeBtn.disabled = !s.detector_ok;
    }).catch(function () {
      document.getElementById("d-cam").className = "dot off";
      if (!homeBusy) homeBtn.disabled = true;
    });
  }

  // ---- actions ----
  function toast(msg, bad) {
    var t = document.createElement("div");
    t.className = "toast" + (bad ? " bad" : "");
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 2600);
  }

  var unlock = document.getElementById("tilt-unlock");
  var tiltLocked = true;
  function applyLock() {
    tiltLocked = !unlock.checked;
    ["up", "down"].forEach(function (d) {
      var b = document.querySelector('.dpad button[data-dir="' + d + '"]');
      b.disabled = tiltLocked;
      b.title = tiltLocked ? "locked: tilt is held at the reference" : (d === "up" ? "up" : "down");
    });
  }
  unlock.addEventListener("change", applyLock);
  applyLock();

  document.querySelectorAll(".dpad button[data-dir]").forEach(function (b) {
    b.addEventListener("click", function () {
      var dir = b.dataset.dir;
      if ((dir === "up" || dir === "down") && tiltLocked) return;
      b.disabled = true;
      post("/api/ptz", { dir: dir }).then(function (r) {
        if (!r.ok) toast("Camera did not move: " + r.error, true);
      }).catch(function () {
        toast("Camera did not respond", true);
      }).finally(function () {
        setTimeout(function () { b.disabled = false; }, 350);
      });
    });
  });

  homeBtn.addEventListener("click", function () {
    homeBusy = true;
    homeBtn.disabled = true;
    homeBtn.setAttribute("aria-busy", "true");
    homeBtn.textContent = "Sending to bowls…";
    post("/api/home", {}).then(function (r) {
      if (!r.ok) toast("Return not started: " + (r.error || "detector refused"), true);
      else toast("Camera is returning to the bowls");
    }).catch(function () {
      toast("Detector did not respond to the return command", true);
    }).finally(function () {
      homeBusy = false;
      homeBtn.removeAttribute("aria-busy");
      homeBtn.textContent = "Return camera to bowls";
      refresh();
    });
  });

  sw.addEventListener("click", function () {
    var next = sw.getAttribute("aria-checked") !== "true";
    busy = true;
    sw.disabled = true;
    sw.setAttribute("aria-checked", next ? "true" : "false");
    post("/api/tracking", { on: next }).then(function (r) {
      if (!r.ok) { toast("Did not switch: " + r.error, true); }
      else { toast(next ? "Dog following on" : "Dog following off"); }
    }).catch(function () {
      toast("Camera did not respond", true);
    }).finally(function () {
      // the camera does not confirm the new value instantly: re-read later
      setTimeout(function () { busy = false; sw.disabled = false; refresh(); }, 1500);
    });
  });

  // keyboard: arrows pan the camera, but only if nothing else claims them.
  // Previously only INPUT was excluded: with tabs, an arrow on a focused tab
  // would both switch the tab and pan the camera.
  function keyBusy(e) {
    if (frozen()) return true;      // arrows move the box, not the camera
    var t = e.target;
    if (!t) return false;
    if (/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(t.tagName)) return true;
    if (t.isContentEditable) return true;
    return !!(t.closest && t.closest('[role="tablist"], .kfilter'));
  }
  document.addEventListener("keydown", function (e) {
    var map = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };
    var dir = map[e.key];
    if (!dir || keyBusy(e)) return;
    e.preventDefault();
    var btn = document.querySelector('.dpad button[data-dir="' + dir + '"]');
    if (btn && !btn.disabled) btn.click();
    else if (dir === "up" || dir === "down") toast("Tilt is locked: tick \"allow tilt\"", true);
  });

  fit();
  try { setTab(localStorage.getItem("pawsight.tab") || "cam"); } catch (e) { setTab("cam"); }
  connectLive();
  loadCamlog();
  refresh();
  setInterval(refresh, 5000);
})();
