# Changelog

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
