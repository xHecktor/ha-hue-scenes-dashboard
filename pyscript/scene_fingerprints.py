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
CALIB_LOG = "/config/scene_calibrate_log.txt"

# Hue-bridge protection: every scene/light command from every worker passes
# through one gate that spaces commands by at least this many seconds, so
# running several rooms in parallel can never flood the bridge or outrun the
# Zigbee radio / HA state updates. 0.5 s = at most 2 commands/second total.
MIN_CMD_INTERVAL = 0.5
_cmd_busy = [False]     # list holders = module-mutable without `global`
_writing = [False]      # serialises DB file writes across parallel workers
_scene_count = [0]      # scenes finished (for the dashboard status)
_abort = [False]        # set by the stop service; workers check and bail out


@pyscript_compile
def _clog_init(path, room, contrast, parallel):
    import datetime
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"scene_calibrate  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write(f"room={room or 'all'}  contrast={contrast}  parallel={parallel}\n\n")
            f.flush()
        return True
    except Exception as e:
        log.error(f"CALIB: cannot init log {path}: {e}")
        return False


@pyscript_compile
def _clog(path, line):
    # Append one line and flush immediately, so nothing is lost on a crash.
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except Exception:
        pass


def _group_for(room, lights_all):
    """Resolve a room's (or zone's) native Hue group light, or None. Keep in
    sync with scene_match.py.

    The native Hue group light (`is_hue_group: true`, `friendly_name` = the
    room name) the integration already provides -- no manual group, no naming
    convention, and `is_hue_group` separates the group from a same-named
    single bulb.
    """
    for lid in lights_all:
        a = state.getattr(lid) or {}
        if a.get("is_hue_group") and a.get("friendly_name") == room:
            return lid
    return None


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
    # Progress shown on the dashboard admin card. Keep it minimal: the state
    # stays "running"/"idle" and only the `label` attribute changes -- this is
    # the form that reliably re-renders the card live. The counter and ETA are
    # baked into the label text itself. (Changing the state value or adding
    # extra attributes each scene stopped the card from refreshing mid-run.)
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
    # stopped transitioning (some Hue lamps drift their colour temperature -- or
    # briefly report the wrong color_mode -- for several seconds after a scene
    # change). color_mode is part of the snapshot so we never record a lamp
    # mid-mode-flip and bake in the wrong colour axis. Keep in sync with
    # scene_match.py.
    s = []
    for lid in members:
        if state.get(lid) != "on":
            s.append((lid, "off"))
            continue
        a = state.getattr(lid) or {}
        ct = a.get("color_temp_kelvin")
        xy = a.get("xy_color") or [0, 0]
        s.append((lid, a.get("brightness") or 0, a.get("color_mode"),
                  (ct // 25 if ct else -1), round(xy[0], 2), round(xy[1], 2)))
    return s


def _force_refresh(members):
    # Some Hue lamps report their colour temperature with a long lag after a
    # scene change: brightness updates at once, but ct stays stuck on the
    # previous scene's value for many seconds (light.kuche does this). Ask the
    # integration to re-fetch the real state so we don't fingerprint a stale ct.
    try:
        homeassistant.update_entity(entity_id=members)
    except Exception as e:
        log.warning(f"FINGERPRINT: update_entity failed: {e}")


def _wait_settled(members, settle, max_wait=45.0):
    # Wait at least `settle` seconds and until two 1s-apart reads are identical
    # (brightness AND color_mode AND colour, or max_wait), so we record the
    # settled state, not a mid-transition one. The cap is generous because a
    # laggy lamp can take 20-30 s for its colour temperature to catch up; fast
    # lamps still return in a few seconds via the early-exit. A forced refresh
    # each second nudges a lamp whose ct is stuck on a stale reported value.
    prev = None
    waited = 0.0
    while waited < max_wait:
        if _abort[0]:
            return
        task.sleep(1.0)
        waited += 1.0
        if int(waited) % 3 == 0:
            _force_refresh(members)
        snap = _snap(members)
        if snap == prev and waited >= float(settle):
            return
        prev = snap


def _capture(members):
    # One settled snapshot of the members as fingerprint records.
    e = {}
    for lid in members:
        la = state.getattr(lid) or {}
        if la.get("dynamics") == "dynamic_palette":
            continue  # foreign-dynamic member -> don't record its cycling colour
        on = state.get(lid) == "on"
        e[lid] = _fp_light(la, on)
    return e


# --------------------------------------------------------------------------
# Throttled Hue commands (one shared gate for all parallel workers).
# --------------------------------------------------------------------------

def _gate_acquire():
    while _cmd_busy[0]:
        task.sleep(0.05)
    _cmd_busy[0] = True


def _gate_release():
    # Hold the gate for the spacing interval so the NEXT command is delayed --
    # this is what caps the global command rate and protects the bridge.
    task.sleep(MIN_CMD_INTERVAL)
    _cmd_busy[0] = False


def _gated_scene(eid):
    _gate_acquire()
    try:
        scene.turn_on(entity_id=eid)
    except Exception as e:
        log.warning(f"CALIB: scene {eid} failed: {e}")
    _gate_release()


def _gated_light(**kw):
    _gate_acquire()
    try:
        light.turn_on(**kw)
    except Exception as e:
        log.warning(f"CALIB: light cmd failed: {e}")
    _gate_release()


def _prime_group(group, warm):
    # Prime ALL members with one/two group commands (far gentler on the bridge
    # than per-lamp). Brightness contrast (bright vs dim) + a colour-temp nudge
    # (warm vs cool; ignored by brightness-only members). Running the scene from
    # each opposite prime and diffing per attribute reveals what it controls.
    _gated_light(entity_id=group, transition=0, brightness=230 if warm else 30)
    _gated_light(entity_id=group, transition=0,
                 color_temp_kelvin=2200 if warm else 6000)


def _uncontrolled_attrs(ra, rb):
    # Compare the two contrast passes PER attribute. Returns the list of
    # attributes the scene did NOT drive to the same value -> uncontrolled;
    # ["on"] if even the on/off state was left free (full wildcard); [] if the
    # scene controls everything.
    if bool(ra.get("off")) != bool(rb.get("off")):
        return ["on"]
    if ra.get("off") and rb.get("off"):
        return []
    un = []
    if abs((ra.get("bri") or 0) - (rb.get("bri") or 0)) > 40:
        un.append("bri")
    if "ct" in ra and "ct" in rb:
        if abs(ra["ct"] - rb["ct"]) > 300:
            un.append("ct")
    if "xy" in ra and "xy" in rb:
        dx = ra["xy"][0] - rb["xy"][0]
        dy = ra["xy"][1] - rb["xy"][1]
        if (dx * dx + dy * dy) ** 0.5 > 0.05:
            un.append("xy")
    return un


def _save_db(db):
    # Serialise DB writes so two parallel workers never clobber the file.
    while _writing[0]:
        task.sleep(0.1)
    _writing[0] = True
    task.executor(_write_json, FINGERPRINT_FILE, db)
    _writing[0] = False


def _calibrate_scene(group, members, eid, settle, contrast):
    if not contrast:
        _gated_scene(eid)
        _wait_settled(members, settle)
        return _capture(members)
    # Contrast: activate the scene from a warm+bright prime and again from a
    # cool+dim prime; per attribute, values that end up different are the ones
    # the scene does not control.
    _prime_group(group, True)
    task.sleep(1.5)
    _gated_scene(eid)
    _wait_settled(members, settle)
    run_a = _capture(members)
    _prime_group(group, False)
    task.sleep(1.5)
    _gated_scene(eid)
    _wait_settled(members, settle)
    run_b = _capture(members)
    entry = {}
    for lid, rb in run_b.items():
        ra = run_a.get(lid)
        if ra is not None:
            un = _uncontrolled_attrs(ra, rb)
            if un == ["on"]:
                rb = dict(rb)
                rb["dontcare"] = True
            elif un:
                rb = dict(rb)
                rb["dontcare"] = un
        entry[lid] = rb
    return entry


def _calibrate_room(rname, group, scene_list, settle, contrast, db):
    members = (state.getattr(group) or {}).get("entity_id") or []
    db[rname] = {}
    _clog(CALIB_LOG, f"[{rname}] start  group={group}  {len(scene_list)} scenes")
    for eid, name in scene_list:
        if _abort[0]:
            _clog(CALIB_LOG, f"[{rname}] ABORTED")
            break
        try:
            entry = _calibrate_scene(group, members, eid, settle, contrast)
            db[rname][eid] = entry
            _scene_count[0] += 1
            dcs = []
            for lid, r in entry.items():
                v = r.get("dontcare")
                if v is True:
                    dcs.append(f"{lid.split('.')[-1]}=ALL")
                elif v:
                    dcs.append(f"{lid.split('.')[-1]}={'+'.join(v)}")
            line = f"  {rname}: {name}  ({len(entry)} lamps)"
            if dcs:
                line += "  don't-care: " + "; ".join(dcs)
            _clog(CALIB_LOG, line)
            _status(f"{rname}: {name} ({_scene_count[0]})", True)
            log.warning(f"CALIB {rname}: {name}" + (f"  dc={dcs}" if dcs else ""))
        except Exception as e:
            _clog(CALIB_LOG, f"  {rname}: {name}  ERROR {e}")
            log.error(f"CALIB {rname} {eid}: {e}")
    _save_db(db)          # crash-safe: the room's results are on disk now
    _clog(CALIB_LOG, f"[{rname}] done, saved")


def _calib_worker(queue, rooms, settle, contrast, db, done):
    while queue and not _abort[0]:
        rname = queue.pop()
        try:
            info = rooms[rname]
            _calibrate_room(rname, info["group"], info["scenes"], settle, contrast, db)
        except Exception as e:
            log.error(f"CALIB worker {rname}: {e}")
            _clog(CALIB_LOG, f"[{rname}] WORKER ERROR {e}")
    done[0] += 1


_CALIB_TASK = None
_CALIB_WORKERS = []


@service
def scene_fingerprint_calibrate(room=None, settle=4, contrast=True, parallel=1):
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
  contrast:
    description: Thorough mode (default ON) -- run each scene twice from opposite
      states and detect, per attribute, what the scene does NOT control (marked
      don't-care and ignored when matching). ~2x the flashing. Turn off only for
      a quick re-capture of scenes you know control every lamp.
    example: true
  parallel:
    description: How many rooms to calibrate at once (whole-home only). Commands
      are rate-limited to protect the bridge, so this mainly overlaps the settle
      waits. 1 = sequential.
    example: 3
"""
    global _CALIB_TASK
    _CALIB_TASK = task.create(_calibrate_run, room, settle, contrast, parallel)
    log.warning(f"CALIB spawned (room={room or 'all'}, contrast={contrast}, "
                f"parallel={parallel}) -> log {CALIB_LOG}")


@service
def scene_fingerprint_calibrate_stop(**kwargs):
    """yaml
name: Stop a running calibration
description: Abort the background calibration. Workers finish the current scene,
  write what they have, and stop. The dashboard status shows ABGEBROCHEN.
"""
    _abort[0] = True
    _status("Wird abgebrochen …", True)
    log.warning("CALIB: abort requested")


def _calibrate_run(room=None, settle=4, contrast=True, parallel=1):
    task.unique("scene_calibrate")
    _cmd_busy[0] = False
    _writing[0] = False
    _scene_count[0] = 0
    _abort[0] = False
    _clog_init(CALIB_LOG, room, contrast, parallel)
    _status(f"Kalibriere {room or 'alle Räume'} …", True)
    log.warning(f"CALIB worker started (room={room or 'all'}, contrast={contrast})")

    db = task.executor(_read_json, FINGERPRINT_FILE)
    if room:
        for k in list(db.keys()):
            if slugify(k) == slugify(room):
                db.pop(k, None)
    else:
        db = {}

    lights_all = state.names("light")
    rooms = {}
    for eid in state.names("scene"):
        a = state.getattr(eid) or {}
        if a.get("group_type") not in ("room", "zone"):
            continue
        rn = a.get("group_name")
        if not rn:
            continue
        if room and slugify(rn) != slugify(room):
            continue
        g = _group_for(rn, lights_all)
        if g not in lights_all:
            _clog(CALIB_LOG, f"[{rn}] skipped (no Hue group)")
            continue
        # Skip whole-home meta-zones (a group spanning most lamps, e.g. an
        # "apartment" zone) on whole-home runs -- fingerprinting them is
        # pointless (they overlap every room) and their huge settles stall the
        # run. Still allowed if the user calibrates that zone by name.
        gm = (state.getattr(g) or {}).get("entity_id") or []
        if not room and len(lights_all) and len(gm) > 0.6 * len(lights_all):
            _clog(CALIB_LOG, f"[{rn}] skipped (meta-zone, {len(gm)}/{len(lights_all)} lamps)")
            continue
        rooms.setdefault(rn, {"group": g, "scenes": []})
        rooms[rn]["scenes"].append((eid, a.get("name") or eid))

    total = 0
    for rn in rooms:
        total += len(rooms[rn]["scenes"])
    _clog(CALIB_LOG, f"{len(rooms)} rooms, {total} scenes\n")

    queue = list(rooms.keys())
    nworkers = int(parallel) if parallel else 1
    if nworkers < 1:
        nworkers = 1
    if nworkers > len(queue):
        nworkers = max(1, len(queue))

    if nworkers == 1:
        while queue and not _abort[0]:
            rn = queue.pop()
            info = rooms[rn]
            _calibrate_room(rn, info["group"], info["scenes"], settle, contrast, db)
    else:
        done = [0]
        global _CALIB_WORKERS
        _CALIB_WORKERS = []
        for _ in range(nworkers):
            _CALIB_WORKERS.append(
                task.create(_calib_worker, queue, rooms, settle, contrast, db, done))
        while done[0] < nworkers:
            task.sleep(0.5)

    _save_db(db)
    tag = "ABGEBROCHEN" if _abort[0] else "FERTIG"
    _status(f"{tag} – {_scene_count[0]}/{total} Szenen ({room or 'alle'})", False)
    _clog(CALIB_LOG, f"\n{tag}: {_scene_count[0]}/{total} scenes")
    log.warning(f"CALIB {tag}: {_scene_count[0]}/{total} scenes -> DB saved")
    _abort[0] = False
