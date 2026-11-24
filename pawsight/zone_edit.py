"""Zone editor backend: read zones.yaml for the editor, validate edits, write it back.

The editor lets the owner draw their own zones on a detector frame and choose
which zones send Telegram notifications. Two kinds of drawn zones:

  bowls  -- the polygon is stored as offsets from the floor point of the bowl
            PAIR (select: all), so it moves with the camera like the built-in
            zones. Needs both bowls visible in the frame it was drawn on.
  fixed  -- plain frame coordinates. Only correct while the camera does not
            pan (patrol and following off).

The three built-in zones (bowls, water, food) carry the visit logic: their
geometry is not editable here (the setup page swaps water/food), only their
label and notification flag. Any other zone the editor did not create (a
custom anchor, an expand rectangle) is preserved as-is in the same way.

This runs in the detector process, because the detector lives on the host,
where zones.yaml is, in both stacks. The brain in a container only has a
read-only copy.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .anchors import AnchoredZone
from .config import Config

BUILTIN = ("bowls", "water", "food")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_ZONES = 24
MAX_POINTS = 40
MAX_LABEL = 40


class ZoneEditError(ValueError):
    """An edit that must not be written. The message is shown to the owner."""


def pair_point(bowls: list[tuple[float, float, float, float]]) -> tuple[int, int] | None:
    """Floor point of the bowl pair, computed exactly as anchored zones do it.

    The same AnchoredZone code decides what counts as "the pair" (two close
    bowls), so a zone drawn here lands where the detector will later put it.
    """
    a = AnchoredZone(name="_pair", label="", anchor_class="bowl", select="all",
                     shape=((0, 0), (1, 0), (0, 1)))
    a.update([("bowl", 1.0, tuple(b)) for b in bowls], 0.0)
    box = a.anchor_box(0.0)
    if box is None:
        return None
    x1, _, x2, y2 = box
    return int(round((x1 + x2) / 2.0)), int(round(y2))


def _kind(name: str, spec: dict) -> str:
    if name in BUILTIN:
        return "builtin"
    a = spec.get("anchor")
    if a:
        if (a.get("class") == "bowl" and a.get("select", "all") == "all"
                and a.get("shape") and "expand" not in a):
            return "bowls"
        return "custom"
    if spec.get("polygon"):
        return "fixed"
    return "custom"


def describe(raw: dict) -> dict:
    """Editor view of a parsed zones.yaml."""
    zones = []
    for name, spec in (raw.get("zones") or {}).items():
        spec = spec or {}
        kind = _kind(name, spec)
        z = {"name": name, "label": spec.get("label", name),
             "notify": bool(spec.get("notify", True)), "kind": kind}
        if kind == "bowls" or (kind == "builtin" and (spec.get("anchor") or {}).get("shape")):
            z["shape"] = [list(p) for p in spec["anchor"]["shape"]]
            z["select"] = spec["anchor"].get("select", "all")
        elif kind == "fixed":
            z["polygon"] = [list(p) for p in spec["polygon"]]
        zones.append(z)
    return {"zones": zones,
            "notify_outside": bool(raw.get("notify_outside_zones", True))}


def _points(z: dict, key: str, lo: float, hi_x: float, hi_y: float) -> list[list[int]]:
    pts = z.get(key)
    if not isinstance(pts, list) or not 3 <= len(pts) <= MAX_POINTS:
        raise ZoneEditError(f"zone \"{z.get('label') or z.get('name')}\": "
                            f"draw between 3 and {MAX_POINTS} points")
    out = []
    for p in pts:
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)):
            raise ZoneEditError("a zone point must be a pair of numbers")
        x, y = int(round(p[0])), int(round(p[1]))
        if not (lo <= x <= hi_x and lo <= y <= hi_y):
            raise ZoneEditError(f"zone \"{z.get('label') or z.get('name')}\": "
                                "a point lies outside the frame")
        out.append([x, y])
    return out


def _slug(label: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "zone"
    if not base[0].isalpha():
        base = "z_" + base
    base = base[:28]
    name, n = base, 2
    while name in taken or name in BUILTIN:
        name = f"{base}_{n}"
        n += 1
    return name


def merge(raw: dict, edit: dict, frame: tuple[int, int]) -> dict:
    """Apply an editor payload to a parsed zones.yaml and return the new mapping.

    edit = {"zones": [{"name"?, "label", "notify", "kind", "shape"|"polygon"}],
            "notify_outside": bool}
    Built-in and custom zones keep their geometry; zones missing from the edit
    are deleted, except built-in ones, which cannot be deleted.
    """
    w, h = frame
    old = dict(raw.get("zones") or {})
    items = edit.get("zones")
    if not isinstance(items, list):
        raise ZoneEditError("zones must be a list")
    if len(items) > MAX_ZONES:
        raise ZoneEditError(f"at most {MAX_ZONES} zones")

    taken = set(old)
    seen: set[str] = set()
    built: dict[str, dict] = {}
    for z in items:
        if not isinstance(z, dict):
            raise ZoneEditError("a zone must be an object")
        label = str(z.get("label") or "").strip()
        if not label or len(label) > MAX_LABEL:
            raise ZoneEditError(f"every zone needs a name of 1-{MAX_LABEL} characters")
        name = z.get("name")
        if name:
            if not isinstance(name, str) or not NAME_RE.match(name):
                raise ZoneEditError(f"invalid zone id: {name!r}")
        else:
            name = _slug(label, taken | seen)
        if name in seen:
            raise ZoneEditError(f"zone \"{label}\" appears twice")
        seen.add(name)
        notify = bool(z.get("notify", True))

        kind = _kind(name, old[name]) if name in old else z.get("kind")
        if kind in ("builtin", "custom"):
            if name not in old:
                raise ZoneEditError(f"unknown zone: {name}")
            spec = dict(old[name])
        elif kind == "bowls":
            shape = _points(z, "shape", -max(w, h), max(w, h), max(w, h))
            spec = {"anchor": {"class": "bowl", "select": "all", "min_conf": 0.20,
                               "shape": shape, "memory_sec": 20}}
        elif kind == "fixed":
            spec = {"polygon": _points(z, "polygon", 0, w, h)}
        else:
            raise ZoneEditError(f"zone \"{label}\": unknown kind {kind!r}")
        spec["label"] = label
        spec["notify"] = notify
        built[name] = spec

    missing = [b for b in BUILTIN if b in old and b not in built]
    if missing:
        raise ZoneEditError(f"built-in zones cannot be deleted: {', '.join(missing)}")

    # Order is logic: the brain takes the FIRST non-hint zone, and the visit
    # trigger (bowls) overlaps room zones on purpose. Built-in zones go first,
    # in their original order; the owner's zones follow in the editor's order.
    zones: dict[str, dict] = {}
    for n in old:
        if n in BUILTIN and n in built:
            zones[n] = _ordered(built.pop(n))
    for n, spec in built.items():
        zones[n] = _ordered(spec)

    out = {k: v for k, v in raw.items() if k not in ("zones", "notify_outside_zones")}
    out["zones"] = zones
    out["notify_outside_zones"] = bool(edit.get("notify_outside", True))
    # The result must load exactly as the detector and the brain will load it.
    Config.zones_from_raw(out)
    return out


def _ordered(spec: dict) -> dict:
    """label and notify first: that is what a person reading the file looks for."""
    head = {k: spec[k] for k in ("label", "notify") if k in spec}
    return {**head, **{k: v for k, v in spec.items() if k not in head}}


def _header(text: str) -> str:
    """Everything above the `zones:` key: the file's explanation and the
    water-bowl header that the setup page maintains."""
    m = re.search(r"^zones:\s*$", text, re.M)
    return text[:m.start()] if m else ""


class _Dumper(yaml.SafeDumper):
    pass


def _flow_points(dumper, data):
    """Vertex lists on one line, as people write them: [[1, 2], [3, 4]]."""
    if data and all(isinstance(p, list) and len(p) == 2
                    and all(isinstance(v, int) for v in p) for p in data):
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data)


_Dumper.add_representer(list, _flow_points)


def render(old_text: str, new: dict) -> str:
    body = yaml.dump({"zones": new["zones"]}, Dumper=_Dumper, sort_keys=False,
                     allow_unicode=True, default_flow_style=False, width=200)
    rest = {k: v for k, v in new.items() if k != "zones"}
    tail = ("\n" + yaml.dump(rest, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
                             default_flow_style=False)) if rest else ""
    return _header(old_text) + body + tail


def save(path: Path, edit: dict, frame: tuple[int, int]) -> dict:
    """Validate, back up the old file to zones.yaml.bak, write the new one.

    The file is rewritten IN PLACE, not replaced by rename: in the docker stack
    the brain container mounts this single file, and a bind mount of a file
    keeps pointing at the old inode after a rename.

    Comments inside the zones section are not kept (the header above `zones:`
    is): the backup holds the previous version word for word.
    """
    old_text = path.read_text() if path.exists() else ""
    raw = yaml.safe_load(old_text) or {}
    new = merge(raw, edit, frame)
    text = render(old_text, new)
    if yaml.safe_load(text) != new:
        raise ZoneEditError("internal error: the rendered zones.yaml does not read back")
    if old_text:
        path.with_name(path.name + ".bak").write_text(old_text)
    with open(path, "r+" if path.exists() else "w") as f:
        f.seek(0)
        f.write(text)
        f.truncate()
    return new
