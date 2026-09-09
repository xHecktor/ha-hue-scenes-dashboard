"""
Scene-detection diagnostics for Home Assistant (pyscript).

An automated, reproducible self-test that drives the lights itself and checks
what the live matcher makes of the result.

Requires pyscript with allow_all_imports: true.
"""

import json
from homeassistant.util import slugify


VERSION = "d7"

# Strong reference to the background task so it isn't garbage-collected
# (and cancelled) the moment the service function returns.
_DIAG_TASK = None

FINGERPRINT_FILE = "/config/scene_fingerprints.json"
REPORT_FILE = "/config/scene_diagnose_report.txt"
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
    """
    Append exactly one line to the report file.

    Returns True when writing succeeded.
    Returns False and logs the error when writing failed.
    """
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
    """
    Create/truncate the report and write the header.

    This is deliberately done before the actual diagnosis starts so
    a write-permission/path problem is detected immediately.
    """
    import datetime

    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with open(path, "w", encoding="utf-8") as f:
            f.write(f"scene_diagnose {VERSION}  {now}\n")
            f.write(
                f"room={room or 'all'}  "
                f"settle={settle}s  "
                f"mode={mode}\n"
            )
            f.write("\n")
            f.flush()

        return True

    except Exception as e:
        log.error(f"DIAGNOSE: cannot initialize report {path}: {e}")
        return False


@pyscript_compile
def _now():
    import datetime

    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _group_for(room, lights_all):
    """
    Find the native Hue group belonging to the room.
    """
    for lid in lights_all:
        a = state.getattr(lid) or {}

        if (
            a.get("is_hue_group")
            and a.get("friendly_name") == room
        ):
            return lid

    return None


def _excluded(sc):
    for suf in EXCLUDE_SUFFIXES:
        if sc.endswith(suf):
            return True

    return False


def _detected(room):
    """
    What the running matcher currently thinks is active in this room.
    """
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


def _live_str(members):
    """
    Live per-lamp brightness/ct values, for showing WHY a transition
    was misread.
    """
    parts = []

    for lid in members:
        a = state.getattr(lid) or {}

        if state.get(lid) != "on":
            parts.append(f"{lid.split('.')[-1]}=off")
        else:
            parts.append(
                f"{lid.split('.')[-1]}="
                f"b{a.get('brightness')}/"
                f"ct{a.get('color_temp_kelvin')}"
            )

    return " ".join(parts)


def _wait_stable_match(
    room,
    members,
    min_wait=3.0,
    max_wait=45.0,
    need=3,
):
    """
    Self-adjusting settle.

    Poll the matcher's verdict once a second until it holds steady
    for `need` reads, or max_wait is reached.

    Returns:
        (detected_scene, seconds_waited)
    """

    last = "<init>"
    stable = 0
    waited = 0.0

    while waited < max_wait:

        try:
            homeassistant.update_entity(
                entity_id=members
            )
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
def scene_diagnose(
    room=None,
    settle=4,
    mode="full",
    **kwargs,
):
    """
    yaml
    name: Diagnose scene detection (multi-path)
    description: Drive every static scene from other scenes and check the live
      matcher identifies each one, waiting a self-adjusting time until the
      verdict is stable. In 'full' mode every scene is approached from EVERY
      other scene; 'quick' uses just the coolest and dimmest sources.
      Logs source -> target, seconds to stabilise and PASS/FAIL for every run
      with the live lamp values on a FAIL, plus a summary, and writes the
      full report to /config/scene_diagnose_report.txt.
      The lights flash through the scenes. Read-only -- never changes the
      database.
    fields:
      room:
        description: Only this room (group_name). Empty = whole home.
        example: Flur
      settle:
        description: Seconds to hold each SOURCE scene before switching to the target.
        example: 4
      mode:
        description: "full = every scene from every other (thorough, slow); quick = only coolest + dimmest sources"
        example: full
    """

    # Run in the background so the Actions UI call returns at once.
    # A full matrix over many scenes takes minutes and would otherwise
    # hit the service-call timeout.
    global _DIAG_TASK

    _DIAG_TASK = task.create(
        _diagnose_run,
        room,
        settle,
        mode,
    )

    log.warning(
        f"DIAGNOSE {VERSION}: spawned background run "
        f"(room={room or 'all'}, mode={mode}) "
        f"-> report to {REPORT_FILE}"
    )


def _diagnose_run(
    room=None,
    settle=4,
    mode="full",
):
    task.unique("scene_diagnose")

    log.warning(
        f"DIAGNOSE {VERSION}: worker started "
        f"(room={room or 'all'}, "
        f"settle={settle}, "
        f"mode={mode})"
    )

    # ------------------------------------------------------------
    # Initialize and TEST report file BEFORE doing any real work.
    # ------------------------------------------------------------

    if not _init_report(
        REPORT_FILE,
        room,
        settle,
        mode,
    ):
        log.error(
            "DIAGNOSE: stopping because the report file "
            "could not be initialized"
        )
        return

    log.warning(
        f"DIAGNOSE: report file writable: {REPORT_FILE}"
    )

    # ------------------------------------------------------------
    # Load fingerprint database.
    # ------------------------------------------------------------

    try:
        db = task.executor(
            _read_json,
            FINGERPRINT_FILE,
        )
    except Exception as e:
        log.error(
            f"DIAGNOSE: could not read fingerprint file: {e}"
        )

        _write_report_line(
            REPORT_FILE,
            f"ERROR: could not read fingerprint file: {e}",
        )

        return

    if not db:
        log.error(
            "DIAGNOSE: fingerprint database is empty "
            f"or could not be read: {FINGERPRINT_FILE}"
        )

        _write_report_line(
            REPORT_FILE,
            "ERROR: fingerprint database is empty "
            "or could not be read.",
        )

        return

    lights_all = state.names("light")

    n_ok = 0
    n_fail = 0

    fails = []

    slowest = 0.0
    slowest_what = "-"

    # ------------------------------------------------------------
    # Process every room.
    # ------------------------------------------------------------

    for rname, scenes in db.items():

        if room and slugify(rname) != slugify(room):
            continue

        group = _group_for(
            rname,
            lights_all,
        )

        if not group:
            log.warning(
                f"DIAGNOSE {rname}: skipped (no Hue group)"
            )

            _write_report_line(
                REPORT_FILE,
                f"[{rname}] SKIPPED - no Hue group",
            )

            continue

        members = (
            state.getattr(group) or {}
        ).get("entity_id") or []

        # --------------------------------------------------------
        # Build scene list.
        # --------------------------------------------------------

        names = []

        for s in scenes:
            if not _excluded(s):
                names.append(s)

        if len(names) < 2:
            log.warning(
                f"DIAGNOSE {rname}: skipped "
                f"(less than 2 scenes)"
            )

            _write_report_line(
                REPORT_FILE,
                f"[{rname}] SKIPPED - less than 2 scenes",
            )

            continue

        # --------------------------------------------------------
        # Determine source scenes.
        #
        # full:
        #   every scene is tested from every other scene.
        #
        # quick:
        #   only coolest and dimmest scenes are used.
        # --------------------------------------------------------

        if mode == "quick":

            coolest = None
            dimmest = None

            best_ct = -1.0
            low_bri = 1e9

            for s in names:

                mb, mc = _mean_bri_ct(
                    scenes[s]
                )

                if mc > best_ct:
                    best_ct = mc
                    coolest = s

                if mb < low_bri:
                    low_bri = mb
                    dimmest = s

            sources = []

            for s in (
                coolest,
                dimmest,
            ):
                if s and s not in sources:
                    sources.append(s)

        else:
            sources = names

        transitions = (
            len(names) *
            (len(sources) - 1)
        )

        log.warning(
            f"DIAGNOSE {rname}: "
            f"{len(names)} scenes, "
            f"mode={mode}"
        )

        _write_report_line(
            REPORT_FILE,
            f"[{rname}]  "
            f"{len(names)} scenes  "
            f"mode={mode}  "
            f"{transitions} transitions",
        )

        # --------------------------------------------------------
        # Test every target from every source.
        # --------------------------------------------------------

        for target in names:

            tshort = target.split(".")[-1]

            for src in sources:

                if src == target:
                    continue

                sshort = src.split(".")[-1]

                try:

                    # --------------------------------------------
                    # Set source scene.
                    # --------------------------------------------

                    scene.turn_on(
                        entity_id=src
                    )

                    task.sleep(
                        float(settle)
                    )

                    # --------------------------------------------
                    # Set target scene.
                    # --------------------------------------------

                    scene.turn_on(
                        entity_id=target
                    )

                    # --------------------------------------------
                    # Wait for matcher to stabilize.
                    # --------------------------------------------

                    det, elapsed = _wait_stable_match(
                        rname,
                        members,
                    )

                    dshort = (
                        (det or "-")
                        .split(".")[-1]
                    )

                    # --------------------------------------------
                    # Track slowest transition.
                    # --------------------------------------------

                    if elapsed > slowest:
                        slowest = elapsed

                        slowest_what = (
                            f"{rname}: "
                            f"{sshort} -> "
                            f"{tshort}"
                        )

                    # --------------------------------------------
                    # PASS
                    # --------------------------------------------

                    if det == target:

                        n_ok += 1

                        log.warning(
                            f"DIAGNOSE OK {rname}: "
                            f"{sshort} -> {tshort} "
                            f"(stable in "
                            f"{elapsed:.0f}s)"
                        )

                        _write_report_line(
                            REPORT_FILE,
                            f"  OK   "
                            f"{sshort:28} -> "
                            f"{tshort:28} "
                            f"{elapsed:.0f}s",
                        )

                    # --------------------------------------------
                    # FAIL
                    # --------------------------------------------

                    else:

                        n_fail += 1

                        live = _live_str(
                            members
                        )

                        fail_text = (
                            f"{rname}: "
                            f"{sshort} -> "
                            f"{tshort} = "
                            f"{dshort} "
                            f"({elapsed:.0f}s)"
                        )

                        fails.append(
                            fail_text
                        )

                        log.warning(
                            f"DIAGNOSE FAIL {rname}: "
                            f"{sshort} -> {tshort} "
                            f"detected {dshort} "
                            f"({elapsed:.0f}s) "
                            f"live: {live}"
                        )

                        _write_report_line(
                            REPORT_FILE,
                            f"  FAIL "
                            f"{sshort:28} -> "
                            f"{tshort:28} "
                            f"erkannt: {dshort}  "
                            f"{elapsed:.0f}s",
                        )

                        _write_report_line(
                            REPORT_FILE,
                            f"       live: {live}",
                        )

                except Exception as e:

                    error_text = (
                        f"{rname}: "
                        f"{sshort} -> "
                        f"{tshort}: {e}"
                    )

                    log.error(
                        f"DIAGNOSE error "
                        f"{rname} "
                        f"{sshort}->{tshort}: "
                        f"{e}"
                    )

                    _write_report_line(
                        REPORT_FILE,
                        f"  ERR  "
                        f"{sshort} -> "
                        f"{tshort}: {e}",
                    )

    # ------------------------------------------------------------
    # Final summary.
    # ------------------------------------------------------------

    summary = (
        f"DIAGNOSE done: "
        f"{n_ok} OK, "
        f"{n_fail} FAIL | "
        f"slowest stabilise "
        f"{slowest:.0f}s "
        f"({slowest_what})"
    )

    log.warning(summary)

    _write_report_line(
        REPORT_FILE,
        "",
    )

    _write_report_line(
        REPORT_FILE,
        summary,
    )

    # ------------------------------------------------------------
    # Write FAIL summary without generator expressions.
    # ------------------------------------------------------------

    if fails:

        _write_report_line(
            REPORT_FILE,
            "",
        )

        _write_report_line(
            REPORT_FILE,
            "FAIL SUMMARY:",
        )

        for f in fails:
            _write_report_line(
                REPORT_FILE,
                f"  FAIL {f}",
            )

    log.warning(
        f"DIAGNOSE report written to {REPORT_FILE}"
    )
