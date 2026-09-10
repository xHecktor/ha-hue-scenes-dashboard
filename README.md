# Hue Scenes for Home Assistant

Control Philips **Hue dynamic scenes** from a Lovelace dashboard, adjust their
speed, and — the hard part — **know which scene is currently active** in each
room, even when it was started from the Hue app or a wall switch.

Home Assistant does not tell you which Hue scene is active. This project fills
that gap by *fingerprinting* each scene's light state and matching the live
lights against those fingerprints. The result is written to a
`pyscript.scene_tracker` entity and used to highlight the active scene on the
dashboard.

> Status: working config bundle (not a one-click integration). It grew out of a
> personal setup, so expect to adapt a few things to yours — see
> [Adapting to your setup](#adapting-to-your-setup).

## Screenshots

| Active scene highlighted | Dynamic scene + speed slider |
|---|---|
| ![Static scene highlighted in orange](images/active-scene-highlight.png) | ![Dynamic scene highlighted in red with a speed slider](images/dynamic-scene-speed.png) |

The running scene is marked automatically — **orange** for a static scene,
**red** for a dynamic one — even when it was started from the Hue app. The
speed slider only appears while a dynamic scene is playing, and the wand icon
marks scenes that support dynamic mode.

## Features

- **Scene tiles per room** in three brightness tiers, with a curated top row of
  the standard scenes.
- **Active-scene highlight** — the running scene is marked (orange = static,
  red = dynamic), including scenes started outside Home Assistant. Passive
  displays pull the active scene to the front of its row; the device you are
  actually tapping on keeps the normal order and its scroll position, so
  stepping through scenes doesn't reshuffle or jump back under your finger
  (per-device via a `localStorage` id; the highlight itself is a reactive
  card-mod template, so tapping never rebuilds the row — no extra integration
  needed).
- **Dynamic scenes** — double-tap a tile to start it dynamically; a speed slider
  appears only while dynamic is running and is preset to the scene's own speed.
- **Self-maintaining database** — every time you tap a scene its fingerprint is
  re-learned, so edits and new scenes are picked up automatically.
- **Remote-friendly** — a `scene_cycle` script steps through scenes in dashboard
  order for a physical button.
- **Global template** — one dashboard definition, one line (`room:`) per room.

## How it works

1. `scene_fingerprint_calibrate` (pyscript) activates each room scene once and
   records the resulting per-light state (`brightness` + `color_temp` or `xy`,
   **including which member lights stay off**) into
   `/config/hue_scenes/fingerprints.json`. That off/on subset is what tells apart
   scenes lighting up different parts of a group (e.g. *gedimmt Diele* vs
   *gedimmt kleiner Flur*).
2. `scene_match.py` runs a small loop that compares the live lights of each room
   against the fingerprints and writes the best match to
   `pyscript.scene_tracker` (attribute `active_scene`, a `{room: scene}` map).
3. The dashboard reads that map (plus `input_text.dynamic_scenes` and
   `sensor.dynamische_szenen`) to colour the tiles.

## Requirements

- Home Assistant with the official **Philips Hue** integration.
- **pyscript** (HACS) with `allow_all_imports: true`.
- HACS frontend cards: **config-template-card**, **swipe-card**, **card-mod**,
  **decluttering-card**.
- No manual light groups needed: the code uses the **native Hue group light**
  the integration already creates for every room and zone (`is_hue_group: true`,
  `friendly_name` = the room name, `entity_id` = the members). Nothing to name
  or create.

## Install

1. **pyscript** — in `configuration.yaml`:
   ```yaml
   pyscript:
     allow_all_imports: true
     hass_is_global: true
   ```
   Copy `pyscript/scene_fingerprints.py`, `pyscript/scene_match.py` and
   (optional) `pyscript/scene_diagnose.py` to `/config/pyscript/`.
2. **Backend** — copy `packages/hue_scenes.yaml` to `/config/packages/`
   (enable packages) or merge its blocks into your config.
3. **Frontend** — put the `decluttering_templates:` block from
   `lovelace/decluttering_template.yaml` at the top level of your dashboard's
   raw config, then add one card per room (see `lovelace/example_room.yaml`).
4. Restart Home Assistant.

## Calibrate

Developer Tools → Actions:

```yaml
action: pyscript.scene_fingerprint_calibrate      # whole home
# or a single room:
action: pyscript.scene_fingerprint_calibrate
data:
  room: Wohnzimmer
```

The lights flash briefly through every scene. After that the tracker fills
within a few seconds and stays fresh on its own (learn-on-tap).

**Contrast mode (thorough, default on).** If a scene shares a group with a lamp
it does *not* control (that lamp then holds whatever a previous scene left,
polluting the fingerprint), contrast calibration detects it. It runs **by
default**; pass `contrast: false` only for a quick re-capture of scenes you know
drive every lamp:

```yaml
action: pyscript.scene_fingerprint_calibrate
data:
  room: Badezimmer      # contrast: true is the default
```

Each scene is run **twice from opposite starting states** (warm+bright vs
cool+dim) and the two results are compared **per attribute**. Any attribute
that ends up different is one the scene doesn't control, and it is stored in
the fingerprint as `dontcare` (a list, e.g. `["ct"]`) so the matcher ignores
*only that attribute* for that lamp — e.g. a scene that sets a lamp's
brightness but leaves its colour temperature free is still matched on
brightness. A lamp whose on/off itself is free becomes a full wildcard
(`dontcare: true`). The flag survives learn-on-tap.

Priming is done through the group light (one command) and every Hue command
is **rate-limited**, so whole-home runs can calibrate several rooms at once
without flooding the bridge:

```yaml
action: pyscript.scene_fingerprint_calibrate
data:
  contrast: true
  parallel: 3        # rooms in parallel (whole-home); 1 = sequential
```

A readable log is written to **`/config/hue_scenes/calibrate_log.txt`**, flushed
line by line (crash-safe, shows which attributes were marked don't-care per
lamp). The whole run is a background task, so the action returns immediately.

For a dashboard-friendly version, add `lovelace/calibration_card.yaml` — a
room picker (or "Alle Räume") with a **Kalibrieren** button and a live status
line fed by the `pyscript.calibration` entity.

## Diagnose (optional)

`pyscript/scene_diagnose.py` adds a `pyscript.scene_diagnose` action that
verifies detection automatically instead of clicking through scenes by hand:

```yaml
action: pyscript.scene_diagnose
data:
  room: Flur          # or empty for the whole home
```

It drives every static scene **from two contrasting source scenes** (the
coolest and the dimmest in the room — the transitions that expose a lamp
reporting its colour temperature with a lag), waits a **self-adjusting** time
until the live matcher's verdict is stable, and logs each run as
`source -> target`, the detected scene, the seconds it took to stabilise and
PASS/FAIL, ending with a summary and the slowest stabilisation seen (a good
basis for the matcher's settle). It only reads — it never changes the database.

Because the Home Assistant log UI collapses repeated lines and hides most
runs, the **full report is also written to `/config/hue_scenes/diagnose_report.txt`**
(overwritten each run) — open that for the complete per-transition list.

### Dim drift (deep-dimming a colour scene)

`pyscript.scene_dim_drift` measures how well detection survives when you turn a
**colourful** scene far down. It auto-selects the most colourful scenes per room,
activates each, then steps every lamp through decreasing brightness levels
(100 → 50 → 25 → 12 → 6 %), recording per lamp how the reported colour drifts and
at which level the live matcher stops recognising the scene:

```yaml
action: pyscript.scene_dim_drift
data:
  room: Wohnzimmer     # empty = whole home
  # scenes: wohnzimmer_rio,wohnzimmer_blue_planet   # optional: force a set
```

The report is written to **`/config/hue_scenes/dim_drift_report.txt`**. It is
read-only and restores nothing (the next activation resets the lights).

> All persistent data lives under **`/config/hue_scenes/`** (`fingerprints.json`,
> `calibrate_log.txt`, `diagnose_report.txt`, `dim_drift_report.txt`). The folder
> is created automatically; an existing `/config/scene_fingerprints.json` from an
> older install is read as a fallback until the next calibration migrates it.

## Per-room setup

For each room:

- one card: `- room: <group_name>` (see `example_room.yaml`)
- one speed helper line: `<slug>_dynamic_speed: *dyn_speed`
- one speed-automation trigger line: `- input_number.<slug>_dynamic_speed`

List the exact room names with:

```jinja
{{ states.scene | map(attribute='attributes.group_name') | reject('none') | unique | list }}
```

## Adapting to your setup

- **Room light group** — the code finds a room's lights on the native Hue
  group light (matched by `is_hue_group` + `friendly_name`), so there is
  nothing to name or create. This bundle targets the Philips Hue integration;
  a room without a native Hue group light is simply skipped.
- **Standard scene names** — block 1 recognises the standard scenes by name
  (`Hell`, `Kühl hell`, …). Adjust the `NAMED` list in the template for your
  language.
- **Aggregation (`BLEND_WEIGHT`)** — a scene's distance is the *mean* of its
  per-lamp distances (robust: no single lamp dominates) blended with the single
  largest per-lamp distance: `(1-w)·mean + w·max`. This matters when two scenes
  differ in only **one** lamp of a big group and the rest are identical (a
  shared always-on strip, say), which dilutes the mean — the lone differing
  lamp still lifts them apart. Default `0.3` (a measured compromise); `0` is
  pure mean, and higher values separate tight pairs more but make a single
  noisy lamp count for more. Blend only ever raises cross-scene distances, so a
  scene always still matches itself at ~0.
- **Entity names** are German (`sensor.dynamische_szenen`, …) to match the
  bundle; rename freely, but keep them consistent across pyscript, package and
  dashboard.

## Known limitations

Fingerprint matching is inherently approximate:

- **Identical scenes** (same colours/brightness) can't be told apart.
- **Very dim scenes** are matched mainly by brightness and on/off, because Hue
  reports colour unreliably at low brightness — but only for **near-white**
  tints. A **saturated** colour (a lamp well off the white/colour-temperature
  locus) keeps full colour weight even when dim, and if you turn such a scene
  *below* its captured brightness the brightness penalty is capped, so a deeply
  dimmed colour scene is still recognised by its colour (down to ~single-digit
  %) instead of collapsing into a warm night-light. Two equally dim **warm**
  scenes that differ only in tint may still be treated as the same. Brightness
  is compared on a square-root scale, sensitive both at the dim end (7 vs 23)
  and in the middle (90 vs 143), so warm scenes that differ only in brightness
  (e.g. *Entspannen* vs *Ruhephase*) are still told apart.
- **A lamp switched off / unavailable** (power cut at the wall, zigbee dropout)
  is treated as a wildcard, and an on/off mismatch is a bounded penalty kept out
  of the worst-lamp term — one missing lamp can't drag the room's match off what
  the remaining lamps' colours clearly say.
- **Subset scenes** (a scene that only touches one lamp of a bigger group)
  need calibration to record the other lamps as *off* — otherwise the scene
  matches any state where its one lamp happens to look right. Re-run
  `scene_fingerprint_calibrate` for the room after adding such a scene.
- **Uncontrolled lamps** — a scene that leaves a group member untouched bakes
  in whatever a previous scene left on it, which then fights the match. Run a
  `contrast: true` calibration (see [Calibrate](#calibrate)) to detect and
  flag those lamps as `dontcare` so they are ignored.
- **Gradient / multi-colour lights** report a single averaged colour, so scenes
  relying on them match less reliably.
- **Dynamic scenes** cycle their colours; they are detected as "dynamic running"
  via the light attribute, not fingerprinted.
- **Shared lights** that belong to two rooms are fine: a member currently
  running a dynamic palette (e.g. a light playing a dynamic scene for another
  room) is treated as a wildcard, so it can't drag a room's static match off.
- **Adaptive scenes** like *Natural Light* drift over the day and are excluded
  from matching (`EXCLUDE_SUFFIXES`).

## License

MIT — see [LICENSE](LICENSE).
