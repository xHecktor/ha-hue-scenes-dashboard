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

VERSION = "v19"  # bumped on every change; printed in the log to confirm what runs

# All persistent data for this integration lives in one folder so it is easy to
# find and back up (was scattered as scene_*.* in /config root). The old paths
# are still read as a fallback so an existing install keeps working until the
# next calibration writes the file to its new home.
DATA_DIR = "/config/hue_scenes"
FINGERPRINT_FILE = DATA_DIR + "/fingerprints.json"
_LEGACY_FINGERPRINT_FILE = "/config/scene_fingerprints.json"
# Optional overrides for the tunable scoring parameters, written by
# tools/tune_matcher.py. Absent -> the code defaults below are used.
PARAMS_FILE = DATA_DIR + "/params.json"
DYNAMIC_SENSOR = "sensor.dynamische_szenen"
TRACKER = "pyscript.scene_tracker"

# room name -> resolved group entity (cheap cache; invalidated if it vanishes)
GROUP_CACHE = {}


def _group_for(room, lights_all):
    """Resolve a room's (or zone's) native Hue group light, or None.

    The Philips Hue integration exposes one group light per room and zone
    (`is_hue_group: true`, `friendly_name` = the room name, `entity_id`
    listing the members), so we just match on that -- no manual helper group
    and no naming convention. `is_hue_group` also separates the group (e.g.
    `light.kuche_2`) from a same-named single bulb (`light.kuche`).
    """
    cached = GROUP_CACHE.get(room)
    if cached and cached in lights_all:
        return cached
    for lid in lights_all:
        a = state.getattr(lid) or {}
        if a.get("is_hue_group") and a.get("friendly_name") == room:
            GROUP_CACHE[room] = lid
            return lid
    return None

ACCEPT = 0.35        # max distance to accept a match
CLEAR = 0.60         # above this: clearly nothing -> mark room unknown
KEEP_MARGIN = 0.05   # keep current scene only while within this of the best
#                      (0.05 tuned from the Flur diagnose grid search, paired
#                      with CT_TERM_CAP 0.10 -- see that constant below)
LOCK_SECONDS = 30    # after a user tap, don't override the room for this long
LOOP_SECONDS = 15    # background re-evaluation interval (backstop; events drive speed)
SETTLE_AFTER_CHANGE = 1.5  # min wait after the last change before matching
SETTLE_MAX = 12.0          # cap on the extra wait-until-lights-hold-still. Raised
#                            from 8 s: a few laggy lamps crawl their colour for
#                            ~10 s after a big jump, and matching before they land
#                            was the last source of a wrong verdict.
CONFIRM_COUNT = 2    # an auto-detected scene CHANGE must repeat across this many
#                      evaluations before it replaces the tracked scene, so a
#                      single noisy / mid-settle reading can't flip a room. A user
#                      tap sets the scene instantly (scene_set_active) and is never
#                      gated by this; steady state is unaffected (a stable reading
#                      simply confirms on the next evaluation).
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
# separation at the cost of a single noisy lamp mattering more. 0.2 (was 0.3):
# an offline replay of a full-home diagnose (1516 transitions) showed 0.2 keeps
# the tight-pair separation while letting a single noisy/mode-flipping lamp
# (e.g. a colour-play bar reporting xy one moment, ct the next) count for less.
BLEND_WEIGHT = 0.2
# Penalty for a lamp whose on/off state disagrees with the fingerprint. Kept
# below 1.0 and (see aggregation) OUT of the blend max, so a single accidentally
# powered-off / unavailable lamp cannot alone push an otherwise well-matching
# room over CLEAR -- "one lamp off doesn't change the colours of the rest".
# On/off still discriminates subset scenes because those differ in SEVERAL lamps.
OFF_PENALTY = 0.5
# Penalty for a lamp whose effect (sparkle/candle/fire/prism/...) differs from
# the fingerprint. An effect is a strong, brightness-independent scene marker;
# two scenes identical in colour+brightness but differing only in a lamp's
# effect (Stille Nacht's sparkling tree vs Hell) are separated by this.
EFFECT_PENALTY = 1.0
# Multiplier on an effect-MATCHED lamp's colour+brightness term. 1.0 = no change
# (default; the measured data shows HA reports a stable base colour for effect
# lamps). Lower it (e.g. 0.3) only if an effect scene ever flickers in detection
# -- then colour/brightness are trusted less and the effect match carries.
EFFECT_COLOR_WEIGHT = 1.0
# Cap on a single lamp's colour-temperature distance term. A laggy Hue lamp can
# keep reporting the PREVIOUS scene's ct for many seconds after a change (its
# brightness updates at once), and an uncapped ct term then dominates and picks
# whichever scene happens to share that stale ct (e.g. a dim room still reading
# ct4000 matches "Nachtlicht" instead of the warm "Ruhephase" it just became).
# Capping it lets the reliable brightness decide such cases, while genuine small
# ct differences (Hell 2702 vs Lesen 2890 -> 0.157, capped to 0.10) still tell
# the ct-only scenes apart. 0.10 is tuned from real data: a full Flur diagnose
# run (every transition, scene_diagnose) fed an offline grid search -- 0.10 with
# KEEP_MARGIN 0.05 is the corner that gets all 90 transitions right. Higher lets
# an unreliable ct override brightness (Ruhephase read as Entspannen); lower
# collapses the all-b255 scenes (Hell/Lesen/Kühl hell/...) that differ ONLY in
# ct into each other. 0.20 (was 0.10) makes colour primary: the full-home replay
# proved the all-b255 white scenes (Arbeitszimmer Konzentrieren vs Kühl hell,
# ~100-500 K apart) need it to separate. It relies on uncontrolled ct being
# flagged `dontcare:["ct"]` (contrast calibration) so a stale/laggy ct that the
# scene doesn't actually drive can't dominate -- without those flags a warm
# scene's polluted ct would win at this cap.
CT_TERM_CAP = 0.20
# Colour reliability when dim. Brightness normally down-weights the colour term
# (a dim lamp's colour is barely visible / Hue reports it noisily). But that
# collapsed strongly-coloured scenes when dimmed hard (a deep-red or blue scene
# turned down to ~6% was read as the warm "Nachtlicht"). Two refinements keep
# saturated colour reliable when dim:
#  * COLOUR WEIGHT uses max(saturation, sqrt(brightness)) -- a saturated colour
#    (far from white) keeps full weight even when dim; a near-white tint still
#    gets the brightness down-weight (its xy is unreliable there anyway).
#  * DEEP-DIM CAP: for a colourful lamp (off the white/ct locus) that the user
#    has dimmed BELOW its fingerprint brightness, the brightness-distance term is
#    capped, so a big manual dim can't reject a clear colour match. Asymmetric
#    (only when live < fingerprint) so it never makes matching more permissive in
#    the normal case -- offline replay: colours stay identified down to ~1-3%
#    with zero new mismatches. Warm-white pairs that differ only in brightness
#    (Entspannen vs Ruhephase) are on the locus, so the cap never touches them.
CF_THRESHOLD = 0.10   # xy-distance from the Planckian locus above which a colour
#                       counts as "saturated / colourful" (warm whites are <0.06)
BRI_TERM_CAP = 0.15   # cap on a colourful, dimmed-below-fingerprint lamp's bri term
_LOCUS = ((0.5267, 0.4133), (0.4909, 0.4173), (0.4578, 0.4102), (0.4369, 0.4041),
          (0.4009, 0.3827), (0.3805, 0.3768), (0.3608, 0.3636), (0.3451, 0.3516),
          (0.3287, 0.3417), (0.3123, 0.3282))  # Planckian (white/ct) locus in xy
EXCLUDE_SUFFIXES = ("_naturliches_licht",)  # adaptive scenes to ignore

FP = {}
LOCK = {}
PENDING = {}  # room -> (candidate_scene, consecutive_count) for the switch debounce
_DB_BROKEN = [False]  # True when the fingerprint file is unreadable -> never write


@pyscript_compile
def _read_json(path):
    import json, os
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


@pyscript_compile
def _write_json(path, data):
    # Atomic write: dump to a temp file in the same dir, fsync, then os.replace.
    # A plain open("w") truncates first and a concurrent reader (the matcher) or
    # a second writer (calibration vs. scene_learn both write this file) could
    # see a half-written, torn file -> "Extra data" JSON corruption. os.replace
    # is atomic on POSIX, so readers always see either the old or the new whole
    # file, never a mix.
    import json, os, tempfile
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


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


@pyscript_compile
def _colorfulness(xy):
    # Minimum distance from the Planckian (white/colour-temperature) locus in xy.
    # ~0 for any white or warm tint (which lie on the locus), large for a
    # saturated colour (blue/green/magenta lie well off it). This -- not distance
    # from the D65 white point -- is what tells "colourful" from "warm white",
    # because a warm 2200 K white is far from D65 yet still a white.
    import math
    best = 1.0
    for i in range(len(_LOCUS) - 1):
        ax, ay = _LOCUS[i]
        bx, by = _LOCUS[i + 1]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((xy[0] - ax) * dx + (xy[1] - ay) * dy) / L))
        d = math.hypot(xy[0] - (ax + t * dx), xy[1] - (ay + t * dy))
        if d < best:
            best = d
    return best


@pyscript_compile
def _saturation(xy):
    # 0..1 saturation as xy-distance from the D65 white point (0.20 ~ fully
    # saturated). Used only to keep a saturated colour's weight up when dim.
    import math
    return min(math.hypot(xy[0] - 0.3127, xy[1] - 0.3290) / 0.20, 1.0)


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


def _force_refresh(members):
    # Some Hue lamps report their colour temperature with a long lag after a
    # scene change (brightness updates at once, ct stays stuck on the previous
    # scene's value for many seconds -- light.kuche does this). Ask the
    # integration to re-fetch the real state so we don't learn a stale ct.
    try:
        homeassistant.update_entity(entity_id=members)
    except Exception as e:
        log.warning(f"Matcher: update_entity failed: {e}")


def _wait_settled(members, settle, max_wait=45.0):
    # Wait at least `settle` seconds and until two 1s-apart reads are identical
    # (brightness AND color_mode AND colour), so a lamp that keeps drifting its
    # colour -- or is still flipping colour_mode -- after a scene change is
    # recorded only once it has fully stopped moving. The cap is generous
    # because a laggy lamp's ct can take 20-30 s to catch up; fast lamps still
    # return in a few seconds. A forced refresh nudges a stuck reported value.
    prev = None
    waited = 0.0
    while waited < max_wait:
        task.sleep(1.0)
        waited += 1.0
        if int(waited) % 3 == 0:
            _force_refresh(members)
        snap = _snap(members)
        if snap == prev and waited >= float(settle):
            return
        prev = snap


def _load_params():
    # Apply tunable-parameter overrides from params.json (written by the offline
    # tuner). Only known keys are honoured; everything else keeps its code
    # default, so a partial or stale file can't break the matcher. Explicit
    # `global` (not globals()[k]=) so it reliably rebinds the module constants
    # the scoring functions read.
    global CT_TERM_CAP, BLEND_WEIGHT, KEEP_MARGIN, ACCEPT, CLEAR
    global OFF_PENALTY, BRI_TERM_CAP, CF_THRESHOLD, CONFIRM_COUNT, SETTLE_MAX
    global EFFECT_PENALTY, EFFECT_COLOR_WEIGHT
    try:
        over = task.executor(_read_json, PARAMS_FILE)
    except Exception as e:
        # A corrupt params.json must never break the matcher -- it used to throw
        # here every loop ("Matcher error: Extra data ..."). Ignore it and keep
        # the code defaults; delete/fix the file to re-enable tuned overrides.
        log.error(f"Matcher {VERSION}: {PARAMS_FILE} unreadable ({e}); using "
                  f"default parameters. Delete or fix that file.")
        return
    if not over:
        return
    applied = {}
    if "EFFECT_PENALTY" in over:
        EFFECT_PENALTY = over["EFFECT_PENALTY"]; applied["EFFECT_PENALTY"] = EFFECT_PENALTY
    if "EFFECT_COLOR_WEIGHT" in over:
        EFFECT_COLOR_WEIGHT = over["EFFECT_COLOR_WEIGHT"]; applied["EFFECT_COLOR_WEIGHT"] = EFFECT_COLOR_WEIGHT
    if "CT_TERM_CAP" in over:
        CT_TERM_CAP = over["CT_TERM_CAP"]; applied["CT_TERM_CAP"] = CT_TERM_CAP
    if "BLEND_WEIGHT" in over:
        BLEND_WEIGHT = over["BLEND_WEIGHT"]; applied["BLEND_WEIGHT"] = BLEND_WEIGHT
    if "KEEP_MARGIN" in over:
        KEEP_MARGIN = over["KEEP_MARGIN"]; applied["KEEP_MARGIN"] = KEEP_MARGIN
    if "ACCEPT" in over:
        ACCEPT = over["ACCEPT"]; applied["ACCEPT"] = ACCEPT
    if "CLEAR" in over:
        CLEAR = over["CLEAR"]; applied["CLEAR"] = CLEAR
    if "OFF_PENALTY" in over:
        OFF_PENALTY = over["OFF_PENALTY"]; applied["OFF_PENALTY"] = OFF_PENALTY
    if "BRI_TERM_CAP" in over:
        BRI_TERM_CAP = over["BRI_TERM_CAP"]; applied["BRI_TERM_CAP"] = BRI_TERM_CAP
    if "CF_THRESHOLD" in over:
        CF_THRESHOLD = over["CF_THRESHOLD"]; applied["CF_THRESHOLD"] = CF_THRESHOLD
    if "CONFIRM_COUNT" in over:
        CONFIRM_COUNT = over["CONFIRM_COUNT"]; applied["CONFIRM_COUNT"] = CONFIRM_COUNT
    if "SETTLE_MAX" in over:
        SETTLE_MAX = over["SETTLE_MAX"]; applied["SETTLE_MAX"] = SETTLE_MAX
    if applied:
        log.warning(f"Matcher {VERSION}: params.json overrides {applied}")


def _load():
    global FP
    _load_params()
    try:
        FP = task.executor(_read_json, FINGERPRINT_FILE)
        if not FP:
            # fall back to the pre-v16 location (/config/scene_fingerprints.json)
            FP = task.executor(_read_json, _LEGACY_FINGERPRINT_FILE)
    except Exception as e:
        # A corrupt fingerprint DB must not crash-loop the matcher either; log
        # clearly which file to check and carry on with what we had. Mark the DB
        # broken so scene_learn won't overwrite the file with a partial FP (which
        # would turn a fixable corrupt file into permanent data loss).
        _DB_BROKEN[0] = True
        log.error(f"Matcher {VERSION}: {FINGERPRINT_FILE} unreadable ({e}); "
                  f"detection paused until the file is valid JSON.")
        return
    _DB_BROKEN[0] = False
    total = 0
    for v in FP.values():
        total += len(v)
    log.info(f"Matcher {VERSION}: {total} fingerprints loaded")


def _excluded(sc):
    for suf in EXCLUDE_SUFFIXES:
        if sc.endswith(suf):
            return True
    return False


def _light_snapshot():
    # Coarse state of every light (on/off, brightness, quantised ct), used to
    # detect when a scene change has stopped transitioning. ct is bucketed so a
    # 1-2 K jitter doesn't read as "still moving".
    s = []
    for lid in state.names("light"):
        if state.get(lid) != "on":
            s.append((lid, 0, -1))
            continue
        a = state.getattr(lid) or {}
        ct = a.get("color_temp_kelvin")
        s.append((lid, a.get("brightness") or 0, (ct // 25 if ct else -1)))
    return tuple(s)


def _scene_distance(lights):
    dists = []       # per-lamp brightness/colour distances of agreeing-on lamps
    off_pens = []    # on/off mismatch penalties (kept out of the blend max)
    for lid, fp in lights.items():
        # `dontcare` marks what a contrast calibration proved this scene does
        # NOT control. Legacy value True = the whole lamp is a wildcard (it
        # holds whatever the previous scene left) -> ignore it. A LIST of
        # attribute names (e.g. ["ct"]) means only those attributes are
        # uncontrolled: compare the rest (use brightness, ignore colour temp).
        dc = fp.get("dontcare")
        if dc is True:
            continue
        dc = dc if isinstance(dc, list) else []
        attrs = state.getattr(lid) or {}
        # A member currently running its own dynamic palette (e.g. a light
        # shared with another room that is playing a dynamic scene there) is
        # cycling colours and would only add noise -> treat it as a wildcard
        # so a foreign-dynamic light can never disturb this room's match.
        if attrs.get("dynamics") == "dynamic_palette":
            continue
        # An unavailable lamp (power cut at the switch, zigbee dropout) carries no
        # information about the scene -> treat it as a wildcard. One lamp being
        # off the mains must not change what the colours of the rest say.
        if state.get(lid) == "unavailable":
            continue
        is_on = state.get(lid) == "on"
        # Brightness 0 == off, even when the state/flag says otherwise: a scene
        # can capture a lamp as on-at-bri-0 (or HA reports on with brightness 0),
        # which otherwise looks like "on but very dim" and collides with real
        # scenes. Treat it as off on both the fingerprint and the live side.
        if is_on and attrs.get("brightness") == 0:
            is_on = False
        want_on = not fp.get("off") and fp.get("bri") != 0
        skip_on = "on" in dc
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
            if not skip_on and want_on != is_on:
                off_pens.append(OFF_PENALTY)
            continue
        # On/off state is a hard signal. Crucially, we do NOT read colour from an
        # off light: many Hue lamps keep reporting their last color_temp_kelvin
        # while off, which used to make an off light look almost like an on one
        # (only the brightness differed). The penalty is a moderate OFF_PENALTY
        # (not 1.0) and kept out of the blend max, so a lone off/unavailable lamp
        # can't veto a room the other lamps clearly identify.
        if not skip_on and want_on != is_on:
            off_pens.append(OFF_PENALTY)
            continue
        if not is_on:
            continue
        cur_bri = attrs.get("brightness") or 0
        # Accumulate distance from the controlled attributes only. `contributed`
        # guards against an all-dontcare lamp diluting the average with a zero.
        d = 0.0
        contributed = False
        # Brightness distance on a square-root scale. Linear /255 flattened dim
        # scenes into noise (7 vs 23 -> 0.06); a log scale fixed the dim end but
        # over-compressed the middle, so two mid scenes that differ only in
        # brightness (e.g. 90 vs 143) collapsed to ~0.08 and could not be told
        # apart. sqrt stays sensitive at both ends: 7 vs 23 -> 0.13, 90 vs 143
        # -> 0.15, while high-end noise (240 vs 255) stays small.
        # Colourfulness of the fingerprint colour (distance off the white/ct
        # locus); drives both the deep-dim brightness cap and the colour weight.
        cfl = _colorfulness(fp["xy"]) if fp.get("xy") else 0.0
        if "bri" not in dc:
            bt = abs(math.sqrt(fp_bri) - math.sqrt(cur_bri)) / 16.0
            # Deep-dim cap: for a clearly-coloured lamp that has been dimmed BELOW
            # its fingerprint brightness (the user turned a colour scene way down),
            # cap the brightness penalty so the still-distinctive colour decides.
            # Asymmetric (only cur < fp) so it never loosens matching otherwise.
            if cfl > CF_THRESHOLD and cur_bri < fp_bri:
                bt = min(bt, BRI_TERM_CAP)
            d += bt
            contributed = True
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
        # Colour is skipped when the scene doesn't control it ("color", or the
        # specific axis, in dontcare) -- this is what lets Ruhephase be matched
        # on brightness alone when it leaves the small lamps' ct uncommanded.
        color_off = ("color" in dc) or (axis is not None and axis in dc)
        if not color_off:
            if axis is None:
                # fingerprint had a colour but the live lamp reports none now
                if pc is not None:
                    d += math.sqrt(base) * 1.0
                    contributed = True
            elif axis == "ct":
                # /1200: a ~190 K gap (Hell 2702 vs Lesen 2890) is a real,
                # visible difference. Capped so a stale/laggy ct can't dominate.
                d += min(base * (abs(live["ct"] - fp["ct"]) / 1200.0), CT_TERM_CAP)
                contributed = True
            elif axis == "xy":
                # Weight = max(saturation, sqrt(brightness)): a saturated colour
                # stays reliable when dim (so keep its weight up), a near-white
                # tint falls back to the brightness weight (its xy is noisy dim).
                cw = max(_saturation(fp["xy"]), math.sqrt(base))
                cur = live["xy"]
                dx = cur[0] - fp["xy"][0]
                dy = cur[1] - fp["xy"][1]
                d += cw * (((dx * dx + dy * dy) ** 0.5) / 0.25)
                contributed = True
            elif axis == "hs":
                cw = max(min(fp["hs"][1] / 100.0, 1.0), math.sqrt(base))
                cur = live["hs"]
                dh = abs(cur[0] - fp["hs"][0])
                dh = min(dh, 360 - dh)
                d += cw * (dh / 180.0 + abs(cur[1] - fp["hs"][1]) / 100.0)
                contributed = True
        # Effect (sparkle / candle / fire / prism ...) is part of a scene's
        # identity and is brightness-independent -- and it is NOT a dynamic
        # palette (dyn stays 'none'), so it isn't wildcarded above. Two scenes
        # can be byte-identical in colour+brightness yet differ only in that one
        # lamp sparkles (e.g. Stille Nacht's tree vs Hell). Colour is still
        # compared (the effect animates over a stable base colour); the effect
        # is an ADDITIONAL discriminator. "effect" in dontcare opts out.
        if "effect" not in dc:
            fp_eff = fp.get("effect")
            live_eff = attrs.get("effect")
            live_eff = live_eff if (live_eff and live_eff != "off") else None
            if fp_eff or live_eff:
                if fp_eff != live_eff:
                    d += EFFECT_PENALTY
                    contributed = True
                elif EFFECT_COLOR_WEIGHT != 1.0:
                    # Effect MATCHES. A running effect can animate the lamp's
                    # colour/brightness, so on some lamps/firmware those live
                    # values are unreliable -- optionally down-weight this lamp's
                    # colour+brightness term and lean on the (stable) effect
                    # match instead. Default 1.0 = no change: in the measured
                    # data HA reports a stable base colour for effect lamps, so
                    # this is only a safety valve if you ever see an effect scene
                    # flicker in detection.
                    d *= EFFECT_COLOR_WEIGHT
        if not contributed:
            continue
        dists.append(d)
    all_d = dists + off_pens
    if not all_d:
        return None
    mean = sum(all_d) / len(all_d)
    if BLEND_WEIGHT <= 0.0:
        return mean
    # Blend in the single largest per-lamp distance so a pair of scenes that
    # differ in only ONE lamp (which the mean dilutes) still separates. On/off
    # penalties are deliberately excluded from the max: a lone off/unavailable
    # lamp still counts in the mean but can't dominate via the max term.
    max_pool = dists if dists else all_d
    return (1.0 - BLEND_WEIGHT) * mean + BLEND_WEIGHT * max(max_pool)


def _ranked(scenes):
    # Every non-excluded scene scored against the live lights, closest first.
    scored = []
    for sc, lights in scenes.items():
        if _excluded(sc):
            continue
        d = _scene_distance(lights)
        if d is not None:
            scored.append((d, sc))
    scored.sort()
    return scored


def _best(scenes, current, ranked=None):
    if ranked is None:
        ranked = _ranked(scenes)
    if not ranked:
        return None, None
    best_d = ranked[0][0]
    # Scenes within a hair of the best are treated as a tie. If the currently
    # tracked scene is among them, keep it (stability -- don't reshuffle on
    # measurement noise). Otherwise pick the STRICTLY lowest, not the
    # alphabetically first: two genuinely distinct scenes can land ~0.015 apart
    # (e.g. Küche ruhephase 0.148 vs entspannen 0.163), and an alphabetical
    # tie-break would then mislabel the active scene as its neighbour.
    near = [sc for d, sc in ranked if d <= best_d + 0.02]
    if current in near:
        return current, best_d
    return ranked[0][1], best_d


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
        PENDING.pop(room, None)
        return f"{room}:off"
    if room in ar:
        result.pop(room, None)
        PENDING.pop(room, None)
        return f"{room}:dyn"
    current = result.get(room)
    # freshly tapped -> locked, keep
    if current and time.time() < LOCK.get(room, 0):
        PENDING.pop(room, None)
        return f"{room}:{current.split('.')[-1]}:lock"
    ranked = _ranked(scenes)
    best, d = _best(scenes, current, ranked)
    # runner-up, for the log: seeing the second-best and its distance is what
    # tells "clean win" from "two scenes almost tied" at a glance.
    r2 = None
    for cand, sc in ranked:
        if sc != best:
            r2 = f" | 2nd {sc.split('.')[-1]}={round(cand, 2)}"
            break
    r2 = r2 or ""
    # Hold the current scene only while it stays within a small margin of the
    # best candidate. This damps flip-flop between near-identical scenes but,
    # unlike the old absolute threshold, still lets the room switch to a
    # clearly different scene (e.g. Stillen <-> Nachtlicht, ~0.20 apart).
    if current and current in scenes and not _excluded(current):
        dc = _scene_distance(scenes[current])
        if dc is not None and dc <= CLEAR and (d is None or dc <= d + KEEP_MARGIN):
            PENDING.pop(room, None)
            return f"{room}:{current.split('.')[-1]}=KEEP({round(dc, 2)}){r2}"
    short = best.split(".")[-1] if best else "-"
    act = "hold"
    if best is not None and d is not None:
        if d <= ACCEPT:
            if best == current:
                # reaffirming the scene already shown -> apply immediately
                result[room] = best
                PENDING.pop(room, None)
                act = "SET"
            else:
                # a CHANGE: require CONFIRM_COUNT consecutive evaluations agreeing
                # on the same new scene before switching, so one noisy / not-yet-
                # settled reading can't flip the room. In steady state the very
                # next evaluation confirms, so this only suppresses transients.
                cand, cnt = PENDING.get(room, (None, 0))
                cnt = cnt + 1 if cand == best else 1
                if cnt >= CONFIRM_COUNT:
                    result[room] = best
                    PENDING.pop(room, None)
                    act = "SET"
                else:
                    PENDING[room] = (best, cnt)
                    act = f"confirm{cnt}/{CONFIRM_COUNT}->{short}"
        elif d >= CLEAR:
            result.pop(room, None)
            PENDING.pop(room, None)
            act = "CLEAR"
        else:
            PENDING.pop(room, None)
    return f"{room}:{short}={round(d, 2) if d is not None else '-'} {act}{r2}"


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
    if _DB_BROKEN[0]:
        # DB file is corrupt/unreadable -- do NOT write, or we'd overwrite the
        # whole database with just this one freshly-learned scene.
        log.warning(f"Learn skipped for {scene}: fingerprint DB is unreadable; "
                    f"fix {FINGERPRINT_FILE} first.")
        return
    attrs = state.getattr(scene) or {}
    room = attrs.get("group_name")
    if not room:
        return
    # Guard against fast tapping through scenes corrupting the database: each tap
    # starts a learn that waits for the lights to settle before capturing. If a
    # new scene in the SAME room is tapped first, task.unique cancels this
    # still-waiting learn so it never records the next scene's light state under
    # this scene's name. Different rooms keep their own key and run in parallel.
    task.unique(f"scene_learn_{slugify(room)}")
    lights_all = state.names("light")
    group = _group_for(room, lights_all)
    if group not in lights_all:
        return
    members = (state.getattr(group) or {}).get("entity_id") or []
    # Only record once the lights have actually stopped moving (>= settle seconds
    # AND two identical 1 s reads), so a mid-fade / mid-transition state is never
    # baked into the fingerprint.
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
            # preserve whatever the contrast calibration decided (True for the
            # whole lamp, or a list of uncontrolled attributes)
            rec["dontcare"] = p["dontcare"]
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
        # Routine per-change summary -> info, not warning, so it stops flooding
        # HA's error log every few seconds. To watch detection live, set the
        # logger `custom_components.pyscript.file.scene_match` to info.
        log.info(f"Matcher {VERSION} " + " | ".join(dbg))


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
def scene_trace(room=None, seconds=25, scene=None, **kwargs):
    """yaml
name: Trace a room live
description: Log a room's live per-lamp state and the closest scene matches
  once a second for N seconds. Start it, then switch the scene in the Hue app
  to see exactly what the matcher sees, frame by frame -- transient during the
  fade and where it settles. Pass `scene` to also print that scene's stored
  fingerprint next to the live values (to spot a stale calibration).
fields:
  room:
    description: Room name (group_name)
    example: Flur
  seconds:
    description: How long to trace
    example: 25
  scene:
    description: Optional scene entity_id to compare live values against
    example: scene.flur_ruhephase
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
        log.warning(f"TRACE: room '{room}' not in DB")
        return
    group = _group_for(key, state.names("light"))
    members = (state.getattr(group) or {}).get("entity_id") or [] if group else []
    fp = FP[key].get(scene) if scene else None
    if scene and fp:
        stored = []
        for lid, r in fp.items():
            if r.get("off"):
                stored.append(f"{lid.split('.')[-1]}=off")
            else:
                stored.append(f"{lid.split('.')[-1]}=b{r.get('bri')}/ct{r.get('ct')}")
        log.warning(f"TRACE {key} stored[{scene.split('.')[-1]}]: {' '.join(stored)}")
    log.warning(f"TRACE {key}: {int(seconds)}s  group={group}={state.get(group)}")
    i = 0
    while i < int(seconds):
        parts = []
        for lid in members:
            a = state.getattr(lid) or {}
            if state.get(lid) != "on":
                parts.append(f"{lid.split('.')[-1]}=off")
                continue
            ct = a.get("color_temp_kelvin")
            parts.append(f"{lid.split('.')[-1]}=b{a.get('brightness')}/ct{ct}")
        ranked = _ranked(FP[key])
        top = "  ".join(f"{sc.split('.')[-1]}={round(dd, 2)}" for dd, sc in ranked[:3])
        log.warning(f"TRACE {key} t={i:2d}s | {' '.join(parts)} | {top}")
        task.sleep(1.0)
        i += 1
    log.warning(f"TRACE {key}: done")


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


@event_trigger("state_changed", "entity_id.startswith('light.')")
def _on_light_change(**kwargs):
    # Any light changed (a scene was set, dimmed, turned off, ...). We watch
    # ALL lights on purpose: with the native Hue integration a room's group and
    # members are named for the room (light.kuche_2, light.kuche,
    # light.badezimmer, ...) with no shared prefix to filter on, so watching a
    # subset would miss changes and leave detection to the slow background loop
    # (a room frozen on its last scene). Re-evaluate right after the Hue fade
    # settles; task.unique coalesces a whole scene's worth of per-lamp change
    # events into a single run, 1.5 s after the last change.
    task.unique("scene_light_settle")
    task.sleep(SETTLE_AFTER_CHANGE)
    # Then wait until the lights actually HOLD STILL before matching. A Hue lamp
    # snaps its brightness fast but can crawl its colour temperature for several
    # seconds after a scene change; matching mid-drift makes a scene look like a
    # different one (dim + not-yet-warm reads as a night scene). The drift is
    # longest when coming from a far-away colour -- which is exactly why the
    # mismatch is source-dependent. Poll until two 1 s reads are identical (or
    # the cap), so we always match the settled state, whatever we came from.
    prev = None
    waited = 0.0
    while waited < SETTLE_MAX:
        snap = _light_snapshot()
        if snap == prev:
            break
        prev = snap
        task.sleep(1.0)
        waited += 1.0
    scene_match_all()


@time_trigger("startup")
def _loop():
    # task.unique kills any previous instance of this loop. Without it, every
    # pyscript reload left the old `while True` loop running with its OLD code,
    # so several loops (different versions) fought over the tracker and made
    # rooms flip between scenes. One loop only now; a full HA restart clears any
    # already-accumulated old loops from before this guard existed.
    task.unique("matcher_loop")
    log.info(f"Matcher loop started {VERSION}")
    while True:
        try:
            scene_match_all()
        except Exception as e:
            log.error(f"Matcher error: {e}")
        task.sleep(LOOP_SECONDS)
