"""Dashboard routes: freeze frames, dataset, fine-tuning, cleanup.

Mixin for web.Dashboard: the state (self._snaps, self.ask, self.store …)
lives there. Moved out of web.py with no change in behaviour or URLs.
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from aiohttp import web

from . import cleanup
from .bus import decode
from .config import ROOT as ROOT_PATH
from .labelcheck import validate_boxes
from .labels import DATASET_CLASSES, MIN_IMAGES
from .webguard import _int_arg

# Same logger name as before: the activity feed and tests use it to recognise dashboard records.
log = logging.getLogger("pawsight.web")


class DatasetRoutes:
    # ---- freeze frames for labelling ----

    def on_snapshot(self, raw: bytes) -> None:
        """From the bus thread: parsing happens on the event loop.

        self._snaps is read and pruned by request handlers on the event loop,
        but it used to be written by the MQTT thread: "dictionary changed size
        during iteration" and lost frames. Now only the loop touches the dict;
        without a loop (tests, serverless mode) we parse in place.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            self._on_snapshot(raw)
            return
        loop.call_soon_threadsafe(self._on_snapshot, raw)

    def _on_snapshot(self, raw: bytes) -> None:
        """Frames from the detector: the description first, then the JPEGs themselves.

        The images do NOT go into the general broadcast: a hundred kilobytes of
        base64 to every open dashboard for a single click is wasteful, and the
        frame here is temporary; it belongs neither in storage/ nor in the event
        stream. We keep it in memory and serve it on a separate request.
        """
        try:
            d, jpeg = decode(raw)
        except Exception:
            return
        sid = d.get("id")
        if not sid:
            return
        if not d.get("ok", True):
            self.on_reply(raw)
            return

        if d.get("cmd") == "dataset_frame":
            if jpeg is not None:
                self._snaps["ds:" + str(d.get("name"))] = {"at": time.time(), "jpeg": jpeg}
                for k in [k for k in self._snaps if k.startswith("ds:")][:-12]:
                    self._snaps.pop(k, None)
            self.on_reply(raw)
            return
        if d.get("cmd") == "snapshot_frame":
            snap = self._snaps.get(sid)
            if snap is None or jpeg is None:
                return
            snap["jpegs"][int(d.get("n") or 0)] = jpeg
            if all(x is not None for x in snap["jpegs"]):
                self._snap_ready(sid, snap)
            return

        frames = d.get("frames") or []
        self._snaps[sid] = {"at": time.time(), "meta": d,
                            "jpegs": [None] * len(frames), "sent": False}
        for old_id in sorted(self._snaps, key=lambda k: self._snaps[k]["at"])[:-4]:
            self._snaps.pop(old_id, None)

    def _snap_ready(self, sid: str, snap: dict) -> None:
        if snap.get("sent"):
            return
        snap["sent"] = True
        d = dict(snap["meta"])
        d["frames"] = [dict(f, url=f"/api/snapshot/{sid}/{i}.jpg")
                       for i, f in enumerate(d.get("frames") or [])]
        d["type"] = "snapshot"
        # Kept for GET /api/snapshot/{sid}: the labelling page asks for the
        # ring by id instead of listening to the dashboard's live broadcast.
        snap["ready"] = d
        self.push(d)
        fut = self._pending.get(sid)
        loop = self._loop
        if fut and loop and not fut.done():
            loop.call_soon_threadsafe(
                lambda: fut.done() or fut.set_result({"ok": True, "id": sid,
                                                      "frames": len(d["frames"])}))

    async def api_snapshot(self, _req):
        """Ask the detector to freeze a frame. The reply comes once all the ring's
        images have arrived, otherwise the browser would fetch them too early."""
        r = await self.ask("snapshot", timeout=8.0)
        return web.json_response(r, status=200 if r.get("ok") else 409)

    async def api_snapshot_jpeg(self, req):
        snap = self._snaps.get(req.match_info["sid"])
        try:
            n = int(req.match_info["n"])
        except ValueError:
            raise web.HTTPNotFound()
        if snap is None or not (0 <= n < len(snap["jpegs"])) or snap["jpegs"][n] is None:
            raise web.HTTPNotFound(text="frame expired — take a new one")
        return web.Response(body=snap["jpegs"][n], content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def api_snapshot_meta(self, req):
        """The ring of a still frame, once all its JPEGs have arrived.

        POST /api/snapshot answers only after that moment, so the labelling
        page reads the description here rather than from the /api/live
        broadcast: it does not need the observation stream at all."""
        snap = self._snaps.get(req.match_info["sid"])
        if snap is None or "ready" not in snap:
            return web.json_response({"ok": False, "error": "frame expired — take a new one"},
                                     status=404)
        return web.json_response({"ok": True, **snap["ready"]})

    async def api_snapshot_save(self, req):
        sid = req.match_info["sid"]
        if sid not in self._snaps:
            return web.json_response({"ok": False, "error": "frame expired — take a new one"},
                                     status=410)
        body = await self._body(req)
        frame = _int_arg(body.get("frame"), 0, "frame")
        boxes = body.get("boxes") or []
        ok, err = validate_boxes(boxes)
        if not ok:
            return web.json_response({"ok": False, "error": err}, status=400)
        r = await self.ask("snapshot_save", {
            "snap": sid, "frame": frame, "boxes": boxes,
            "confirm_empty": bool(body.get("confirm_empty")),
        }, timeout=10.0)
        if r.get("ok"):
            self._snaps.pop(sid, None)
            self.camlog(time.time(), "dataset", "info",
                        f"frame labelled by hand and saved: {r.get('image')} "
                        f"({r.get('labels')} boxes)")
        return web.json_response(r, status=200 if r.get("ok") else 409)

    async def api_snapshot_drop(self, req):
        self._snaps.pop(req.match_info["sid"], None)
        return web.json_response({"ok": True})

    # ---- labelling page ----

    async def label_page(self, _req):
        return web.FileResponse(Path(__file__).parent / "static" / "label.html")

    async def api_label_classes(self, _req):
        """Dataset classes in index order: the page builds its buttons and
        hotkeys from this, so the class list lives in ONE place (labels.py)
        and the page cannot offer a class the detector would refuse."""
        return web.json_response({
            "ok": True,
            "classes": [DATASET_CLASSES[i] for i in sorted(DATASET_CLASSES)],
            "min_images": MIN_IMAGES,
        })

    # ---- reviewing accumulated labels ----

    async def api_dataset(self, req):
        limit = max(1, min(_int_arg(req.query.get("limit"), 400, "limit"), 5000))
        r = await self.ask("dataset_list", {
            "order": req.query.get("order") or "suspicious",
            "limit": limit}, timeout=10.0)
        return web.json_response(r, status=200 if r.get("ok") else 503)

    async def api_dataset_frame(self, req):
        """A frame from dataset/ — only via the detector: the dataset volume is
        not mounted into the container, and there is no reason to mount it (the
        image has neither cv2 nor numpy, and manual labels would end up behind an HTTP endpoint)."""
        name = req.match_info["name"]
        hit = self._snaps.get("ds:" + name)
        if hit and hit.get("jpeg"):
            return web.Response(body=hit["jpeg"], content_type="image/jpeg",
                                headers={"Cache-Control": "no-store"})
        r = await self.ask("dataset_frame", {"name": name}, timeout=10.0)
        if not r.get("ok"):
            raise web.HTTPNotFound(text=str(r.get("error") or "frame not received"))
        hit = self._snaps.get("ds:" + name)
        if not hit or not hit.get("jpeg"):
            raise web.HTTPBadGateway(text="frame did not arrive")
        return web.Response(body=hit["jpeg"], content_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

    async def api_dataset_meta(self, req):
        r = await self.ask("dataset_frame", {"name": req.match_info["name"]}, timeout=10.0)
        return web.json_response(r, status=200 if r.get("ok") else 404)

    async def api_dataset_hint(self, req):
        r = await self.ask("dataset_hint", {"name": req.match_info["name"]}, timeout=30.0)
        return web.json_response(r, status=200 if r.get("ok") else 409)

    async def api_dataset_save(self, req):
        body = await self._body(req)
        boxes = body.get("boxes") or []
        ok, err = validate_boxes(boxes)
        if not ok:
            return web.json_response({"ok": False, "error": err}, status=400)
        r = await self.ask("dataset_save", {"name": req.match_info["name"],
                                            "boxes": boxes,
                                            "confirm_empty": bool(body.get("confirm_empty"))},
                           timeout=10.0)
        if r.get("ok"):
            self._snaps.pop("ds:" + req.match_info["name"], None)
        return web.json_response(r, status=200 if r.get("ok") else 409)

    async def api_dataset_delete(self, req):
        """Drop a frame from the dataset together with its labels.

        The action is irreversible, so it has its own route and the DELETE
        method rather than a flag on /save: the X-Pawsight header and the Origin
        check are required for both, but after this the page code cannot mix up
        "save" and "delete". The page asks for confirmation with a second press
        of the same button, as with empty labels.
        """
        name = req.match_info["name"]
        r = await self.ask("dataset_delete", {"name": name}, timeout=10.0)
        if r.get("ok"):
            self._snaps.pop("ds:" + name, None)
            self.camlog(time.time(), "dataset", "warning",
                        f"frame deleted from the dataset by hand: {name} "
                        f"({r.get('pairs', '?')} pairs left)"
                        + (", it was in the validation split" if r.get("was_val") else ""))
        return web.json_response(r, status=200 if r.get("ok") else 409)

    # ---- fine-tuning ----

    async def api_train(self, req):
        body = {}
        try:
            body = await req.json()
        except Exception:
            pass
        r = await self.ask("train_start",
                           {"pause_detection": bool(body.get("pause_detection"))},
                           timeout=15.0)
        if r.get("ok"):
            self.camlog(time.time(), "train", "info",
                        f"fine-tuning started from the dashboard (run {r.get('run')})")
        return web.json_response(r, status=200 if r.get("ok") else 409)

    async def api_train_stop(self, _req):
        r = await self.ask("train_stop", timeout=10.0)
        if r.get("ok"):
            self.camlog(time.time(), "train", "warning", "fine-tuning stopped from the dashboard")
        return web.json_response(r, status=200 if r.get("ok") else 409)

    # ---- cleanup ----

    def _camlog_plan(self, days: int) -> dict:
        cut = time.time() - days * 86400
        try:
            n = self.store().camlog_count_before(cut)
        except Exception:
            n = 0
        return {"id": "camlog", "owner": "brain",
                "label": f"Activity feed records older than {days} days",
                "items": n, "bytes": n * 120, "sample": [],
                "kept": ["events, bowl visits and drinking are not touched"]}

    async def api_cleanup_preview(self, req):
        body = {}
        try:
            body = await req.json()
        except Exception:
            pass
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="JSON object expected")
        # The "or" is deliberate: 0 days or 0 runs from the form means
        # "default", as before; a malformed value is a 400.
        days = max(1, min(_int_arg(body.get("days") or None, self._clean_days, "days"), 365))
        keep = max(0, min(_int_arg(body.get("keep_runs") or None, self._clean_keep,
                                   "keep_runs"), 20))
        storage = self.cfg.db_path.parent
        groups = await self._exec(cleanup.storage_plan, storage, days)
        groups.append(await self._exec(self._camlog_plan, days))
        host = await self.ask("cleanup_preview", {"keep_runs": keep}, timeout=8.0)
        if host.get("ok"):
            groups = list(host.get("groups") or []) + groups
        else:
            groups.append({"id": "runs", "owner": "detector",
                           "label": "Training outputs (runs/)", "items": 0, "bytes": 0,
                           "sample": [], "kept": [],
                           "blocked": "detector did not answer: " + str(host.get("error"))})
        token = uuid.uuid4().hex
        # The preview issues a TOKEN, and deletion must present it.
        # That makes "look first" a property of the protocol, not a UI
        # convention: a blind POST to apply deletes nothing.
        self._clean_tokens = {t: v for t, v in self._clean_tokens.items()
                              if time.time() - v["at"] < 300}
        self._clean_tokens[token] = {"at": time.time(), "days": days, "keep": keep,
                                     "groups": {g["id"] for g in groups if not g.get("blocked")}}
        return web.json_response({
            "ok": True, "token": token, "expires_in": 300, "days": days,
            "keep_runs": keep, "groups": groups,
            "total_bytes": sum(g.get("bytes") or 0 for g in groups),
        })

    async def api_cleanup_apply(self, req):
        body = await self._body(req)
        tok = self._clean_tokens.pop(str(body.get("token") or ""), None)
        if not tok or time.time() - tok["at"] > 300:
            return web.json_response(
                {"ok": False, "error": "preview expired — recalculate"},
                status=410)
        want = set(body.get("groups") or []) & tok["groups"]
        if not want:
            return web.json_response({"ok": False, "error": "nothing selected"}, status=400)

        removed, storage = [], self.cfg.db_path.parent
        if "runs" in want:
            r = await self.ask("cleanup_apply", {"keep_runs": tok["keep"]}, timeout=60.0)
            if not r.get("ok"):
                return web.json_response(r, status=409)
            removed += r.get("removed") or []
        removed += await self._exec(cleanup.storage_apply, ROOT_PATH, storage,
                                    want, tok["days"])
        if "camlog" in want:
            removed.append(await self._exec(self._trim_camlog, tok["days"]))

        freed = sum(x.get("bytes") or 0 for x in removed)
        self.camlog(time.time(), "manual", "info",
                    "cleanup from the dashboard: freed %.1f MB (%s)"
                    % (freed / 2**20, ", ".join(sorted(want))))
        return web.json_response({"ok": True, "removed": removed, "freed_bytes": freed})

    def _trim_camlog(self, days: int) -> dict:
        cut = time.time() - days * 86400
        n = self.store().camlog_trim(cut)
        try:
            # VACUUM takes an exclusive lock and may fail if the brain is
            # writing an event at that moment. We do not pretend it worked.
            self.store().vacuum()
        except Exception as e:
            log.info("VACUUM postponed: %s", e)
            return {"id": "camlog", "items": n, "bytes": 0,
                    "note": "rows deleted, space will be freed later"}
        return {"id": "camlog", "items": n, "bytes": n * 120}

