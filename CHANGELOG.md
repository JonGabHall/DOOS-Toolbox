# Changelog

## Unreleased

Everything here concerns *Generate Deep Ocean Video Metadata* unless stated otherwise.

### Added

- **Three new controls under Key Sensor Information.** A **Z / Depth / Altitude Field**
  picker, populated from the loaded table's header like X, Y and Timestamp; a **Constant
  Z Value** applied to any row the telemetry leaves blank; and **Additional Field
  Mapping**, a value table that routes any other column to any of the 19 video metadata
  fields the tool does not compute for itself. Z previously came from auto-detection
  alone, with no way to name the column or to supply a value when the log had none.
- **Telemetry Z is a height above the ellipsoid**, an opt-in that also writes Z to
  `Sensor Ellipsoid Height Extended`. It is off by default and should stay off unless
  that is genuinely what the log records: true altitude and ellipsoid height are
  different references, and there is no bathymetry here to convert between them. An
  explicit field mapping to that field wins over it. Turning it on when the two are not
  the same thing suppresses the video footprint, because the ellipsoid figure resolves to
  an orthometric height below the elevation surface. Together with **Write Sensor Far
  Distance**, these are the two settings to clear first when a footprint does not appear.
- **Parameter help in the tool dialog, for every tool.** The information icon beside each
  parameter now describes what the parameter expects: units, accepted formats, sign
  conventions, and what happens to a row that cannot be read. Coverage is every input
  parameter of all five tools.

### Fixed

- **Parameter help had never reached the dialog.** `arcpy.Parameter` has no `description`
  property, so the descriptions written in the toolbox were discarded by the
  geoprocessing framework and the information icons were empty. The text lives in each
  tool's metadata sidecar instead, generated from those same descriptions.
- **Editing an acquisition profile's override values did nothing.** The pre-fill guard
  remembered the last profile on the tool object, which ArcGIS Pro rebuilds for every
  validation pass, so all eight override fields were rewritten after each edit anywhere
  on the dialog. A preset profile reverted the entered numbers, and *Custom*, whose
  defaults are all empty, blanked each field as focus moved.
- **A day-first, dot-separated timestamp was unreadable**, and a log using it was rejected
  in full: every row was skipped as having an unparseable timestamp, and the run ended
  with `No telemetry rows had usable X, Y, and Timestamp values`. `01.09.2022 00:00:00`
  now parses as 1 September. Slash-separated dates are still read month-first.
- **Video footprints never appeared, because camera tilt was written in the wrong
  convention.** The acquisition profiles measure pitch from nadir, where 0 is straight
  down. ArcGIS reads `Sensor Relative Elevation Angle` as tilt from the horizontal plane,
  positive up. The profile value went through unchanged, so the Diver profile claimed
  +30° — thirty degrees above the horizon — and a view ray aimed at the sky has no ground
  intersection to draw a footprint from. Nothing warned, because the number was valid.
  The value written is now `pitch − 90`.
- **Far distance never reached the multiplexer.** Convert Video Metadata carries it only
  when the column is named `Sensor Far Distance` *and* the value has no decimal point:
  `4` survives, `4.0` is silently blanked, `Far Distance` is dropped entirely. It is now
  rounded to whole metres, so all six named profiles deliver a value.
- **Two tools failed after their work was already done when no portal was active.**
  `arcpy.GetPortalDescription()` raises `ValueError`, which the best-effort handler around
  it did not catch, so an optional lookup of the signed-in user's name took down
  *Extracted Frame Image Metadata Generation* and *Cross-Reference Video Player Frame
  Exports*.
- **A table picked from the map emptied every field pick-list** and reported the table as
  missing. A CSV added to a map becomes a table view whose name still ends in `.csv`, and
  the reader chose its strategy from that extension, so it tried to open a view name as a
  file. Values now resolve through `Describe().catalogPath` first.
- **A folder passed as the output geodatabase reached `CreateMosaicDataset` and came back
  as ERROR 000837**, which names nothing. *Build Mosaic and Oriented Imagery Datasets*
  now rejects it up front and says which parameter is wrong.
- **A WKT2 coordinate system passed from Python was silently ignored** and replaced with
  the default. Such a value arrives wrapped in a geoprocessing value object rather than as
  a string, and the recovery path only ran for strings — so any projection other than the
  fallback would have produced wrong coordinates with no error.
- **Resampling a log with no Z column raised a `TypeError`** on `None` arithmetic.
- The rename summary was logged twice by
  *Extracted Frame Image Metadata Generation*.

### Changed

- The camera pitch override is now labelled **Camera Tilt from Nadir**, since the value is
  converted before being written rather than passed through as the MISB field. The
  parameter name is unchanged, so existing scripts keep working.
- Parameter and tool descriptions across all five tools were rewritten in a plainer,
  more technical register. Wording only; no behaviour changed.

## 1.0.0 — first public release

First release of the toolbox as a shareable package. Everything below describes how this
release differs from the internal version it was built from.

### Breaking changes

These matter only if you have existing scripts or models built against the internal
version.

- The toolbox file is now `OceanVideoToolsForArcGISPro.pyt` and its alias is
  `OceanVideoTools`. Scripts calling `arcpy.videoframeimgfusion.<Tool>()` must be updated
  to `arcpy.OceanVideoTools.<Tool>()`, and saved references to the old toolbox path will
  not resolve.
- The tool *Video Frame Image Metadata Fusion* is now **Extracted Frame Image Metadata
  Generation**, and its class is `ExtractedFrameImageMetadataGeneration`. The new name
  describes what it does — generating metadata for extracted frames — rather than how it
  once did it.
- Metadata written into output artifacts now records the new tool name. The XMP toolkit
  tag, the iFDO `software` field, and `SourceManifest.json`'s `tool` field all change from
  `DOOS Video Frame Image Metadata Fusion` to
  `DOOS Extracted Frame Image Metadata Generation`. Artifacts produced before and after
  this release therefore differ in that field, which matters if you group past outputs by
  it.

### Fixed

- **Inspect Video and Sensor Data was unusable without PyAV.** The tool declared PyAV a
  licensing requirement, so ArcGIS Pro greyed out the whole tool when it was absent — even
  though the tool runs perfectly well against a sensor table with no video and no extra
  packages. PyAV is now required only when a video is actually supplied, and the message
  says so.
- Three validation failures were silently discarded, leaving empty field pick-lists with
  no explanation. Failing to read a table's field names, or to load a Video Acquisition
  Profile's defaults, now reports the reason in the dialog.
- `Constant Elevation` and `Heading Offset` used a truthiness check that would have
  discarded a deliberate value of `0`. Both now use an explicit check.

### Changed

- **Every input parameter across all five tools now has help-panel text** — 143
  parameters. Previously most had none.
- All five tool descriptions rewritten: acronyms are expanded on first use, and each tool
  states its licence, extension or package requirement up front instead of only revealing
  it by greying out.
- The three input-table parameters are now named in parallel, making clear that they take
  genuinely different things: a raw vehicle log, a MISB video metadata table, or either.
- Imagery Category, Elevation Source, Mosaic Raster Type and the footprint options now
  explain what to choose and what the consequence of choosing wrongly is.

### Not included

Rule-based frame extraction — an alternative automated route at the frame-export stage,
driven by sampling interval, UTC time windows and sensor-value constraints — is developed
in this project but not part of this release, because it depends on a third-party library
that must be installed into a cloned Python environment. Excluding it keeps this package
installable on a default ArcGIS Pro installation.
