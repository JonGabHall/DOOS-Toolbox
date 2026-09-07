# Vocabulary

Terms and acronyms used by these tools, by the ArcGIS tools they sit alongside, and by
the marine imaging standards they target.

## Imagery and metadata

**Frame Table / Camera Table** — the CSV pair that describes a set of extracted video
frames. The Frame Table holds one row per image (file path, timestamp, position,
camera orientation); the Camera Table describes the physical camera (sensor size, focal
length, pixel size). Esri's *Extract Video Frames To Images* writes this pair
automatically, and every tool here reads or writes the same schema. Files are named
`*_FrameTable.csv` and `*_CameraTable.csv`, which is how the tools find them.

**iFDO** — *image FAIR Digital Object*. The marine imaging community's metadata standard
for image sets, describing what was imaged, where, when, by whom, under what licence, and
with what equipment. Written here as `*.ifdo.json`, targeting version 2.2.1.
See <https://www.ifdo-schema.org>.

**BIIGLE** — a web platform for annotating marine imagery. It imports per-image metadata
from a CSV with a fixed set of columns (`filename`, `taken_at`, `lng`, `lat`,
`gps_altitude`, `distance_to_ground`, `area`, `SUB_heading`). See <https://biigle.de>.

**PAM / `.aux.xml`** — *Persistent Auxiliary Metadata*. A GDAL sidecar file holding
metadata that the image format itself cannot store. ArcGIS reads these, and writing one
never modifies the image.

**XMP** — Adobe's *Extensible Metadata Platform*. A sidecar (`.xmp`) read by Lightroom,
Bridge and Photoshop. Written here as an interoperability companion to `.aux.xml`, not a
replacement — ArcGIS Pro is not confirmed to read `.xmp` for rasters.

**EXIF** — metadata tags stored *inside* a JPEG or TIFF. Embedding into EXIF rewrites the
image file, so the tools keep it optional and off by default where it matters.

**World file** (`.tfw`, `.jgw`, `.pgw`) — a small text file giving an image's position and
pixel size. Note that frames from video extraction usually carry a *placeholder* world
file describing pixel space rather than real ground coordinates; see
[docs/LESSONS-LEARNED.md](docs/LESSONS-LEARNED.md).

## Video

**FMV** — *Full Motion Video*. ArcGIS's capability for video whose frames carry
geospatial metadata, so the video can be played back on a map.

**MISB ST 0601** — the Motion Imagery Standards Board specification defining the metadata
fields carried alongside geospatial video: sensor latitude/longitude, platform
heading/pitch/roll, field of view, and a precision timestamp. Positions in MISB are always
WGS84 geographic degrees.

**KLV** — *Key-Length-Value*. The binary encoding used to embed MISB metadata inside a
video stream. A multiplexed video carries its telemetry as KLV packets, which
*Inspect Video and Sensor Data* can read back out directly.

**Multiplexing** — combining a video file with a metadata table so the metadata is
embedded in the video itself. Done by Esri's *Video Multiplexer*.

**Precision Time Stamp** — MISB's timestamp field: microseconds since the Unix epoch,
stored as an integer.

## ArcGIS data types

**Mosaic dataset** — a geodatabase container that presents many rasters as one continuous
image, while keeping each source image addressable. Requires a Standard or Advanced
licence.

**OID / Oriented imagery dataset** — a dataset that stores each image together with the
camera's position and viewing direction, so images can be inspected in their real
viewing geometry rather than flattened onto a map.

**Footprint** — the polygon describing where an image actually falls on the ground.

**Image Analyst** — the ArcGIS Pro extension providing the imagery and video tools.
Required by *Generate Deep Ocean Video Metadata*.

## Vehicles

**ROV** — remotely operated vehicle; tethered, pilot-controlled.
**AUV** — autonomous underwater vehicle; untethered, pre-programmed.
**Towed sled / drop camera** — camera platforms towed behind, or lowered from, a vessel.

Each has a *Video Acquisition Profile* supplying the camera geometry a navigation log
does not record.

## Project-specific

**Video Acquisition Profile** — a named set of camera assumptions (pitch, roll, field of
view, near/far distance, height above seafloor) used to fill in the values a navigation
log does not contain, so a usable video metadata file can still be produced.

**Description report** — the plain-text and JSON output of *Inspect Video and Sensor
Data*, summarising a video and/or sensor table: time extent, frame rate, gaps, and
whether the two overlap in time.

**Variable table** — an optional combined table produced by *Inspect Video and Sensor
Data*, joining a primary and secondary sensor table on nearest timestamp.
