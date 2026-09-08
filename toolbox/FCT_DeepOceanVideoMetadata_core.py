# -*- coding: utf-8 -*-
"""
FCT_DeepOceanVideoMetadata_core.py

Reference implementation for "Generate Deep Ocean Video Metadata" - builds a
video metadata CSV from a navigation/telemetry log for a MOVING platform
(ROV/AUV/towed sled/drop camera), filling FMV (Full Motion Video) fields a
raw nav log usually lacks (camera pitch/roll/heading/FOV/near-far/height)
from a selectable Video Acquisition Profile, then runs the result through
Esri's Convert Video Metadata (Image Analyst, REQUIRED) to produce a Video
Multiplexer-ready output - see run_convert_video_metadata(). Esri's own
"Generate Video Metadata (Stationary)" only handles a single fixed sensor,
which is the gap this module fills; Convert Video Metadata's own field-
matching/reformatting job is delegated to it directly, not reimplemented.

X, Y, and Timestamp are required (the minimum to place/time-order a row for
the multiplexer); everything else degrades to the profile default, a manual
override, or (heading only) is computed from consecutive track positions.

Output: VIDEO_METADATA_TABLE_FIELDS uses Convert Video Metadata's own
Target Field names verbatim, so no Input Field Matching table is needed for
that pass. Not a replacement for Extract Video Frames To Images' Frame/
Camera Table pair - frame extraction and mosaic/OID loading are handled by
other tools in this toolbox.

Like FCT_ImageFusion_core.py, every function takes explicit arguments and
reports progress via an injected `log` callable instead of calling arcpy
messaging directly; a bad telemetry row is skipped and counted, never abort.
"""
import csv
import importlib.util
import inspect
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import arcpy

# Safe import of FCT_ImageFusion_core.py (same folder) - reuses its
# value-normalization/table-reading helpers rather than duplicating them.
_THIS_DIR = Path(__file__).parent.absolute()
_FUSION_CORE_PATH = _THIS_DIR / "FCT_ImageFusion_core.py"
if not _FUSION_CORE_PATH.exists():
    raise FileNotFoundError(
        f"FCT_ImageFusion_core.py not found in {_THIS_DIR}. "
        "All FCT_*.py modules must be shipped together in the same folder."
    )
_spec = importlib.util.spec_from_file_location("FCT_ImageFusion_core", str(_FUSION_CORE_PATH))
fusion_core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fusion_core)

is_null_like = fusion_core.is_null_like
to_float = fusion_core.to_float
resolve_field = fusion_core.resolve_field


# Constants
# Includes both raw-telemetry-log names AND this project's own MISB target
# field names (VIDEO_METADATA_TABLE_FIELDS below / the Multiplexer's own
# metadata CSV) - so resolve_telemetry_fields() (used by Tool 2 to read a
# raw log AND by Tool 6/Inspect Video and Sensor Data to read either a raw
# log OR this project's own already-converted video metadata table) works
# against EITHER input, not just a raw nav log's looser column names.
CANDIDATE_X_FIELDS = [
    "X", "Longitude", "Lon", "Long", "Easting", "Platform Longitude", "E_ROV_INS",
    "Sensor Longitude",
]
CANDIDATE_Y_FIELDS = [
    "Y", "Latitude", "Lat", "Northing", "Platform Latitude", "N_ROV_INS",
    "Sensor Latitude",
]
CANDIDATE_Z_FIELDS = [
    "Z", "Depth", "Altitude", "Elevation", "Height", "Sensor Altitude", "ALT",
    "Sensor True Altitude", "Sensor Ellipsoid Height Extended",
]
CANDIDATE_TIMESTAMP_FIELDS = [
    "Timestamp", "Time", "DateTime", "Date_Time", "UTC", "Precision Time Stamp"
]
# Platform* listed first - the platform's own attitude (IMU/nav feed), as
# distinct from the profile's Sensor Relative Azimuth/Elevation/Roll Angle
# (camera gimbal angle). "CameraHeading"/"CameraPitch"/"CameraRoll" still
# match as a fallback for logs with only one estimate. The "... Angle
# (Full)"/"... Angle" forms are this project's own MISB target field names
# (see the module docstring above).
CANDIDATE_HEADING_FIELDS = [
    "PlatformHeading", "Heading", "CameraHeading", "Yaw", "Bearing", "COG", "Course",
    "Platform Heading Angle",
]
CANDIDATE_PITCH_FIELDS = [
    "PlatformPitch", "Pitch", "CameraPitch", "Tilt", "Platform Pitch Angle (Full)",
]
CANDIDATE_ROLL_FIELDS = [
    "PlatformRoll", "Roll", "CameraRoll", "Platform Roll Angle (Full)",
]
CANDIDATE_FILENAME_FIELDS = ["Filename", "Frame", "Image", "ImageName", "FrameFile"]

HEADING_MODE_FROM_TRACK = "from_track"
HEADING_MODE_CONSTANT = "constant"

CUSTOM_PROFILE_NAME = "Custom (define manually below)"

# Deliberately opinionated defaults per deep-ocean platform type, modeled
# after Oriented Imagery Dataset per-category defaults and Generate Video
# Metadata (Stationary)'s Tilt/Relative Azimuth/HFOV/VFOV/Far Distance.
# camera_pitch: 0 = nadir (straight down), 90 = horizon.
VIDEO_ACQUISITION_PROFILES = {
    "ROV - Down-looking (Nadir) Camera": {
        "description": "ROV-mounted camera looking straight down at the seafloor.",
        "camera_pitch": 0.0, "camera_roll": 0.0,
        "hfov": 48.0, "vfov": 36.0,
        "camera_height": 2.0, "near_distance": 0.3, "far_distance": 8.0,
        "heading_mode": HEADING_MODE_FROM_TRACK, "heading_constant": 0.0,
    },
    "ROV - Forward-Oblique Camera": {
        "description": "ROV-mounted camera angled forward/down - the most common ROV dive-cam setup.",
        "camera_pitch": 45.0, "camera_roll": 0.0,
        "hfov": 60.0, "vfov": 45.0,
        "camera_height": 3.0, "near_distance": 0.5, "far_distance": 10.0,
        "heading_mode": HEADING_MODE_FROM_TRACK, "heading_constant": 0.0,
    },
    "AUV - Nadir Survey Camera": {
        "description": "Autonomous underwater vehicle flying a fixed-altitude nadir survey line.",
        "camera_pitch": 0.0, "camera_roll": 0.0,
        "hfov": 50.0, "vfov": 38.0,
        "camera_height": 3.0, "near_distance": 0.5, "far_distance": 12.0,
        "heading_mode": HEADING_MODE_FROM_TRACK, "heading_constant": 0.0,
    },
    "Towed Camera Sled - Nadir": {
        "description": "Towed/deep-tow camera sled, nadir-mounted, altitude held by tow-wire length.",
        "camera_pitch": 0.0, "camera_roll": 0.0,
        "hfov": 45.0, "vfov": 34.0,
        "camera_height": 2.5, "near_distance": 0.5, "far_distance": 8.0,
        "heading_mode": HEADING_MODE_FROM_TRACK, "heading_constant": 0.0,
    },
    "Drop / Lander Camera - Stationary Nadir": {
        "description": "Stationary drop camera or benthic lander - no horizontal track, heading is fixed.",
        "camera_pitch": 0.0, "camera_roll": 0.0,
        "hfov": 48.0, "vfov": 36.0,
        "camera_height": 1.5, "near_distance": 0.3, "far_distance": 5.0,
        "heading_mode": HEADING_MODE_CONSTANT, "heading_constant": 0.0,
    },
    "Diver / Handheld Camera": {
        "description": "Diver-operated or handheld camera, moderate forward tilt.",
        "camera_pitch": 30.0, "camera_roll": 0.0,
        "hfov": 60.0, "vfov": 45.0,
        "camera_height": 1.0, "near_distance": 0.2, "far_distance": 4.0,
        "heading_mode": HEADING_MODE_FROM_TRACK, "heading_constant": 0.0,
    },
    CUSTOM_PROFILE_NAME: {
        "description": "All values must be supplied manually via the override parameters.",
        "camera_pitch": None, "camera_roll": None,
        "hfov": None, "vfov": None,
        "camera_height": None, "near_distance": None, "far_distance": None,
        "heading_mode": HEADING_MODE_CONSTANT, "heading_constant": None,
    },
}

# Column order for the output table, using Esri's own MISB target field
# names (Convert Video Metadata's Target Fields / Multiplexer's essential-13)
# so it's directly Multiplexer-ready. Platform Heading/Pitch/Roll Angle =
# the platform's own attitude (telemetry, falling back to computed track
# bearing or the profile's heading_constant for heading); Sensor Relative
# Azimuth/Elevation/Roll Angle = the camera gimbal's angle relative to the
# platform, always from the profile/override (Azimuth has no data source in
# this project - every profile assumes a forward/nadir-fixed mount - so
# it's always 0.0). CameraNCols/NRows/FocalLength/PixelSize/NearDistance/
# CameraHeight are this project's own extras (one camera per run, repeated
# per row instead of a separate camera table).
VIDEO_METADATA_TABLE_FIELDS = [
    "ObjectID", "Filename", "Precision Time Stamp", "AcquisitionDate",
    "Sensor Longitude", "Sensor Latitude", "Sensor True Altitude", "Sensor Ellipsoid Height Extended",
    "CameraID", "CameraNCols", "CameraNRows", "CameraFocalLength", "CameraPixelSize",
    "Platform Heading Angle", "Platform Pitch Angle (Full)", "Platform Roll Angle (Full)",
    "Sensor Relative Azimuth Angle", "Sensor Relative Elevation Angle", "Sensor Relative Roll Angle",
    "Sensor Horizontal Field of View", "Sensor Vertical Field of View",
    "Near Distance", "Far Distance", "Camera Height Above Seafloor",
]

# arcpy field type per column, for the geodatabase-table write path.
# Precision Time Stamp is LONG (integer microseconds since epoch, the
# format Multiplexer/MISB require) - AcquisitionDate keeps a human-readable
# DATE copy for QA.
VIDEO_METADATA_FIELD_TYPES = {
    "ObjectID": "LONG", "Filename": "TEXT", "Precision Time Stamp": "LONG",
    "AcquisitionDate": "DATE",
    "Sensor Longitude": "DOUBLE", "Sensor Latitude": "DOUBLE", "Sensor True Altitude": "DOUBLE",
    "Sensor Ellipsoid Height Extended": "DOUBLE",
    "CameraID": "LONG", "CameraNCols": "LONG", "CameraNRows": "LONG",
    "CameraFocalLength": "DOUBLE", "CameraPixelSize": "DOUBLE",
    "Platform Heading Angle": "DOUBLE", "Platform Pitch Angle (Full)": "DOUBLE",
    "Platform Roll Angle (Full)": "DOUBLE",
    "Sensor Relative Azimuth Angle": "DOUBLE", "Sensor Relative Elevation Angle": "DOUBLE",
    "Sensor Relative Roll Angle": "DOUBLE",
    "Sensor Horizontal Field of View": "DOUBLE", "Sensor Vertical Field of View": "DOUBLE",
    "Near Distance": "DOUBLE", "Far Distance": "DOUBLE", "Camera Height Above Seafloor": "DOUBLE",
}

# This tool computes these itself - ObjectID is the row index, the two time
# fields come from the parsed timestamp, and lon/lat are reprojected to WGS84.
# Mapping a telemetry column onto one of them would contradict the row it
# belongs to, so they are not offered as mapping targets.
RESERVED_METADATA_FIELDS = frozenset({
    "ObjectID", "Precision Time Stamp", "AcquisitionDate",
    "Sensor Longitude", "Sensor Latitude",
})

MAPPABLE_METADATA_FIELDS = [
    name for name in VIDEO_METADATA_TABLE_FIELDS if name not in RESERVED_METADATA_FIELDS
]


def coerce_to_field_type(value, target_field):
    """Cast a raw telemetry value to the output column's declared type,
    returning None when it cannot be represented."""
    if is_null_like(value):
        return None
    field_type = VIDEO_METADATA_FIELD_TYPES.get(target_field, "TEXT")
    if field_type == "DOUBLE":
        return to_float(value)
    if field_type == "LONG":
        number = to_float(value)
        return None if number is None else int(number)
    return str(value)


def normalize_field_mappings(mappings, field_names=None, log=print):
    """Validate user-supplied (telemetry column -> metadata field) pairs.

    Accepts a dict or any sequence of two-item pairs. Every rejection is
    logged rather than raised: a bad mapping row should cost the user that
    one field, not the whole run.
    """
    if not mappings:
        return []
    pairs = mappings.items() if isinstance(mappings, dict) else mappings
    known_targets = {name.lower(): name for name in MAPPABLE_METADATA_FIELDS}
    available = {str(name).lower() for name in (field_names or [])}

    normalized = []
    for pair in pairs:
        try:
            source, target = pair
        except (TypeError, ValueError):
            log(f"WARNING: ignoring malformed field mapping entry: {pair!r}")
            continue
        if is_null_like(source) or is_null_like(target):
            continue
        source, target = str(source).strip(), str(target).strip()
        resolved_target = known_targets.get(target.lower())
        if resolved_target is None:
            if target in RESERVED_METADATA_FIELDS:
                log(f"WARNING: '{target}' is computed by this tool and cannot be mapped to - "
                    f"ignoring the mapping from '{source}'.")
            else:
                log(f"WARNING: '{target}' is not a video metadata field - ignoring the mapping "
                    f"from '{source}'.")
            continue
        if available and source.lower() not in available:
            log(f"WARNING: telemetry column '{source}' is not in the input table - "
                f"'{resolved_target}' will keep its existing value.")
            continue
        normalized.append((source, resolved_target))
    return normalized


def build_profile(profile_name, manual_overrides=None):
    """Merge a named Video Acquisition Profile with any non-empty manual overrides.

    manual_overrides values of None are ignored (profile default wins); any
    other value replaces the profile default for that key.
    """
    if profile_name not in VIDEO_ACQUISITION_PROFILES:
        raise ValueError(
            f"Unknown video acquisition profile '{profile_name}'. "
            f"Available profiles: {list(VIDEO_ACQUISITION_PROFILES.keys())}"
        )
    profile = dict(VIDEO_ACQUISITION_PROFILES[profile_name])
    profile["name"] = profile_name
    for key, value in (manual_overrides or {}).items():
        if value is not None:
            profile[key] = value
    return profile


def import_profile_template(template_path):
    """Load a Video Acquisition Profile previously exported by this tool."""
    with open(template_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if "profile" not in data or not isinstance(data["profile"], dict):
        raise ValueError(
            "Invalid Video Acquisition Profile template: missing a 'profile' object."
        )
    return data["profile"]


def export_profile_template(profile, output_path):
    """Write a Video Acquisition Profile to a reusable JSON template file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump({"profile": profile}, handle, indent=2)
    return str(output_path)


# Telemetry ingestion
def resolve_table_path(table):
    """Return a real catalog path for whatever a table parameter handed over.

    A GPTableView yields a table view name rather than a path when the table was
    picked from the map, and a CSV added to a map keeps its .csv extension in that
    name - so testing the extension alone will cheerfully try to open something
    that is not a file. Describe() resolves the view back to its source.
    """
    text = str(table)
    if Path(text).exists():
        return text
    try:
        described = arcpy.Describe(table)
    except (OSError, RuntimeError, AttributeError, ValueError):
        return text
    return str(getattr(described, "catalogPath", "") or text)


def read_telemetry_table(table_path):
    """Read a telemetry table (CSV/TXT, geodatabase table, dBASE, or Excel
    worksheet - anything arcpy recognizes as a table) into a list of dicts."""
    rows = fusion_core.read_metadata_table(resolve_table_path(table_path))
    if not rows:
        raise ValueError(f"Telemetry table has no rows: {table_path}")
    return rows


def read_telemetry_header(table_path):
    """Read just the field names of a telemetry table, so the tool dialog
    (updateParameters/updateMessages) can show/validate available fields
    without reading the whole (potentially large) table."""
    resolved = resolve_table_path(table_path)
    # Reading the file directly preserves the header verbatim; ListFields would
    # hand back names ArcGIS has sanitised, which then fail to match the raw rows.
    if Path(resolved).is_file() and resolved.lower().endswith((".csv", ".txt")):
        with open(resolved, newline="", encoding="utf-8-sig") as csv_file:
            reader = csv.reader(csv_file)
            try:
                header = next(reader)
            except StopIteration:
                raise ValueError(f"Telemetry table has no header row: {resolved}")
            return [name.strip() for name in header]

    return [f.name for f in arcpy.ListFields(resolved) if f.type not in ("Geometry", "Blob", "Raster")]


# X/Y/Timestamp are required (place/time-order a row for the multiplexer);
# Z/heading/pitch/roll/filename are optional.
TELEMETRY_FIELD_CANDIDATES = {
    "x": (CANDIDATE_X_FIELDS, True),
    "y": (CANDIDATE_Y_FIELDS, True),
    "z": (CANDIDATE_Z_FIELDS, False),
    "timestamp": (CANDIDATE_TIMESTAMP_FIELDS, True),
    "heading": (CANDIDATE_HEADING_FIELDS, False),
    "pitch": (CANDIDATE_PITCH_FIELDS, False),
    "roll": (CANDIDATE_ROLL_FIELDS, False),
    "filename": (CANDIDATE_FILENAME_FIELDS, False),
}


def resolve_telemetry_fields(field_names, overrides=None):
    """Resolve X/Y/Timestamp (required) and Z/heading/pitch/roll/filename
    (optional) column names from a telemetry table's header, honoring any
    user-supplied override names."""
    overrides = overrides or {}
    return {
        key: resolve_field(field_names, candidates, overrides.get(key), required=required)
        for key, (candidates, required) in TELEMETRY_FIELD_CANDIDATES.items()
    }


def preview_resolve_telemetry_fields(field_names, overrides=None):
    """Like resolve_telemetry_fields(), but never raises - every key is
    resolved with required=False, returning None for anything unresolved.
    For dialog-time previews; a missing required field should surface as a
    parameter error message instead of an exception."""
    overrides = overrides or {}
    resolved = {}
    for key, (candidates, _required) in TELEMETRY_FIELD_CANDIDATES.items():
        try:
            resolved[key] = resolve_field(field_names, candidates, overrides.get(key), required=False)
        except KeyError:
            resolved[key] = None
    return resolved


# Non-ISO formats seen in real telemetry logs this tool has been run
# against (e.g. a real ROV telemetry export uses "1/9/2022 1:28:32" and
# "1/9/2022 1:28:32 AM") - tried after ISO 8601 fails, in this order.
_TIMESTAMP_STRPTIME_FORMATS = (
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%m-%d-%Y %H:%M:%S",
)


def parse_any_timestamp(raw_value):
    """Best-effort timestamp parse: numeric epoch (any precision, via
    fusion_core.parse_datetime) first, then ISO 8601 text, then a handful of
    common non-ISO date/time formats (see _TIMESTAMP_STRPTIME_FORMATS).
    Returns None (never raises) if raw_value can't be parsed by any of them.
    """
    if is_null_like(raw_value):
        return None
    if isinstance(raw_value, datetime):
        # A Date-type field read via arcpy.da.SearchCursor (gdb table,
        # dBASE, Excel worksheet) - already a real datetime, not text.
        return raw_value if raw_value.tzinfo else raw_value.replace(tzinfo=timezone.utc)
    parsed = fusion_core.parse_datetime(raw_value)
    if parsed is not None:
        return parsed
    text = str(raw_value).strip()
    for candidate in (text, text.replace(" ", "T", 1), text.replace("Z", "+00:00")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    for fmt in _TIMESTAMP_STRPTIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

# Geometry helpers
def compute_bearing(x1, y1, x2, y2):
    """Bearing in degrees clockwise from north, from point 1 to point 2.

    Uses a flat-plane approximation (atan2 of the coordinate deltas), which
    is adequate for the short frame-to-frame distances typical of a single
    dive/deployment. For long-range surveys, supplying an input coordinate
    system that is locally projected (rather than geographic) improves
    accuracy.
    """
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return None
    return math.degrees(math.atan2(dx, dy)) % 360.0


# Outermost ID[...] clause of a CRS's WKT (its own EPSG/ESRI code, as
# opposed to a nested datum/ellipsoid ID) - last-resort fallback when
# arcpy.SpatialReference() can't parse the WKT text itself.
_EPSG_ID_PATTERN = re.compile(r'ID\["(?:EPSG|ESRI)",\s*"?(\d+)"?\]')


def _coerce_spatial_reference(value, default_factory_code=4326, label="coordinate system", log=print):
    """Return a real arcpy.SpatialReference from a GPCoordinateSystem
    parameter's value - a plain WKT/WKID string, a real SpatialReference, or
    (rarely) an opaque 'ValueObject' wrapper (seen when invoked as a direct
    Python function call rather than through the Pro GUI) whose str()/
    exportToString() may not yield parseable WKT. Any fallback to
    default_factory_code is logged as a WARNING, never silent - a
    wrong-but-valid default CRS produces no exception, just silently-wrong
    coordinates.
    """
    if value is None or value == "":
        return arcpy.SpatialReference(default_factory_code)
    try:
        _ = value.factoryCode
        return value
    except AttributeError:
        pass

    # Prefer exportToString() (the documented, round-trip-safe way to get a
    # SpatialReference-like object's WKT) over a bare str(), which some
    # internal GP 'ValueObject' wrappers do not render as valid WKT.
    text = None
    export = getattr(value, "exportToString", None)
    if callable(export):
        try:
            text = export()
        except Exception:
            text = None
    if not text:
        text = str(value)

    try:
        sr = arcpy.SpatialReference(text)
        if not sr.factoryCode and sr.name in (None, "", "Unknown"):
            raise ValueError(f"arcpy.SpatialReference() produced an unset/'Unknown' result from {text!r}")
        return sr
    except Exception as exc:
        # Last resort: some CRSs (e.g. a "dynamic"/time-dependent datum's
        # WKT2) aren't parseable by arcpy.SpatialReference(text) directly -
        # pull the EPSG/ESRI code embedded in the WKT itself instead.
        match = _EPSG_ID_PATTERN.search(text) if text else None
        if match:
            try:
                return arcpy.SpatialReference(int(match.group(1)))
            except Exception:
                pass
        log(f"WARNING: could not interpret the '{label}' parameter value "
            f"({type(value).__name__}: {value!r}) as a spatial reference - falling back to "
            f"EPSG:{default_factory_code}. This likely produces WRONG projected coordinates if "
            f"that isn't actually the CRS your data is in. Underlying error: {exc}")
        return arcpy.SpatialReference(default_factory_code)


def project_point(x, y, in_sr, out_sr):
    """Project (x, y) from in_sr to out_sr using arcpy geometry (no
    extension license required). Returns (x, y) unchanged if the two
    spatial references have the same factory code."""
    if in_sr is None or out_sr is None or in_sr.factoryCode == out_sr.factoryCode:
        return x, y
    point = arcpy.PointGeometry(arcpy.Point(x, y), in_sr)
    projected = point.projectAs(out_sr)
    if projected is None or projected.firstPoint is None:
        # arcpy silently returns a null geometry (no exception) when x/y are
        # out of range for in_sr - e.g. large projected-meter values fed in
        # as if they were geographic degrees - so raise a clear, actionable
        # error here instead of letting the caller hit an AttributeError on
        # firstPoint.X against None.
        raise ValueError(
            f"Could not project telemetry point ({x}, {y}) from '{in_sr.name}' "
            f"(factory code {in_sr.factoryCode}) to '{out_sr.name}'. arcpy returned an "
            "empty result, which usually means the X/Y values are out of range for the "
            "declared 'Coordinate System of Telemetry X/Y' - e.g. projected-meter "
            "coordinates supplied with a geographic (degrees) input CRS. Check that "
            "input_srs actually matches the units/datum of the telemetry table's X/Y "
            "columns."
        )
    return projected.firstPoint.X, projected.firstPoint.Y


# Resampling (optional fixed-cadence interpolation)
def resample_records(records, interval_seconds):
    """Linearly interpolate x/y/z at a fixed time step between the first and
    last timestamped record. Records without a parsed timestamp are dropped
    for this step (resampling requires a time axis); if fewer than two
    timestamped records remain, the original records are returned as-is."""
    timed = [r for r in records if r.get("_dt") is not None]
    if len(timed) < 2:
        return records
    timed.sort(key=lambda r: r["_dt"])

    start = timed[0]["_dt"]
    end = timed[-1]["_dt"]
    total_seconds = (end - start).total_seconds()
    if total_seconds <= 0:
        return records

    resampled = []
    # Number of sample points from 0 to total_seconds inclusive, at a fixed
    # interval - e.g. total_seconds=100, interval=10 gives 11 points (0..100).
    point_count = int(total_seconds // interval_seconds) + 1
    for i in range(point_count):
        target_seconds = min(i * interval_seconds, total_seconds)
        target_dt = start + timedelta(seconds=target_seconds)

        # Find bracketing samples for linear interpolation
        before = timed[0]
        after = timed[-1]
        for j in range(len(timed) - 1):
            if timed[j]["_dt"] <= target_dt <= timed[j + 1]["_dt"]:
                before, after = timed[j], timed[j + 1]
                break

        span = (after["_dt"] - before["_dt"]).total_seconds()
        frac = 0.0 if span <= 0 else (target_dt - before["_dt"]).total_seconds() / span

        def lerp(key):
            # Z is optional, and a log with no Z at all would otherwise fail here
            # on None arithmetic rather than simply carrying the gap through.
            start_value, end_value = before[key], after[key]
            if start_value is None or end_value is None:
                return start_value if end_value is None else end_value
            return start_value + (end_value - start_value) * frac

        new_record = dict(before)
        new_record["x"] = lerp("x")
        new_record["y"] = lerp("y")
        new_record["z"] = lerp("z")
        new_record["_dt"] = target_dt
        # Replaces the "before" sample's stale raw timestamp so downstream
        # Precision Time Stamp / AcquisitionDate reflect the resampled time.
        new_record["timestamp_raw"] = target_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        new_record["heading"] = None  # recomputed from track after resampling
        resampled.append(new_record)

    return resampled


# Video metadata table construction
def build_video_metadata_table(
    telemetry_table_path,
    output_folder,
    output_name,
    profile_name,
    field_overrides=None,
    manual_overrides=None,
    input_srs=None,
    resample_interval_seconds=None,
    z_constant=None,
    z_is_ellipsoid_height=False,
    extra_field_mappings=None,
    camera_id=1,
    camera_ncols=None,
    camera_nrows=None,
    camera_focal_length=None,
    camera_pixel_size=None,
    log=print,
):
    """Build a video metadata table from a telemetry log, then run it
    through Convert Video Metadata to produce the Multiplexer-ready CSV +
    its required mapping file, returning a summary dict. Requires Image
    Analyst to already be checked out by the caller (see the .pyt's
    execute()).

    X, Y, and Timestamp are required (place/time-order a row for the
    downstream multiplexer). Z (written as-is to Sensor True Altitude - no
    unit/sign conversion) and platform heading/pitch/roll are used when
    present; the camera's Sensor Relative Azimuth/Elevation/Roll Angle
    always come from the selected Video Acquisition Profile (or its manual
    override), not telemetry - they describe the gimbal's angle relative to
    the platform, not the platform's own attitude. Field-matching/unit
    conversion of telemetry columns is Convert Video Metadata's job, not
    reimplemented here - only X/Y, needed for the WGS84 reprojection
    Convert Video Metadata itself can't do, are resolved by this tool.
    """
    input_srs = _coerce_spatial_reference(
        input_srs, default_factory_code=4326, label="Coordinate System of Telemetry X/Y", log=log
    )  # WGS 1984
    wgs84 = arcpy.SpatialReference(4326)

    raw_rows = read_telemetry_table(telemetry_table_path)
    if not raw_rows:
        raise ValueError(f"Telemetry table is empty: {telemetry_table_path}")

    field_names = list(raw_rows[0].keys())
    resolved = resolve_telemetry_fields(field_names, field_overrides)
    profile = build_profile(profile_name, manual_overrides)
    log(f"Resolved telemetry fields: {resolved}")
    log(f"Using Video Acquisition Profile '{profile_name}': {profile.get('description', '')}")
    mappings = normalize_field_mappings(extra_field_mappings, field_names, log=log)
    if mappings:
        log("Additional field mappings: "
            + ", ".join(f"{source} -> {target}" for source, target in mappings))
    if z_is_ellipsoid_height:
        log("Treating the telemetry Z as a height above the ellipsoid: it is written to "
            "Sensor Ellipsoid Height Extended as well as Sensor True Altitude.")

    # --- Normalize rows, skipping any missing the required X/Y/Timestamp ---
    records = []
    skipped = 0
    for raw_row in raw_rows:
        x = to_float(raw_row.get(resolved["x"]))
        y = to_float(raw_row.get(resolved["y"]))
        timestamp_raw = raw_row.get(resolved["timestamp"])
        dt = parse_any_timestamp(timestamp_raw) if not is_null_like(timestamp_raw) else None
        if x is None or y is None or dt is None:
            skipped += 1
            continue
        z = to_float(raw_row.get(resolved["z"])) if resolved["z"] else None
        records.append({
            "x": x, "y": y, "z": z,
            "_dt": dt,
            "timestamp_raw": timestamp_raw,
            "heading": to_float(raw_row.get(resolved["heading"])) if resolved["heading"] else None,
            "pitch": to_float(raw_row.get(resolved["pitch"])) if resolved["pitch"] else None,
            "roll": to_float(raw_row.get(resolved["roll"])) if resolved["roll"] else None,
            "filename": raw_row.get(resolved["filename"]) if resolved["filename"] else None,
            "_mapped": {
                target: coerce_to_field_type(raw_row.get(source), target)
                for source, target in mappings
            },
        })
    if skipped:
        log(f"WARNING: skipped {skipped} telemetry row(s) missing X, Y, or a parseable Timestamp")
    if not records:
        raise ValueError("No telemetry rows had usable X, Y, and Timestamp values.")

    # --- Order by timestamp (always present now) ---
    records.sort(key=lambda r: r["_dt"])

    # --- Fill Z from the supplied constant where the telemetry has none ---
    # Ahead of resampling, so interpolation has real values at both ends.
    if z_constant is not None:
        filled = [record for record in records if record["z"] is None]
        for record in filled:
            record["z"] = z_constant
        if filled:
            log(f"Applied the constant Z value {z_constant} to {len(filled)} row(s) that had "
                "no Z in the telemetry.")
    elif all(record["z"] is None for record in records):
        log("WARNING: no Z column was resolved and no constant Z was supplied - Sensor True "
            "Altitude will be empty on every row.")

    # --- Optional resampling to a fixed time interval ---
    if resample_interval_seconds:
        before_count = len(records)
        records = resample_records(records, resample_interval_seconds)
        log(f"Resampled {before_count} telemetry row(s) to {len(records)} row(s) "
            f"at a {resample_interval_seconds}s interval")

    # --- Fill platform heading from track when the profile calls for it ---
    if profile["heading_mode"] == HEADING_MODE_FROM_TRACK:
        for i, record in enumerate(records):
            if record["heading"] is not None:
                continue
            if i + 1 < len(records):
                record["heading"] = compute_bearing(
                    record["x"], record["y"], records[i + 1]["x"], records[i + 1]["y"]
                )
            elif i > 0:
                record["heading"] = records[i - 1]["heading"]
    for record in records:
        if record["heading"] is None:
            record["heading"] = profile.get("heading_constant")

    # --- Build video metadata rows ---
    # Periodic progress logging at a computed increment (not every row) for
    # large telemetry logs - mirrors Esri's own documented progressor
    # guidance (see "Controlling the progress dialog box"): updating on
    # every iteration of a potentially-large loop is itself a performance/
    # verbosity concern, so log at a base-10 increment instead.
    total_records = len(records)
    progress_increment = 10 ** max(int(math.log10(total_records)) - 1, 0) if total_records > 100 else 0
    metadata_rows = []
    for i, record in enumerate(records):
        if progress_increment and i % progress_increment == 0:
            log(f"Building video metadata rows: {i}/{total_records}...")
        lon, lat = project_point(record["x"], record["y"], input_srs, wgs84)

        row = {
            "ObjectID": i,
            "Filename": record["filename"],
            "Precision Time Stamp": int(record["_dt"].timestamp() * 1_000_000),
            "AcquisitionDate": record["_dt"],
            "Sensor Longitude": lon,
            "Sensor Latitude": lat,
            "Sensor True Altitude": record["z"],
            "Sensor Ellipsoid Height Extended": record["z"] if z_is_ellipsoid_height else None,
            "CameraID": camera_id,
            "CameraNCols": camera_ncols,
            "CameraNRows": camera_nrows,
            "CameraFocalLength": camera_focal_length,
            "CameraPixelSize": camera_pixel_size,
            "Platform Heading Angle": record["heading"],
            "Platform Pitch Angle (Full)": record["pitch"],
            "Platform Roll Angle (Full)": record["roll"],
            "Sensor Relative Azimuth Angle": 0.0,
            "Sensor Relative Elevation Angle": profile.get("camera_pitch"),
            "Sensor Relative Roll Angle": profile.get("camera_roll"),
            "Sensor Horizontal Field of View": profile.get("hfov"),
            "Sensor Vertical Field of View": profile.get("vfov"),
            "Near Distance": profile.get("near_distance"),
            "Far Distance": profile.get("far_distance"),
            "Camera Height Above Seafloor": profile.get("camera_height"),
        }
        # A blank cell must not wipe out a profile-supplied value.
        row.update({
            target: value
            for target, value in (record.get("_mapped") or {}).items()
            if value is not None
        })
        metadata_rows.append(row)

    intermediate_table_path = write_video_metadata_table(metadata_rows, output_folder, output_name, log=log)
    final_table_path, mapping_file_path = run_convert_video_metadata(
        intermediate_table_path, output_folder, output_name, log=log
    )

    return {
        "video_metadata_table": final_table_path,
        "intermediate_table": intermediate_table_path,
        "metadata_mapping_file": mapping_file_path,
        "row_count": len(metadata_rows),
        "skipped_row_count": skipped,
        "profile": profile,
        "metadata_rows": metadata_rows,
    }


def write_video_metadata_table(rows, output_folder, output_name, log=print):
    """Write rows to <output_name>_VideoMetadataTable.csv. Always a CSV,
    never a geodatabase table - Convert Video Metadata and Video Multiplexer
    both only accept CSV/JSON/GPX."""
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)
    table_path = output_folder / f"{output_name}_VideoMetadataTable.csv"
    with open(table_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=VIDEO_METADATA_TABLE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    log(f"Wrote {len(rows)} row(s) to {table_path}")
    return str(table_path)


def _resolve_convert_video_metadata_tool():
    """Find Convert Video Metadata across arcpy's submodules rather than
    hardcoding arcpy.ia.ConvertVideoMetadata - a tool's documented toolset
    doesn't reliably predict its live arcpy namespace."""
    for namespace_name in ("ia", "management"):
        namespace = getattr(arcpy, namespace_name, None)
        tool = getattr(namespace, "ConvertVideoMetadata", None) if namespace else None
        if callable(tool):
            return tool
    tool = getattr(arcpy, "ConvertVideoMetadata_ia", None)
    if callable(tool):
        return tool
    raise RuntimeError(
        "Could not find ConvertVideoMetadata under arcpy.ia, arcpy.management, or the legacy "
        "arcpy.ConvertVideoMetadata_ia alias. This tool requires the Image Analyst extension."
    )


def run_convert_video_metadata(in_csv, output_folder, output_name, log=print):
    """Run Convert Video Metadata over this tool's intermediate CSV to
    produce the final Multiplexer-ready file + its required mapping file.
    Requires Image Analyst already checked out by the caller. No Input
    Field Matching table is passed: VIDEO_METADATA_TABLE_FIELDS already uses
    Convert Video Metadata's own Target Field names verbatim, and unmatched
    correct-format fields are copied through as-is per its own doc.
    """
    output_folder = Path(output_folder)
    out_csv = output_folder / f"{output_name}_Multiplexed.csv"
    out_mapping_csv = output_folder / f"{output_name}_MetadataMapping.csv"

    tool = _resolve_convert_video_metadata_tool()
    log(f"Convert Video Metadata signature: {inspect.signature(tool)}")
    try:
        tool(in_csv, str(out_csv), str(out_mapping_csv))
    except arcpy.ExecuteError as exc:
        # Capture GetMessages() now - a later cleanup step could overwrite it.
        raise RuntimeError(f"Convert Video Metadata failed: {arcpy.GetMessages(2)}") from exc
    log(f"Convert Video Metadata wrote {out_csv} and {out_mapping_csv}")
    return str(out_csv), str(out_mapping_csv)


def build_track_feature_class(metadata_rows, out_fc_path, log=print):
    """Create a WGS84 point feature class of the sensor track for map-based
    QA, from build_video_metadata_table()'s Sensor Longitude/Latitude."""
    out_fc_path = Path(out_fc_path)
    gdb = str(out_fc_path.parent)
    fc_name = out_fc_path.name

    arcpy.management.CreateFeatureclass(
        gdb, fc_name, "POINT", spatial_reference=arcpy.SpatialReference(4326), has_z="ENABLED"
    )
    fields = [
        ("FrameOID", "LONG"), ("Filename", "TEXT"), ("PlatformHeading", "DOUBLE"),
        ("SensorRelElevation", "DOUBLE"), ("SensorRelRoll", "DOUBLE"), ("SensorAltitude", "DOUBLE"),
    ]
    for field_name, field_type in fields:
        arcpy.management.AddField(str(out_fc_path), field_name, field_type)

    cursor_fields = ["SHAPE@XYZ"] + [f[0] for f in fields]
    with arcpy.da.InsertCursor(str(out_fc_path), cursor_fields) as cursor:
        for row in metadata_rows:
            z = row["Sensor True Altitude"]
            if z is None:
                z = row["Sensor Ellipsoid Height Extended"]
            cursor.insertRow((
                (row["Sensor Longitude"], row["Sensor Latitude"], z or 0.0),
                row["ObjectID"], row["Filename"], row["Platform Heading Angle"],
                row["Sensor Relative Elevation Angle"], row["Sensor Relative Roll Angle"],
                z,
            ))
    log(f"Created sensor track feature class with {len(metadata_rows)} point(s): {out_fc_path}")
    return str(out_fc_path)
