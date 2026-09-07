# -*- coding: utf-8 -*-
"""
FCT_VideoPlayerFrameCrossReference_core.py

Reference implementation for "Cross-Reference Video Player Frame Exports" -
builds a standard Frame/Camera Table CSV pair (the same schema Extract Video
Frames To Images produces, and this project's Fusion/Mosaic-OID tools already
understand) from a folder of images exported one-at-a-time from the ArcGIS
Pro FMV video player, rather than in bulk via Extract Video Frames To Images.

Those exports carry no Frame/Camera Table of their own - only a filename
suffix encoding the video's elapsed playback time in milliseconds at the
moment of export (e.g. a frame grabbed at 000:01.767 is saved as
"..._1767.tif"; NOT a sequential frame index like Extract Video Frames To
Images' own "..._0000000.tif" naming). This module recovers that elapsed
time, converts it to an absolute timestamp using the input video metadata
table's own earliest timestamp as "video frame 0" (overridable), and matches
each image to its nearest metadata row within a tolerance - images/rows
outside tolerance are kept in the output with Matched=False rather than
dropped, so they remain visible for review.

The input video metadata table is treated generically (any table arcpy can
read): only its timestamp field is user-designatable (mirrors this
project's own field-override UX); every other value (position, altitude,
heading/pitch/roll, sensor-relative angles, field of view, near/far
distance, camera model) is auto-detect-only from a candidate-name list that
extends FCT_DeepOceanVideoMetadata_core.py's own telemetry candidates with
the MISB/Multiplexer target field names that module's own output already
uses (e.g. "Sensor Longitude", "Platform Heading Angle") - a table produced
by this project's own Generate Deep Ocean Video Metadata + Convert Video
Metadata chain therefore needs no manual field mapping at all.

Like the other FCT_*_core.py modules, every function takes explicit
arguments and reports progress via an injected `log` callable instead of
calling arcpy messaging directly; a bad/unmatched image is counted and
flagged, never aborts the run.
"""
import bisect
import csv
import importlib.util
import re
from datetime import timedelta, timezone
from pathlib import Path

import arcpy

# Safe import of the two sibling core modules (same folder) - reuses their
# value-normalization/table-reading/timestamp-parsing helpers rather than
# duplicating them, the same static importlib technique every FCT_*_core.py
# module in this project uses (no sys.path mutation).
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

_VIDEO_METADATA_CORE_PATH = _THIS_DIR / "FCT_DeepOceanVideoMetadata_core.py"
if not _VIDEO_METADATA_CORE_PATH.exists():
    raise FileNotFoundError(
        f"FCT_DeepOceanVideoMetadata_core.py not found in {_THIS_DIR}. "
        "All FCT_*.py modules must be shipped together in the same folder."
    )
_spec = importlib.util.spec_from_file_location(
    "FCT_DeepOceanVideoMetadata_core", str(_VIDEO_METADATA_CORE_PATH)
)
video_metadata_core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(video_metadata_core)

is_null_like = fusion_core.is_null_like
to_float = fusion_core.to_float
resolve_field = fusion_core.resolve_field
parse_any_timestamp = video_metadata_core.parse_any_timestamp
project_point = video_metadata_core.project_point


# -----------------------------------------------------------------------
# Filename timestamp parsing
# -----------------------------------------------------------------------
# The ArcGIS Pro video player's own per-frame export names a file
# "<anything>_<elapsed milliseconds>.<ext>" - e.g. a frame grabbed at
# 000:01.767 (1 second, 767 ms into the video) is saved as "..._1767.tif".
# No fixed digit width (a later frame at 5m23.456s -> "..._323456.tif").
_FRAME_FILENAME_TIMESTAMP_PATTERN = re.compile(r"_(\d+)\.(tif|tiff|jpe?g|png)$", re.IGNORECASE)


def parse_elapsed_milliseconds(filename):
    """Extract the elapsed-milliseconds suffix from an exported frame's
    filename, or None if the filename doesn't match the expected pattern."""
    match = _FRAME_FILENAME_TIMESTAMP_PATTERN.search(str(filename))
    return int(match.group(1)) if match else None


def list_frame_images(folder):
    """List every image in folder matching this project's recognized
    export extensions (same list FCT_ImageFusion_core.py itself uses)."""
    folder = Path(folder)
    images = []
    for pattern in fusion_core.EXPORT_IMAGE_EXTENSIONS:
        images.extend(folder.glob(pattern))
    return sorted(images)


# -----------------------------------------------------------------------
# Metadata table field resolution
# -----------------------------------------------------------------------
# Extends FCT_DeepOceanVideoMetadata_core.py's own candidate lists with the
# MISB/Multiplexer target field names that module's own output already
# uses, so a table produced by this project's Generate Deep Ocean Video
# Metadata + Convert Video Metadata chain auto-detects with no field
# mapping at all, while a raw telemetry CSV still works via the original
# candidates.
# Geographic-only names (unlike GenerateDeepOceanVideoMetadata's own
# CANDIDATE_X_FIELDS/CANDIDATE_Y_FIELDS, deliberately NOT reused verbatim
# here): this tool's metadata table is expected to already be the
# Multiplexer's own Metadata File (or its Convert Video Metadata output),
# whose Sensor Longitude/Latitude are ALWAYS geographic WGS84 degrees per
# the MISB spec - never a projected Easting/Northing/X/Y pair. Including
# those ambiguous generic names here would risk silently auto-detecting a
# projected coordinate column and reprojecting it as if it were WGS84
# lon/lat (wgs84 is hardcoded below as the input CRS for exactly this
# reason - there is no Input Coordinate System param for the metadata
# table since the Multiplexer format fixes it).
CANDIDATE_LON_FIELDS = ["Longitude", "Lon", "Long", "Platform Longitude", "Sensor Longitude"]
CANDIDATE_LAT_FIELDS = ["Latitude", "Lat", "Platform Latitude", "Sensor Latitude"]
CANDIDATE_ALTITUDE_FIELDS = video_metadata_core.CANDIDATE_Z_FIELDS + [
    "Sensor True Altitude", "Sensor Ellipsoid Height Extended",
]
CANDIDATE_TIMESTAMP_FIELDS = video_metadata_core.CANDIDATE_TIMESTAMP_FIELDS
CANDIDATE_HEADING_FIELDS = video_metadata_core.CANDIDATE_HEADING_FIELDS + ["Platform Heading Angle"]
CANDIDATE_PITCH_FIELDS = video_metadata_core.CANDIDATE_PITCH_FIELDS + ["Platform Pitch Angle (Full)"]
CANDIDATE_ROLL_FIELDS = video_metadata_core.CANDIDATE_ROLL_FIELDS + ["Platform Roll Angle (Full)"]
CANDIDATE_SENSOR_AZIMUTH_FIELDS = ["Sensor Relative Azimuth Angle"]
CANDIDATE_SENSOR_ELEVATION_FIELDS = ["Sensor Relative Elevation Angle"]
CANDIDATE_SENSOR_ROLL_FIELDS = ["Sensor Relative Roll Angle"]
CANDIDATE_HFOV_FIELDS = ["Sensor Horizontal Field of View", "HFOV", "Horizontal Field of View"]
CANDIDATE_VFOV_FIELDS = ["Sensor Vertical Field of View", "VFOV", "Vertical Field of View"]
CANDIDATE_NEAR_DISTANCE_FIELDS = ["Near Distance", "NearDistance"]
CANDIDATE_FAR_DISTANCE_FIELDS = ["Far Distance", "FarDistance"]
CANDIDATE_CAMERA_HEIGHT_FIELDS = ["Camera Height Above Seafloor", "CameraHeight"]
CANDIDATE_CAMERA_ID_FIELDS = ["CameraID", "Camera_ID", "CamID"]
CANDIDATE_CAMERA_NCOLS_FIELDS = ["CameraNCols", "NCols"]
CANDIDATE_CAMERA_NROWS_FIELDS = ["CameraNRows", "NRows"]
CANDIDATE_CAMERA_FOCAL_LENGTH_FIELDS = ["CameraFocalLength", "FocalLength"]
CANDIDATE_CAMERA_PIXEL_SIZE_FIELDS = ["CameraPixelSize", "PixelSize"]

# key -> (candidate names, required). Only "timestamp" is ever user-
# overridable (time_field_override in resolve_cross_reference_fields) -
# every other key is auto-detect-only, matching this project's own
# narrowed-scope decision for GenerateDeepOceanVideoMetadata (avoids
# reintroducing the GPValueTable field-mapping bugs documented for that tool).
CROSS_REF_FIELD_CANDIDATES = {
    "x": (CANDIDATE_LON_FIELDS, True),
    "y": (CANDIDATE_LAT_FIELDS, True),
    "z": (CANDIDATE_ALTITUDE_FIELDS, False),
    "timestamp": (CANDIDATE_TIMESTAMP_FIELDS, True),
    "heading": (CANDIDATE_HEADING_FIELDS, False),
    "pitch": (CANDIDATE_PITCH_FIELDS, False),
    "roll": (CANDIDATE_ROLL_FIELDS, False),
    "sensor_azimuth": (CANDIDATE_SENSOR_AZIMUTH_FIELDS, False),
    "sensor_elevation": (CANDIDATE_SENSOR_ELEVATION_FIELDS, False),
    "sensor_roll": (CANDIDATE_SENSOR_ROLL_FIELDS, False),
    "hfov": (CANDIDATE_HFOV_FIELDS, False),
    "vfov": (CANDIDATE_VFOV_FIELDS, False),
    "near_distance": (CANDIDATE_NEAR_DISTANCE_FIELDS, False),
    "far_distance": (CANDIDATE_FAR_DISTANCE_FIELDS, False),
    "camera_height": (CANDIDATE_CAMERA_HEIGHT_FIELDS, False),
    "camera_id": (CANDIDATE_CAMERA_ID_FIELDS, False),
    "camera_ncols": (CANDIDATE_CAMERA_NCOLS_FIELDS, False),
    "camera_nrows": (CANDIDATE_CAMERA_NROWS_FIELDS, False),
    "camera_focal_length": (CANDIDATE_CAMERA_FOCAL_LENGTH_FIELDS, False),
    "camera_pixel_size": (CANDIDATE_CAMERA_PIXEL_SIZE_FIELDS, False),
}


def resolve_cross_reference_fields(field_names, time_field_override=None):
    """Resolve every CROSS_REF_FIELD_CANDIDATES key from a metadata table's
    header. Never raises (every lookup uses required=False) - the caller
    checks the required x/y/timestamp keys itself, so a dialog-time preview
    can show every other successfully-resolved field alongside a missing one."""
    resolved = {}
    for key, (candidates, _required) in CROSS_REF_FIELD_CANDIDATES.items():
        override = time_field_override if key == "timestamp" else None
        try:
            resolved[key] = resolve_field(field_names, candidates, override, required=False)
        except KeyError:
            resolved[key] = None
    return resolved


def read_metadata_rows(table_path):
    """Read the input video metadata table into a list of dicts."""
    rows = fusion_core.read_metadata_table(table_path)
    if not rows:
        raise ValueError(f"Metadata table has no rows: {table_path}")
    return rows


# -----------------------------------------------------------------------
# Timestamp matching
# -----------------------------------------------------------------------
def resolve_video_start_time(rows, timestamp_field, override_start_time=None, log=print):
    """Return the absolute datetime corresponding to elapsed time zero.

    override_start_time wins if given (a real datetime, e.g. a GPDate
    parameter's .value). Otherwise assumes the metadata table's own
    earliest timestamp is video frame 0 - true for a table produced by
    this project's own Generate Deep Ocean Video Metadata + multiplexer
    chain, since one metadata row is written per output video frame
    starting at the beginning.
    """
    if override_start_time is not None:
        if hasattr(override_start_time, "tzinfo"):
            return override_start_time if override_start_time.tzinfo else override_start_time.replace(tzinfo=timezone.utc)
        parsed = parse_any_timestamp(override_start_time)
        if parsed is None:
            raise ValueError(f"Could not parse the Video Start Time override: {override_start_time!r}")
        return parsed

    parsed_timestamps = [parse_any_timestamp(row.get(timestamp_field)) for row in rows]
    parsed_timestamps = [dt for dt in parsed_timestamps if dt is not None]
    if not parsed_timestamps:
        raise ValueError(
            f"No row in the metadata table had a parsable value in the '{timestamp_field}' field - "
            "cannot determine a video start time."
        )
    start_time = min(parsed_timestamps)
    log(f"Video Start Time not supplied - assuming the metadata table's earliest "
        f"'{timestamp_field}' value is video frame 0: {start_time.isoformat()}")
    return start_time


def build_timestamp_index(rows, timestamp_field, log=print):
    """Return (sorted_timestamps, sorted_rows) - two parallel lists, sorted
    ascending by parsed timestamp, dropping rows with an unparsable value."""
    indexed = [(parse_any_timestamp(row.get(timestamp_field)), row) for row in rows]
    indexed = [(dt, row) for dt, row in indexed if dt is not None]
    if not indexed:
        raise ValueError(f"No row in the metadata table had a parsable '{timestamp_field}' value.")
    indexed.sort(key=lambda pair: pair[0])
    dropped = len(rows) - len(indexed)
    if dropped:
        log(f"WARNING: {dropped} metadata row(s) had an unparsable '{timestamp_field}' "
            "value and were excluded from matching.")
    sorted_timestamps = [dt for dt, _ in indexed]
    sorted_rows = [row for _, row in indexed]
    return sorted_timestamps, sorted_rows


def compute_auto_tolerance_seconds(sorted_timestamps, log=print):
    """Data-driven default match tolerance: half the median interval between
    consecutive metadata rows. A true 0-second "exact match" is almost never
    achievable - video frames land at arbitrary elapsed milliseconds while
    the metadata table only has discrete samples (e.g. 1 Hz) - so this is
    the tightest tolerance that still lets every frame pair with its
    genuinely-nearest sample instead of a fixed, arbitrarily-guessed
    constant. Falls back to 1.0s if fewer than 2 rows are available.
    """
    if len(sorted_timestamps) < 2:
        return 1.0
    intervals = sorted(
        (b - a).total_seconds() for a, b in zip(sorted_timestamps, sorted_timestamps[1:])
    )
    mid = len(intervals) // 2
    median_interval = (
        intervals[mid] if len(intervals) % 2 else (intervals[mid - 1] + intervals[mid]) / 2
    )
    tolerance = max(median_interval / 2, 0.05)
    log(f"Match Tolerance not supplied - auto-computed {tolerance:.3f}s from the metadata "
        f"table's own median sampling interval ({median_interval:.3f}s).")
    return tolerance


def find_nearest_row(abs_dt, sorted_timestamps, sorted_rows, tolerance_seconds):
    """Nearest-timestamp lookup via bisect. Returns (row, delta_seconds) if
    the closest row is within tolerance_seconds, else (None, delta_seconds)
    - delta_seconds is still returned on a miss so the caller can log how
    far off the nearest candidate was."""
    if not sorted_timestamps:
        return None, None
    position = bisect.bisect_left(sorted_timestamps, abs_dt)
    candidate_indexes = [i for i in (position - 1, position) if 0 <= i < len(sorted_timestamps)]
    best_index = min(candidate_indexes, key=lambda i: abs((sorted_timestamps[i] - abs_dt).total_seconds()))
    delta_seconds = (sorted_timestamps[best_index] - abs_dt).total_seconds()
    if abs(delta_seconds) > tolerance_seconds:
        return None, delta_seconds
    return sorted_rows[best_index], delta_seconds


# -----------------------------------------------------------------------
# Output Frame/Camera Table schema (matches Extract Video Frames To Images'
# own schema plus this project's additive columns - see repo notes - so the
# output is a drop-in Frame/Camera Table pair for the Fusion and Mosaic/OID
# tools, plus a few QA-only extra columns at the end).
# -----------------------------------------------------------------------
FRAME_TABLE_FIELDS = [
    "ObjectID", "Raster", "Filename", "Precision Time Stamp", "AcquisitionDate",
    "Platform Longitude", "Platform Latitude", "Sensor Altitude",
    "PerspectiveX", "PerspectiveY", "PerspectiveZ", "SRS",
    "CameraID", "CameraHeading", "CameraPitch", "CameraRoll",
    "HorizontalFieldOfView", "VerticalFieldOfView", "NearDistance", "FarDistance", "CameraHeight",
    "SensorRelativeAzimuthAngle", "SensorRelativeElevationAngle", "SensorRelativeRollAngle",
    "ElapsedMilliseconds", "MatchTimeDeltaSeconds", "Matched",
]

CAMERA_TABLE_FIELDS = ["CameraID", "NCols", "NRows", "FocalLength", "PixelSize"]


def _resolve_camera_value(key, rows, resolved, default):
    """First numeric value found in rows for the resolved column name, else
    default (an explicit None-check, not `or`, so a real 0 isn't discarded)."""
    field = resolved.get(key)
    if field:
        for row in rows:
            value = to_float(row.get(field))
            if value is not None:
                return value
    return default


def _matched_value(matched_row, resolved, key):
    field = resolved.get(key)
    return to_float(matched_row.get(field)) if field else None


def _blank_frame_row(object_id, image_path, camera_id, elapsed_ms=None, delta_seconds=None):
    row = {field: None for field in FRAME_TABLE_FIELDS}
    row.update({
        "ObjectID": object_id,
        "Raster": str(image_path),
        "Filename": image_path.name,
        "CameraID": camera_id,
        "ElapsedMilliseconds": elapsed_ms,
        "MatchTimeDeltaSeconds": delta_seconds,
        "Matched": False,
    })
    return row


def _build_frame_row(object_id, image_path, matched_row, resolved, abs_dt, elapsed_ms,
                      delta_seconds, wgs84, output_srs, camera_id):
    longitude = to_float(matched_row.get(resolved["x"]))
    latitude = to_float(matched_row.get(resolved["y"]))
    altitude = _matched_value(matched_row, resolved, "z")

    perspective_x = perspective_y = None
    if longitude is not None and latitude is not None:
        perspective_x, perspective_y = project_point(longitude, latitude, wgs84, output_srs)

    return {
        "ObjectID": object_id,
        "Raster": str(image_path),
        "Filename": image_path.name,
        "Precision Time Stamp": int(abs_dt.timestamp() * 1_000_000),
        "AcquisitionDate": abs_dt.replace(tzinfo=None).isoformat(sep=" "),
        "Platform Longitude": longitude,
        "Platform Latitude": latitude,
        "Sensor Altitude": altitude,
        "PerspectiveX": perspective_x,
        "PerspectiveY": perspective_y,
        "PerspectiveZ": altitude,
        "SRS": output_srs.factoryCode,
        "CameraID": camera_id,
        "CameraHeading": _matched_value(matched_row, resolved, "heading"),
        "CameraPitch": _matched_value(matched_row, resolved, "pitch"),
        "CameraRoll": _matched_value(matched_row, resolved, "roll"),
        "HorizontalFieldOfView": _matched_value(matched_row, resolved, "hfov"),
        "VerticalFieldOfView": _matched_value(matched_row, resolved, "vfov"),
        "NearDistance": _matched_value(matched_row, resolved, "near_distance"),
        "FarDistance": _matched_value(matched_row, resolved, "far_distance"),
        "CameraHeight": _matched_value(matched_row, resolved, "camera_height"),
        "SensorRelativeAzimuthAngle": _matched_value(matched_row, resolved, "sensor_azimuth"),
        "SensorRelativeElevationAngle": _matched_value(matched_row, resolved, "sensor_elevation"),
        "SensorRelativeRollAngle": _matched_value(matched_row, resolved, "sensor_roll"),
        "ElapsedMilliseconds": elapsed_ms,
        "MatchTimeDeltaSeconds": delta_seconds,
        "Matched": True,
    }


def write_frame_table_csv(frame_rows, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FRAME_TABLE_FIELDS, restval="")
        writer.writeheader()
        writer.writerows(frame_rows)
    return str(output_path)


def load_existing_frame_table(frame_table_path):
    """Load a prior run's own "<output_name>_FrameTable.csv" (if present) so
    a repeat run against the same, still-growing video-player export folder
    only matches/writes NEW images instead of redoing the whole folder every
    time. Returns (existing_rows, existing_filenames, next_object_id) -
    ([], set(), 1) if no prior table exists or it can't be read.
    """
    frame_table_path = Path(frame_table_path)
    if not frame_table_path.exists():
        return [], set(), 1
    with open(frame_table_path, newline="", encoding="utf-8") as handle:
        existing_rows = list(csv.DictReader(handle))
    existing_filenames = {row["Filename"] for row in existing_rows if row.get("Filename")}
    object_ids = [to_float(row.get("ObjectID")) for row in existing_rows]
    object_ids = [int(v) for v in object_ids if v is not None]
    next_object_id = max(object_ids) + 1 if object_ids else len(existing_rows) + 1
    return existing_rows, existing_filenames, next_object_id


def write_matched_metadata_csv(existing_rows, new_rows, field_names, output_path):
    """Write the MISB-compatible "matched metadata" table: the ORIGINAL
    input metadata table's own rows (untouched, original column order/
    schema - a passthrough, not a reprojection) for just the rows that
    matched a frame, one per matched frame. This is a plain subset/passthrough
    of the input, suitable for re-feeding into the Video Multiplexer or
    another MISB-consuming tool - unlike the Frame/Camera Table (a different,
    unrelated schema this project's own Fusion/Mosaic-OID tools require).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=field_names, restval="")
        writer.writeheader()
        writer.writerows(existing_rows + new_rows)
    return str(output_path)



def write_camera_table_csv(camera_id, ncols, nrows, focal_length, pixel_size, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CAMERA_TABLE_FIELDS)
        writer.writeheader()
        writer.writerow({
            "CameraID": camera_id, "NCols": ncols, "NRows": nrows,
            "FocalLength": focal_length, "PixelSize": pixel_size,
        })
    return str(output_path)


def _resolve_output_srs(output_srs_value):
    """Build a SpatialReference from a GPSpatialReference param's .value, which
    may already be a SpatialReference object, a factory code, or WKT/WKT2 text
    (e.g. Web Mercator's dynamic-datum WKT2). Passing WKT text as the
    positional `item` arg routes through createFromFile (prj-file/name lookup)
    and fails, so WKT text must go through loadFromString instead.
    """
    if isinstance(output_srs_value, arcpy.SpatialReference):
        return output_srs_value
    if not output_srs_value:
        return arcpy.SpatialReference(3857)
    text = str(output_srs_value).strip()
    if text.isdigit():
        return arcpy.SpatialReference(int(text))
    srs = arcpy.SpatialReference()
    srs.loadFromString(text)
    return srs


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------
def cross_reference_frames(image_folder, metadata_table_path, output_name,
                            time_field_override=None, video_start_time_override=None,
                            tolerance_seconds=None, output_srs_text=None,
                            camera_model_defaults=None, write_frame_camera_table=True,
                            write_matched_metadata_table=False, log=print):
    """Build a Frame/Camera Table CSV pair for image_folder's video-player-
    exported frames, cross-referenced against metadata_table_path by
    elapsed-time-derived absolute timestamp. Writes
    "<output_name>_FrameTable.csv"/"<output_name>_CameraTable.csv" into
    image_folder (Fusion/Mosaic-OID-tool-discoverable layout) and returns a
    summary dict.

    tolerance_seconds=None auto-computes a tolerance from the metadata
    table's own sampling cadence (see compute_auto_tolerance_seconds) -
    pass an explicit value to override.

    camera_model_defaults (optional dict with camera_id/camera_ncols/
    camera_nrows/camera_focal_length/camera_pixel_size): used only for
    values not found anywhere in the metadata table.

    write_frame_camera_table: this project's own Frame/Camera Table schema -
    required by Extracted Frame Image Metadata Generation and Build Mosaic and
    Oriented Imagery Datasets. write_matched_metadata_table: an ADDITIONAL,
    unrelated output - a MISB-compatible passthrough of just the input
    metadata table's own matched rows (same columns/schema as
    metadata_table_path, untouched), for re-feeding into the Video
    Multiplexer or another MISB-consuming tool. Independent flags - either,
    both, or (harmlessly) neither can be enabled.
    """
    camera_model_defaults = camera_model_defaults or {}
    output_srs = _resolve_output_srs(output_srs_text)
    wgs84 = arcpy.SpatialReference(4326)

    images = list_frame_images(image_folder)
    if not images:
        raise ValueError(
            f"No images found in {image_folder} matching {fusion_core.EXPORT_IMAGE_EXTENSIONS}"
        )

    # Incremental rerun support: the video player export folder this tool
    # targets keeps growing (a user exports more frames, then reruns against
    # the SAME output_name) - only match/append images this tool hasn't
    # already written into its own Frame Table, rather than redoing the
    # whole folder (and every downstream fuse_folder sidecar write) every
    # single run.
    frame_table_path_obj = Path(image_folder) / f"{output_name}_FrameTable.csv"
    existing_rows, existing_filenames, next_object_id = load_existing_frame_table(frame_table_path_obj)
    new_images = [image_path for image_path in images if image_path.name not in existing_filenames]
    already_processed_count = len(images) - len(new_images)
    if existing_rows and not new_images:
        log(f"No new images since the last run - {already_processed_count} already in "
            f"{frame_table_path_obj.name}, nothing to do.")
        return {
            "frame_table_path": str(frame_table_path_obj),
            "camera_table_path": str(Path(image_folder) / f"{output_name}_CameraTable.csv"),
            "matched_metadata_table_path": None,
            "total_images": len(images),
            "new_images": 0,
            "already_processed_count": already_processed_count,
            "matched_count": 0,
            "unmatched_count": 0,
            "no_timestamp_count": 0,
        }
    if existing_rows:
        log(f"Incremental run: {already_processed_count} image(s) already in "
            f"{frame_table_path_obj.name}, {len(new_images)} new image(s) to process.")

    rows = read_metadata_rows(metadata_table_path)
    field_names = list(rows[0].keys())
    resolved = resolve_cross_reference_fields(field_names, time_field_override)
    missing_required = [key for key in ("x", "y", "timestamp") if not resolved.get(key)]
    if missing_required:
        raise ValueError(
            f"Could not resolve required field(s) {missing_required} in the metadata table. "
            f"Available fields: {field_names}"
        )

    start_time = resolve_video_start_time(rows, resolved["timestamp"], video_start_time_override, log=log)
    sorted_timestamps, sorted_rows = build_timestamp_index(rows, resolved["timestamp"], log=log)
    if tolerance_seconds is None:
        tolerance_seconds = compute_auto_tolerance_seconds(sorted_timestamps, log=log)


    camera_id = _resolve_camera_value("camera_id", rows, resolved, camera_model_defaults.get("camera_id"))
    camera_id = int(camera_id) if camera_id is not None else 1
    camera_ncols = _resolve_camera_value("camera_ncols", rows, resolved, camera_model_defaults.get("camera_ncols"))
    camera_nrows = _resolve_camera_value("camera_nrows", rows, resolved, camera_model_defaults.get("camera_nrows"))
    camera_focal_length = _resolve_camera_value(
        "camera_focal_length", rows, resolved, camera_model_defaults.get("camera_focal_length")
    )
    camera_pixel_size = _resolve_camera_value(
        "camera_pixel_size", rows, resolved, camera_model_defaults.get("camera_pixel_size")
    )

    frame_rows = []
    matched_metadata_rows = []
    matched_count = 0
    unmatched_count = 0
    no_timestamp_count = 0
    for object_id, image_path in enumerate(new_images, start=next_object_id):
        elapsed_ms = parse_elapsed_milliseconds(image_path.name)
        if elapsed_ms is None:
            no_timestamp_count += 1
            log(f"WARNING: '{image_path.name}' has no recognizable elapsed-time suffix "
                "- no metadata match attempted.")
            frame_rows.append(_blank_frame_row(object_id, image_path, camera_id))
            continue

        abs_dt = start_time + timedelta(milliseconds=elapsed_ms)
        matched_row, delta_seconds = find_nearest_row(abs_dt, sorted_timestamps, sorted_rows, tolerance_seconds)
        if matched_row is None:
            unmatched_count += 1
            if delta_seconds is not None:
                log(f"WARNING: '{image_path.name}' nearest metadata row is {delta_seconds:.3f}s away "
                    f"(tolerance {tolerance_seconds}s) - left unmatched.")
            frame_rows.append(_blank_frame_row(object_id, image_path, camera_id, elapsed_ms, delta_seconds))
            continue

        matched_count += 1
        matched_metadata_rows.append(matched_row)
        frame_rows.append(_build_frame_row(
            object_id, image_path, matched_row, resolved, abs_dt, elapsed_ms,
            delta_seconds, wgs84, output_srs, camera_id
        ))

    output_dir = Path(image_folder)
    if write_frame_camera_table:
        frame_table_path = write_frame_table_csv(
            existing_rows + frame_rows, output_dir / f"{output_name}_FrameTable.csv"
        )
        camera_table_path = write_camera_table_csv(
            camera_id, camera_ncols, camera_nrows, camera_focal_length, camera_pixel_size,
            output_dir / f"{output_name}_CameraTable.csv"
        )
    else:
        frame_table_path = None
        camera_table_path = None

    matched_metadata_table_path = None
    if write_matched_metadata_table:
        matched_metadata_path_obj = output_dir / f"{output_name}_MatchedMetadataTable.csv"
        existing_matched_rows = []
        if matched_metadata_path_obj.exists():
            with open(matched_metadata_path_obj, newline="", encoding="utf-8") as handle:
                existing_matched_rows = list(csv.DictReader(handle))
        matched_metadata_table_path = write_matched_metadata_csv(
            existing_matched_rows, matched_metadata_rows, field_names, matched_metadata_path_obj
        )

    log(f"{len(new_images)} new image(s): {matched_count} matched, {unmatched_count} unmatched "
        f"(outside {tolerance_seconds}s tolerance), {no_timestamp_count} with no elapsed-time suffix "
        f"({already_processed_count} already processed in a prior run, {len(images)} total in folder).")

    return {
        "frame_table_path": frame_table_path,
        "camera_table_path": camera_table_path,
        "matched_metadata_table_path": matched_metadata_table_path,
        "total_images": len(images),
        "new_images": len(new_images),
        "already_processed_count": already_processed_count,
        "matched_count": matched_count,
        "unmatched_count": unmatched_count,
        "no_timestamp_count": no_timestamp_count,
    }
