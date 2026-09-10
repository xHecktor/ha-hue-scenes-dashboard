# Tools

Offline helpers that run on any machine with Python 3 (stdlib only) — e.g. your
NUC. They never touch Home Assistant directly; they read the files the pyscript
services produce and (optionally) write a `params.json` the matcher picks up.

## `tune_matcher.py` — parameter analyser / tuner

Replays `scene_match.py`'s distance function over a real diagnose report and its
fingerprint database, so the matcher's parameters are tuned on measured
behaviour instead of guessed.

```bash
# 1. On the HA box: run a full diagnose, then copy the two files off it:
#      /config/hue_scenes/diagnose_report.txt
#      /config/hue_scenes/fingerprints.json

# 2. Evaluate the current parameters (mismatches + full NxN separation):
python3 tune_matcher.py --report diagnose_report.txt --fingerprints fingerprints.json --eval

# 3. Grid-search and write the recommended values:
python3 tune_matcher.py --report diagnose_report.txt --fingerprints fingerprints.json \
        --write-params params.json

# 4. Copy params.json to /config/hue_scenes/params.json and reload pyscript.
```

`--eval` reports the sequential replay (real vs structural/duplicate mismatches),
the full scene-vs-scene identifiability matrix (self-collisions), and the
thinnest separation margins (pairs below ~0.05 are physical near-duplicates that
no parameter can separate — merge or rename them).

**`params.json`** may contain any of: `CT_TERM_CAP`, `BLEND_WEIGHT`,
`KEEP_MARGIN`, `ACCEPT`, `CLEAR`, `OFF_PENALTY`, `BRI_TERM_CAP`, `CF_THRESHOLD`,
`CONFIRM_COUNT`, `SETTLE_MAX`. The matcher loads it at startup and logs which
values it overrode; anything absent keeps the built-in default, so a partial
file is fine.

> Keep the distance function in `tune_matcher.py` in sync with
> `pyscript/scene_match.py` — both are marked "keep in sync". When you change the
> matcher's scoring, mirror it here or the tuner's numbers drift from reality.
