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

# Per-room light group entity = GROUP_PREFIX + slugify(room).
# Its `entity_id` attribute must list the room's member lights.
GROUP_PREFIX = "light.dimmer_"


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
    rec = {"bri": (ml.get("brightness") if is_on else 0), "mode": ml.get("color_mode")}
    if ml.get("color_mode") == "color_temp":
        rec["ct"] = ml.get("color_temp_kelvin")
    else:
        if ml.get("xy_color"):
            rec["xy"] = list(ml.get("xy_color"))
        if ml.get("hs_color"):
            rec["hs"] = list(ml.get("hs_color"))
    return rec


@service
def scene_fingerprint_calibrate(room=None, settle=2):
    """yaml
name: Calibrate scene fingerprints
fields:
  room:
    description: Only this room (group_name, case-insensitive). Empty = whole home.
    example: Wohnzimmer
  settle:
    description: Seconds to wait per scene before measuring
    example: 2
"""
    log.warning(f"FINGERPRINT: start (room={room or 'all'})")
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
            if attrs.get("group_type") != "room":
                continue
            rname = attrs.get("group_name")
            if not rname:
                continue
            if room and slugify(rname) != slugify(room):
                continue
            group = GROUP_PREFIX + slugify(rname)
            if group not in lights_all:
                log.warning(f"FINGERPRINT: {eid} skipped (no {group})")
                continue

            scene.turn_on(entity_id=eid)
            task.sleep(float(settle))

            members = (state.getattr(group) or {}).get("entity_id") or []
            entry = {}
            for lid in members:
                if state.get(lid) != "on":
                    continue
                entry[lid] = _fp_light(state.getattr(lid) or {}, True)

            db.setdefault(rname, {})[eid] = entry
            count += 1
            log.warning(f"FINGERPRINT: {eid} ({len(entry)} lights)")
        except Exception as e:
            log.error(f"FINGERPRINT: error at {eid}: {e}")

    task.executor(_write_json, FINGERPRINT_FILE, db)
    log.warning(f"FINGERPRINT: done, {count} scenes stored")
