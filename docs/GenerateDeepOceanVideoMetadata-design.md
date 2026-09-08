# Generate Deep Ocean Video Metadata — Requirements & Design Reference

Compiled 2026-08-31 from the full design conversation for this tool, before
a final test pass. Tool lives in [OceanVideoToolsForArcGISPro.pyt](../toolbox/OceanVideoToolsForArcGISPro.pyt)
(class `GenerateDeepOceanVideoMetadata`), core logic in
[FCT_DeepOceanVideoMetadata_core.py](../toolbox/FCT_DeepOceanVideoMetadata_core.py).

## 1. Purpose

Generate **synthetic/estimated video metadata** for deep ocean video collected
from a **moving** underwater platform (ROV, AUV, towed sled, drop camera,
diver), when the real telemetry log is incomplete — filling in the FMV
(Full Motion Video) fields a multiplexer needs but a raw nav log usually
lacks (camera pitch/roll/heading, field of view, near/far distance, height).

This is **not** a Frame/Camera Table producer for the toolbox's mosaic/OID
tools (that's a separate, unrelated concern, "handled elsewhere"). Its sole
job is to get a user from "partial navigation log" to "a metadata file the
Video Multiplexer can consume" — enabling or improving multiplexing that
would otherwise be impossible or low-quality from an incomplete source
dataset.

## 2. Design lineage: two Esri tools combined

The user supplied screenshots of two Esri Image Analyst tool dialogs and
asked how their parameter-exposure patterns transferred to this tool.

### 2a. Generate Video Metadata (Stationary) — the "fill in what's missing" half

Screenshot layout: **Input Video** (top-level) → **Sensor Information**
(collapsible: Sensor Location, Height + unit + interpretation dropdown,
Tilt, Relative Azimuth, Horizontal/Vertical FOV, Far Distance checkbox) →
**Dynamic Preview** toggle → **Time** (collapsible: Start Time) → **Output
Files** (collapsible: Output Metadata File, Output Video File).

What transferred:
- The **collapsible category grouping** pattern itself: this tool's
  `category=` groupings ("Sensor Information", "Camera Model", "Output")
  directly mirror that visual structure.
- **Z Value Type** stands in for Height/"Height Above Terrain" — but ours
  varies *per telemetry row* instead of being one fixed manually-entered
  value, since the platform moves.
- **Tilt/Relative Azimuth/HFOV/VFOV/Far Distance** parameters were the
  direct model for the Video Acquisition Profile's
  camera_pitch/heading_constant/hfov/vfov/far_distance fields.
- What did **NOT** transfer, deliberately:
  - **Sensor Location** (a feature-layer picker) — not needed. X/Y always
    come from the telemetry CSV itself, never a separate location input.
  - **Dynamic Preview** — not applicable. This tool never touches a video
    file, so there is nothing to preview live.

### 2b. Convert Video Metadata — the "field-match to the right format" half

Screenshot layout: **Input Metadata File**, **Output Metadata File**,
**Output Metadata Mapping File** (all required), then a **Field Matching**
value table with rows of **Target Fields** (dropdown: Precision Time Stamp,
Sensor Longitude, Sensor Latitude, Sensor Ellipsoid Height Extended, Sensor
True Altitude, Platform Roll/Pitch Angle (Full), Platform Heading Angle,
Sensor Relative Roll/Elevation/Azimuth Angle, Sensor Horizontal/Vertical
Field of View), **Source Fields**, **Source Unit**.

What transferred:
- The **Telemetry Field Mapping** value table (Target Field / Source Field
  columns) is directly modeled on this Field Matching table — same
  Target-Field-dropdown + Source-Field-pick-list shape, applied to mapping
  a *raw telemetry log's* columns instead of an already-video-shaped
  metadata file.
- The **exact MISB target field names** from that dropdown became this
  tool's own output column names verbatim (`Sensor Longitude`, `Precision
  Time Stamp`, `Platform Heading Angle`, `Sensor Relative Elevation Angle`,
  etc.) — see §4 below for why.
- The **Platform vs. Sensor-Relative** distinction visible in that dropdown
  (Platform Roll/Pitch/Heading Angle vs. Sensor Relative
  Azimuth/Elevation/Roll Angle) directly drove a real semantic fix in this
  tool: Platform attitude (telemetry-sourced) and the camera gimbal's
  angle relative to the platform (profile-sourced) are independent fields,
  not one overriding the other.
- What did **NOT** transfer as a literal UI element: the **Source Unit**
  column. A `GPValueTable` column applies the same pick list to *every
  row* regardless of that row's other column values — there's no
  per-row-conditional filtering. A shared Feet/Meters dropdown next to
  every Target Field (including ones where it means nothing, like
  Timestamp) was confusing. Fixed by pulling "Z Source Unit" out into its
  own standalone parameter, explicitly scoped to "applies only if Z is
  mapped above."

### 2c. Net effect

This tool is a genuine **fusion** of both: Generate Video Metadata
(Stationary)'s "fill in what's missing" logic + Convert Video Metadata's
"field-match to the right format" logic — for a moving platform instead of
one fixed sensor, working from a telemetry log instead of a video file.

## 3. Chronological requirements log

1. **Original bug report**: field auto-detection failed on X because the
   real-world CSV used non-standard column names (`E_ROV_INS`/`N_ROV_INS`).
   → Added `Field Name Override` UX, then later a full field-mapping value
   table; added those exact aliases to `CANDIDATE_X_FIELDS`/`CANDIDATE_Y_FIELDS`.
2. **"See which fields are available, not just a tooltip"** — field
   override params should show a pick list of the loaded CSV's real header
   fields, and the profile's auto-applied values should be visible in the
   GUI, not just documented.
3. **"Remove the ability to override X and Y"** — X/Y are the minimally
   required position fields; must always be auto-detected from the source
   CSV, never redirected via a manual override (unlike Z/timestamp/etc.).
4. Several live-run bug fixes along the way (see repo memory
   `/memories/repo/DOOS_toolbox.md` for full technical detail): displayName
   is not dynamically settable on `arcpy.Parameter`; `.value` for
   `GPCoordinateSystem` can come back as an unusable internal wrapper
   object (fixed by using `.valueAsText` instead); non-ISO US-format
   timestamps needed a fallback parser; `project_point()` needed a clear
   error instead of a bare `AttributeError` when coordinates are out of
   range for the declared input CRS.
5. **"Remove the frame-generation aspect; output should be a table"** —
   this tool doesn't extract/name video frames (that's a different tool's
   job); dropped the synthetic `filename_pattern` fallback; merged the
   Frame+Camera Table pair into one output table.
6. **Design review requested before further changes**: confirmed the
   tool's purpose against Esri's Generate Video Metadata (Stationary) +
   Convert Video Metadata as direct inspiration (see §2).
7. **Required-field flip**: X, Y, and **Timestamp** are the true minimum
   (matches the downstream multiplexer's need to time-align rows to video
   frames); **Z became optional** (useful for footprint/altitude
   calculations, but not essential to place/time-order a row).
8. **Selecting a Video Acquisition Profile should populate the override
   fields**: switching profiles pre-fills the 8 override fields with that
   profile's actual default values (last-selection-wins; switching again
   always overwrites, including manual edits since the previous switch).
9. **"Combination of Convert Video Metadata + Stationary Video Metadata,
   generating rough/synthetic FMV metadata missing from the source dataset
   to enable/improve multiplexing"** — the definitive statement of this
   tool's purpose (§1 above), which drove the MISB-native schema (§4).
10. **"Default to using Convert Video Metadata; simply require Image
    Analyst"** — rather than reimplementing Convert Video Metadata's own
    field-matching/unit-conversion/MISB-format logic, delegate the final
    pass directly to Esri's own tool. This tool's `isLicensed()` now
    requires the Image Analyst extension; `execute()` checks it out/in.
11. **GUI should combine Convert Video Metadata's + Static Sensor's
    parameter-input/UX patterns** (§2) — Telemetry Field Mapping value
    table + Sensor-Information-style category grouping.
12. Bug fixes discovered during first real-CSV testing: `_parse_field_mapping()`
    needed to never raise (value tables mid-edit can throw from arcpy's own
    validation); `GPValueTable.getRow()` only quotes a cell when it
    *contains a space* — a bare single-word cell (e.g. `Timestamp`,
    `DateTimeTest`) is left completely unquoted, so a naive
    quoted-substrings-only regex silently dropped every such mapping.
    Fixed by parsing with `shlex.split()` instead.

## 4. Why the output schema uses literal MISB field names

Since this tool fully controls its own output (generating fresh data, not
reformatting an arbitrary pre-existing file), `VIDEO_METADATA_TABLE_FIELDS`
uses Convert Video Metadata's own Target Field names **verbatim** (`Sensor
Longitude`, `Precision Time Stamp`, `Platform Heading Angle`, `Sensor
Relative Elevation Angle`, etc.). Convert Video Metadata's own doc states:
"Only fields with an unrecognized name or incorrect format need to be
matched. All correct data will be copied to the new metadata file." — so
no Input Field Matching table is needed for that pass; an identity mapping
needs no explicit rows.

Non-MISB extras this tool also carries (not renamed, since they aren't
part of Convert Video Metadata's target set): `CameraID`, `CameraNCols`,
`CameraNRows`, `CameraFocalLength`, `CameraPixelSize`, `Near Distance`,
`Camera Height Above Seafloor`, `PerspectiveX/Y/Z`, `SRS`, `AcquisitionDate`
(a human-readable QA copy alongside the required integer-microseconds
`Precision Time Stamp`).

## 5. Known limitations / open items (not yet addressed)

- **Z sign/reference-frame caveat**: a log's "altitude above seafloor" is
  written into MISB's `Sensor True Altitude` field as-is, but these are
  **not the same physical quantity** (True Altitude is height above mean sea
  level/ellipsoid; there is no bathymetry/seafloor-depth reference in this
  tool to convert between the two). A submerged camera's true altitude is
  negative, so a log recording positive heights above the seafloor places
  the sensor above the waterline as far as the multiplexer is concerned —
  which is one reason a video footprint can fail to appear. Treat the field
  as an approximation, and check the sign of the source column.
  `Sensor Ellipsoid Height Extended` has the same problem and is only
  written when the operator explicitly says the Z is an ellipsoid height, or
  maps a column to it.
- **Sensor Relative Azimuth Angle** is hardcoded to `0.0` — no profile in
  this project's library models an off-axis camera gimbal mount.
- **Camera pitch is stored from nadir and converted on the way out.**
  Profiles keep `camera_pitch` as degrees from straight down, because that is
  how a rig is described; ArcGIS reads `Sensor Relative Elevation Angle` as
  tilt from the horizontal plane with negative pointing down, so the written
  value is `camera_pitch - 90`. The two conventions are easy to conflate and
  nothing downstream complains when they are, so anything reading or writing
  that field needs to be explicit about which one it means.
- The `GPValueTable` parsing approach (`shlex.split()` on `getRow()`'s
  output) and `.filters[i]` per-column indexing are grounded in documented
  ArcGIS patterns but **not yet confirmed against a live ArcGIS Pro
  session**.

## 6. Best-practices review (against Esri's official guidance)

Reviewed against: *A quick tour of creating tools in Python*, *Comparing
custom and Python toolboxes*, *Understanding script tool parameters*,
*Controlling the progress dialog box*, *Customizing script tool behavior*,
*Best practices for internationalization of script tools*, and two ArcGIS
blog posts on building custom tools.

**Already compliant:**
- **Python toolbox choice is correct**: Esri's own comparison doc notes "If
  you use or are planning to use significant validation code in a script
  tool, the experience is more straightforward in a Python toolbox" — this
  tool has substantial `updateParameters`/`updateMessages` logic, matching
  that guidance directly.
- **i18n**: numeric parameters (`camera_pitch_override`, `resample_interval`,
  `camera_ncols`, etc.) are read via `.value` (native float/int types), not
  parsed from `.valueAsText` strings — avoids locale decimal-separator bugs.
  `GPCoordinateSystem` params are the one deliberate, documented exception
  (`.valueAsText` used instead of `.value`, because `.value` can return an
  unusable internal wrapper depending on invocation path — see repo memory
  for the full diagnosis) — this is a justified deviation, not an
  oversight.
- `arcpy.SpatialReference()` is always constructed from a factory
  code/WKT string, never a locale-dependent display name — matches the
  i18n doc's explicit recommendation.
- `isLicensed()`/`updateParameters()`/`updateMessages()` structure mirrors
  the documented `ToolValidator` class shape (`initializeParameters`/
  `isLicensed`/`updateParameters`/`updateMessages`) exactly, just expressed
  as Python toolbox methods instead of a separate validation script.
- Parameter name-based lookup (`params_by_name = {p.name: p for p in
  parameters}`) throughout avoids the documented "parameter order must
  match" pitfall entirely — reordering/inserting parameters can't silently
  break index-based access.

**Gap found and fixed this pass:**
- *Controlling the progress dialog box* warns that incrementing/updating
  progress on every iteration of a potentially-large loop is a real
  performance/verbosity concern, and recommends a computed increment (e.g.
  base-10 log of the total count) instead of a per-row update. The main
  per-record loop in `build_video_metadata_table()` had **no progress
  feedback at all** for a large telemetry log (the real test CSV has
  81,053 rows) beyond one static "default" progressor set before the call.
  Fixed: periodic `log()` progress messages at a computed increment
  (mirrors Esri's own sample code's log10-based increment heuristic),
  without spamming a message per row.

**Noted, accepted tradeoff (not changed):**
- *Customizing script tool behavior* warns against "opening of datasets"
  in validation code. `_read_telemetry_header()` does open the telemetry
  CSV during `updateParameters`/`updateMessages` (to populate the field
  mapping's pick list and validate column names) — but only reads the
  header row (not the whole file) and is cached by path+mtime so it only
  re-reads when the file actually changes, not on every keystroke. Given
  the direct value this provides (turning blind text entry into a real
  pick list, catching typos before Run), this is a deliberate, bounded
  exception to that guidance rather than an oversight.
