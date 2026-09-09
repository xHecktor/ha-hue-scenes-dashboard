"""Scene-detection diagnostics for Home Assistant (pyscript).

An automated, reproducible self-test that drives the lights itself and checks
what the live matcher makes of the result -- so verifying detection no longer
means clicking through scenes by hand and reading logs.

For every static scene it:
  * approaches the scene from several DIFFERENT source scenes (the coolest and
    the dimmest in the room), because a laggy lamp misbehaves depending on
    where it came from -- a cool->warm transition is the worst case;
  * waits with a SELF-ADJUSTING settle: it polls `pyscript.scene_tracker`
    (the real matcher's verdict) until that verdict holds steady for a few
    reads, nudging laggy lamps with a forced refresh, and records how long
    that took;
  * logs every run -- source, target, detected scene, seconds-to-stable,
    PASS/FAIL -- plus a summary and the longest stabilisation time seen (a
    good basis for tuning the matcher's settle).

Kept separate from the runtime matcher (scene_match.py) and the calibration
(scene_fingerprints.py) on purpose: this file only observes and reports, it
never changes the fingerprint database.

Requires pyscript with `allow_all_imports: true`.
"""

import json
from homeassistant.util import slugify

VERSION = "d2"  # printed in the log so the running version is visible

FINGERPRINT_FILE = "/config/scene_fingerprints.json"
TRACKER = "pyscript.scene_tracker"
EXCLUDE_SUFFIXES = ("_naturliches_licht",)


@pyscript_compile
def _read_json(path):
    import json, os
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _group_for(room, lights_all):
    # Native Hue group light for the room/zone (kept in sync with the others).
    for lid in lights_all:
        a = state.getattr(lid) or {}
        if a.get("is_hue_group") and a.get("friendly_name") == room:
            return lid
    return None


def _excluded(sc):
    for suf in EXCLUDE_SUFFIXES:
        if sc.endswith(suf):
            return True
    return False


def _detected(room):
    # What the running matcher currently thinks is active in this room.
    try:
        return ((state.getattr(TRACKER) or {}).get("active_scene") or {}).get(room)
    except Exception:
        return None


def _mean_bri_ct(entry):
    bris, cts = [], []
    for r in entry.values():
        if r.get("off"):
            continue
        if r.get("bri") is not None:
            bris.append(r["bri"])
        if r.get("ct") is not None:
            cts.append(r["ct"])
    return (sum(bris) / len(bris) if bris else 0,
            sum(cts) / len(cts) if cts else 0)


def _wait_stable_match(room, members, min_wait=3.0, max_wait=45.0, need=3):
    # Self-adjusting settle: poll the matcher's verdict once a second (nudging
    # laggy lamps with a forced refresh) until it holds steady for `need` reads,
    # or max_wait. Returns (detected_scene, seconds_waited).
    last = "<init>"
    stable = 0
    waited = 0.0
    while waited < max_wait:
        try:
            homeassistant.update_entity(entity_id=members)
        except Exception:
            pass
        task.sleep(1.0)
        waited += 1.0
        det = _detected(room)
        if det == last:
            stable += 1
        else:
            stable = 0
            last = det
        if stable >= need and waited >= min_wait:
            break
    return last, waited


@service
def scene_diagnose(room=None, settle=4, **kwargs):
    """yaml
name: Diagnose scene detection (multi-path)
description: Drive every static scene from several different source scenes and
  check the live matcher identifies each one, waiting a self-adjusting time
  until the verdict is stable. Logs source -> target, detected scene, seconds
  to stabilise and PASS/FAIL for every run, plus a summary. The lights flash
  through the scenes. Read-only -- it never changes the fingerprint database.
fields:
  room:
    description: Only this room (group_name). Empty = whole home.
    example: Flur
  settle:
    description: Seconds to hold each SOURCE scene before switching to the target
    example: 4
"""
    task.unique("scene_diagnose")
    db = task.executor(_read_json, FINGERPRINT_FILE)
    lights_all = state.names("light")
    n_ok = 0
    n_fail = 0
    fails = []
    slowest = 0.0
    slowest_what = "-"
    log.warning(f"DIAGNOSE {VERSION}: start (room={room or 'all'}, settle={settle})")

    for rname, scenes in db.items():
        if room and slugify(rname) != slugify(room):
            continue
        group = _group_for(rname, lights_all)
        if not group:
            log.warning(f"DIAGNOSE {rname}: skipped (no Hue group)")
            continue
        members = (state.getattr(group) or {}).get("entity_id") or []
        # NB: build lists with plain loops, not comprehensions calling pyscript
        # functions, and pick extremes without max/min+lambda -- pyscript can't
        # pass its (async) functions as a `key=` to builtins.
        names = []
        for s in scenes:
            if not _excluded(s):
                names.append(s)
        if len(names) < 2:
            continue

        # Contrasting sources: the coolest (highest mean ct) and the dimmest
        # (lowest mean brightness). Approaching a target from these exercises
        # the worst ct- and brightness-lag transitions.
        coolest = None
        dimmest = None
        best_ct = -1.0
        low_bri = 1e9
        for s in names:
            mb, mc = _mean_bri_ct(scenes[s])
            if mc > best_ct:
                best_ct = mc
                coolest = s
            if mb < low_bri:
                low_bri = mb
                dimmest = s
        sources = []
        for s in (coolest, dimmest):
            if s and s not in sources:
                sources.append(s)
        log.warning(f"DIAGNOSE {rname}: {len(names)} scenes, "
                    f"sources={[s.split('.')[-1] for s in sources]}")

        for target in names:
            tshort = target.split(".")[-1]
            for src in sources:
                if src == target:
                    continue
                sshort = src.split(".")[-1]
                try:
                    scene.turn_on(entity_id=src)
                    task.sleep(float(settle))
                    scene.turn_on(entity_id=target)
                    det, elapsed = _wait_stable_match(rname, members)
                    dshort = (det or "-").split(".")[-1]
                    if elapsed > slowest:
                        slowest = elapsed
                        slowest_what = f"{rname}: {sshort} -> {tshort}"
                    if det == target:
                        n_ok += 1
                        log.warning(f"DIAGNOSE  OK  {rname}: {sshort} -> {tshort} "
                                    f"(stable in {elapsed:.0f}s)")
                    else:
                        n_fail += 1
                        fails.append(f"{rname}: {sshort} -> {tshort} = {dshort} ({elapsed:.0f}s)")
                        log.warning(f"DIAGNOSE FAIL {rname}: {sshort} -> {tshort} "
                                    f"detected {dshort} (after {elapsed:.0f}s)")
                except Exception as e:
                    log.error(f"DIAGNOSE error {rname} {sshort}->{tshort}: {e}")

    summary = (f"DIAGNOSE done: {n_ok} OK, {n_fail} FAIL | "
               f"slowest stabilise {slowest:.0f}s ({slowest_what})")
    if fails:
        summary += "".join(f"\n  FAIL {f}" for f in fails)
    log.warning(summary)
