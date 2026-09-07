# -*- coding: utf-8 -*-
"""
FCT_ImageFusion_core.py

Reference implementation for embedding Frame/Camera Table (FCT) metadata,
produced by the "Extract Video Frames To Images" geoprocessing tool (Image
Analyst), into the corresponding extracted video frame images.

This module is kept separate from FCT_ImageFusion.py while the fusion logic
is reworked to tolerate real-world variance in FCT content and in the
extracted images, ahead of splitting that logic across two consumers:
    - a Jupyter notebook, for interactive/ad hoc runs
    - a custom ArcGIS Pro Python toolbox (.pyt) geoprocessing tool, for a
      GUI-driven run against arbitrary tool output

Because both future consumers need to call the same logic with different
parameter sources (notebook variables vs. arcpy.Parameter values) and
different progress reporting (print vs. arcpy.AddMessage), every function
here takes explicit arguments and reports progress through an injected
`log` callable rather than reading globals or calling arcpy messaging
directly. fuse_folder() at the bottom is the single entry point both future
consumers are expected to call.

=============================================================================
 Variance this module accounts for (found by inspecting real output from
 Extract Video Frames To Images across several example exports - notes are
 also inline next to the relevant code):
=============================================================================
  1. Field names in the frame/camera CSVs are not fixed across tool runs -
     Esri does not publish a schema for them. Every lookup scans a
     prioritized candidate list case-insensitively instead of using a
     hardcoded key, with a fuzzy substring fallback for latitude/longitude.
  2. "Missing" values show up as '', None, and text sentinels such as the
     literal string 'nan' (seen in real CameraTable.csv FocalLength values),
     'NULL', 'N/A', etc. - handled centrally by is_null_like() instead of
     ad hoc None/'' checks scattered through the code.
  3. CameraID join keys can differ in type/formatting between the frame and
     camera tables (e.g. "1" vs "1.0" vs 1) - normalized before joining.
  4. SRS values can be a bare WKID, an ESRI WKT string, a .prj path, or a
     compound "horizontal;vertical" pair (seen in real data as "3857;3855")
     - only the horizontal half is used to decide whether PerspectiveX/Y are
     safe to treat as longitude/latitude.
  5. An export folder can contain more than one FrameTable/CameraTable pair
     (a real export had two *_FrameTable.csv files sharing a
     single *_CameraTable.csv), or a camera table can be missing entirely -
     handled by discover_table_pairs(), which pairs tables by longest shared
     file name prefix and falls back to a lone camera table when there's
     only one to choose from.
  6. Extracted frames can be TIFF, JPEG, PNG, or NITF - all four are valid
     "Image Type" choices for Extract Video Frames To Images. All four get
     an *.aux.xml sidecar; JPEG and TIFF additionally get EXIF-style tags
     (TIFF's own tag numbering is the base EXIF is built from, so the same
     GPS IFD/UserComment approach works for both - see embed_tiff_metadata())
     and PNG additionally gets tEXt chunks. NITF is left aux.xml-only -
     embedding into NITF's TRE structures needs a dedicated NITF library,
     which is out of scope for a default-library-only script.
  7. A frame table row's image reference can be an absolute path from a
     different machine, a relative path, or just a bare file name - resolved
     by trying the raw value as-is and then falling back to the configured
     image folder.
  8. Numeric fields (coordinates, altitude, timestamps) can be missing,
     blank, or NaN for a given row without the rest of that row being
     unusable - every numeric read fails soft (returns None) instead of
     raising, and downstream tag writers just skip whatever piece is
     unavailable.
  9. A folder-level batch run (fuse_folder) must not let one malformed or
     unrecognized table pair abort every other pair in the same folder -
     each pair's failure is caught and counted separately.
 10. Position and orientation completeness varies by source, independent of
     everything above. A frame table may carry:
       - full exterior orientation (position + Omega/Phi/Kappa, per Esri's
         Frames table schema),
       - position only with no orientation at all (every real example export
         example is exactly this - there is no Omega/Phi/Kappa, Heading,
         Yaw, Pitch, or Roll field anywhere in that data),
       - orientation only (e.g. a compass heading with no GPS fix), or
       - a partial triad, such as heading with no pitch/roll.
     get_lat_lon()/get_altitude() already degrade row-by-row for position;
     get_orientation() does the same for orientation, and - critically -
     never blends the two rotation conventions it recognizes. Omega/Phi/Kappa
     and yaw/pitch/roll are related by a rotation-matrix conversion, not a
     1:1 relabeling, so a row reporting Omega/Phi/Kappa is tagged with that
     convention and is never relabeled as yaw/pitch/roll (see build_ifdo_fields()
     below for where this matters).

=============================================================================
 iFDO alignment (https://www.ifdo-schema.org/overview/iFDO-content/)
=============================================================================
iFDO is a companion document for an entire image set (image-set-header +
image-set-items, one entry per file) - it is not an EXIF/XMP embedding
scheme, and there is no such thing as an "iFDO EXIF tag". So full iFDO
conformance is fundamentally a *document-generation* problem, not a
tag-naming problem. Two things are built here:

  1. build_ifdo_fields() produces an "ifdo" sub-object, named after real
     iFDO v2.x field names, that sits alongside the raw Frame_/Camera_
     passthrough in both the aux.xml sidecar (its own "iFDO" domain) and the
     JPEG/PNG JSON comment (its own "ifdo" key) - additive, never replacing
     the raw fields.
  2. write_ifdo_sidecar() (see the "Per-image iFDO document generation"
     section below) writes an actual "<image>.ifdo.json" document per image,
     shaped after the real example at
     https://www.ifdo-schema.org/standard/v2.2.1/examples/ifdo-image-example.json
     ($schema / image-set-header / image-set-items). This is a deliberate
     deviation from iFDO's own one-file-per-SET convention (see that
     section's comment for why), not a limitation of what iFDO defines.

Both draw from the same field mapping - only fields with an unambiguous
source are included:

  Maps cleanly:
    image-latitude / image-longitude   <- resolved lat/lon fields (get_lat_lon)
    image-coordinate-reference-system  <- fixed "EPSG:4326" (iFDO defines
                                           these two fields as decimal
                                           degrees, which is EPSG:4326 by
                                           definition)
    image-datetime                     <- resolved timestamp, reformatted to
                                           iFDO's ISO8601 form
    image-camera-yaw/pitch/roll-degrees <- ONLY when get_orientation() found
                                           the yaw/pitch/roll convention

  Maps with a caveat, included anyway (documented, not silently guessed):
    image-altitude-meters              <- resolved altitude value, passed
                                           through as-is. iFDO defines this
                                           as negative below sea level; the
                                           source's sign convention is not
                                           guaranteed (the real Squid data's
                                           "Sensor Altitude" is a large
                                           positive number that does not
                                           look like a real depth/altitude
                                           at all - almost certainly a
                                           synthetic placeholder in that
                                           example export, not a value this
                                           module should try to "fix").

  Does NOT map, and is deliberately left out of the "ifdo" block:
    - Omega/Phi/Kappa: numerically different from yaw/pitch/roll (a rotation
      matrix apart, not a relabeling), so it is never written to
      image-camera-*-degrees. It still appears in the raw Frame_Omega/
      Frame_Phi/Frame_Kappa passthrough fields, just not under an iFDO name.
    - image-camera-pose (UTM position + 3x3 orientation matrix): would need
      PerspectiveX/Y in a *UTM* CRS specifically (the real Squid data is Web
      Mercator, not UTM) plus a validated axis-convention conversion for the
      orientation matrix - not attempted.
    - image-camera-calibration-model (focal length/principal point in
      pixels): Esri's Cameras table schema documents FocalLength/PrincipalX/Y
      in microns, not pixels, so converting would require knowing PixelSize
      is trustworthy for that conversion - the real CameraTable.csv's
      FocalLength is the literal string 'nan' in every row, so there is
      nothing to validate this conversion against yet.
    - image-meters-above-ground (camera-to-seafloor distance) vs
      image-altitude-meters (absolute Z) are different iFDO concepts; the
      source data doesn't say which physical quantity "Sensor Altitude"
      actually is, so it is only ever mapped to image-altitude-meters, never
      both.

  What ArcGIS Pro can and can't fill in for the image-set-header (see
  get_arcgis_pro_context(), verified against the real
  Workspace/DOOS/DeepOceanVideo.aprx project):
    - image-set-local-path: always available (aprx.homeFolder).
    - image-pi / image-creators: available whenever Pro is signed in to a
      Portal/ArcGIS Online org (arcpy.GetPortalDescription()['user']) - real
      name and email, but only a mailto: URI, not a proper persistent
      identifier like the ORCID the official example uses.
    - image-set-name / image-abstract / image-copyright: technically
      available via aprx.metadata.title/summary/description/credits, but
      only if a person has actually filled in Project Properties > General
      or the project's Item Description - in the real project used to
      verify this, none of them were.
    - Not available from ArcGIS Pro at all, regardless of how well the
      project is documented: image-set-handle (needs an actual Handle/DOI
      minting service), image-license, and every study-design/methodology
      field (image-objective, image-target-environment, image-deployment,
      image-navigation, image-illumination, image-scale-reference,
      image-marine-zone, image-spatial-constraints, image-temporal-
      constraints, image-time-synchronisation, image-curation-protocol,
      image-visual-constraints, ...). See the module-level "master schema
      table" discussion for how these should be handled instead.
    - arcpy.mp.ArcGISProject("CURRENT") - the only way to reach "the
      project currently open" - is only valid when this code runs inside
      Pro's own process (a script tool, the Python window, or an ArcGIS
      Notebook). It cannot be used from a standalone python.exe run, which
      is how every example in this module has been validated so far;
      get_arcgis_pro_context(aprx_path=...) accepts an explicit .aprx path
      as a fallback for that case.

=============================================================================
 Master schema table - not built, recommended as a next step
=============================================================================
Every field listed as "not available from ArcGIS Pro at all" just above -
plus image-platform/image-sensor (a real name+uri, not a bare CameraID),
image-project/image-event/image-context (real identifiers, not a guessed
name), and anything under image-camera-housing-viewport/flatport-parameters/
domeport-parameters/photometric-calibration (not part of Esri's Frames/
Cameras table schema at all) - has no source in the FCT, the images, or an
ArcGIS Pro project, at any level of project documentation. These aren't
measurements; they're administrative/methodological facts a person decides
once per deployment (which vehicle, which camera, which license, which
DOI once minted, what counts as "seafloor" vs "water column" for this dive).
A small persistent lookup - one row per deployment/CameraID, filled in once
and reused by every future fuse_folder() run against that vehicle/project -
would remove the only remaining reason to hand-author these per run. See the
reply to this design pass for the full discussion of why it's worth building.

Requirements:
    ArcGIS Pro 3.7. Uses only arcpy and Pillow, both of which ship with the
    default arcgispro-py3 environment.

Author:      Jonah Hall
Created:     2026-08-25
"""

import csv
import hashlib
import json
import math
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import arcpy

try:
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational
    from PIL.PngImagePlugin import PngInfo
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# -----------------------------------------------------------------------
# Field-name candidates, checked in priority order and case-insensitively.
# Esri does not publish a fixed schema for the frame/camera CSVs, so these
# lists are grown from real example exports rather than assumed up front.
# -----------------------------------------------------------------------
CANDIDATE_IMAGE_FIELDS = ["Raster", "ImagePath", "Image", "FrameName", "Filename", "FileName", "Name"]
CANDIDATE_CAMERA_ID_FIELDS = ["CameraID", "Camera_ID", "CamID"]
CANDIDATE_SRS_FIELDS = ["SRS", "SpatialReference", "CoordinateSystem"]
CANDIDATE_LAT_LON_FIELDS = [
    ("Platform Latitude", "Platform Longitude"),
    ("Latitude", "Longitude"),
    ("Lat", "Lon"),
]
CANDIDATE_ALTITUDE_FIELDS = ["Sensor Altitude", "Platform Altitude", "Altitude", "PerspectiveZ"]
CANDIDATE_TIMESTAMP_FIELDS = ["Precision Time Stamp", "Time", "Timestamp", "DateTime"]

# Orientation candidates, grouped by rotation convention. Omega/Phi/Kappa
# (Esri's photogrammetric exterior-orientation convention) and yaw/pitch/roll
# (the NED body-frame convention iFDO expects) are never mixed - see
# get_orientation() and the module docstring's iFDO section for why.
CANDIDATE_OPK_FIELDS = {"omega": ["Omega"], "phi": ["Phi"], "kappa": ["Kappa"]}
CANDIDATE_YAW_FIELDS = ["Yaw", "Heading", "Platform Heading", "Camera Heading", "Camera Yaw"]
CANDIDATE_PITCH_FIELDS = ["Pitch", "Platform Pitch", "Camera Pitch"]
CANDIDATE_ROLL_FIELDS = ["Roll", "Platform Roll", "Camera Roll"]

# BIIGLE's file metadata CSV has no counterpart elsewhere in this project for
# these two columns (biigle.de/manual/tutorials/volumes/file-metadata) - not
# every Frame Table carries them, so a manual per-run override is offered
# alongside auto-detection (see build_biigle_fields()).
CANDIDATE_DISTANCE_TO_GROUND_FIELDS = [
    "Camera Height Above Seafloor", "CameraHeight", "Distance To Ground", "Altitude Above Seafloor"
]
CANDIDATE_AREA_FIELDS = ["Area", "Footprint Area", "Ground Area"]
BIIGLE_CSV_FIELDS = ["filename", "taken_at", "lng", "lat", "gps_altitude", "distance_to_ground", "area", "SUB_heading"]

# iFDO version this module targets. The documents this module writes are
# validated against the real example at
# https://www.ifdo-schema.org/standard/v2.2.1/examples/ifdo-image-example.json
IFDO_VERSION = "v2.2.1"
# The iFDO structure spec requires $schema to reference the version HANDLE
# (the schema's own $id), not the schemas/ convenience URL.
IFDO_SCHEMA_URL = "https://hdl.handle.net/20.500.12085/22aba7d7-c432-49ab-bebe-ee8ed4f70150"

NULL_LIKE_TEXT = {"", "nan", "none", "null", "n/a", "na", "-", "unknown"}

EXTENSIONS_WITH_EXIF = (".jpg", ".jpeg")
EXTENSIONS_WITH_PNG_TEXT = (".png",)
EXTENSIONS_WITH_TIFF_TAGS = (".tif", ".tiff")

# Compression names img.info["compression"] can return that are also valid
# Image.save(format="TIFF", compression=...) values - "raw" (the read-side
# value for uncompressed TIFFs) is deliberately excluded since it is NOT a
# valid write-side keyword (would raise); omitting compression defaults to
# uncompressed, which is the correct equivalent.
_TIFF_WRITABLE_COMPRESSIONS = {
    "group3", "group4", "jpeg", "lzma", "packbits",
    "tiff_adobe_deflate", "tiff_ccitt", "tiff_lzw",
    "tiff_raw_16", "tiff_sgilog", "tiff_sgilog24",
    "tiff_thunderscan", "webp", "zstd",
}

# Export folder structure constants
EXPORT_IMAGE_EXTENSIONS = ("*.tif", "*.tiff", "*.jpg", "*.jpeg", "*.png")
EXPORT_METADATA_EXTENSIONS = ("*.aux.xml", "*.ifdo.json", "*.xmp")
EXPORT_FOLDER_IMAGES_DIR = "images"
EXPORT_FOLDER_METADATA_DIR = "metadata"

# World file extensions follow the standard "drop vowels" convention, not a
# simple "+w" suffix (.tif/.tiff -> .tfw, not .tifw).
WORLD_FILE_EXTENSIONS = {
    ".tif": ".tfw", ".tiff": ".tfw",
    ".jpg": ".jgw", ".jpeg": ".jgw",
    ".png": ".pgw",
}

# A frame image's trailing "_<digits>" is its sequential-index or elapsed-
# time suffix (Extract Video Frames To Images/video-player exports) - the
# part rename_frame_images() preserves when the user supplies a new base name.
FRAME_SUFFIX_PATTERN = re.compile(r"^(.+?)(_\d+)(\.[A-Za-z0-9]+)$")

# iFDO output format labels (kept in sync with the tool.pyt constants).
# Behavior is resolved by resolve_ifdo_format() rather than by matching these
# exact strings, so the wording can change without breaking a saved script or
# model that still passes an older label.
IFDO_FORMAT_SET_LEVEL = "Single image-set JSON file (iFDO standard) - DEFAULT"
IFDO_FORMAT_PER_IMAGE = "Per-image JSON sidecars (one JSON file per image)"


def resolve_ifdo_format(value):
    """Map any iFDO Output Format label onto one of the two behaviors.

    Blank/unrecognized resolves to the single image-set document, which is this
    tool's default; only an explicit per-image label selects sidecars.
    """
    if "per-image" in str(value or "").strip().lower():
        return IFDO_FORMAT_PER_IMAGE
    return IFDO_FORMAT_SET_LEVEL

# Everything below is transcribed from the v2.2.1 JSON Schema's own "required"
# lists and "anyOf"/const enumerations at
# https://www.ifdo-schema.org/schemas/v2.2.1/ifdo.json - used by
# validate_ifdo_document() to warn rather than to enforce.
IFDO_REQUIRED_HEADER_FIELDS = (
    "image-set-name", "image-set-uuid", "image-set-handle",
    "image-set-ifdo-version", "image-datetime", "image-latitude",
    "image-longitude", "image-altitude-meters",
    "image-coordinate-reference-system", "image-coordinate-uncertainty-meters",
    "image-context", "image-project", "image-event", "image-platform",
    "image-sensor", "image-pi", "image-creators", "image-license",
    "image-copyright", "image-abstract",
)
IFDO_REQUIRED_ITEM_FIELDS = ("image-uuid", "image-hash-sha256", "image-handle")

# Fields the schema types as an object with a required "name" (+ optional "uri").
IFDO_OBJECT_FIELDS = (
    "image-context", "image-project", "image-event",
    "image-platform", "image-sensor", "image-pi", "image-license",
)
IFDO_ARRAY_OF_OBJECT_FIELDS = ("image-creators",)

IFDO_CONTROLLED_VOCABULARIES = {
    "image-acquisition": ("photo", "video", "slide"),
    "image-quality": ("raw", "processed", "product"),
    "image-deployment": ("mapping", "stationary", "survey", "exploration",
                          "experiment", "sampling"),
    "image-navigation": ("satellite", "beacon", "transponder", "reconstructed"),
    "image-scale-reference": ("3D camera", "calibrated camera", "laser marker",
                               "optical flow"),
    "image-illumination": ("sunlight", "artificial light", "mixed light"),
    "image-pixel-magnitude": ("km", "hm", "dam", "m", "dm", "cm", "mm", "\u00b5m"),
    "image-marine-zone": ("seafloor", "water column", "sea surface",
                           "atmosphere", "laboratory"),
    "image-spectral-resolution": ("grayscale", "rgb", "multi-spectral",
                                   "hyper-spectral"),
    "image-capture-mode": ("timer", "manual", "mixed"),
    "image-fauna-attraction": ("none", "baited", "light"),
}

# Set-identity keys that must never be lifted out of one image set's header
# and reused as another set's template defaults.
IFDO_SET_IDENTITY_FIELDS = (
    "image-set-uuid", "image-set-name", "image-set-local-path",
    "image-set-ifdo-version",
)


# -----------------------------------------------------------------------
# Value-normalization helpers
# -----------------------------------------------------------------------
def is_null_like(value):
    """True if value represents a missing entry.

    Covers actual None/empty-string values as well as the text sentinels
    ('nan', 'NULL', 'N/A', ...) that show up next to them in real frame and
    camera tables - e.g. every FocalLength in the real Squid CameraTable.csv
    is the literal string 'nan' rather than a blank cell.
    """
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip().lower() in NULL_LIKE_TEXT:
        return True
    return False


def to_float(value):
    """Best-effort float conversion. Returns None instead of raising."""
    if is_null_like(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_first_float(row, candidate_fields):
    """Value of the first candidate column in row that parses as a float."""
    for field in candidate_fields:
        value = to_float(row.get(field))
        if value is not None:
            return value
    return None


def normalize_join_key(value):
    """Normalize a CameraID-like value so '1', '1.0', 1, and ' 1 ' all match."""
    if is_null_like(value):
        return None
    text = str(value).strip()
    try:
        return str(int(float(text)))
    except (TypeError, ValueError):
        return text.lower()


# -----------------------------------------------------------------------
# Table reading
# -----------------------------------------------------------------------
def read_metadata_table(table_path):
    """Read a Frames or Cameras table into a list of dicts, one per row.

    Handles both a plain .csv export (Extract Video Frames To Images) and a
    geodatabase/dBASE table (Build Frames and Cameras Tables). Field names
    are stripped of surrounding whitespace, since padded headers have been
    seen in hand-edited or re-exported copies of these tables.
    """
    if str(table_path).lower().endswith(".csv"):
        with open(table_path, newline="", encoding="utf-8-sig") as csv_file:
            reader = csv.DictReader(csv_file)
            reader.fieldnames = [name.strip() for name in reader.fieldnames]
            return [dict(row) for row in reader]

    field_names = [f.name for f in arcpy.ListFields(table_path)
                   if f.type not in ("Geometry", "Blob", "Raster")]
    rows = []
    with arcpy.da.SearchCursor(table_path, field_names) as cursor:
        for row in cursor:
            rows.append(dict(zip(field_names, row)))
    return rows


def discover_table_pairs(export_folder):
    """Find (frame_table, camera_table_or_None) pairs in an export folder.

    Extract Video Frames To Images names its two CSVs '<basename>_FrameTable.csv'
    and '<basename>_CameraTable.csv'. A folder can hold more than one such
    pair, and a frame table's basename does not always exactly match a
    camera table's basename - a real export had two
    FrameTable.csv files sharing a single CameraTable.csv. Pairing is done
    by longest shared file name prefix, falling back to a lone camera table
    when there is only one candidate to choose from.
    """
    export_folder = Path(export_folder)
    frame_tables = sorted(export_folder.glob("*FrameTable.csv"))
    camera_tables = sorted(export_folder.glob("*CameraTable.csv"))

    def shared_prefix_length(a, b):
        length = 0
        for char_a, char_b in zip(a, b):
            if char_a != char_b:
                break
            length += 1
        return length

    pairs = []
    for frame_table in frame_tables:
        if not camera_tables:
            pairs.append((frame_table, None))
        elif len(camera_tables) == 1:
            pairs.append((frame_table, camera_tables[0]))
        else:
            frame_stem = frame_table.stem.replace("_FrameTable", "")
            best_match = max(
                camera_tables,
                key=lambda camera_table: shared_prefix_length(
                    frame_stem, camera_table.stem.replace("_CameraTable", "")
                ),
            )
            pairs.append((frame_table, best_match))

    return pairs


# -----------------------------------------------------------------------
# Field resolution (done once per table, not once per row, so every row in
# a run is read the same way even if some rows are missing individual
# values)
# -----------------------------------------------------------------------
def resolve_field(field_names, candidates, override=None, required=True):
    """Case-insensitive lookup of the first candidate present in field_names."""
    lower_lookup = {name.lower(): name for name in field_names}

    if override:
        match = lower_lookup.get(override.lower())
        if not match:
            raise KeyError(f"Field override '{override}' was not found. Available fields: {field_names}")
        return match

    for candidate in candidates:
        match = lower_lookup.get(candidate.lower())
        if match:
            return match

    if required:
        raise KeyError(f"None of {candidates} were found. Available fields: {field_names}")
    return None


def resolve_lat_lon_fields(field_names):
    """Return (lat_field, lon_field) or None.

    Checks known field-name pairs first, then falls back to a fuzzy scan
    for an unfamiliar schema - if exactly one field name contains 'lat' and
    exactly one contains 'lon'/'lng', that pair is used.
    """
    lower_lookup = {name.lower(): name for name in field_names}
    for lat_candidate, lon_candidate in CANDIDATE_LAT_LON_FIELDS:
        lat_match = lower_lookup.get(lat_candidate.lower())
        lon_match = lower_lookup.get(lon_candidate.lower())
        if lat_match and lon_match:
            return lat_match, lon_match

    lat_guesses = [name for name in field_names if "lat" in name.lower()]
    lon_guesses = [name for name in field_names if "lon" in name.lower() or "lng" in name.lower()]
    if len(lat_guesses) == 1 and len(lon_guesses) == 1:
        return lat_guesses[0], lon_guesses[0]

    return None


# -----------------------------------------------------------------------
# Image resolution
# -----------------------------------------------------------------------
def find_image_path(raw_value, image_folder):
    """Resolve a frame table image reference to a file that exists on disk.

    Prioritizes the file in image_folder (the common case when images have been
    copied or moved to the export location), then falls back to the raw value
    as a full path (legacy case where images are at their original location).
    """
    if is_null_like(raw_value):
        return None

    # Try the filename in the current image_folder FIRST (images copied/moved here)
    candidate = Path(image_folder) / Path(str(raw_value)).name
    if candidate.is_file():
        return candidate

    # Fall back to the raw value as a full path (original location still exists)
    candidate = Path(os.path.normpath(str(raw_value)))
    if candidate.is_file():
        return candidate

    return None


# -----------------------------------------------------------------------
# Geolocation
# -----------------------------------------------------------------------
def is_geographic_srs(srs_value):
    """True if srs_value's horizontal component is a geographic (lat/lon) CS.

    srs_value may be a bare WKID, an ESRI WKT string, a .prj path, or a
    compound "horizontal;vertical" pair (seen in real Frames table output as
    "3857;3855") - only the horizontal half is meaningful here.
    """
    if is_null_like(srs_value):
        return False
    horizontal = str(srs_value).split(";")[0].strip()
    try:
        spatial_ref = arcpy.SpatialReference(horizontal)
        return spatial_ref.type == "Geographic"
    except Exception:
        return False


def get_lat_lon(frame_row, lat_lon_fields, srs_field):
    """Return (latitude, longitude) in decimal degrees for a frame row, or None.

    Prefers an explicit lat/lon field pair (already in decimal degrees) and
    only falls back to PerspectiveX/PerspectiveY when the row's SRS is
    confirmed geographic - PerspectiveX/Y are frequently in a projected
    coordinate system (e.g. Web Mercator in the real Squid example data),
    which would produce wrong GPS tags if used directly.
    """
    if lat_lon_fields:
        lat_field, lon_field = lat_lon_fields
        latitude = to_float(frame_row.get(lat_field))
        longitude = to_float(frame_row.get(lon_field))
        if latitude is not None and longitude is not None:
            return latitude, longitude

    perspective_x = to_float(frame_row.get("PerspectiveX"))
    perspective_y = to_float(frame_row.get("PerspectiveY"))
    srs_value = frame_row.get(srs_field) if srs_field else None
    if perspective_x is not None and perspective_y is not None and is_geographic_srs(srs_value):
        return perspective_y, perspective_x

    return None


def get_altitude(frame_row):
    """Return an altitude value (in meters) for a frame row, or None."""
    for field in CANDIDATE_ALTITUDE_FIELDS:
        altitude = to_float(frame_row.get(field))
        if altitude is not None:
            return altitude
    return None


def get_timestamp_raw(frame_row):
    """Return the first populated timestamp-like value for a frame row, or None."""
    for field in CANDIDATE_TIMESTAMP_FIELDS:
        value = frame_row.get(field)
        if not is_null_like(value):
            return value
    return None


def parse_datetime(raw_value):
    """Best-effort parse of a frame table timestamp into a UTC datetime.

    Handles a plain epoch value in seconds, milliseconds, or microseconds
    (Esri's own "Precision Time Stamp" field uses microseconds since
    1970-01-01). Returns None if raw_value is missing or isn't numeric - an
    already-formatted date string isn't re-parsed here since its format
    isn't guaranteed; parse_exif_datetime()/parse_iso_datetime() fall back to
    passing such a string through as-is.
    """
    if is_null_like(raw_value):
        return None

    numeric = to_float(raw_value)
    if numeric is None:
        return None

    if numeric > 1e14:
        seconds = numeric / 1_000_000  # microseconds since epoch
    elif numeric > 1e11:
        seconds = numeric / 1_000  # milliseconds since epoch
    else:
        seconds = numeric  # seconds since epoch

    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_exif_datetime(raw_value):
    """Convert a frame table timestamp to EXIF's 'YYYY:MM:DD HH:MM:SS' form."""
    if is_null_like(raw_value):
        return None
    parsed = parse_datetime(raw_value)
    return parsed.strftime("%Y:%m:%d %H:%M:%S") if parsed is not None else str(raw_value)


def parse_iso_datetime(raw_value):
    """Convert a frame table timestamp to iFDO's ISO8601
    'YYYY-MM-DD hh:mm:ss.sss' form (image-datetime)."""
    if is_null_like(raw_value):
        return None
    parsed = parse_datetime(raw_value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if parsed is not None else str(raw_value)


def get_orientation(frame_row):
    """Return whatever orientation info a frame row has, tagged with which
    of two conventions it's in - never both, and never converted between
    them (they are not numerically interchangeable, only related by a full
    rotation-matrix conversion):

      - "omega_phi_kappa": Esri's photogrammetric exterior-orientation
        convention (the Frames table schema's Omega/Phi/Kappa fields).
      - "yaw_pitch_roll": the NED body-frame convention iFDO's
        image-camera-yaw/pitch/roll-degrees fields expect (also covers a
        plain compass "Heading" field, which is used as yaw).

    Any subset of a convention's three components can be present - e.g. a
    heading-only telemetry feed with no pitch/roll still returns a result
    with pitch/roll left as None, rather than the row being treated as
    having no orientation at all.

    Returns None if no field from either convention is present (this is the
    common case for the real example export data, which has neither).
    """
    omega = None
    for field in CANDIDATE_OPK_FIELDS["omega"]:
        omega = to_float(frame_row.get(field))
        if omega is not None:
            break

    phi = None
    for field in CANDIDATE_OPK_FIELDS["phi"]:
        phi = to_float(frame_row.get(field))
        if phi is not None:
            break

    kappa = None
    for field in CANDIDATE_OPK_FIELDS["kappa"]:
        kappa = to_float(frame_row.get(field))
        if kappa is not None:
            break

    if omega is not None or phi is not None or kappa is not None:
        return {"convention": "omega_phi_kappa", "omega": omega, "phi": phi, "kappa": kappa}

    yaw = None
    for field in CANDIDATE_YAW_FIELDS:
        yaw = to_float(frame_row.get(field))
        if yaw is not None:
            break

    pitch = None
    for field in CANDIDATE_PITCH_FIELDS:
        pitch = to_float(frame_row.get(field))
        if pitch is not None:
            break

    roll = None
    for field in CANDIDATE_ROLL_FIELDS:
        roll = to_float(frame_row.get(field))
        if roll is not None:
            break

    if yaw is not None or pitch is not None or roll is not None:
        return {"convention": "yaw_pitch_roll", "yaw": yaw, "pitch": pitch, "roll": roll}

    return None


def build_ifdo_fields(frame_row, lat_lon_fields, srs_field, ifdo_metadata=None):
    """Best-effort mapping of one frame row onto iFDO v2.x field names, for
    the subset where a mapping is unambiguous. See the module docstring's
    "iFDO alignment" section for exactly what is and isn't included, and
    why. Returns {} if nothing on the row maps.

    ifdo_metadata (dict, optional): user-supplied iFDO field values that are
    merged into the output, overriding any auto-detected values for those keys.
    """
    ifdo_fields = {}

    lat_lon = get_lat_lon(frame_row, lat_lon_fields, srs_field)
    if lat_lon:
        ifdo_fields["image-latitude"], ifdo_fields["image-longitude"] = lat_lon
        ifdo_fields["image-coordinate-reference-system"] = "EPSG:4326"

    altitude = get_altitude(frame_row)
    if altitude is not None:
        ifdo_fields["image-altitude-meters"] = altitude

    iso_datetime = parse_iso_datetime(get_timestamp_raw(frame_row))
    if iso_datetime:
        ifdo_fields["image-datetime"] = iso_datetime

    orientation = get_orientation(frame_row)
    if orientation and orientation["convention"] == "yaw_pitch_roll":
        if orientation["yaw"] is not None:
            ifdo_fields["image-camera-yaw-degrees"] = orientation["yaw"]
        if orientation["pitch"] is not None:
            ifdo_fields["image-camera-pitch-degrees"] = orientation["pitch"]
        if orientation["roll"] is not None:
            ifdo_fields["image-camera-roll-degrees"] = orientation["roll"]
    # omega_phi_kappa is intentionally not mapped to image-camera-*-degrees -
    # see the module docstring's iFDO section.

    # Same Frame Table columns BIIGLE's distance_to_ground/area already use.
    meters_above_ground = resolve_first_float(frame_row, CANDIDATE_DISTANCE_TO_GROUND_FIELDS)
    if meters_above_ground is not None:
        ifdo_fields["image-meters-above-ground"] = meters_above_ground

    area = resolve_first_float(frame_row, CANDIDATE_AREA_FIELDS)
    if area is not None and area > 0:
        ifdo_fields["image-area-square-meters"] = area

    # Merge user-supplied iFDO metadata values, which override auto-detected ones
    if ifdo_metadata:
        ifdo_fields.update(ifdo_metadata)

    return ifdo_fields


def build_biigle_fields(image_name, frame_row, ifdo_fields, distance_to_ground_override=None, area_override=None):
    """Map one frame row (+ its already-resolved iFDO fields) onto BIIGLE's
    file metadata CSV columns (biigle.de/manual/tutorials/volumes/file-metadata).

    Only 'filename' is mandatory there; every other column is left blank
    when no value is available (auto-detected or override) - a sparse
    column is valid BIIGLE input, it just means that piece of metadata
    isn't imported for that file. distance_to_ground/area have no
    equivalent elsewhere in this project, so a per-run manual override is
    the fallback when no matching Frame Table column exists.
    """
    row = {"filename": image_name}

    if "image-datetime" in ifdo_fields:
        # BIIGLE's own doc/example use second precision ("2016-12-19 17:09:00"),
        # not iFDO's fractional-second ISO form - truncate rather than pass through.
        row["taken_at"] = ifdo_fields["image-datetime"].split(".")[0]

    if "image-latitude" in ifdo_fields and "image-longitude" in ifdo_fields:
        row["lat"] = ifdo_fields["image-latitude"]
        row["lng"] = ifdo_fields["image-longitude"]

    if "image-altitude-meters" in ifdo_fields:
        row["gps_altitude"] = ifdo_fields["image-altitude-meters"]

    if "image-camera-yaw-degrees" in ifdo_fields:
        row["SUB_heading"] = ifdo_fields["image-camera-yaw-degrees"]

    distance_to_ground = resolve_first_float(frame_row, CANDIDATE_DISTANCE_TO_GROUND_FIELDS)
    if distance_to_ground is None:
        distance_to_ground = distance_to_ground_override
    if distance_to_ground is not None:
        row["distance_to_ground"] = distance_to_ground

    area = resolve_first_float(frame_row, CANDIDATE_AREA_FIELDS)
    if area is None:
        area = area_override
    if area is not None:
        row["area"] = area

    return row


def write_biigle_metadata_csv(rows, output_csv_path, log=print):
    """Write a BIIGLE-compatible file metadata CSV covering every processed
    image in one file (biigle.de/manual/tutorials/volumes/file-metadata) -
    unlike the per-image *.aux.xml/*.xmp/*.ifdo.json sidecars, BIIGLE
    expects one CSV for the whole imported volume."""
    output_csv_path = Path(output_csv_path)
    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=BIIGLE_CSV_FIELDS, restval="")
        writer.writeheader()
        writer.writerows(rows)
    log(f"Wrote BIIGLE file metadata CSV ({len(rows)} row(s)) -> {output_csv_path}")
    return str(output_csv_path)


# -----------------------------------------------------------------------
# Per-image iFDO document generation
#
# iFDO itself defines one document per whole image SET (image-set-header +
# image-set-items, one entry per file) - there is no "iFDO per image" in the
# spec. write_ifdo_sidecar() below deliberately deviates from that to
# produce one file per image (matching the aux.xml-per-image pattern the
# rest of this module already uses): each file repeats the same
# image-set-header and carries exactly one image-set-items entry, so every
# file is still a structurally valid iFDO document on its own, just scoped
# to a single item. See the module docstring's iFDO section for the full
# feasibility discussion.
# -----------------------------------------------------------------------
def compute_sha256(path, chunk_size=1024 * 1024):
    """SHA256 of a file's current on-disk bytes, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_arcgis_pro_context(aprx_path=None):
    """Best-effort collection of iFDO header values from an ArcGIS Pro project.

    Tries the project actually open in the current Pro session first
    (arcpy.mp.ArcGISProject("CURRENT") - only valid when this code is
    running inside Pro itself: a script tool, the Python window, or an
    ArcGIS Notebook, never a standalone python.exe run like the one this
    module was validated with), then falls back to aprx_path if given.
    Never raises - returns whatever it could find, which may be {}.

    What was actually verified against a real project
    (Workspace/DOOS/DeepOceanVideo.aprx) while building this:
      - aprx.homeFolder: always available -> image-set-local-path.
      - aprx.metadata.title/summary/description/credits: available via the
        API, but only populated if someone has filled in Project
        Properties > General / the project's Item Description - in the
        real project used to validate this, all of them were still None.
      - arcpy.GetPortalDescription()['user']: populated whenever Pro is
        signed in to a Portal/ArcGIS Online org (it was, in that project) -
        gives a real fullName/email for image-pi/image-creators, though
        only a mailto: URI, not a proper persistent identifier like ORCID.
      - There is no project-level field for image-set-handle, image-license,
        image-abstract's real content, or any of the study-design fields
        (image-objective, image-target-environment, etc.) - see the module
        docstring's iFDO section and the master-schema-table discussion for
        where those need to come from instead.
    """
    context = {}

    aprx = None
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
    except (RuntimeError, OSError):
        if aprx_path:
            try:
                aprx = arcpy.mp.ArcGISProject(str(aprx_path))
            except (RuntimeError, OSError):
                aprx = None

    if aprx is not None:
        try:
            if not is_null_like(aprx.homeFolder):
                context["image-set-local-path"] = aprx.homeFolder
        except (RuntimeError, AttributeError):
            pass
        try:
            if not is_null_like(aprx.metadata.title):
                context["image-set-name"] = aprx.metadata.title
        except (RuntimeError, AttributeError):
            pass
        try:
            abstract = aprx.metadata.summary or aprx.metadata.description
            if not is_null_like(abstract):
                context["image-abstract"] = abstract
        except (RuntimeError, AttributeError):
            pass
        try:
            if not is_null_like(aprx.metadata.credits):
                context["image-copyright"] = aprx.metadata.credits
        except (RuntimeError, AttributeError):
            pass

    try:
        portal_info = arcpy.GetPortalDescription()
        user = portal_info.get("user") if isinstance(portal_info, dict) else None
        if user and not is_null_like(user.get("fullName")):
            person = {"name": user["fullName"]}
            if not is_null_like(user.get("email")):
                person["uri"] = f"mailto:{user['email']}"
            context["image-pi"] = person
            context["image-creators"] = [person]
    except (RuntimeError, KeyError, TypeError):
        pass

    return context


def _load_existing_ifdo(ifdo_path):
    if not os.path.exists(ifdo_path):
        return None
    try:
        with open(ifdo_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def get_or_create_set_uuid(image_folder, metadata_folder=None):
    """Reuse the image-set-uuid from any *.ifdo.json already in image_folder
    so re-running this tool doesn't mint a new set identity every time -
    whichever run wrote the first sidecar decides the identity for every run
    after it, including a second, differently-named frame table that
    happens to target the same images (seen for real in an export where
    which has two FrameTable.csv files for one set of images). Otherwise
    generates a fresh one.

    metadata_folder (optional): also checked when a structured export
    layout has already moved prior *.ifdo.json sidecars out of image_folder
    into a sibling metadata/ directory (see fuse_folder's Phase 2a) - without
    this, re-running the tool against an already-exported output folder
    would never find the prior run's sidecars and would mint a new set
    identity (and new per-image UUIDs) every time.
    """
    search_dirs = [Path(image_folder)]
    if metadata_folder:
        search_dirs.append(Path(metadata_folder))

    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for existing_path in directory.glob("*.ifdo.json"):
            existing = _load_existing_ifdo(existing_path)
            if existing:
                set_uuid = existing.get("image-set-header", {}).get("image-set-uuid")
                if set_uuid:
                    return set_uuid
    return str(uuid.uuid4())


def build_ifdo_header(image_folder, set_uuid, default_name, project_context=None,
                       ifdo_metadata=None, log=print):
    """Assemble the image-set-header block shared by every per-image iFDO
    sidecar written in one fuse_frame_table() call.

    image-set-name and image-set-local-path use project_context (see
    get_arcgis_pro_context()) when available and fall back to
    default_name / image_folder otherwise. image-set-handle defaults to ""
    (matching how the official iFDO example represents an
    unpublished/un-handled image) unless the caller supplied one via
    ifdo_metadata. Every fallback/omission is logged so it can't pass as
    curated metadata.

    ifdo_metadata (dict, optional): user-supplied/template iFDO values
    (image-license, image-platform, image-sensor, image-deployment, ...).
    Per the iFDO structure spec, these are image-set-header "default values
    for the whole image set" facts a person decides once per deployment -
    not per-image values - so they are written here, once, rather than
    duplicated into every image-set-items entry (see build_ifdo_fields()
    for the fields that genuinely do vary per image).
    """
    project_context = project_context or {}
    ifdo_metadata = ifdo_metadata or {}
    header = {
        "image-set-ifdo-version": IFDO_VERSION,
        "image-set-uuid": set_uuid,
    }

    header["image-set-name"] = project_context.get("image-set-name") or default_name
    if "image-set-name" not in project_context:
        log(f"  iFDO: image-set-name defaulted to '{header['image-set-name']}' (no title found in "
            "the ArcGIS Pro project) - set one in Project Properties, or pass image_set_name=, to override.")

    if "image-set-handle" in ifdo_metadata:
        header["image-set-handle"] = ifdo_metadata["image-set-handle"]
        log("  iFDO: image-set-handle sourced from user-supplied metadata/template.")
    else:
        header["image-set-handle"] = ""
        log("  iFDO: image-set-handle left blank - iFDO requires a resolvable Handle/DOI for the "
            "published data, which nothing in the FCT, the images, or ArcGIS Pro can provide.")

    header["image-set-local-path"] = project_context.get("image-set-local-path", str(image_folder))
    if "image-set-local-path" not in project_context:
        log(f"  iFDO: image-set-local-path defaulted to '{header['image-set-local-path']}'")

    for key in ("image-pi", "image-creators", "image-abstract", "image-copyright"):
        if key in project_context:
            header[key] = project_context[key]
            log(f"  iFDO: {key} sourced from ArcGIS Pro project")
        else:
            log(f"  iFDO: {key} not available (not set in ArcGIS Pro project)")

    # Every remaining user-supplied field (image-license, image-platform,
    # image-sensor, image-deployment, image-navigation, image-illumination,
    # image-scale-reference, image-objective, image-target-environment,
    # image-marine-zone, ...) is a set-wide default, so it belongs here.
    for key, value in ifdo_metadata.items():
        if key == "image-set-handle":
            continue
        header[key] = value
        log(f"  iFDO: {key} sourced from user-supplied metadata/template.")

    return header


def write_ifdo_sidecar(image_path, header, item_fields):
    """Write one self-contained iFDO document for a single image: the
    shared image-set-header plus a one-entry image-set-items block keyed by
    the image's file name - see the section comment above for why this is
    one-file-per-image rather than iFDO's normal one-file-per-set.
    """
    ifdo_path = image_path.with_name(image_path.name + ".ifdo.json")
    document = {
        "$schema": IFDO_SCHEMA_URL,
        "image-set-header": header,
        "image-set-items": {image_path.name: item_fields},
    }
    with open(ifdo_path, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, default=str)


def decimal_degrees_to_dms(decimal_degrees):
    """Convert a decimal degree value to the (deg, min, sec) rationals EXIF expects.

    Each component must be an IFDRational rather than a plain (num, den)
    tuple - Pillow's TIFF/EXIF writer otherwise mistakes the tuple for a
    second value and raises a TypeError when it serializes the tag.
    """
    decimal_degrees = abs(decimal_degrees)
    degrees = int(decimal_degrees)
    minutes_full = (decimal_degrees - degrees) * 60
    minutes = int(minutes_full)
    seconds = round((minutes_full - minutes) * 60 * 100)
    return (IFDRational(degrees, 1), IFDRational(minutes, 1), IFDRational(seconds, 100))


# -----------------------------------------------------------------------
# Sidecar / native metadata writers
# -----------------------------------------------------------------------
def write_aux_xml(image_path, metadata, domain="FrameCameraMetadata"):
    """Write frame/camera metadata to a GDAL/Esri-style *.aux.xml sidecar.

    Works identically for every image format Extract Video Frames To Images
    can produce (TIFF, JPEG, PNG, NITF) and never touches pixel data.
    Metadata is written under a dedicated domain so re-running this updates
    values instead of duplicating them, and so it never overwrites unrelated
    PAM metadata (such as raster statistics) already written to the same
    sidecar file.
    """
    aux_path = str(image_path) + ".aux.xml"

    if os.path.exists(aux_path):
        tree = ET.parse(aux_path)
        root = tree.getroot()
    else:
        root = ET.Element("PAMDataset")
        tree = ET.ElementTree(root)

    for existing in root.findall(f"./Metadata[@domain='{domain}']"):
        root.remove(existing)

    metadata_elem = ET.SubElement(root, "Metadata", {"domain": domain})
    for key, value in metadata.items():
        mdi = ET.SubElement(metadata_elem, "MDI", {"key": str(key)})
        mdi.text = "" if value is None else str(value)

    ET.indent(tree, space="  ")
    tree.write(aux_path, encoding="utf-8", xml_declaration=True)


# Namespaces for the *.xmp sidecar - dc/xmp/exif are the standard public
# Adobe XMP/EXIF-in-XMP schemas (for the handful of fields Lightroom/Bridge/
# Photoshop already know how to display); "doos" is this project's own,
# carrying the full Frame/Camera/iFDO payload as one JSON-valued property,
# the same comment_payload already embedded natively for JPEG/TIFF/PNG.
XMP_NAMESPACES = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "doos": "https://github.com/esri/DOOS-fct-ifdo/1.0/",
}


def _decimal_degrees_to_xmp_gps(decimal_degrees, positive_ref, negative_ref):
    """Format a decimal-degree value as the XMP EXIF schema's "D,M.mmmmmmR"
    GPS coordinate string (Adobe XMP Specification Part 2, EXIF schema)."""
    ref = positive_ref if decimal_degrees >= 0 else negative_ref
    decimal_degrees = abs(decimal_degrees)
    degrees = int(decimal_degrees)
    minutes = (decimal_degrees - degrees) * 60
    return f"{degrees},{minutes:.6f}{ref}"


def write_xmp_sidecar(image_path, description, timestamp_formatted, lat_lon, altitude, metadata):
    """Write a standalone Adobe XMP sidecar (*.xmp) alongside image_path.

    An interoperability companion to write_aux_xml() for tools outside the
    Esri/GDAL ecosystem (Lightroom, Bridge, Photoshop) that read XMP
    sidecars but do not recognize GDAL's *.aux.xml PAM format - ArcGIS Pro
    itself is not confirmed to read *.xmp sidecars for a raster dataset, so
    this is additive, not a replacement for *.aux.xml.
    """
    for prefix, uri in XMP_NAMESPACES.items():
        ET.register_namespace(prefix, uri)

    rdf_ns = XMP_NAMESPACES["rdf"]
    description_elem = ET.Element(f"{{{rdf_ns}}}Description")
    description_elem.set(f"{{{rdf_ns}}}about", "")

    dc_desc = ET.SubElement(description_elem, f"{{{XMP_NAMESPACES['dc']}}}description")
    dc_desc.text = f"Frame {description}"

    if timestamp_formatted:
        create_date = ET.SubElement(description_elem, f"{{{XMP_NAMESPACES['xmp']}}}CreateDate")
        # EXIF's "YYYY:MM:DD HH:MM:SS" -> XMP/ISO 8601 "YYYY-MM-DDTHH:MM:SS"
        create_date.text = timestamp_formatted.replace(":", "-", 2).replace(" ", "T", 1)

    if lat_lon:
        latitude, longitude = lat_lon
        exif_ns = XMP_NAMESPACES["exif"]
        ET.SubElement(description_elem, f"{{{exif_ns}}}GPSLatitude").text = (
            _decimal_degrees_to_xmp_gps(latitude, "N", "S")
        )
        ET.SubElement(description_elem, f"{{{exif_ns}}}GPSLongitude").text = (
            _decimal_degrees_to_xmp_gps(longitude, "E", "W")
        )
        if altitude is not None:
            ET.SubElement(description_elem, f"{{{exif_ns}}}GPSAltitudeRef").text = (
                "0" if altitude >= 0 else "1"
            )
            ET.SubElement(description_elem, f"{{{exif_ns}}}GPSAltitude").text = (
                f"{int(round(abs(altitude) * 100))}/100"
            )

    doos_metadata = ET.SubElement(description_elem, f"{{{XMP_NAMESPACES['doos']}}}Metadata")
    doos_metadata.text = json.dumps(metadata, default=str)

    rdf_root = ET.Element(f"{{{rdf_ns}}}RDF")
    rdf_root.append(description_elem)

    xmpmeta = ET.Element(f"{{{XMP_NAMESPACES['x']}}}xmpmeta")
    xmpmeta.set(f"{{{XMP_NAMESPACES['x']}}}xmptk", "DOOS Extracted Frame Image Metadata Generation")
    xmpmeta.append(rdf_root)

    body = ET.tostring(xmpmeta, encoding="unicode")
    packet = (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        f"{body}\n"
        '<?xpacket end="w"?>'
    )
    xmp_path = str(image_path) + ".xmp"
    with open(xmp_path, "w", encoding="utf-8") as f:
        f.write(packet)


def embed_jpeg_exif(image_path, description, timestamp_formatted, lat_lon, altitude, metadata):
    """Embed a JSON copy of metadata plus a handful of standard EXIF tags.

    Saves to a temporary file first and swaps it in with os.replace(), so a
    save failure can never leave the original frame image truncated or
    corrupted.
    """
    with Image.open(image_path) as img:
        exif = img.getexif()
        exif[0x010E] = f"Frame {description}"  # ImageDescription

        if timestamp_formatted:
            exif[0x0132] = timestamp_formatted  # DateTime

        if lat_lon:
            latitude, longitude = lat_lon
            gps_ifd = exif.get_ifd(0x8825)  # GPSInfo IFD
            gps_ifd[1] = "N" if latitude >= 0 else "S"       # GPSLatitudeRef
            gps_ifd[2] = decimal_degrees_to_dms(latitude)     # GPSLatitude
            gps_ifd[3] = "E" if longitude >= 0 else "W"       # GPSLongitudeRef
            gps_ifd[4] = decimal_degrees_to_dms(longitude)    # GPSLongitude
            if altitude is not None:
                gps_ifd[5] = 0 if altitude >= 0 else 1                            # GPSAltitudeRef
                gps_ifd[6] = IFDRational(int(round(abs(altitude) * 100)), 100)    # GPSAltitude

        exif_ifd = exif.get_ifd(0x8769)  # Exif IFD
        comment = json.dumps(metadata, default=str)
        exif_ifd[0x9286] = b"ASCII\x00\x00\x00" + comment.encode("ascii", errors="replace")  # UserComment

        tmp_path = image_path.with_name(image_path.name + ".tmp")
        try:
            try:
                img.save(tmp_path, format="JPEG", exif=exif, quality="keep")
            except (ValueError, OSError):
                img.save(tmp_path, format="JPEG", exif=exif, quality=95)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    os.replace(tmp_path, image_path)


def embed_tiff_metadata(image_path, description, timestamp_formatted, lat_lon, altitude, metadata):
    """Embed a JSON copy of metadata plus GPS/UserComment EXIF tags and
    baseline TIFF tags (ImageDescription/DateTime/Software).

    TIFF's own tag numbering is the base the EXIF spec extends, so this
    reuses the identical GPS IFD/UserComment approach as embed_jpeg_exif() -
    only the save() plumbing differs. description/date_time/software are
    also passed as dedicated save() keywords (not just inside the Exif
    sub-IFD) since those are the plain baseline tags most readers - GDAL's
    GTiff driver included, which is what ArcGIS Pro's raster metadata view
    reads for a generic TIFF - surface directly.
    Preserves the original compression scheme so re-saving doesn't
    silently balloon file size or require recompressing.
    """
    with Image.open(image_path) as img:
        original_compression = img.info.get("compression")

        exif = img.getexif()
        if lat_lon:
            latitude, longitude = lat_lon
            gps_ifd = exif.get_ifd(0x8825)  # GPSInfo IFD
            gps_ifd[1] = "N" if latitude >= 0 else "S"       # GPSLatitudeRef
            gps_ifd[2] = decimal_degrees_to_dms(latitude)     # GPSLatitude
            gps_ifd[3] = "E" if longitude >= 0 else "W"       # GPSLongitudeRef
            gps_ifd[4] = decimal_degrees_to_dms(longitude)    # GPSLongitude
            if altitude is not None:
                gps_ifd[5] = 0 if altitude >= 0 else 1                            # GPSAltitudeRef
                gps_ifd[6] = IFDRational(int(round(abs(altitude) * 100)), 100)    # GPSAltitude

        exif_ifd = exif.get_ifd(0x8769)  # Exif IFD
        comment = json.dumps(metadata, default=str)
        exif_ifd[0x9286] = b"ASCII\x00\x00\x00" + comment.encode("ascii", errors="replace")  # UserComment

        save_kwargs = {
            "format": "TIFF",
            "exif": exif,
            "description": f"Frame {description}",
            "software": "DOOS Extracted Frame Image Metadata Generation",
        }
        if timestamp_formatted:
            save_kwargs["date_time"] = timestamp_formatted
        if original_compression in _TIFF_WRITABLE_COMPRESSIONS:
            save_kwargs["compression"] = original_compression

        tmp_path = image_path.with_name(image_path.name + ".tmp")
        try:
            img.save(tmp_path, **save_kwargs)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    os.replace(tmp_path, image_path)


def embed_png_text(image_path, description, timestamp_formatted, lat_lon, altitude, metadata):
    """Embed metadata into PNG tEXt chunks - PNG's counterpart to JPEG EXIF.

    PNG has no EXIF/GPS IFD structure of its own, so geolocation is written
    as plain decimal-degree text fields instead of the DMS rationals EXIF
    requires.
    """
    with Image.open(image_path) as img:
        png_info = PngInfo()
        png_info.add_text("Description", f"Frame {description}")
        if timestamp_formatted:
            png_info.add_text("Creation Time", timestamp_formatted)
        if lat_lon:
            latitude, longitude = lat_lon
            png_info.add_text("GPSLatitude", f"{latitude:.8f}")
            png_info.add_text("GPSLongitude", f"{longitude:.8f}")
            if altitude is not None:
                png_info.add_text("GPSAltitude", f"{altitude:.2f}")
        png_info.add_text("FrameCameraMetadata", json.dumps(metadata, default=str))

        tmp_path = image_path.with_name(image_path.name + ".tmp")
        try:
            img.save(tmp_path, format="PNG", pnginfo=png_info)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    os.replace(tmp_path, image_path)


# -----------------------------------------------------------------------
# Phase 2-4: Set-level iFDO, folder export, template export/import
# -----------------------------------------------------------------------

def derive_setlevel_header_defaults(items_by_image):
    """Fill the header's data-derived required fields from the items present.

    image-datetime/-latitude/-longitude/-altitude-meters are required in the
    header as "default values for the whole image set", but only ever exist
    per-image here, so they are summarized: earliest datetime, mean position.
    """
    defaults = {}
    datetimes = sorted(str(item["image-datetime"]) for item in items_by_image.values()
                       if not is_null_like(item.get("image-datetime")))
    if datetimes:
        defaults["image-datetime"] = datetimes[0]

    for key in ("image-latitude", "image-longitude", "image-altitude-meters"):
        values = [to_float(item.get(key)) for item in items_by_image.values()]
        values = [v for v in values if v is not None]
        if values:
            defaults[key] = sum(values) / len(values)

    crs = next((item["image-coordinate-reference-system"] for item in items_by_image.values()
                if not is_null_like(item.get("image-coordinate-reference-system"))), None)
    if crs:
        defaults["image-coordinate-reference-system"] = crs

    latitudes = [to_float(item.get("image-latitude")) for item in items_by_image.values()]
    latitudes = [v for v in latitudes if v is not None]
    longitudes = [to_float(item.get("image-longitude")) for item in items_by_image.values()]
    longitudes = [v for v in longitudes if v is not None]
    if latitudes:
        defaults["image-set-min-latitude-degrees"] = min(latitudes)
        defaults["image-set-max-latitude-degrees"] = max(latitudes)
    if longitudes:
        defaults["image-set-min-longitude-degrees"] = min(longitudes)
        defaults["image-set-max-longitude-degrees"] = max(longitudes)

    return defaults


def validate_ifdo_document(document):
    """Check one iFDO document against the v2.2.1 schema's own requirements.

    Returns a list of human-readable issue strings; never raises. This warns
    rather than enforces - an incomplete iFDO document is still more useful
    to a user than no document at all.
    """
    issues = []
    header = document.get("image-set-header") or {}
    items = document.get("image-set-items") or {}

    if not isinstance(items, dict):
        issues.append("image-set-items must be a JSON object keyed by filename, "
                      f"not a {type(items).__name__}")
        items = {}

    missing_header = [f for f in IFDO_REQUIRED_HEADER_FIELDS if is_null_like(header.get(f))]
    if missing_header:
        issues.append("image-set-header is missing required field(s): "
                      + ", ".join(missing_header))

    for field in IFDO_OBJECT_FIELDS:
        value = header.get(field)
        if value is None:
            continue
        if not isinstance(value, dict):
            issues.append(f"{field} must be an object with a 'name' (and optional 'uri'), "
                          f"not a plain {type(value).__name__}")
        elif is_null_like(value.get("name")):
            issues.append(f"{field} object is missing its required 'name'")

    for field in IFDO_ARRAY_OF_OBJECT_FIELDS:
        value = header.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or not value:
            issues.append(f"{field} must be a non-empty array of objects with a 'name'")
        elif any(not isinstance(entry, dict) or is_null_like(entry.get("name")) for entry in value):
            issues.append(f"{field} entries must each be an object with a 'name'")

    for field, allowed in IFDO_CONTROLLED_VOCABULARIES.items():
        value = header.get(field)
        if not is_null_like(value) and value not in allowed:
            issues.append(f"{field} value {value!r} is not one of the allowed values: "
                          + ", ".join(allowed))

    for image_name, item in items.items():
        if not isinstance(item, dict):
            issues.append(f"image-set-items['{image_name}'] must be an object")
            continue
        missing_item = [f for f in IFDO_REQUIRED_ITEM_FIELDS if is_null_like(item.get(f))
                        and is_null_like(header.get(f))]
        if missing_item:
            issues.append(f"image-set-items['{image_name}'] is missing required field(s): "
                          + ", ".join(missing_item))

    return issues


def write_ifdo_setlevel(output_dir, set_name, header, items_by_image, log=print):
    """Write ONE iFDO document describing the whole image set.

    This is the iFDO standard's own layout: a single file with one
    image-set-header and an image-set-items OBJECT keyed by image filename
    (the schema types image-set-items as "object", so a list of
    {"$filename": ...} entries - which earlier versions of this module wrote -
    is not valid iFDO). Returns the written path.
    """
    output_path = Path(output_dir) / f"{set_name}.ifdo.json"

    full_header = dict(header or {})
    full_header.setdefault("image-set-ifdo-version", IFDO_VERSION)
    for key, value in derive_setlevel_header_defaults(items_by_image).items():
        if is_null_like(full_header.get(key)):
            full_header[key] = value

    document = {
        "$schema": IFDO_SCHEMA_URL,
        "image-set-header": full_header,
        "image-set-items": dict(items_by_image),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, default=str)

    log(f"Wrote single image-set iFDO document ({len(items_by_image)} image(s)) -> {output_path}")
    for issue in validate_ifdo_document(document):
        log(f"  WARNING: iFDO compliance: {issue}")

    return str(output_path)


def _write_ifdo_template_file(ifdo_metadata, output_path):
    """Export current iFDO metadata values as a reusable template JSON file."""
    template = {
        "description": "iFDO metadata template for Extracted Frame Image Metadata Generation tool. Import into future runs to reuse these values.",
        "created_timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": ifdo_metadata
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2, default=str)


# iFDO field to GP tool category mapping (for UI preview functionality)
IFDO_FIELD_CATEGORIES = {
    # Project Context
    "image-context": "Project Context",
    "image-project": "Project Context",
    "image-event": "Project Context",
    # Deployment Information
    "image-deployment": "Deployment Information",
    "image-navigation": "Deployment Information",
    "image-illumination": "Deployment Information",
    "image-scale-reference": "Deployment Information",
    "image-fauna-attraction": "Deployment Information",
    "image-capture-mode": "Deployment Information",
    # Sensor / Camera Information
    "image-platform": "Sensor / Camera Information",
    "image-sensor": "Sensor / Camera Information",
    "image-acquisition": "Sensor / Camera Information",
    "image-quality": "Sensor / Camera Information",
    "image-spectral-resolution": "Sensor / Camera Information",
    "image-pixel-magnitude": "Sensor / Camera Information",
    # Licensing & Identifiers
    "image-license": "Licensing & Identifiers",
    "image-set-handle": "Licensing & Identifiers",
    "image-copyright": "Licensing & Identifiers",
    "image-pi": "Licensing & Identifiers",
    "image-creators": "Licensing & Identifiers",
    # Study Design & Classification
    "image-objective": "Study Design & Classification",
    "image-target-environment": "Study Design & Classification",
    "image-marine-zone": "Study Design & Classification",
    "image-target-timescale": "Study Design & Classification",
    "image-spatial-constraints": "Study Design & Classification",
    "image-temporal-constraints": "Study Design & Classification",
    "image-abstract": "Study Design & Classification",
    # Curation & Quality
    "image-time-synchronisation": "Curation & Quality",
    "image-item-identification-scheme": "Curation & Quality",
    "image-curation-protocol": "Curation & Quality",
    "image-visual-constraints": "Curation & Quality",
    "image-coordinate-uncertainty-meters": "Curation & Quality",
    # Auto-detected (not user-entered)
    "image-latitude": "Auto-detected (Position)",
    "image-longitude": "Auto-detected (Position)",
    "image-altitude-meters": "Auto-detected (Position)",
    "image-coordinate-reference-system": "Auto-detected (Position)",
    "image-set-min-latitude-degrees": "Auto-detected (Position)",
    "image-set-max-latitude-degrees": "Auto-detected (Position)",
    "image-set-min-longitude-degrees": "Auto-detected (Position)",
    "image-set-max-longitude-degrees": "Auto-detected (Position)",
    "image-datetime": "Auto-detected (Timestamp)",
    "image-camera-yaw-degrees": "Auto-detected (Orientation)",
    "image-camera-pitch-degrees": "Auto-detected (Orientation)",
    "image-camera-roll-degrees": "Auto-detected (Orientation)",
    "image-meters-above-ground": "Auto-detected (Geometry)",
    "image-area-square-meters": "Auto-detected (Geometry)",
    "image-uuid": "Auto-detected (Identity)",
    "image-hash-sha256": "Auto-detected (Identity)",
}


def _load_template_metadata(template_path):
    """Load metadata values from a template JSON, returning {} if invalid.

    Single source of truth for all template file parsing operations. Accepts
    this tool's own wrapper shape ({"metadata": {...}}) and, failing that, a
    raw iFDO document - so a *.ifdo.json written by a previous run can be fed
    straight back in as a template. Set-identity keys are never carried over.
    """
    if not Path(template_path).is_file():
        return {}
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            template = json.load(f)
    except Exception:
        return {}

    if not isinstance(template, dict):
        return {}

    metadata = template.get("metadata")
    if isinstance(metadata, dict) and metadata:
        return metadata

    header = template.get("image-set-header")
    if isinstance(header, dict):
        return {k: v for k, v in header.items() if k not in IFDO_SET_IDENTITY_FIELDS}

    return {}


def preview_template_coverage(template_path):
    """Preview which iFDO categories will have values from template.

    Returns formatted dict: {category: [fields_with_values]}
    Useful for showing users what template will provide before running tool.
    """
    metadata = _load_template_metadata(template_path)

    coverage = {}
    for field_name, value in metadata.items():
        category = IFDO_FIELD_CATEGORIES.get(field_name, "Other")
        if category not in coverage:
            coverage[category] = []
        coverage[category].append((field_name, value))

    return coverage


def format_template_preview(coverage_dict):
    """Format template coverage dict into human-readable string."""
    if not coverage_dict:
        return "Template contains no iFDO metadata"

    lines = ["Template will provide values for:"]
    for category in sorted(coverage_dict.keys()):
        lines.append(f"  {category}:")
        for field, value in coverage_dict[category]:
            # Truncate long values for readability
            display_value = str(value)[:50]
            if len(str(value)) > 50:
                display_value += "..."
            lines.append(f"    • {field}: {display_value}")

    return "\n".join(lines)


def import_ifdo_template(template_path):
    """Import iFDO metadata from a template file."""
    return _load_template_metadata(template_path)


def rename_frame_images(export_folder, new_base_name, log=print):
    """Rename every frame image in export_folder to a new base name, keeping
    each file's trailing "_<digits>" sequential-index/elapsed-time suffix
    (FRAME_SUFFIX_PATTERN) and extension - e.g. "Squid_0000000.tif" with
    new_base_name="ROV1" becomes "ROV1_0000000.tif".

    Runs BEFORE fuse_folder()'s own processing (see the .pyt's execute()),
    so every Frame Table CSV, sidecar, BIIGLE CSV, and manifest this tool
    generates downstream already reflects the new names - there is no
    separate "apply the rename" step needed anywhere else. Each Frame
    Table's image-path column is rewritten to match; the Camera Table is
    untouched (it has no per-image filename column). Each image's existing
    world file (.tfw/.jgw/.pgw) and any pre-existing *.aux.xml/*.xmp/
    *.ifdo.json sidecars from a prior run are renamed alongside it so
    nothing is orphaned.

    Returns a dict with 'renamed', 'skipped', and 'errors' counts.
    """
    export_path = Path(export_folder)
    pairs = discover_table_pairs(export_path)
    renamed = skipped = errors = 0

    for frame_table, _camera_table in pairs:
        rows = read_metadata_table(str(frame_table))
        if not rows:
            continue
        field_names = list(rows[0].keys())
        image_field = resolve_field(field_names, CANDIDATE_IMAGE_FIELDS, required=False)
        if not image_field:
            log(f"  WARNING: {frame_table.name} has no recognizable image field - skipping rename.")
            continue
        # Case-insensitive, resolved once per table (not a raw "in row" check) -
        # a real column named e.g. "FileName" must still match the fixed-case
        # "Filename"/"FileName" entries in CANDIDATE_IMAGE_FIELDS.
        lower_field_lookup = {name.lower(): name for name in field_names}
        other_image_fields = [lower_field_lookup[c.lower()] for c in CANDIDATE_IMAGE_FIELDS if c.lower() in lower_field_lookup]

        table_changed = False
        for row in rows:
            raw_reference = row.get(image_field)
            current_path = find_image_path(raw_reference, export_path)
            if current_path is None:
                skipped += 1
                continue

            match = FRAME_SUFFIX_PATTERN.match(current_path.name)
            if not match:
                log(f"  WARNING: '{current_path.name}' has no _<digits> suffix to preserve - skipping rename.")
                skipped += 1
                continue

            _old_base, suffix, extension = match.groups()
            new_name = f"{new_base_name}{suffix}{extension}"
            if new_name == current_path.name:
                continue
            new_path = current_path.with_name(new_name)

            if new_path.exists():
                log(f"  ERROR: cannot rename '{current_path.name}' -> '{new_name}': target already exists.")
                errors += 1
                continue

            try:
                current_path.rename(new_path)
            except OSError as exc:
                log(f"  ERROR renaming '{current_path.name}' -> '{new_name}': {exc}")
                errors += 1
                continue

            # World file and any pre-existing sidecars from a prior run share
            # the image's own base name - rename them alongside it (best
            # effort; a missing one is normal, not an error).
            world_suffix = WORLD_FILE_EXTENSIONS.get(extension.lower())
            if world_suffix:
                old_world = current_path.with_suffix(world_suffix)
                if old_world.is_file():
                    try:
                        old_world.rename(new_path.with_suffix(world_suffix))
                    except OSError as exc:
                        log(f"  WARNING: could not rename world file {old_world.name}: {exc}")

            for sidecar_ext in (".aux.xml", ".xmp", ".ifdo.json"):
                old_sidecar = current_path.with_name(current_path.name + sidecar_ext)
                if old_sidecar.is_file():
                    try:
                        old_sidecar.rename(new_path.with_name(new_path.name + sidecar_ext))
                    except OSError as exc:
                        log(f"  WARNING: could not rename sidecar {old_sidecar.name}: {exc}")

            # Update EVERY column that references this image, not just
            # image_field - Extract Video Frames To Images' own Frame Table
            # commonly carries the filename in more than one column at once
            # (e.g. "Raster" as a full path AND a separate "Filename" column),
            # and all of them go stale otherwise. Derived directly from
            # new_name (the file just renamed on disk) rather than requiring
            # each column's old value to exactly match current_path.name -
            # a secondary column occasionally disagrees slightly with the
            # resolved image field (case, stale value, etc.) and would
            # otherwise be silently skipped. Preserves whichever "shape" each
            # column already had - a full path stays a full path (just with
            # the new filename), a bare filename stays bare.
            for candidate_field in other_image_fields:
                value = row.get(candidate_field)
                if is_null_like(value):
                    continue
                value_path = Path(str(value))
                row[candidate_field] = (
                    str(value_path.with_name(new_name)) if value_path.parent != Path(".") else new_name
                )

            renamed += 1
            table_changed = True
            log(f"  Renamed '{current_path.name}' -> '{new_name}'")

        if table_changed:
            with open(frame_table, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), restval="")
                writer.writeheader()
                writer.writerows(rows)
            log(f"  Updated image references in {frame_table.name}")

    log(f"Renamed {renamed} image(s) to base name '{new_base_name}' ({skipped} skipped, {errors} error(s)).")
    return {"renamed": renamed, "skipped": skipped, "errors": errors}


def export_folder_structure(export_folder, output_folder, if_exists="Overwrite", exclude_file=None, log=print):
    """Copy export_folder contents to output_folder with preserved structure.

    Structure:
      output_folder/
        images/            (all .tif, .jpg, .png)
        metadata/          (all .aux.xml, .ifdo.json)
        *.csv              (Frame/Camera tables)

    if_exists: "Overwrite" (replace), "Skip" (keep existing), or "Fail" (error).
    exclude_file: Optional file path to exclude from copying (e.g., input template file).
    """
    export_path = Path(export_folder)
    output_path = Path(output_folder)
    exclude_path = Path(exclude_file) if exclude_file else None

    if output_path.exists():
        if if_exists == "Fail":
            raise ValueError(f"Output folder exists and if_exists='Fail': {output_folder}")
        elif if_exists == "Skip":
            log(f"Output folder exists; skipping export per user preference.")
            return

    output_path.mkdir(parents=True, exist_ok=True)
    images_dir = output_path / EXPORT_FOLDER_IMAGES_DIR
    metadata_dir = output_path / EXPORT_FOLDER_METADATA_DIR
    images_dir.mkdir(exist_ok=True)
    metadata_dir.mkdir(exist_ok=True)

    # Copy images
    for ext in EXPORT_IMAGE_EXTENSIONS:
        for img_file in export_path.glob(ext):
            shutil.copy2(img_file, images_dir / img_file.name)

    # Copy each image's world file (.tfw/.jgw/.pgw), if present, into
    # images_dir alongside its image - these are not in
    # EXPORT_IMAGE_EXTENSIONS or EXPORT_METADATA_EXTENSIONS so were silently
    # dropped by this export step, leaving every exported TIFF ungeoreferenced
    # (confirmed live: this broke the mosaic dataset's "Table" raster type,
    # which requires each image to carry its own georeferencing).
    for ext in EXPORT_IMAGE_EXTENSIONS:
        for img_file in export_path.glob(ext):
            world_suffix = WORLD_FILE_EXTENSIONS.get(img_file.suffix.lower())
            world_file = img_file.with_suffix(world_suffix) if world_suffix else None
            if world_file and world_file.is_file():
                shutil.copy2(world_file, images_dir / world_file.name)

    # Copy metadata (excluding input template file if present). Per-image
    # sidecars belong in metadata/; a set-level *.ifdo.json describes the whole
    # set, so it stays at the root with the Frame/Camera Tables.
    for ext in EXPORT_METADATA_EXTENSIONS:
        for meta_file in export_path.glob(ext):
            # Skip the input template file if it's in the export folder
            if exclude_path and meta_file.resolve() == exclude_path.resolve():
                log(f"Skipping input template file: {meta_file.name} (values already merged)")
                continue
            set_level = meta_file.name.endswith(".ifdo.json") and not _is_per_image_ifdo(meta_file)
            destination = output_path if set_level else metadata_dir
            shutil.copy2(meta_file, destination / meta_file.name)

    # Copy Frame/Camera tables. The Frame Table's image-path column (Raster/
    # ImagePath/etc.) is rewritten to point at the copied images_dir instead
    # of being duplicated as-is - a raw copy would leave every path pointing
    # back at export_folder's original images, not the ones just copied into
    # this structured export. The Camera Table has no such column (CameraID/
    # NCols/NRows/FocalLength/PixelSize only) so it's copied unchanged.
    for csv_file in export_path.glob("*Table.csv"):
        rows = read_metadata_table(str(csv_file))
        field_names = list(rows[0].keys()) if rows else []
        image_field = resolve_field(field_names, CANDIDATE_IMAGE_FIELDS, required=False) if rows else None
        if not rows or not image_field:
            shutil.copy2(csv_file, output_path / csv_file.name)
            continue

        # Case-insensitive, resolved once per table (not a raw "in row" check) -
        # a real column named e.g. "FileName" must still match the fixed-case
        # "Filename"/"FileName" entries in CANDIDATE_IMAGE_FIELDS.
        lower_field_lookup = {name.lower(): name for name in field_names}
        other_image_fields = [lower_field_lookup[c.lower()] for c in CANDIDATE_IMAGE_FIELDS if c.lower() in lower_field_lookup]

        rewritten = 0
        for row in rows:
            old_value = row.get(image_field)
            resolved = find_image_path(old_value, images_dir)
            if resolved is None:
                continue

            # Update EVERY column referencing this same image's old path, not
            # just image_field - a Frame Table commonly carries the filename
            # in more than one column at once (e.g. "Raster" as a full path
            # AND a separate "Filename" column also holding a full path), and
            # each needs the same images_dir rewrite independently. A bare
            # filename column is left untouched - it stays valid regardless
            # of which folder now holds the image.
            old_name = Path(str(old_value)).name
            for candidate_field in other_image_fields:
                value = row.get(candidate_field)
                if is_null_like(value) or Path(str(value)).name != old_name:
                    continue
                if Path(str(value)).parent != Path("."):
                    row[candidate_field] = str(resolved)
            rewritten += 1

        with open(output_path / csv_file.name, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), restval="")
            writer.writeheader()
            writer.writerows(rows)
        log(f"  Rewrote {rewritten}/{len(rows)} image path(s) in {csv_file.name} -> {images_dir}")

    log(f"Exported complete folder structure to {output_folder}")


def write_source_manifest(export_folder, output_folder=None, run_settings=None, log=print):
    """Write a JSON manifest recording where exported images/metadata came from.

    Lets an exported deliverable (or a person, later) trace any image back to
    the export_folder it was fused from, plus the settings this run used -
    see the "Export JSON Manifest" option in Extracted Frame Image Metadata Generation.
    Written to output_folder if one was used, otherwise to export_folder itself.
    """
    export_path = Path(export_folder)
    images = []
    for ext in EXPORT_IMAGE_EXTENSIONS:
        for img_file in sorted(export_path.glob(ext)):
            images.append({
                "filename": img_file.name,
                "source_path": str(img_file.resolve()),
            })

    manifest = {
        "tool": "Extracted Frame Image Metadata Generation",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_folder": str(export_path.resolve()),
        "output_folder": str(Path(output_folder).resolve()) if output_folder else None,
        "image_count": len(images),
        "images": images,
        "settings": run_settings or {},
    }

    destination = Path(output_folder) if output_folder else export_path
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "SourceManifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, default=str)

    log(f"Wrote source manifest ({len(images)} image(s)) -> {manifest_path}")
    return str(manifest_path)


def _ifdo_search_dirs(export_folder):
    """Every directory a *.ifdo.json could legitimately be sitting in, for
    either the flat or the structured export layout."""
    export_path = Path(export_folder)
    candidates = [export_path,
                  export_path / EXPORT_FOLDER_IMAGES_DIR,
                  export_path / EXPORT_FOLDER_METADATA_DIR]
    return [d for d in dict.fromkeys(candidates) if d.is_dir()]


def _is_per_image_ifdo(path):
    """True for an "<image>.<ext>.ifdo.json" sidecar, False for a set-level
    "<set name>.ifdo.json" - the two live in different places and must not be
    confused for each other."""
    stem = path.name[: -len(".ifdo.json")]
    return Path(stem).suffix.lower() in EXTENSIONS_WITH_EXIF + EXTENSIONS_WITH_PNG_TEXT + EXTENSIONS_WITH_TIFF_TAGS


def _load_existing_setlevel_document(export_folder):
    """Return (header, items) from an existing set-level *.ifdo.json anywhere in
    this export folder.

    Once the single-file format stops writing per-image sidecars, this file is
    the only surviving record of the set's identity - without reading it back,
    every re-run would mint a fresh image-set-uuid and fresh per-image
    image-uuids. Never raises.
    """
    for directory in _ifdo_search_dirs(export_folder):
        for candidate in sorted(directory.glob("*.ifdo.json")):
            if _is_per_image_ifdo(candidate):
                continue
            document = _load_existing_ifdo(candidate)
            if not document:
                continue
            items = document.get("image-set-items") or {}
            if isinstance(items, list):
                items = {entry.get("$filename"): {k: v for k, v in entry.items() if k != "$filename"}
                         for entry in items if isinstance(entry, dict) and entry.get("$filename")}
            return document.get("image-set-header") or {}, items if isinstance(items, dict) else {}
    return {}, {}


def _absorb_stray_ifdo_sidecars(export_folder, items_by_image, log=print):
    """Fold any pre-existing per-image *.ifdo.json into items_by_image, then
    delete them.

    The set-level document is built directly from in-memory values, so this
    only exists to catch sidecars a previous run left behind or that
    export_folder_structure() copied in from the input folder - without it
    they would sit alongside the single set file contradicting it. Mutates
    items_by_image in place; never raises.
    """
    absorbed = removed = 0
    for directory in _ifdo_search_dirs(export_folder):
        for candidate in sorted(directory.glob("*.ifdo.json")):
            if not _is_per_image_ifdo(candidate):
                continue
            try:
                doc = json.loads(candidate.read_text(encoding="utf-8"))
                items = doc.get("image-set-items") or {}
                # Accept the legacy list-of-{"$filename": ...} form too.
                if isinstance(items, list):
                    items = {entry.get("$filename"): {k: v for k, v in entry.items() if k != "$filename"}
                             for entry in items if isinstance(entry, dict) and entry.get("$filename")}
                if isinstance(items, dict):
                    for image_name, fields in items.items():
                        if image_name not in items_by_image and isinstance(fields, dict):
                            items_by_image[image_name] = fields
                            absorbed += 1
                candidate.unlink()
                removed += 1
            except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
                log(f"  WARNING: could not absorb {candidate.name}: {exc}")

    if absorbed or removed:
        log(f"  Absorbed {absorbed} image(s) from {removed} pre-existing per-image "
            "iFDO sidecar(s), which were then removed.")


# -----------------------------------------------------------------------
# Entry points
# -----------------------------------------------------------------------
def fuse_frame_table(frame_table_path, camera_table_path, image_folder,
                      write_aux=True, write_xmp=False, write_native_metadata=True, write_ifdo=True,
                      write_per_image_ifdo=True,
                      write_biigle=False, distance_to_ground_override=None, area_override=None,
                      image_field_override=None, image_set_name=None, project_context=None,
                      ifdo_metadata=None, metadata_folder=None, existing_ifdo_items=None,
                      existing_set_uuid=None, log=print):
    """Fuse one Frame/Camera Table pair into its corresponding images.

    Raises if the frame table can't be read at all or has no recognizable
    image field - callers processing a single, explicit pair (a notebook
    cell, a geoprocessing tool run against one dataset) generally want that
    failure to surface directly. fuse_folder() below catches it instead, so
    one bad pair in a batch doesn't abort the rest.

    ifdo_metadata (dict, optional): user-supplied/template iFDO field values.
    These are set-wide defaults (image-license, image-platform, image-deployment,
    ...), so they are written once to the image-set-header (see
    build_ifdo_header()) rather than duplicated into every image's
    image-set-items entry. They are still merged into the aux.xml/EXIF/PNG
    per-image convenience blocks, which have no header/item concept of their own.

    write_per_image_ifdo: when False, iFDO item fields are still computed and
    returned but no per-image "<image>.ifdo.json" sidecar is written - this is
    what lets fuse_folder() build the single set-level document directly from
    memory instead of writing, re-reading and deleting one file per image.

    metadata_folder (optional): sibling directory (see fuse_folder) already
    holding *.ifdo.json sidecars from a prior run against a structured export
    layout - checked so image-set-uuid/image-uuid stay stable across reruns.

    Returns a dict with 'processed', 'skipped', and 'errors' counts, plus
    'ifdo_header' and 'ifdo_items' (filename -> item fields).
    """
    frame_rows = read_metadata_table(frame_table_path)
    if not frame_rows:
        log(f"  No rows in {frame_table_path} - nothing to do.")
        return {"processed": 0, "skipped": 0, "errors": 0,
                "ifdo_header": None, "ifdo_items": {}, "biigle_rows": []}

    field_names = list(frame_rows[0].keys())
    image_field = resolve_field(field_names, CANDIDATE_IMAGE_FIELDS, image_field_override)
    srs_field = resolve_field(field_names, CANDIDATE_SRS_FIELDS, required=False)
    lat_lon_fields = resolve_lat_lon_fields(field_names)
    camera_id_field = resolve_field(field_names, CANDIDATE_CAMERA_ID_FIELDS, required=False)
    log(f"  Using '{image_field}' as the image field.")

    camera_lookup = {}
    if camera_table_path:
        camera_rows = read_metadata_table(camera_table_path)
        if camera_rows:
            camera_field_names = list(camera_rows[0].keys())
            camera_id_key_field = resolve_field(camera_field_names, CANDIDATE_CAMERA_ID_FIELDS, required=False)
            if camera_id_key_field:
                for camera_row in camera_rows:
                    key = normalize_join_key(camera_row.get(camera_id_key_field))
                    if key is not None:
                        camera_lookup[key] = camera_row
            else:
                log(f"  Camera table {camera_table_path} has no recognizable CameraID field - skipping join.")

    write_native = write_native_metadata and PIL_AVAILABLE
    if write_native_metadata and not PIL_AVAILABLE:
        log("  Pillow is not available - JPEG/TIFF/PNG native metadata embedding will be "
            "skipped (aux.xml sidecars are unaffected).")

    ifdo_header = None
    if write_ifdo:
        set_uuid = existing_set_uuid or get_or_create_set_uuid(image_folder, metadata_folder)
        default_name = image_set_name or Path(frame_table_path).stem.replace("_FrameTable", "")
        ifdo_header = build_ifdo_header(image_folder, set_uuid, default_name, project_context,
                                         ifdo_metadata=ifdo_metadata, log=log)

    processed = skipped = errors = 0
    biigle_rows = []
    ifdo_items = {}

    for frame_row in frame_rows:
        image_path = find_image_path(frame_row.get(image_field), image_folder)
        if image_path is None:
            log(f"  Skipping - image not found for '{frame_row.get(image_field)}'")
            skipped += 1
            continue

        metadata = {f"Frame_{key}": value for key, value in frame_row.items()}

        camera_row = None
        if camera_id_field:
            key = normalize_join_key(frame_row.get(camera_id_field))
            if key is not None:
                camera_row = camera_lookup.get(key)
        if camera_row:
            metadata.update({f"Camera_{key}": value for key, value in camera_row.items()})

        ifdo_fields = build_ifdo_fields(frame_row, lat_lon_fields, srs_field, ifdo_metadata=ifdo_metadata)

        if write_biigle:
            biigle_rows.append(build_biigle_fields(
                image_path.name, frame_row, ifdo_fields,
                distance_to_ground_override=distance_to_ground_override,
                area_override=area_override,
            ))

        image_uuid = None
        if write_ifdo:
            existing_item = (existing_ifdo_items or {}).get(image_path.name)
            if not existing_item:
                existing_ifdo = _load_existing_ifdo(image_path.with_name(image_path.name + ".ifdo.json"))
                if existing_ifdo is None and metadata_folder:
                    existing_ifdo = _load_existing_ifdo(Path(metadata_folder) / (image_path.name + ".ifdo.json"))
                existing_item = (existing_ifdo or {}).get("image-set-items", {}).get(image_path.name, {})
            image_uuid = (existing_item or {}).get("image-uuid") or str(uuid.uuid4())

        # Nested under separate keys, never merged into `metadata` - an iFDO
        # field name and a raw Frame_/Camera_ field name are different
        # vocabularies describing the same value, not different values.
        # image-hash-sha256 is deliberately not included here - the comment
        # is embedded *into* the file, so it can't hash its own final bytes
        # (see the standalone .ifdo.json sidecar below for that field).
        comment_payload = {"frame_camera_table": metadata}
        if ifdo_fields or image_uuid:
            comment_ifdo = dict(ifdo_fields)
            if image_uuid:
                comment_ifdo["image-uuid"] = image_uuid
            comment_payload["ifdo"] = comment_ifdo

        try:
            if write_aux:
                write_aux_xml(image_path, metadata, domain="FrameCameraMetadata")
                if ifdo_fields:
                    write_aux_xml(image_path, ifdo_fields, domain="iFDO")

            # Shared by the XMP sidecar and native embedding below - computed
            # once, only when at least one of them actually needs it.
            if write_xmp or write_native:
                description = (frame_row.get("Filename") or frame_row.get("Name")
                               or frame_row.get(image_field) or image_path.stem)
                timestamp_formatted = parse_exif_datetime(get_timestamp_raw(frame_row))
                lat_lon = get_lat_lon(frame_row, lat_lon_fields, srs_field)
                altitude = get_altitude(frame_row)

            if write_xmp:
                write_xmp_sidecar(image_path, description, timestamp_formatted, lat_lon, altitude, comment_payload)

            suffix = image_path.suffix.lower()
            if write_native and suffix in EXTENSIONS_WITH_EXIF + EXTENSIONS_WITH_PNG_TEXT + EXTENSIONS_WITH_TIFF_TAGS:
                if suffix in EXTENSIONS_WITH_EXIF:
                    embed_jpeg_exif(image_path, description, timestamp_formatted, lat_lon, altitude, comment_payload)
                elif suffix in EXTENSIONS_WITH_TIFF_TAGS:
                    embed_tiff_metadata(image_path, description, timestamp_formatted, lat_lon, altitude, comment_payload)
                else:
                    embed_png_text(image_path, description, timestamp_formatted, lat_lon, altitude, comment_payload)

            if write_ifdo:
                # Computed last, after any native embedding above may have
                # rewritten the file, so the hash matches what's actually on
                # disk once this run finishes. Fields already promoted to the
                # set-wide header (ifdo_metadata) are excluded here so they
                # aren't duplicated into every single item entry.
                item_fields = {k: v for k, v in ifdo_fields.items()
                               if not (ifdo_metadata and k in ifdo_metadata)}
                item_fields["image-uuid"] = image_uuid
                item_fields["image-hash-sha256"] = compute_sha256(image_path)
                # Required by the schema; blank is the official example's own
                # representation of an image with no published handle yet.
                item_fields.setdefault("image-handle", "")
                ifdo_items[image_path.name] = item_fields
                if write_per_image_ifdo:
                    write_ifdo_sidecar(image_path, ifdo_header, item_fields)

            processed += 1
            log(f"  Wrote metadata for {image_path.name}")
        except Exception as exc:
            errors += 1
            log(f"  ERROR writing metadata for {image_path.name}: {exc}")

    return {"processed": processed, "skipped": skipped, "errors": errors,
            "biigle_rows": biigle_rows, "ifdo_header": ifdo_header,
            "ifdo_items": ifdo_items}


def fuse_folder(export_folder, write_aux=True, write_xmp=False, write_native_metadata=True, write_ifdo=True,
                 write_biigle=False, distance_to_ground_override=None, area_override=None,
                 image_set_name=None, aprx_path=None, ifdo_metadata=None,
                 ifdo_output_format=IFDO_FORMAT_SET_LEVEL,
                 export_output_folder=None, if_output_exists="Overwrite",
                 export_ifdo_template=False, ifdo_template_output=None,
                 ifdo_template_input=None, log=print):
    """Discover every Frame/Camera Table pair in export_folder and fuse each
    into the images alongside it.

    This is the entry point a notebook cell or a geoprocessing tool's
    execute() is expected to call - it takes a folder rather than an exact
    table path so a GUI tool can expose a single folder parameter instead of
    requiring the user to pick out individual CSVs.

    ifdo_metadata (dict, optional): user-supplied iFDO field values to merge
    into every image's iFDO sidecar/set - e.g., {'image-license': 'CC-BY-4.0',
    'image-deployment': 'ROV mission 2024'}. These values apply to all images
    in the folder. Can also be exported as a reusable template.

    ifdo_output_format: IFDO_FORMAT_SET_LEVEL (default - one
    "<image set name>.ifdo.json" for the whole set, written at the root of
    export_folder alongside the Frame/Camera Tables and BiigleMetadata.csv)
    or IFDO_FORMAT_PER_IMAGE (one "<image>.ifdo.json" beside each image).
    The set-level document is assembled directly from the values
    fuse_frame_table() already computes in memory - no per-image sidecar is
    written and re-read to produce it.

    export_output_folder (optional): User-designated output folder for complete
    deliverable with preserved structure (images/, metadata/, *.csv files).

    if_output_exists: "Overwrite", "Skip existing files", or "Fail" - controls
    behavior when export_output_folder already exists.

    export_ifdo_template / ifdo_template_output: Export current iFDO metadata
    values as a reusable template JSON for future runs.

    project_context (see get_arcgis_pro_context()) is resolved once here,
    not per table pair, and logged either way so it's clear whether
    image-pi/image-creators/image-set-name actually came from ArcGIS Pro or
    fell back to a default.
    """
    pairs = discover_table_pairs(export_folder)
    if not pairs:
        log(f"No *_FrameTable.csv files found in {export_folder}")
        return {"processed": 0, "skipped": 0, "errors": 0, "tables_failed": 0,
                "biigle_metadata_path": None, "ifdo_setlevel_path": None}

    # Detect if images are in a subdirectory (created by export_folder_structure).
    # This handles the case where export_folder_structure() created a structured
    # output with images/ and metadata/ subdirectories.
    image_folder = export_folder
    metadata_folder = None
    images_subdir = Path(export_folder) / EXPORT_FOLDER_IMAGES_DIR
    if images_subdir.is_dir():
        image_folder = str(images_subdir)
        metadata_folder = str(Path(export_folder) / EXPORT_FOLDER_METADATA_DIR)
        log(f"Using structured export layout: images found in {image_folder}")

    project_context = {}
    if write_ifdo:
        project_context = get_arcgis_pro_context(aprx_path)
        if project_context:
            log(f"iFDO: sourced from the ArcGIS Pro project - {sorted(project_context.keys())}")
        else:
            log("iFDO: no ArcGIS Pro project context available (not running inside Pro, no aprx_path "
                "given, or nothing populated in Project Properties) - falling back to defaults for "
                "image-set-name/image-pi/image-creators.")

    set_level_ifdo = write_ifdo and resolve_ifdo_format(ifdo_output_format) == IFDO_FORMAT_SET_LEVEL
    existing_header, existing_ifdo_items = (
        _load_existing_setlevel_document(export_folder) if set_level_ifdo else ({}, {})
    )

    totals = {"processed": 0, "skipped": 0, "errors": 0, "tables_failed": 0,
              "biigle_metadata_path": None, "ifdo_setlevel_path": None}
    all_biigle_rows = []
    all_ifdo_items = {}
    set_ifdo_header = None
    for frame_table, camera_table in pairs:
        pairing_note = f" with {camera_table.name}" if camera_table else " (no camera table found)"
        log(f"Processing {frame_table.name}{pairing_note}")
        try:
            result = fuse_frame_table(frame_table, camera_table, image_folder,
                                       write_aux=write_aux, write_xmp=write_xmp,
                                       write_native_metadata=write_native_metadata,
                                       write_ifdo=write_ifdo,
                                       write_per_image_ifdo=write_ifdo and not set_level_ifdo,
                                       write_biigle=write_biigle,
                                       distance_to_ground_override=distance_to_ground_override,
                                       area_override=area_override,
                                       image_set_name=image_set_name,
                                       project_context=project_context, ifdo_metadata=ifdo_metadata,
                                       metadata_folder=metadata_folder,
                                       existing_ifdo_items=existing_ifdo_items,
                                       existing_set_uuid=existing_header.get("image-set-uuid"),
                                       log=log)
        except Exception as exc:
            log(f"  ERROR processing {frame_table.name}: {exc}")
            totals["tables_failed"] += 1
            continue
        for key in ("processed", "skipped", "errors"):
            totals[key] += result[key]
        all_biigle_rows.extend(result.get("biigle_rows", []))
        # Filename keys de-dupe the known case of two Frame Tables describing
        # one physical image set.
        all_ifdo_items.update(result.get("ifdo_items") or {})
        if set_ifdo_header is None and result.get("ifdo_header"):
            set_ifdo_header = result["ifdo_header"]

    if write_biigle and all_biigle_rows:
        totals["biigle_metadata_path"] = write_biigle_metadata_csv(
            all_biigle_rows, Path(export_folder) / "BiigleMetadata.csv", log=log
        )

    log(f"\nDone. {totals['processed']} image(s) updated, {totals['skipped']} skipped, "
        f"{totals['errors']} error(s), {totals['tables_failed']} table pair(s) failed entirely.")

    # Phase 2a: Move sidecar files to metadata subdirectory if structured export layout detected
    # In structured exports, sidecars are written next to images but belong in metadata/
    images_subdir = Path(export_folder) / EXPORT_FOLDER_IMAGES_DIR
    metadata_subdir = Path(export_folder) / EXPORT_FOLDER_METADATA_DIR
    if images_subdir.is_dir() and metadata_subdir.is_dir():
        # Move *.aux.xml, *.xmp, and *.ifdo.json files from images/ to metadata/
        for ext_pattern in ["*.aux.xml", "*.xmp", "*.ifdo.json"]:
            for sidecar_file in images_subdir.glob(ext_pattern):
                try:
                    dest = metadata_subdir / sidecar_file.name
                    shutil.move(str(sidecar_file), str(dest))
                except Exception as exc:
                    log(f"  WARNING: Could not move {sidecar_file.name} to metadata/: {exc}")

    # Phase 2b: Write the single set-level iFDO document. It is built from the
    # items fuse_frame_table() returned in memory, so it does not depend on
    # where (or whether) any per-image sidecar was written, and it lands at the
    # export root next to the Frame/Camera Tables and BiigleMetadata.csv.
    if set_level_ifdo:
        _absorb_stray_ifdo_sidecars(export_folder, all_ifdo_items, log=log)
        # Carry forward entries from a prior run whose image is still on disk,
        # so re-running against one Frame Table doesn't drop images another
        # Frame Table contributed to the same set earlier.
        for image_name, fields in existing_ifdo_items.items():
            if image_name not in all_ifdo_items and find_image_path(image_name, image_folder):
                all_ifdo_items[image_name] = fields
        if all_ifdo_items:
            set_name = (image_set_name
                        or (set_ifdo_header or {}).get("image-set-name")
                        or "image-set")
            totals["ifdo_setlevel_path"] = write_ifdo_setlevel(
                export_folder, set_name, set_ifdo_header or {}, all_ifdo_items, log=log
            )
        else:
            log("  WARNING: iFDO single-file output was requested but no image "
                "produced iFDO fields - no set-level document written.")

    # Phase 2c/4: Optional exports (template and folder archive)
    if export_ifdo_template and ifdo_template_output and ifdo_metadata:
        _write_ifdo_template_file(ifdo_metadata, ifdo_template_output)
        log(f"iFDO metadata template exported to {ifdo_template_output}")

    if export_output_folder:
        export_folder_structure(export_folder, export_output_folder,
                               if_exists=if_output_exists, exclude_file=ifdo_template_input, log=log)

    return totals


if __name__ == "__main__":
    EXPORT_FOLDER = r"C:\path\to\output"
    fuse_folder(EXPORT_FOLDER)
