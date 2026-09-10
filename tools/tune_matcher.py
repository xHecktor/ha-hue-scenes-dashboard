#!/usr/bin/env python3
"""Offline matcher analyser / parameter tuner for the Hue scene detector.

Runs anywhere with Python 3 (stdlib only) -- e.g. on the NUC. It replays the
scene_match.py distance function over a real diagnose report and its fingerprint
database, so matcher parameters can be TUNED on measured behaviour instead of
guessed. It can:

  * evaluate the current parameters (sequential replay + full N x N per-room
    identifiability matrix + separation margins),
  * grid-search the parameters and rank them by fewest real mismatches,
  * write the recommended values to a params.json the matcher loads at startup.

Inputs:
  --report        a diagnose report (scene_diagnose -> diagnose_report.txt)
  --fingerprints  the fingerprint database (fingerprints.json)

Examples:
  python3 tune_matcher.py --report diagnose_report.txt --fingerprints fingerprints.json --eval
  python3 tune_matcher.py --report r.txt --fingerprints fp.json --grid
  python3 tune_matcher.py --report r.txt --fingerprints fp.json --grid --write-params params.json

IMPORTANT: the distance function below MUST mirror pyscript/scene_match.py. When
you change the matcher's scoring, mirror it here (both are marked "keep in sync").
"""
import argparse
import json
import math
import re
import statistics
from collections import defaultdict

# --------------------------------------------------------------------------- #
# Parameters (defaults mirror scene_match.py). A params.json may override them.
# --------------------------------------------------------------------------- #
DEFAULTS = {
    "CT_TERM_CAP": 0.20,
    "BLEND_WEIGHT": 0.20,
    "KEEP_MARGIN": 0.05,
    "ACCEPT": 0.35,
    "CLEAR": 0.60,
    "OFF_PENALTY": 0.50,
    "BRI_TERM_CAP": 0.15,
    "CF_THRESHOLD": 0.10,
}
# Grid search space (only the parameters worth sweeping on detection accuracy).
GRID = {
    "CT_TERM_CAP": [0.10, 0.15, 0.20, 0.25],
    "BLEND_WEIGHT": [0.2, 0.3],
    "KEEP_MARGIN": [0.05, 0.08],
    "BRI_TERM_CAP": [0.15, 0.20],
}
EXCLUDE_SUFFIXES = ("_naturliches_licht",)
# Rooms whose fails are structural (true duplicate scenes / outdoor gradient),
# not fixable by parameters -- reported separately so tuning ignores them.
STRUCTURAL_ROOMS = {"Weihnachtsbeleuchtung"}

_LOCUS = [(0.5267, 0.4133), (0.4909, 0.4173), (0.4578, 0.4102), (0.4369, 0.4041),
          (0.4009, 0.3827), (0.3805, 0.3768), (0.3608, 0.3636), (0.3451, 0.3516),
          (0.3287, 0.3417), (0.3123, 0.3282)]
_WHITE = (0.3127, 0.3290)


def _colorfulness(xy):
    if not xy:
        return 0.0
    best = 1.0
    for i in range(len(_LOCUS) - 1):
        ax, ay = _LOCUS[i]
        bx, by = _LOCUS[i + 1]
        dx, dy = bx - ax, by - ay
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((xy[0] - ax) * dx + (xy[1] - ay) * dy) / L))
        d = math.hypot(xy[0] - (ax + t * dx), xy[1] - (ay + t * dy))
        best = min(best, d)
    return best


def _saturation(xy):
    return min(math.hypot(xy[0] - _WHITE[0], xy[1] - _WHITE[1]) / 0.20, 1.0)


def _excluded(sc):
    return any(sc.endswith(s) for s in EXCLUDE_SUFFIXES)


# --------------------------------------------------------------------------- #
# Faithful distance -- keep in sync with scene_match.py:_scene_distance
# --------------------------------------------------------------------------- #
def scene_distance(fp_lights, live, p):
    dists = []
    off_pens = []
    for lid, fp in fp_lights.items():
        dc = fp.get("dontcare")
        if dc is True:
            continue
        dc = dc if isinstance(dc, list) else []
        a = live.get(lid) or {"on": False, "unavail": True}
        if a.get("unavail"):
            continue
        if a.get("dyn") == "dynamic_palette":
            continue
        is_on = a.get("on", False)
        want_on = not fp.get("off")
        skip_on = "on" in dc
        if not want_on and not is_on:
            continue
        fp_bri = fp.get("bri")
        if fp_bri is None:
            if not skip_on and want_on != is_on:
                off_pens.append(p["OFF_PENALTY"])
            continue
        if not skip_on and want_on != is_on:
            off_pens.append(p["OFF_PENALTY"])
            continue
        if not is_on:
            continue
        cur_bri = a.get("b") or 0
        d = 0.0
        contributed = False
        cfl = _colorfulness(fp["xy"]) if fp.get("xy") else 0.0
        if "bri" not in dc:
            bt = abs(math.sqrt(fp_bri) - math.sqrt(cur_bri)) / 16.0
            if cfl > p["CF_THRESHOLD"] and cur_bri < fp_bri:
                bt = min(bt, p["BRI_TERM_CAP"])
            d += bt
            contributed = True
        base = min(fp_bri, cur_bri) / 255.0
        pc = fp.get("pc")
        if pc is None:
            pc = "ct" if "ct" in fp else ("xy" if "xy" in fp else ("hs" if "hs" in fp else None))
        live_c = {"ct": a.get("ct"), "xy": a.get("xy"), "hs": a.get("hs")}
        axis = None
        for cand in [pc, "ct", "xy", "hs"]:
            if cand and cand in fp and live_c.get(cand):
                axis = cand
                break
        color_off = ("color" in dc) or (axis is not None and axis in dc)
        if not color_off:
            if axis is None:
                if pc is not None:
                    d += math.sqrt(base)
                    contributed = True
            elif axis == "ct":
                d += min(base * (abs(live_c["ct"] - fp["ct"]) / 1200.0), p["CT_TERM_CAP"])
                contributed = True
            elif axis == "xy":
                cw = max(_saturation(fp["xy"]), math.sqrt(base))
                cur = live_c["xy"]
                dx = cur[0] - fp["xy"][0]
                dy = cur[1] - fp["xy"][1]
                d += cw * (((dx * dx + dy * dy) ** 0.5) / 0.25)
                contributed = True
            elif axis == "hs":
                cw = max(min(fp["hs"][1] / 100.0, 1.0), math.sqrt(base))
                cur = live_c["hs"]
                dh = abs(cur[0] - fp["hs"][0])
                dh = min(dh, 360 - dh)
                d += cw * (dh / 180.0 + abs(cur[1] - fp["hs"][1]) / 100.0)
                contributed = True
        if not contributed:
            continue
        dists.append(d)
    all_d = dists + off_pens
    if not all_d:
        return None
    mean = sum(all_d) / len(all_d)
    if p["BLEND_WEIGHT"] <= 0.0:
        return mean
    max_pool = dists if dists else all_d
    return (1.0 - p["BLEND_WEIGHT"]) * mean + p["BLEND_WEIGHT"] * max(max_pool)


def ranked(scenes, live, p):
    scored = []
    for sc, lights in scenes.items():
        if _excluded(sc):
            continue
        d = scene_distance(lights, live, p)
        if d is not None:
            scored.append((d, sc))
    scored.sort()
    return scored


def eval_room(scenes, current, live, p):
    rk = ranked(scenes, live, p)
    if not rk:
        return current
    best_d = rk[0][0]
    near = [sc for d, sc in rk if d <= best_d + 0.02]
    b = current if current in near else rk[0][1]
    if current in scenes and not _excluded(current):
        dcur = scene_distance(scenes[current], live, p)
        if dcur is not None and dcur <= p["CLEAR"] and dcur <= best_d + p["KEEP_MARGIN"]:
            return current
    if best_d <= p["ACCEPT"]:
        return b
    if best_d >= p["CLEAR"]:
        return None
    return current


# --------------------------------------------------------------------------- #
# Diagnose report parser (matches scene_diagnose.py's output format)
# --------------------------------------------------------------------------- #
def _parse_lamp(rest):
    rest = rest.strip()
    if rest == "off":
        return {"on": False}
    d = {"on": True, "ct": None, "xy": None, "hs": None, "dyn": None}
    m = re.match(r"b(\d+)", rest)
    d["b"] = int(m.group(1)) if m else None
    m = re.search(r"\bct(\d+)", rest)
    d["ct"] = int(m.group(1)) if m else None
    m = re.search(r"\bxy([\d.]+),([\d.]+)", rest)
    if m:
        d["xy"] = [float(m.group(1)), float(m.group(2))]
    m = re.search(r"\bhs([\d.]+),([\d.]+)", rest)
    if m:
        d["hs"] = [float(m.group(1)), float(m.group(2))]
    m = re.search(r"dyn=(\S+)", rest)
    d["dyn"] = m.group(1) if m else None
    return d


def parse_report(path):
    rooms = {}
    transitions = []
    cur_room = None
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    n = len(lines)
    for idx, ln in enumerate(lines):
        if ln.startswith("FAIL SUMMARY") or ln.startswith("DIAGNOSE done"):
            n = idx
            break
    i = 0
    while i < n:
        ln = lines[i]
        mroom = re.match(r"\[(.+?)\]\s+group=(\S+)", ln)
        if mroom:
            cur_room = mroom.group(1)
            rooms[cur_room] = mroom.group(2)
            i += 1
            continue
        mhead = re.match(r"  (\S+) -> (\S+)\s*$", ln)
        if mhead and cur_room:
            src, tgt = mhead.group(1), mhead.group(2)
            j = i + 1
            live = {}
            while j < n:
                lj = lines[j]
                if re.match(r"  \S+ -> \S+\s*$", lj) or re.match(r"\[.+?\]\s+group=", lj):
                    break
                if re.match(r"\s+end t\+", lj):
                    k = j + 1
                    while k < n:
                        lk = lines[k]
                        if re.match(r"\s+(settle/lampe|Dfp|ERGEBNIS):", lk):
                            break
                        mlamp = re.match(r"\s+([A-Za-z0-9_]+):\s+(.*)", lk)
                        if mlamp and mlamp.group(1) != "group":
                            live["light." + mlamp.group(1)] = _parse_lamp(mlamp.group(2))
                        k += 1
                    j = k
                    continue
                j += 1
            transitions.append({"room": cur_room, "src": src, "tgt": tgt, "live": live})
            i = j
            continue
        i += 1
    return rooms, transitions


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def _room_scenes(db):
    return {room: {full.split(".")[-1]: lights for full, lights in sc.items()}
            for room, sc in db.items()}


def sequential_fails(db, transitions, p):
    rs = _room_scenes(db)
    by_room = defaultdict(list)
    for t in transitions:
        by_room[t["room"]].append(t)
    real, struct = [], []
    for room, tlist in by_room.items():
        scenes = rs.get(room)
        if not scenes:
            continue
        current = None
        for t in tlist:
            if t["tgt"] not in scenes:
                continue
            if current is None:
                current = t["src"] if t["src"] in scenes else None
            current = eval_room(scenes, current, t["live"], p)
            my = current if current is not None else "-"
            if my != t["tgt"]:
                (struct if room in STRUCTURAL_ROOMS else real).append((room, t["src"], t["tgt"], my))
    return real, struct


def allpairs(db, transitions, p):
    """Full N x N: each scene's settled state vs all room scenes. Returns
    (collisions, thinnest_margins)."""
    rs = _room_scenes(db)
    canon = defaultdict(dict)
    for t in transitions:
        canon[t["room"]].setdefault(t["tgt"], t["live"])
    collisions = []
    margins = []
    for room, scenes in rs.items():
        if room in STRUCTURAL_ROOMS:
            continue
        lb = canon.get(room, {})
        for s in scenes:
            if _excluded("scene." + s) or s not in lb:
                continue
            sd = [(d, t) for d, t in ranked(scenes, lb[s], p)]
            if len(sd) < 2:
                continue
            ds = dict((t, d) for d, t in sd)[s]
            others = [(d, t) for d, t in sd if t != s]
            if sd[0][1] != s:
                collisions.append((room, s, sd[0][1]))
            margins.append((others[0][0] - ds, room, s, others[0][1]))
    margins.sort()
    return collisions, margins


def run_eval(db, transitions, p):
    real, struct = sequential_fails(db, transitions, p)
    coll, margins = allpairs(db, transitions, p)
    print("== Sequential replay ==")
    print(f"  real mismatches:       {len(real)}")
    print(f"  structural (dupes):    {len(struct)}  (delete those scenes in the Hue app)")
    for r in real:
        print(f"     {r[0]}: {r[2]}  (got {r[3]})")
    print("== Full N x N identifiability ==")
    print(f"  self-collisions:       {len(coll)}")
    for c in coll:
        print(f"     {c[0]}: {c[1]} -> {c[2]}")
    print("  thinnest separation margins (physical near-duplicates if <0.05):")
    for m, room, s, other in margins[:6]:
        print(f"     {m:.3f}  {room}: {s} <-> {other}")


def run_grid(db, transitions):
    keys = list(GRID.keys())
    results = []

    def rec(i, cur):
        if i == len(keys):
            p = dict(DEFAULTS)
            p.update(cur)
            real, struct = sequential_fails(db, transitions, p)
            coll, _ = allpairs(db, transitions, p)
            results.append((len(real), len(coll), dict(cur)))
            return
        for v in GRID[keys[i]]:
            cur[keys[i]] = v
            rec(i + 1, cur)

    rec(0, {})
    results.sort(key=lambda x: (x[0], x[1]))
    print("== Grid search (fewest real mismatches, then fewest collisions) ==")
    print(f"{'real':>4} {'coll':>4}  params")
    for real, coll, cur in results[:12]:
        print(f"{real:>4} {coll:>4}  {cur}")
    best = dict(DEFAULTS)
    best.update(results[0][2])
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True, help="diagnose report txt")
    ap.add_argument("--fingerprints", required=True, help="fingerprints.json")
    ap.add_argument("--eval", action="store_true", help="evaluate current params")
    ap.add_argument("--grid", action="store_true", help="grid-search params")
    ap.add_argument("--params", help="load params.json to evaluate instead of defaults")
    ap.add_argument("--write-params", metavar="PATH",
                    help="write the recommended params to this JSON (implies --grid)")
    args = ap.parse_args()

    db = json.load(open(args.fingerprints, encoding="utf-8"))
    rooms, transitions = parse_report(args.report)
    print(f"Loaded {len(db)} rooms, parsed {len(transitions)} transitions\n")

    p = dict(DEFAULTS)
    if args.params:
        p.update(json.load(open(args.params, encoding="utf-8")))

    if args.eval or (not args.grid and not args.write_params):
        run_eval(db, transitions, p)
        print()

    if args.grid or args.write_params:
        best = run_grid(db, transitions)
        print(f"\nRecommended: {best}")
        if args.write_params:
            json.dump(best, open(args.write_params, "w"), indent=2)
            print(f"Wrote {args.write_params} -> copy to /config/hue_scenes/params.json")


if __name__ == "__main__":
    main()
