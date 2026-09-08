"""Hue active-scene detection for Home Assistant (pyscript).

Figures out which *static* Hue scene is currently active per room by comparing
the live light states against fingerprints (captured during calibration or
learned on activation) and writes the result to `pyscript.scene_tracker`
(attribute `active_scene`, a {room: scene_entity_id} map).

Requires pyscript with `allow_all_imports: true`.
"""

import json
import math
import time
from homeassistant.util import slugify

FINGERPRINT_FILE = "/config/scene_fingerprints.json"
DYNAMIC_SENSOR = "sensor.dynamische_szenen"
TRACKER = "pyscript.scene_tracker"

# Fallback light-group naming for non-Hue setups: GROUP_PREFIX + slugify(room).
GROUP_PREFIX = "light.dimmer_"

# room name -> resolved group entity (cheap cache; invalidated if it vanishes)
GROUP_CACHE = {}


def _group_for(room, lights_all):
    """Resolve a room's group light and its member list.

    The Philips Hue integration already exposes one group light per room and
    zone (`is_hue_group: true`, `friendly_name` = the room name, `entity_id`
    listing the members) -- so we just match on that. No manual helper group,
    no `light.dimmer_` naming convention and no umlaut slug matching needed;
    `is_hue_group` also separates the group (e.g. `light.kuche_2`) from a
    same-named single bulb (`light.kuche`).

    Falls back to the GROUP_PREFIX + slug convention (with umlaut tolerance)
    for non-Hue setups.
    """
    cached = GROUP_CACHE.get(room)
    if cached and cached in lights_all:
        return cached
    found = None
    for lid in lights_all:
        a = state.getattr(lid) or {}
        if a.get("is_hue_group") and a.get("friendly_name") == room:
            found = lid
            break
    if not found:
        primary = GROUP_PREFIX + slugify(room)
        if primary in lights_all:
            found = primary
        else:
            de = room.lower()
            for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
                de = de.replace(a, b)
            alt = GROUP_PREFIX + slugify(de)
            found = alt if alt in lights_all else primary
    GROUP_CACHE[room] = found
    return found

ACCEPT = 0.35        # max distance to accept a match
CLEAR = 0.60         # above this: clearly nothing -> mark room unknown
KEEP_MARGIN = 0.10   # keep current scene only while within this of the best
LOCK_SECONDS = 30    # after a user tap, don't override the room for this long
LOOP_SECONDS = 15    # background re-evaluation interval (backstop; events drive speed)
SETTLE_AFTER_CHANGE = 1.5  # wait for the Hue fade to finish before matching
# Scene distance is the MEAN of the per-lamp distances (robust: no single lamp
# dominates) blended with the single largest per-lamp distance:
#   distance = (1 - w) * mean + w * max
# 0.3 is a measured compromise: when two scenes differ in only ONE lamp of a
# group and the rest are identical (e.g. Hell vs Lesen where a shared always-on
# strip dilutes the mean), the lone differing lamp still lifts them apart --
# Badezimmer Hell/Lesen goes 0.078 -> ~0.10, Arbeitszimmer 0.082 -> ~0.10 --
# while uniformly-differing pairs (mean == max) are untouched. Blend only ever
# raises cross-scene distances (better separation); a true scene still matches
# itself at ~0. Set to 0 for pure mean; raise toward ~0.5 for even tighter
# separation at the cost of a single noisy lamp mattering more.
BLEND_WEIGHT = 0.3
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
    # Record EVERY colour value the lamp reports (ct, xy, hs, rgb, effect), so
    # the JSON is the full picture for diagnosis and future signals -- even the
    # ones matching doesn't use today. `pc` marks which axis matching should
    # actually compare on (a lamp reports several notations of the same colour,
    # and can momentarily report color_mode 'onoff' after a scene change while
    # the colour values are already populated).
    ct = ml.get("color_temp_kelvin")
    xy = ml.get("xy_color")
    hs = ml.get("hs_color")
    rgb = ml.get("rgb_color")
    eff = ml.get("effect")
    if ct is not None:
        rec["ct"] = ct
    if xy:
        rec["xy"] = list(xy)
    if hs:
        rec["hs"] = list(hs)
    if rgb:
        rec["rgb"] = list(rgb)
    if eff and eff != "off":
        rec["effect"] = eff
    if ml.get("color_mode") == "color_temp" and ct is not None:
        rec["pc"] = "ct"
    elif xy:
        rec["pc"] = "xy"
    elif ct is not None:
        rec["pc"] = "ct"
    elif hs:
        rec["pc"] = "hs"
    return rec


def _snap(members):
    s = []
    for lid in members:
        if state.get(lid) != "on":
            s.append((lid, "off"))
            continue
        a = state.getattr(lid) or {}
        ct = a.get("color_temp_kelvin")
        xy = a.get("xy_color") or [0, 0]
        # color_mode is part of the snapshot on purpose: a Hue lamp can briefly
        # report the wrong mode (e.g. xy) right after a scene change before it
        # settles on color_temp. Without the mode here the snapshot looked
        # "stable" mid-transition and calibration baked in the wrong colour axis
        # (that was the Arbeitszimmer ruhephase bug: stored as rgb/xy instead of
        # ct). Waiting for the mode to hold steady too fixes that at the source.
        s.append((lid, a.get("brightness") or 0, a.get("color_mode"),
                  (ct // 25 if ct else -1), round(xy[0], 2), round(xy[1], 2)))
    return s


def _wait_settled(members, settle, max_wait=15.0):
    # Wait at least `settle` seconds and until two 1s-apart reads are identical
    # (brightness AND color_mode AND colour), so a lamp that keeps drifting its
    # colour -- or is still flipping colour_mode -- after a scene change is
    # recorded only once it has fully stopped moving.
    prev = None
    waited = 0.0
    while waited < max_wait:
        task.sleep(1.0)
        waited += 1.0
        snap = _snap(members)
        if snap == prev and waited >= float(settle):
            return
        prev = snap


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
    dists = []
    for lid, fp in lights.items():
        # A lamp a contrast calibration proved this scene does NOT control
        # (flagged `dontcare`) holds whatever the previous scene left behind,
        # so it is not a feature of this scene -> never let it weigh in.
        if fp.get("dontcare"):
            continue
        attrs = state.getattr(lid) or {}
        # A member currently running its own dynamic palette (e.g. a light
        # shared with another room that is playing a dynamic scene there) is
        # cycling colours and would only add noise -> treat it as a wildcard
        # so a foreign-dynamic light can never disturb this room's match.
        if attrs.get("dynamics") == "dynamic_palette":
            continue
        is_on = state.get(lid) == "on"
        want_on = not fp.get("off")
        # A light that is off in both the fingerprint and live is not a
        # discriminating feature. Counting it would dilute the average and
        # drown the lights that actually differ (e.g. two 1-lamp night scenes
        # in a big room that differ only in that lamp's brightness). Skip it
        # entirely so only lights that are on somewhere weigh in.
        if not want_on and not is_on:
            continue
        fp_bri = fp.get("bri")
        # A pure on/off device (no brightness recorded, e.g. a smart plug or an
        # on/off strip) can only inform via its state. When it's on in both it
        # adds nothing, so don't let it dilute the average; count it only when
        # the on/off state actually mismatches.
        if fp_bri is None:
            if want_on != is_on:
                dists.append(1.0)
            continue
        # On/off state is a hard signal and dominates. Crucially, we do NOT
        # read colour from an off light: many Hue lamps keep reporting their
        # last color_temp_kelvin while off, which used to make an off light
        # look almost like an on one (only the brightness differed).
        if want_on != is_on:
            dists.append(1.0)
            continue
        cur_bri = attrs.get("brightness") or 0
        # Brightness distance on a square-root scale. Linear /255 flattened dim
        # scenes into noise (7 vs 23 -> 0.06); a log scale fixed the dim end but
        # over-compressed the middle, so two mid scenes that differ only in
        # brightness (e.g. 90 vs 143) collapsed to ~0.08 and could not be told
        # apart. sqrt stays sensitive at both ends: 7 vs 23 -> 0.13, 90 vs 143
        # -> 0.15, while high-end noise (240 vs 255) stays small.
        d = abs(math.sqrt(fp_bri) - math.sqrt(cur_bri)) / 16.0
        # Colour weight scales the colour term by how bright the lamp is, so a
        # dim lamp (whose colour is barely visible) is told apart mainly by
        # brightness + on/off. But the two colour axes behave differently when
        # dim: color_temp_kelvin jitters wildly at low brightness, so it keeps
        # the aggressive linear weight; an xy colour point stays stable even at
        # bri ~50-100, so it gets a gentler sqrt weight -- otherwise genuinely
        # different colour scenes (a deep orange vs a pink at bri 75) collapsed.
        base = min(fp_bri, cur_bri) / 255.0
        # Which colour axis to compare on. `pc` is the axis the scene was
        # captured in (from its settled color_mode) and stays the primary
        # choice. But a lamp can momentarily report a different mode at match
        # time (only xy while it should be color_temp, say); rather than charge
        # full colour penalty for that transient, fall back to whichever axis
        # the fingerprint and the live lamp currently BOTH carry, preferring the
        # most stable one (ct > xy > hs). New fingerprints carry `pc`; older
        # ones stored only a primary axis, so seed the order from what exists.
        pc = fp.get("pc")
        if pc is None:
            pc = "ct" if "ct" in fp else ("xy" if "xy" in fp else ("hs" if "hs" in fp else None))
        live = {"ct": attrs.get("color_temp_kelvin"),
                "xy": attrs.get("xy_color"), "hs": attrs.get("hs_color")}
        axis = None
        for cand in [pc, "ct", "xy", "hs"]:
            if cand and cand in fp and live.get(cand):
                axis = cand
                break
        if axis is None:
            # fingerprint had a colour but the live lamp reports none right now
            if pc is not None:
                d += math.sqrt(base) * 1.0
        elif axis == "ct":
            # /1200: a ~190 K gap (Hell 2702 vs Lesen 2890) is a real, visible
            # difference; /2000 rated it 0.09 and the two stayed inseparable.
            # ct jitters when dim, so it keeps the aggressive linear brightness
            # weight; xy/hs stay stable when dim and get a gentler sqrt weight.
            d += base * (abs(live["ct"] - fp["ct"]) / 1200.0)
        elif axis == "xy":
            cw = math.sqrt(base)
            cur = live["xy"]
            dx = cur[0] - fp["xy"][0]
            dy = cur[1] - fp["xy"][1]
            d += cw * (((dx * dx + dy * dy) ** 0.5) / 0.25)
        elif axis == "hs":
            cw = math.sqrt(base)
            cur = live["hs"]
            dh = abs(cur[0] - fp["hs"][0])
            dh = min(dh, 360 - dh)
            d += cw * (dh / 180.0 + abs(cur[1] - fp["hs"][1]) / 100.0)
        dists.append(d)
    if not dists:
        return None
    mean = sum(dists) / len(dists)
    if BLEND_WEIGHT <= 0.0:
        return mean
    # Blend in the single largest per-lamp distance so a pair of scenes that
    # differ in only ONE lamp (which the mean dilutes) still separates.
    return (1.0 - BLEND_WEIGHT) * mean + BLEND_WEIGHT * max(dists)


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
    group = _group_for(room, lights_all)
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
    best, d = _best(scenes, current)
    # Hold the current scene only while it stays within a small margin of the
    # best candidate. This damps flip-flop between near-identical scenes but,
    # unlike the old absolute threshold, still lets the room switch to a
    # clearly different scene (e.g. Stillen <-> Nachtlicht, ~0.20 apart).
    if current and current in scenes and not _excluded(current):
        dc = _scene_distance(scenes[current])
        if dc is not None and dc <= CLEAR and (d is None or dc <= d + KEEP_MARGIN):
            return f"{room}:{current.split('.')[-1]}:keep({round(dc, 2)})"
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
def scene_learn(scene=None, settle=4, **kwargs):
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
    lights_all = state.names("light")
    group = _group_for(room, lights_all)
    if group not in lights_all:
        return
    members = (state.getattr(group) or {}).get("entity_id") or []
    _wait_settled(members, settle)
    entry = {}
    for lid in members:
        la = state.getattr(lid) or {}
        if la.get("dynamics") == "dynamic_palette":
            continue  # foreign-dynamic member -> don't bake its cycling colour in
        on = state.get(lid) == "on"
        entry[lid] = _fp_light(la, on)
    if not entry:
        return
    # A single-pass re-learn cannot re-verify which lamps the scene controls,
    # so carry any `dontcare` flags from the previous (contrast-calibrated)
    # entry forward instead of silently dropping them on every tap.
    prev = FP.get(room, {}).get(scene, {})
    for lid, rec in entry.items():
        p = prev.get(lid)
        if isinstance(p, dict) and p.get("dontcare"):
            rec["dontcare"] = True
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
        g = _group_for(r, lights_all)
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
def scene_debug(room=None, **kwargs):
    """yaml
name: Debug scene distances
description: Log the live light state and every scene's distance for one room.
fields:
  room:
    description: Room name (group_name)
    example: Flur
"""
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
        log.warning(f"Matcher DEBUG: room '{room}' not in DB")
        return
    group = _group_for(key, state.names("light"))
    members = (state.getattr(group) or {}).get("entity_id") or []
    lines = [f"DEBUG {key}  group={group}={state.get(group)}"]
    for lid in members:
        a = state.getattr(lid) or {}
        on = state.get(lid) == "on"
        col = ""
        if a.get("color_temp_kelvin") is not None:
            col = f"ct={a.get('color_temp_kelvin')}"
        elif a.get("xy_color"):
            col = f"xy={a.get('xy_color')}"
        dyn = " DYN" if a.get("dynamics") == "dynamic_palette" else ""
        bri = a.get("brightness") if on else 0
        lines.append(f"  {lid.split('.')[-1]}: {'on' if on else 'off'} bri={bri} {col}{dyn}")
    scored = []
    for sc, lights in FP[key].items():
        d = _scene_distance(lights)
        scored.append((999.0 if d is None else d, sc))
    scored.sort()
    lines.append(f"  --- ACCEPT<={ACCEPT} CLEAR>={CLEAR} keep-margin={KEEP_MARGIN} ---")
    for d, sc in scored:
        tag = " [EXCL]" if _excluded(sc) else ""
        val = "-" if d == 999.0 else round(d, 3)
        lines.append(f"  {val}  {sc.split('.')[-1]}{tag}")
    log.warning("Matcher " + "\n".join(lines))


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


@event_trigger("state_changed", f"entity_id.startswith('{GROUP_PREFIX}')")
def _on_light_change(**kwargs):
    # A room group light changed (a scene was set, dimmed, turned off, ...).
    # Re-evaluate right after the Hue fade settles instead of waiting for the
    # background loop. task.unique coalesces a burst of changes into one run,
    # so we match once, 1.5 s after the last change.
    task.unique("scene_light_settle")
    task.sleep(SETTLE_AFTER_CHANGE)
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
