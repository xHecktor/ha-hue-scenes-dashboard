"""Hue scene fingerprint calibration for Home Assistant (pyscript).

Activates each room scene once (statically), records the resulting per-light
state (brightness + color temperature or xy color) and stores it in
`/config/scene_fingerprints.json`. Run once to bootstrap; day-to-day the DB
keeps itself fresh via `scene_learn` in scene_match.py.

NOTE: the lights briefly flash through every scene while calibrating.

Requires pyscript with `allow_all_imports: true`.
"""

import json
from homeassistant.util import slugify

FINGERPRINT_FILE = "/config/scene_fingerprints.json"

# Fallback light-group naming for non-Hue setups: GROUP_PREFIX + slugify(room).
GROUP_PREFIX = "light.dimmer_"


def _group_for(room, lights_all):
    """Resolve a room's group light and its member list. Keep in sync with
    scene_match.py.

    Prefer the native Hue group light (`is_hue_group: true`, `friendly_name`
    = the room name) the integration already provides -- no manual group, no
    naming convention, umlaut-proof, and `is_hue_group` separates the group
    from a same-named single bulb. Fall back to GROUP_PREFIX + slug (with
    umlaut tolerance) for non-Hue setups.
    """
    for lid in lights_all:
        a = state.getattr(lid) or {}
        if a.get("is_hue_group") and a.get("friendly_name") == room:
            return lid
    primary = GROUP_PREFIX + slugify(room)
    if primary in lights_all:
        return primary
    de = room.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        de = de.replace(a, b)
    alt = GROUP_PREFIX + slugify(de)
    if alt in lights_all:
        return alt
    return primary


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


def _status(label, running):
    # Progress shown on the dashboard admin card.
    state.set("pyscript.calibration", "running" if running else "idle", label=label)


def _fp_light(ml, is_on):
    # An off member is a fingerprint of its own ("this light must be off"),
    # the strongest discriminator between scenes that light up different
    # subsets of a group. Keep in sync with scene_match.py.
    if not is_on:
        return {"bri": 0, "off": True}
    rec = {"bri": ml.get("brightness"), "mode": ml.get("color_mode")}
    # Record EVERY colour value the lamp reports (full picture for diagnosis and
    # future signals); `pc` marks the axis matching compares on. Keep in sync
    # with scene_match.py.
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
    # A coarse snapshot of the members' state, used to detect when a scene has
    # stopped transitioning (some Hue lamps drift their colour temperature for
    # several seconds after a scene change).
    s = []
    for lid in members:
        if state.get(lid) != "on":
            s.append((lid, "off"))
            continue
        a = state.getattr(lid) or {}
        ct = a.get("color_temp_kelvin")
        xy = a.get("xy_color") or [0, 0]
        s.append((lid, a.get("brightness") or 0,
                  (ct // 25 if ct else -1), round(xy[0], 2), round(xy[1], 2)))
    return s


def _wait_settled(members, settle, max_wait=15.0):
    # Wait at least `settle` seconds and until two 1s-apart reads are identical
    # (or max_wait), so we record the settled state, not a mid-transition one.
    prev = None
    waited = 0.0
    while waited < max_wait:
        task.sleep(1.0)
        waited += 1.0
        snap = _snap(members)
        if snap == prev and waited >= float(settle):
            return
        prev = snap


@service
def scene_fingerprint_calibrate(room=None, settle=4):
    """yaml
name: Calibrate scene fingerprints
fields:
  room:
    description: Only this room (group_name, case-insensitive). Empty = whole home.
    example: Wohnzimmer
  settle:
    description: Seconds to wait per scene before measuring (raise it if a
      lamp's colour is still drifting when recorded)
    example: 4
"""
    log.warning(f"FINGERPRINT: start (room={room or 'all'})")
    _status(f"Kalibriere {room or 'alle Räume'} …", True)
    db = task.executor(_read_json, FINGERPRINT_FILE)
    if room:
        for k in list(db.keys()):
            if slugify(k) == slugify(room):
                db.pop(k, None)
    else:
        db = {}

    lights_all = state.names("light")
    count = 0

    for eid in state.names("scene"):
        try:
            attrs = state.getattr(eid) or {}
            if attrs.get("group_type") not in ("room", "zone"):
                continue
            rname = attrs.get("group_name")
            if not rname:
                continue
            if room and slugify(rname) != slugify(room):
                continue
            group = _group_for(rname, lights_all)
            if group not in lights_all:
                log.warning(f"FINGERPRINT: {eid} skipped (no {group})")
                continue

            scene.turn_on(entity_id=eid)
            members = (state.getattr(group) or {}).get("entity_id") or []
            _wait_settled(members, settle)

            entry = {}
            for lid in members:
                la = state.getattr(lid) or {}
                if la.get("dynamics") == "dynamic_palette":
                    continue  # foreign-dynamic member -> don't record its cycling colour
                on = state.get(lid) == "on"
                entry[lid] = _fp_light(la, on)

            db.setdefault(rname, {})[eid] = entry
            count += 1
            _status(f"{rname}: {attrs.get('name') or eid} ({count})", True)
            log.warning(f"FINGERPRINT: {eid} ({len(entry)} lights)")
        except Exception as e:
            log.error(f"FINGERPRINT: error at {eid}: {e}")

    task.executor(_write_json, FINGERPRINT_FILE, db)
    _status(f"Fertig – {count} Szenen ({room or 'alle'})", False)
    log.warning(f"FINGERPRINT: done, {count} scenes stored")
