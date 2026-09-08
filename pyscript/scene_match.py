"""Hue active-scene detection for Home Assistant (pyscript).

Figures out which *static* Hue scene is currently active per room by comparing
the live light states against fingerprints (captured during calibration or
learned on activation) and writes the result to `pyscript.scene_tracker`
(attribute `active_scene`, a {room: scene_entity_id} map).

Requires pyscript with `allow_all_imports: true`.
"""

import json
import time
from homeassistant.util import slugify

FINGERPRINT_FILE = "/config/scene_fingerprints.json"
DYNAMIC_SENSOR = "sensor.dynamische_szenen"
TRACKER = "pyscript.scene_tracker"

# Per-room light group entity = GROUP_PREFIX + slugify(room).
# Its `entity_id` attribute must list the room's member lights.
GROUP_PREFIX = "light.dimmer_"

ACCEPT = 0.35        # max distance to accept a match
CLEAR = 0.60         # above this: clearly nothing -> mark room unknown
KEEP = 0.60          # keep the current scene while it still fits this well
LOCK_SECONDS = 30    # after a user tap, don't override the room for this long
LOOP_SECONDS = 5     # background re-evaluation interval
EXCLUDE_SUFFIXES = ("_naturliches_licht",)  # adaptive scenes to ignore

FP = {}
LOCK = {}


@pyscript_compile
def _read_json(path):
    import json, os
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


@pyscript_compile
def _write_json(path, data):
    import json
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _fp_light(ml, is_on):
    # An off member is a fingerprint of its own ("this light must be off"),
    # which is the strongest discriminator between scenes that light up
    # different subsets of a group.
    if not is_on:
        return {"bri": 0, "off": True}
    rec = {"bri": ml.get("brightness"), "mode": ml.get("color_mode")}
    if ml.get("color_mode") == "color_temp":
        rec["ct"] = ml.get("color_temp_kelvin")
    else:
        if ml.get("xy_color"):
            rec["xy"] = list(ml.get("xy_color"))
        if ml.get("hs_color"):
            rec["hs"] = list(ml.get("hs_color"))
    return rec


def _load():
    global FP
    FP = task.executor(_read_json, FINGERPRINT_FILE)
    total = 0
    for v in FP.values():
        total += len(v)
    log.warning(f"Matcher: {total} fingerprints loaded")


def _excluded(sc):
    for suf in EXCLUDE_SUFFIXES:
        if sc.endswith(suf):
            return True
    return False


def _scene_distance(lights):
    dsum = 0.0
    n = 0
    for lid, fp in lights.items():
        if fp.get("mode") == "onoff":
            continue
        n += 1
        is_on = state.get(lid) == "on"
        want_on = not fp.get("off")
        # On/off state is a hard signal and dominates. Crucially, we do NOT
        # read colour from an off light: many Hue lamps keep reporting their
        # last color_temp_kelvin while off, which used to make an off light
        # look almost like an on one (only the brightness differed).
        if want_on != is_on:
            dsum += 1.0
            continue
        if not want_on:  # both off -> exact match
            continue
        attrs = state.getattr(lid) or {}
        fp_bri = fp.get("bri") or 0
        cur_bri = attrs.get("brightness") or 0
        d = abs(cur_bri - fp_bri) / 255.0
        if "ct" in fp:
            cur = attrs.get("color_temp_kelvin")
            d += 1.0 if cur is None else abs(cur - fp["ct"]) / 2000.0
        elif "xy" in fp:
            cur = attrs.get("xy_color")
            if not cur:
                d += 1.0
            else:
                dx = cur[0] - fp["xy"][0]
                dy = cur[1] - fp["xy"][1]
                d += ((dx * dx + dy * dy) ** 0.5) / 0.25
        elif "hs" in fp:
            cur = attrs.get("hs_color")
            if not cur:
                d += 1.0
            else:
                dh = abs(cur[0] - fp["hs"][0])
                dh = min(dh, 360 - dh)
                d += dh / 180.0 + abs(cur[1] - fp["hs"][1]) / 100.0
        dsum += d
    if n == 0:
        return None
    return dsum / n


def _best(scenes, current):
    scored = []
    for sc, lights in scenes.items():
        if _excluded(sc):
            continue
        d = _scene_distance(lights)
        if d is not None:
            scored.append((d, sc))
    if not scored:
        return None, None
    scored.sort()
    best_d = scored[0][0]
    near = []
    for d, sc in scored:
        if d <= best_d + 0.02:
            near.append(sc)
    if current in near:
        return current, best_d
    return sorted(near)[0], best_d


def _ar():
    # "dynamic rooms" = the rooms we started dynamically ourselves. Deriving
    # this from the light `dynamics` attribute is unreliable when a light
    # belongs to more than one room (e.g. a shared string light), so we trust
    # our own dynamic_scenes map instead.
    try:
        return list(json.loads(state.get("input_text.dynamic_scenes") or "{}").keys())
    except Exception:
        return []


def _result():
    try:
        return dict((state.getattr(TRACKER) or {}).get("active_scene") or {})
    except Exception:
        return {}


def _update_room(room, scenes, ar, lights_all, result):
    group = GROUP_PREFIX + slugify(room)
    if group not in lights_all:
        return None
    if state.get(group) != "on":
        result.pop(room, None)
        LOCK.pop(room, None)
        return f"{room}:off"
    if room in ar:
        result.pop(room, None)
        return f"{room}:dyn"
    current = result.get(room)
    # freshly tapped -> locked, keep
    if current and time.time() < LOCK.get(room, 0):
        return f"{room}:{current.split('.')[-1]}:lock"
    # current scene still fits -> keep (no flip-flop)
    if current and current in scenes and not _excluded(current):
        dc = _scene_distance(scenes[current])
        if dc is not None and dc <= KEEP:
            return f"{room}:{current.split('.')[-1]}:keep({round(dc, 2)})"
    # otherwise re-detect
    best, d = _best(scenes, current)
    short = best.split(".")[-1] if best else "-"
    if best is not None and d is not None:
        if d <= ACCEPT:
            result[room] = best
        elif d >= CLEAR:
            result.pop(room, None)
    return f"{room}:{short}:{round(d, 2) if d is not None else '-'}"


@service
def scene_set_active(room=None, scene=None, **kwargs):
    """Set the active scene for a room instantly (called on a user tap)."""
    if not room or not scene:
        return
    result = _result()
    result[room] = scene
    LOCK[room] = time.time() + LOCK_SECONDS
    state.set(TRACKER, "ok", active_scene=result)


@service
def scene_learn(scene=None, settle=2, **kwargs):
    """Refresh one scene's fingerprint from the current light state.

    Called right after a static activation, so the DB self-heals as scenes
    are used (and new/edited scenes get picked up on first tap). Skipped for
    excluded (adaptive) scenes.
    """
    if not scene or _excluded(scene):
        return
    if not FP:
        _load()
    attrs = state.getattr(scene) or {}
    room = attrs.get("group_name")
    if not room:
        return
    group = GROUP_PREFIX + slugify(room)
    if group not in state.names("light"):
        return
    task.sleep(float(settle))
    members = (state.getattr(group) or {}).get("entity_id") or []
    entry = {}
    for lid in members:
        on = state.get(lid) == "on"
        entry[lid] = _fp_light(state.getattr(lid) or {}, on)
    if not entry:
        return
    FP.setdefault(room, {})[scene] = entry
    task.executor(_write_json, FINGERPRINT_FILE, FP)
    log.info(f"Learned: {scene} ({len(entry)} lights)")


@service
def scene_match_all(**kwargs):
    """Re-evaluate every room and update the tracker (logs only on change)."""
    if not FP:
        _load()
    lights_all = state.names("light")
    # prune the dynamic-scene map: a room whose group light is off is not dynamic
    try:
        dmap = json.loads(state.get("input_text.dynamic_scenes") or "{}")
    except Exception:
        dmap = {}
    pruned = {}
    for r, sc in dmap.items():
        g = GROUP_PREFIX + slugify(r)
        if g in lights_all and state.get(g) == "on":
            pruned[r] = sc
    if pruned != dmap:
        input_text.set_value(entity_id="input_text.dynamic_scenes", value=json.dumps(pruned))
    ar = _ar()
    before = _result()
    result = dict(before)
    for r in list(result.keys()):
        if _excluded(result[r]):
            result.pop(r, None)
    dbg = []
    for room, scenes in FP.items():
        line = _update_room(room, scenes, ar, lights_all, result)
        if line:
            dbg.append(line)
    if result != before:
        state.set(TRACKER, "ok", active_scene=result)
        log.warning("Matcher " + " | ".join(dbg))


@service
def scene_match_room(room=None, **kwargs):
    """Re-evaluate a single room on demand (always logs its distance line)."""
    if not room:
        return
    if not FP:
        _load()
    key = None
    for k in FP.keys():
        if slugify(k) == slugify(room):
            key = k
            break
    if key is None:
        log.warning(f"Matcher: room '{room}' not in DB")
        return
    lights_all = state.names("light")
    result = _result()
    line = _update_room(key, FP[key], _ar(), lights_all, result)
    state.set(TRACKER, "ok", active_scene=result)
    log.warning("Matcher " + (line or key))


@state_trigger(f"{DYNAMIC_SENSOR}")
def _on_dyn_change(**kwargs):
    task.sleep(2)
    scene_match_all()


@time_trigger("startup")
def _loop():
    log.warning("Matcher loop started")
    while True:
        try:
            scene_match_all()
        except Exception as e:
            log.error(f"Matcher error: {e}")
        task.sleep(LOOP_SECONDS)
