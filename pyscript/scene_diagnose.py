"""
Scene-detection diagnostics for Home Assistant (pyscript).

An automated, reproducible data collector: it drives the lights itself and
records -- for every scene transition -- the full per-lamp state trajectory,
so the matcher can be tuned against real data instead of guesswork.

For each transition it logs:
  * a REF block per room (each lamp's static capabilities, once);
  * the group light AND every member;
  * every dynamic parameter (brightness, color_mode, ct, xy, hs, rgb, effect,
    dynamics, mode) at the start, at every change (t+Ns) and at rest;
  * the real per-lamp settle time (which lamp lags);
  * the delta of each lamp's settled value vs. its fingerprint (Δfp);
  * the matcher's verdict and OK/FAIL.

The scene order is an Eulerian walk over the complete transition graph, so
every activation is itself a measured transition (no wasted setup switches).

Read-only -- it never changes the fingerprint database.

Requires pyscript with allow_all_imports: true.
"""

import json
from homeassistant.util import slugify


VERSION = "d10"

# Strong reference to the background task so it isn't garbage-collected
# (and cancelled) the moment the service function returns.
_DIAG_TASK = None

# All persistent data lives under one folder (was scattered in /config root).
DATA_DIR = "/config/hue_scenes"
FINGERPRINT_FILE = DATA_DIR + "/fingerprints.json"
_LEGACY_FINGERPRINT_FILE = "/config/scene_fingerprints.json"
REPORT_FILE = DATA_DIR + "/diagnose_report.txt"
DRIFT_REPORT_FILE = DATA_DIR + "/dim_drift_report.txt"
TRACKER = "pyscript.scene_tracker"
EXCLUDE_SUFFIXES = ("_naturliches_licht",)


@pyscript_compile
def _read_json(path):
    import json
    import os
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pyscript_compile
def _write_report_line(path, line):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        return True
    except Exception as e:
        log.error(f"DIAGNOSE: report write failed: {e}")
        return False


@pyscript_compile
def _init_report(path, room, settle, mode):
    import datetime, os
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"scene_diagnose {VERSION}  {now}\n")
            f.write(f"room={room or 'all'}  settle={settle}s  mode={mode}\n")
            f.write("\n")
            f.flush()
        return True
    except Exception as e:
        log.error(f"DIAGNOSE: cannot initialize report {path}: {e}")
        return False


def _group_for(room, lights_all):
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
    try:
        return (
            (state.getattr(TRACKER) or {})
            .get("active_scene", {})
            .get(room)
        )
    except Exception:
        return None


def _mean_bri_ct(entry):
    bris = []
    cts = []
    for r in entry.values():
        if r.get("off"):
            continue
        if r.get("bri") is not None:
            bris.append(r["bri"])
        if r.get("ct") is not None:
            cts.append(r["ct"])
    return (
        sum(bris) / len(bris) if bris else 0,
        sum(cts) / len(cts) if cts else 0,
    )


# ---------------------------------------------------------------------------
# Per-lamp state capture, formatting and change detection.
# ---------------------------------------------------------------------------

def _lamp_state(lid):
    # Full dynamic state of one light. Colour coords are rounded to damp jitter.
    a = state.getattr(lid) or {}
    if state.get(lid) != "on":
        return {"on": False}
    d = {"on": True}
    d["b"] = a.get("brightness")
    d["cm"] = a.get("color_mode")
    d["ct"] = a.get("color_temp_kelvin")
    xy = a.get("xy_color")
    d["xy"] = [round(xy[0], 3), round(xy[1], 3)] if xy else None
    hs = a.get("hs_color")
    d["hs"] = [round(hs[0], 1), round(hs[1], 1)] if hs else None
    rgb = a.get("rgb_color")
    d["rgb"] = [rgb[0], rgb[1], rgb[2]] if rgb else None
    d["eff"] = a.get("effect")
    d["dyn"] = a.get("dynamics")
    d["mode"] = a.get("mode")
    return d


def _fmt_lamp(d):
    # Every dynamic parameter on one line.
    if not d.get("on"):
        return "off"
    parts = [f"b{d.get('b')}", f"cm={d.get('cm')}"]
    if d.get("ct") is not None:
        parts.append(f"ct{d.get('ct')}")
    if d.get("xy"):
        parts.append(f"xy{d['xy'][0]},{d['xy'][1]}")
    if d.get("hs"):
        parts.append(f"hs{d['hs'][0]},{d['hs'][1]}")
    if d.get("rgb"):
        parts.append(f"rgb{d['rgb'][0]},{d['rgb'][1]},{d['rgb'][2]}")
    parts.append(f"eff={d.get('eff')}")
    parts.append(f"dyn={d.get('dyn')}")
    parts.append(f"mode={d.get('mode')}")
    return " ".join(parts)


def _lamp_changes(prev, cur):
    # List of "field old->new" strings for meaningful changes (jitter filtered:
    # ct > 25 K, xy > 0.005, hs > 1). Shows how each parameter evolves/lags.
    ch = []
    if not prev or not cur:
        return ch
    if bool(prev.get("on")) != bool(cur.get("on")):
        ch.append(f"on {prev.get('on')}->{cur.get('on')}")
        return ch
    if not cur.get("on"):
        return ch
    if prev.get("b") != cur.get("b"):
        ch.append(f"b{prev.get('b')}->{cur.get('b')}")
    if prev.get("cm") != cur.get("cm"):
        ch.append(f"cm {prev.get('cm')}->{cur.get('cm')}")
    pc = prev.get("ct")
    cc = cur.get("ct")
    if pc is not None and cc is not None:
        if abs(pc - cc) > 25:
            ch.append(f"ct{pc}->{cc}")
    elif (pc is None) != (cc is None):
        ch.append(f"ct{pc}->{cc}")
    px = prev.get("xy")
    cx = cur.get("xy")
    if px and cx:
        if abs(px[0] - cx[0]) > 0.005 or abs(px[1] - cx[1]) > 0.005:
            ch.append(f"xy{px[0]},{px[1]}->{cx[0]},{cx[1]}")
    ph = prev.get("hs")
    chs = cur.get("hs")
    if ph and chs:
        if abs(ph[0] - chs[0]) > 1.0 or abs(ph[1] - chs[1]) > 1.0:
            ch.append(f"hs{ph[0]},{ph[1]}->{chs[0]},{chs[1]}")
    if prev.get("rgb") != cur.get("rgb"):
        ch.append(f"rgb{prev.get('rgb')}->{cur.get('rgb')}")
    if prev.get("eff") != cur.get("eff"):
        ch.append(f"eff {prev.get('eff')}->{cur.get('eff')}")
    if prev.get("dyn") != cur.get("dyn"):
        ch.append(f"dyn {prev.get('dyn')}->{cur.get('dyn')}")
    if prev.get("mode") != cur.get("mode"):
        ch.append(f"mode {prev.get('mode')}->{cur.get('mode')}")
    return ch


def _ref_line(lid):
    # One-time static capabilities of a lamp (they never change per scene).
    a = state.getattr(lid) or {}
    modes = a.get("supported_color_modes") or []
    mn = a.get("min_color_temp_kelvin")
    mx = a.get("max_color_temp_kelvin")
    effs = a.get("effect_list") or []
    feat = a.get("supported_features")
    name = a.get("friendly_name")
    parts = [f"modes={modes}"]
    if mn is not None or mx is not None:
        parts.append(f"ct={mn}..{mx}")
    if effs:
        parts.append(f"effects={effs}")
    parts.append(f"features={feat}")
    parts.append(f'name="{name}"')
    return " ".join(parts)


def _fp_delta_line(target_fp, labeled_members, end):
    # Settled live value of each member vs. its fingerprint value in the target
    # scene -- surfaces lag and stale calibration at a glance.
    out = []
    for label, lid in labeled_members:
        fp = target_fp.get(lid)
        e = end.get(label)
        if fp is None or e is None:
            continue
        if fp.get("off"):
            if e.get("on"):
                out.append(f"{label}: soll off, ist on")
            continue
        if not e.get("on"):
            out.append(f"{label}: soll on, ist off")
            continue
        segs = []
        fb = fp.get("bri")
        eb = e.get("b")
        if fb is not None and eb is not None and abs(fb - eb) > 10:
            segs.append(f"b {eb} vs {fb}")
        fc = fp.get("ct")
        ec = e.get("ct")
        if fc is not None and ec is not None and abs(fc - ec) > 50:
            segs.append(f"ct {ec} vs {fc} (d{abs(ec - fc)})")
        if segs:
            out.append(f"{label}: " + " ".join(segs))
    if not out:
        return "Dfp: alle Lampen ~ Sollwert"
    return "Dfp: " + " | ".join(out)


def _euler_circuit(names):
    # Eulerian circuit over the COMPLETE directed graph on `names`: visits every
    # ordered (a->b) pair exactly once, so every activation in the walk is a
    # measured transition and none is wasted setting up a source. Hierholzer.
    n = len(names)
    remaining = {}
    for i in range(n):
        lst = []
        for j in range(n):
            if j != i:
                lst.append(j)
        remaining[i] = lst
    circuit = []
    stack = [0]
    while stack:
        v = stack[-1]
        if remaining[v]:
            w = remaining[v].pop()
            stack.append(w)
        else:
            circuit.append(stack.pop())
    circuit.reverse()
    out = []
    for i in circuit:
        out.append(names[i])
    return out


def _measure(room, labeled, ent_ids, min_verdict=10.0, max_wait=25.0, need=3):
    # Single polling loop. Returns:
    #   verdict, light_secs, verdict_secs, trajectory, start, end, settle_per_lamp
    # trajectory is a list of (t_seconds, [change strings per lamp]).
    prev = {}
    for label, lid in labeled:
        prev[label] = _lamp_state(lid)
    start = prev
    traj = []
    settle_at = {}
    for label, lid in labeled:
        settle_at[label] = 0.0
    last_verdict = "<init>"
    verdict_stable = 0
    stable = 0
    waited = 0.0
    light_secs = None
    while waited < max_wait:
        try:
            homeassistant.update_entity(entity_id=ent_ids)
        except Exception:
            pass
        task.sleep(1.0)
        waited += 1.0
        cur = {}
        for label, lid in labeled:
            cur[label] = _lamp_state(lid)
        line_changes = []
        any_change = False
        for label, lid in labeled:
            ch = _lamp_changes(prev.get(label), cur.get(label))
            if ch:
                any_change = True
                settle_at[label] = waited
                line_changes.append(f"{label}: " + " ".join(ch))
        if line_changes:
            traj.append((waited, line_changes))
        if any_change:
            stable = 0
        else:
            stable += 1
        if light_secs is None and stable >= 2:
            light_secs = waited - 2.0
        prev = cur
        det = _detected(room)
        if det == last_verdict:
            verdict_stable += 1
        else:
            verdict_stable = 0
            last_verdict = det
        if (light_secs is not None
                and verdict_stable >= need
                and waited >= min_verdict):
            break
    end = {}
    for label, lid in labeled:
        end[label] = _lamp_state(lid)
    if light_secs is None:
        light_secs = waited
    if light_secs < 0:
        light_secs = 0.0
    return last_verdict, light_secs, waited, traj, start, end, settle_at


# ---------------------------------------------------------------------------
# Report writing for one transition.
# ---------------------------------------------------------------------------

def _log_transition(scenes, target, labeled, labeled_members,
                    tag, sshort, tshort, dshort,
                    light_secs, verdict_secs, traj, start, end, settle_at):
    _write_report_line(REPORT_FILE, f"  {sshort} -> {tshort}")
    _write_report_line(REPORT_FILE, "       start t+0s:")
    for label, lid in labeled:
        _write_report_line(REPORT_FILE, f"         {label}: {_fmt_lamp(start[label])}")
    for t, changes in traj:
        _write_report_line(REPORT_FILE, f"       t+{t:.0f}s  " + "   ".join(changes))
    _write_report_line(REPORT_FILE, f"       end t+{light_secs:.0f}s:")
    for label, lid in labeled:
        _write_report_line(REPORT_FILE, f"         {label}: {_fmt_lamp(end[label])}")
    sp = []
    for label, lid in labeled:
        sp.append(f"{label}={settle_at[label]:.0f}s")
    _write_report_line(REPORT_FILE, "       settle/lampe: " + " ".join(sp))
    _write_report_line(REPORT_FILE, "       " + _fp_delta_line(scenes[target], labeled_members, end))
    _write_report_line(REPORT_FILE, f"       ERGEBNIS: {tag}  verdict={dshort} ({verdict_secs:.0f}s)")
    _write_report_line(REPORT_FILE, "")


# ---------------------------------------------------------------------------
# Service + worker.
# ---------------------------------------------------------------------------

@service
def scene_diagnose(room=None, settle=4, mode="full", **kwargs):
    """yaml
name: Diagnose scene detection (data collector)
description: Drive every scene transition and log the full per-lamp parameter
  trajectory (all dynamic attributes, at start / every change / at rest), the
  real per-lamp settle time, the delta vs the fingerprint, and the matcher's
  verdict -- data to tune the matcher on. 'full' walks an Eulerian circuit so
  every activation is a measured transition; 'quick' only uses the coolest and
  dimmest sources. Writes /config/scene_diagnose_report.txt. Runs in the
  background (returns at once). Read-only -- never changes the database.
fields:
  room:
    description: Only this room (group_name). Empty = whole home.
    example: Flur
  settle:
    description: Seconds to hold a scene before switching (quick mode only).
    example: 4
  mode:
    description: "full = Eulerian walk of every transition; quick = coolest + dimmest sources"
    example: full
"""
    global _DIAG_TASK
    _DIAG_TASK = task.create(_diagnose_run, room, settle, mode)
    log.warning(
        f"DIAGNOSE {VERSION}: spawned background run "
        f"(room={room or 'all'}, mode={mode}) -> report to {REPORT_FILE}"
    )


def _diagnose_run(room=None, settle=4, mode="full"):
    task.unique("scene_diagnose")
    log.warning(
        f"DIAGNOSE {VERSION}: worker started "
        f"(room={room or 'all'}, settle={settle}, mode={mode})"
    )

    if not _init_report(REPORT_FILE, room, settle, mode):
        log.error("DIAGNOSE: stopping - report file not writable")
        return

    try:
        db = task.executor(_read_json, FINGERPRINT_FILE)
        if not db:
            db = task.executor(_read_json, _LEGACY_FINGERPRINT_FILE)
    except Exception as e:
        log.error(f"DIAGNOSE: could not read fingerprint file: {e}")
        return
    if not db:
        log.error("DIAGNOSE: fingerprint database empty")
        _write_report_line(REPORT_FILE, "ERROR: fingerprint database empty")
        return

    lights_all = state.names("light")
    n_ok = 0
    n_fail = 0
    fails = []
    slowest = 0.0
    slowest_what = "-"

    for rname, scenes in db.items():
        if room and slugify(rname) != slugify(room):
            continue
        group = _group_for(rname, lights_all)
        if not group:
            _write_report_line(REPORT_FILE, f"[{rname}] SKIPPED - no Hue group")
            continue
        members = (state.getattr(group) or {}).get("entity_id") or []
        # Skip whole-home meta-zones (a group spanning most lamps, e.g. an
        # "apartment" zone): huge settles stall the run and they overlap every
        # room. Still diagnosable by naming that zone explicitly.
        if not room and len(lights_all) and len(members) > 0.6 * len(lights_all):
            _write_report_line(
                REPORT_FILE,
                f"[{rname}] SKIPPED - meta-zone ({len(members)}/{len(lights_all)} lamps)")
            continue

        names = []
        for s in scenes:
            if not _excluded(s):
                names.append(s)
        if len(names) < 2:
            _write_report_line(REPORT_FILE, f"[{rname}] SKIPPED - <2 scenes")
            continue

        # group first, then members (label, entity_id)
        labeled = [("group", group)]
        for lid in members:
            labeled.append((lid.split(".")[-1], lid))
        labeled_members = labeled[1:]
        ent_ids = [group]
        for lid in members:
            ent_ids.append(lid)

        # Room header + static reference block (once).
        _write_report_line(REPORT_FILE, f"[{rname}]  group={group}  ({len(names)} scenes, mode={mode})")
        _write_report_line(REPORT_FILE, f"REF group: {_ref_line(group)}")
        for label, lid in labeled_members:
            _write_report_line(REPORT_FILE, f"REF {label}: {_ref_line(lid)}")
        _write_report_line(REPORT_FILE, "")

        # Build the transition list.
        if mode == "quick":
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
            steps = []
            for target in names:
                for src in sources:
                    if src != target:
                        steps.append((src, target, True))
        else:
            seq = _euler_circuit(names)
            # prime the first scene and let it settle (not a measured transition)
            scene.turn_on(entity_id=seq[0])
            _measure(rname, labeled, ent_ids)
            steps = []
            for k in range(1, len(seq)):
                steps.append((seq[k - 1], seq[k], False))

        _write_report_line(REPORT_FILE, f"  ({len(steps)} transitions)")
        _write_report_line(REPORT_FILE, "")

        for src, target, activate_src in steps:
            sshort = src.split(".")[-1]
            tshort = target.split(".")[-1]
            try:
                if activate_src:
                    scene.turn_on(entity_id=src)
                    task.sleep(float(settle))
                scene.turn_on(entity_id=target)
                (det, light_secs, verdict_secs,
                 traj, start, end, settle_at) = _measure(rname, labeled, ent_ids)
                dshort = (det or "-").split(".")[-1]
                ok = det == target
                if light_secs > slowest:
                    slowest = light_secs
                    slowest_what = f"{rname}: {sshort} -> {tshort}"
                if ok:
                    n_ok += 1
                    tag = "OK"
                else:
                    n_fail += 1
                    tag = "FAIL"
                    fails.append(f"{rname}: {sshort} -> {tshort} = {dshort}")
                log.warning(
                    f"DIAGNOSE {tag} {rname}: {sshort} -> {tshort} "
                    f"lights={light_secs:.0f}s verdict={dshort}"
                )
                _log_transition(scenes, target, labeled, labeled_members,
                                tag, sshort, tshort, dshort,
                                light_secs, verdict_secs, traj, start, end, settle_at)
            except Exception as e:
                log.error(f"DIAGNOSE error {rname} {sshort}->{tshort}: {e}")
                _write_report_line(REPORT_FILE, f"  ERR  {sshort} -> {tshort}: {e}")

    summary = (
        f"DIAGNOSE done: {n_ok} OK, {n_fail} FAIL | "
        f"slowest settle {slowest:.0f}s ({slowest_what})"
    )
    log.warning(summary)
    _write_report_line(REPORT_FILE, "")
    _write_report_line(REPORT_FILE, summary)
    if fails:
        _write_report_line(REPORT_FILE, "")
        _write_report_line(REPORT_FILE, "FAIL SUMMARY:")
        for f in fails:
            _write_report_line(REPORT_FILE, f"  {f}")
    log.warning(f"DIAGNOSE report written to {REPORT_FILE}")


# ---------------------------------------------------------------------------
# Dim-drift probe: how each lamp's reported colour drifts as a scene is dimmed
# hard, and at which brightness the live matcher stops recognising the scene.
# Auto-selects the most *colourful* scenes per room (the ones where deep dimming
# is a real risk); warm/white scenes are uninteresting here.
# ---------------------------------------------------------------------------

_DRIFT_TASK = None
_DIM_LEVELS = (100, 50, 25, 12, 6)   # percent of each lamp's own scene brightness

# Planckian (white/colour-temperature) locus in xy -- distance off it = "how
# colourful" (0 for any white/warm tint, large for a saturated colour).
_DLOCUS = ((0.5267, 0.4133), (0.4909, 0.4173), (0.4578, 0.4102), (0.4369, 0.4041),
           (0.4009, 0.3827), (0.3805, 0.3768), (0.3608, 0.3636), (0.3451, 0.3516),
           (0.3287, 0.3417), (0.3123, 0.3282))


@pyscript_compile
def _colorfulness(xy):
    import math
    best = 1.0
    for i in range(len(_DLOCUS) - 1):
        ax, ay = _DLOCUS[i]
        bx, by = _DLOCUS[i + 1]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((xy[0] - ax) * dx + (xy[1] - ay) * dy) / L))
        d = math.hypot(xy[0] - (ax + t * dx), xy[1] - (ay + t * dy))
        if d < best:
            best = d
    return best


def _scene_colorfulness(entry):
    # Mean off-locus distance of the scene's on-lamps that carry an xy colour.
    vals = []
    for rec in entry.values():
        if not isinstance(rec, dict) or rec.get("off") or not rec.get("xy"):
            continue
        vals.append(_colorfulness(rec["xy"]))
    return sum(vals) / len(vals) if vals else 0.0


@pyscript_compile
def _xy_dist(a, b):
    import math
    if not a or not b:
        return None
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _wait_hold(labeled, settle, max_wait=20.0):
    # Wait >= settle seconds AND until two 1 s reads are identical, so drift is
    # measured only once the lamps have stopped moving at the new brightness.
    prev = None
    waited = 0.0
    while waited < max_wait:
        task.sleep(1.0)
        waited += 1.0
        snap = tuple(str(_lamp_state(lid)) for _, lid in labeled)
        if snap == prev and waited >= float(settle):
            return
        prev = snap


@service
def scene_dim_drift(room=None, scenes=None, levels=None, settle=4, top=3):
    """yaml
name: Diagnose dim drift (colour vs brightness)
description: For the most colourful scenes (auto-selected), activate the scene
  then step every lamp down through several brightness levels, recording how
  each lamp's reported colour drifts and at which level the live matcher stops
  recognising the scene. Writes /config/hue_scenes/dim_drift_report.txt. Runs in
  the background; read-only (restores nothing -- next activation resets state).
fields:
  room:
    description: Only this room (group_name). Empty = whole home.
    example: Wohnzimmer
  scenes:
    description: Comma-separated scene slugs to force (overrides auto-select).
    example: wohnzimmer_rio,wohnzimmer_blue_planet
  top:
    description: How many of the most colourful scenes per room to probe.
    example: 3
  settle:
    description: Seconds to hold each brightness level before measuring.
    example: 4
"""
    global _DRIFT_TASK
    _DRIFT_TASK = task.create(_dim_drift_run, room, scenes, levels, settle, top)
    log.warning(f"DIM-DRIFT {VERSION}: spawned (room={room or 'all'}) -> {DRIFT_REPORT_FILE}")


def _dim_drift_run(room=None, scenes=None, levels=None, settle=4, top=3):
    task.unique("scene_dim_drift")
    if not _init_report(DRIFT_REPORT_FILE, room, settle, "dim-drift"):
        log.error("DIM-DRIFT: report not writable")
        return
    lvls = _DIM_LEVELS
    if levels:
        try:
            lvls = tuple(int(x) for x in str(levels).replace(" ", "").split(","))
        except Exception:
            pass
    forced = None
    if scenes:
        forced = set(s.strip() for s in str(scenes).split(",") if s.strip())

    db = task.executor(_read_json, FINGERPRINT_FILE)
    if not db:
        db = task.executor(_read_json, _LEGACY_FINGERPRINT_FILE)
    if not db:
        _write_report_line(DRIFT_REPORT_FILE, "ERROR: fingerprint database empty")
        return

    lights_all = state.names("light")
    breaks = []   # (room, scene, first level where detection fails)
    for rname, entries in db.items():
        group = _group_for(rname, lights_all)
        if not group or group not in lights_all:
            continue
        members = (state.getattr(group) or {}).get("entity_id") or []
        if not room and members and len(members) > 0.6 * len(lights_all):
            _write_report_line(DRIFT_REPORT_FILE, f"[{rname}] SKIPPED - meta-zone")
            continue
        if room and slugify(rname) != slugify(room):
            continue

        # choose scenes: forced, else the `top` most colourful (skip warm/white)
        ranked = []
        for full, entry in entries.items():
            sshort = full.split(".")[-1]
            if _excluded(full):
                continue
            if forced is not None:
                if sshort in forced:
                    ranked.append((1.0, full, entry))
                continue
            cf = _scene_colorfulness(entry)
            if cf >= 0.08:
                ranked.append((cf, full, entry))
        ranked.sort(reverse=True)
        if forced is None:
            ranked = ranked[:int(top)]
        if not ranked:
            continue

        labeled = [(lid.split(".")[-1], lid) for lid in members]
        _write_report_line(DRIFT_REPORT_FILE,
                           f"[{rname}]  group={group}  ({len(ranked)} colourful scenes)")

        for cf, full, entry in ranked:
            sshort = full.split(".")[-1]
            try:
                scene.turn_on(entity_id=full)
            except Exception as e:
                _write_report_line(DRIFT_REPORT_FILE, f"  {sshort}: activate failed {e}")
                continue
            _wait_hold(labeled, settle)
            # reference (full-brightness) per-lamp state
            ref = {}
            for lname, lid in labeled:
                ref[lname] = _lamp_state(lid)
            _write_report_line(DRIFT_REPORT_FILE, f"\n  {sshort}  (colourfulness {cf:.3f})")
            verdicts = []
            first_break = None
            for lvl in lvls:
                if lvl < 100:
                    for lname, lid in labeled:
                        r = ref.get(lname) or {}
                        if not r.get("on") or not r.get("b"):
                            continue
                        tb = max(1, int(round(r["b"] * lvl / 100.0)))
                        try:
                            light.turn_on(entity_id=lid, brightness=tb)
                        except Exception:
                            pass
                        task.sleep(0.3)   # space commands, protect the bridge
                    _wait_hold(labeled, settle)
                det = _detected(rname)
                dshort = det.split(".")[-1] if det else "-"
                verdicts.append(f"{lvl}%={dshort}")
                if first_break is None and dshort != sshort:
                    first_break = lvl
                # per-lamp drift line for this level (colourful lamps only)
                for lname, lid in labeled:
                    r0 = ref.get(lname) or {}
                    if not r0.get("on") or not r0.get("xy"):
                        continue
                    if _colorfulness(r0["xy"]) < 0.08:
                        continue
                    cur = _lamp_state(lid)
                    dxy = _xy_dist(cur.get("xy"), r0.get("xy"))
                    drift = f"Δxy{dxy:.3f}" if dxy is not None else "Δxy-"
                    _write_report_line(
                        DRIFT_REPORT_FILE,
                        f"     {lname:20s} {lvl:3d}%: b{cur.get('b')} "
                        f"xy{cur.get('xy')} ct{cur.get('ct')} {drift}")
            _write_report_line(DRIFT_REPORT_FILE, "     verdict/level: " + "  ".join(verdicts))
            if first_break:
                breaks.append((rname, sshort, first_break))
                _write_report_line(DRIFT_REPORT_FILE,
                                   f"     -> Erkennung bricht bei {first_break}%")
            else:
                _write_report_line(DRIFT_REPORT_FILE,
                                   f"     -> bis {lvls[-1]}% korrekt erkannt")

    _write_report_line(DRIFT_REPORT_FILE, "")
    _write_report_line(DRIFT_REPORT_FILE, f"DIM-DRIFT done: {len(breaks)} Szenen brechen vor dem tiefsten Level")
    for r, s, lvl in breaks:
        _write_report_line(DRIFT_REPORT_FILE, f"  {r}: {s} -> {lvl}%")
    log.warning(f"DIM-DRIFT report written to {DRIFT_REPORT_FILE}")
