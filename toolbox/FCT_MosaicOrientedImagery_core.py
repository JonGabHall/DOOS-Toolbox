# -*- coding: utf-8 -*-
"""
FCT_MosaicOrientedImagery_core.py

Reference implementation for "Build Mosaic and Oriented Imagery Datasets" -
a "manage product data" tool that takes the extracted video frame images and
Frame/Camera Table CSVs produced by Extract Video Frames To Images (or by
this project's own "Generate Deep Ocean Video Metadata" tool) and loads them
into a geodatabase as a mosaic dataset and/or an oriented imagery dataset,
the two Esri data structures built for exactly this purpose.

Both destinations are populated from the *_FrameTable.csv / *_CameraTable.csv
pair discovered in the input folder, which is accepted in either layout the
Extracted Frame Image Metadata Generation tool can produce (see
resolve_export_layout()):
  - Flat: the original Extract Video Frames To Images output, optionally
    fused in place - images, *.aux.xml/*.ifdo.json sidecars, and the *.csv
    tables all sit together in one folder.
  - Structured: the Fusion tool's "Export to Output Folder" layout - images/
    (frame images only), metadata/ (*.aux.xml and *.ifdo.json), and the
    *.csv tables at the folder root.

Two ways to load each pair into the mosaic dataset (see MOSAIC_RASTER_TYPE_*):
  - "Table" (default): compile_frame_table_with_ifdo() builds a single table
    that starts from the original Frame Table columns, corrects the
    "Raster" path to wherever the image actually resolves to in this
    folder, and appends the image's iFDO fields as extra ifdo_* columns.
    Loaded via the generic "Table" raster type, which requires each frame
    image to already be individually georeferenced (e.g. the *.tfw world
    file Extract Video Frames To Images writes for TIFF output).
  - "Frame Camera": loads the original, unmodified Frame/Camera Table pair,
    with footprints computed photogrammetrically from the camera model
    instead of relying on each image's own georeferencing. CONFIRMED LIVE
    (2026-08-28): Esri's Frames table schema requires either an
    Omega/Phi/Kappa triplet or a Matrix field for exterior orientation -
    Extract Video Frames To Images' Frame/Camera Table pair has neither (it
    only carries CameraHeading/CameraPitch/CameraRoll and
    CameraID/NCols/NRows/FocalLength/PixelSize, the simpler model meant for
    oriented imagery, not photogrammetric orthorectification), so this
    raster type always fails with "Unable to load camera table." against
    this schema. Only usable if Omega/Phi/Kappa or Matrix columns are added
    to the Frame Table separately.

The oriented imagery dataset is populated directly via arcpy.da.InsertCursor
against its own documented, stable attribute schema (Shape, ImagePath, Name,
CameraHeading, CameraPitch, CameraRoll, HorizontalFieldOfView,
VerticalFieldOfView, NearDistance, FarDistance, CameraHeight,
OrientedImageryType, AcquisitionDate - see OID_STANDARD_FIELDS), not through
AddImagesToOrientedImageryDataset's CSV/table schema auto-detection. That
tool was tried first and confirmed live to silently add 0 rows for this
project's compiled table - AddRastersToMosaicDataset logged "Completed
crawling 0 data source items" and AddImagesToOrientedImageryDataset logged
"WARNING 000229: Cannot open <path>" for the exact same file, and wrapping
the file as a table view (arcpy.management.MakeTableView) still did not
produce a usable outcome. Since every value the OID needs is something this
module already computes (from the compiled FCT+iFDO table), inserting rows
directly removes that entire unreliable, underdocumented auto-detection
step - see insert_rows_into_oid(). Any FCT/iFDO column not mapped to one of
the OID's standard fields is still added as an extra text attribute, so no
data from the compiled table is lost.

Neither Esri tool used here requires the Image Analyst extension, matching
this project's existing tools; they do require a Standard or Advanced
ArcGIS Pro license (mosaic datasets and oriented imagery datasets are not
available under a Basic license) - see licensed_for_mosaic_and_oi().

A folder can hold more than one Frame/Camera Table pair (see
fusion_core.discover_table_pairs()); every pair found is added to the same
mosaic dataset / oriented imagery dataset in turn, and one pair failing to
load is logged and skipped rather than aborting the whole run - the same
fail-soft behavior FCT_ImageFusion_core.fuse_folder() uses.

NOTE ON UNVERIFIED PARAMETER DETAILS: the exact keyword/enumeration strings
for AddRastersToMosaicDataset's "Auxiliary Inputs" value-table string could
not be confirmed against a live ArcGIS Pro Python session while writing this
module - it is filled in from Esri's published tool reference page using the
well-established "Key #value#;Key2 #value2#" convention used elsewhere in
ArcPy for auxiliary/raster-type parameters. The GP tool call is wrapped so a
parameter mismatch surfaces the real arcpy error text instead of failing
silently.
"""
import csv
import datetime
import importlib.util
import inspect
import json
import math
import os
import re
import types
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import arcpy

# ============================================================================
# Safe import of FCT_ImageFusion_core.py (same folder)
# ============================================================================
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

discover_table_pairs = fusion_core.discover_table_pairs


# ============================================================================
# CONSTANTS
# ============================================================================
# CONFIRMED (2026-08-31): the actual registered raster type name is
# "Table / Raster Catalog", not the bare "Table" this project used for the
# entire mosaic debugging session - found by comparing against a live,
# working script (dkwright/arcgis-sitescan-imageserver's SiteScanToolkit.py,
# which calls arcpy.management.AddRastersToMosaicDataset(..., raster_type=
# "Table / Raster Catalog", ...) successfully) and cross-checked against
# Esri's own aerial-imagery-raster-types.html ("Click the Raster Type
# drop-down list and click Table/Raster Catalog"). Passing "Table" appears
# to have been an unrecognized raster type name - likely the actual root
# cause of every bare "ERROR 999999" hit in this project so far, since none
# of the table-schema/content fixes tried before this ever changed that
# error's behavior at all.
MOSAIC_RASTER_TYPE_TABLE = "Table / Raster Catalog"
MOSAIC_RASTER_TYPE_FRAME_CAMERA = "Frame Camera"
MOSAIC_RASTER_TYPE_CHOICES = [MOSAIC_RASTER_TYPE_TABLE, MOSAIC_RASTER_TYPE_FRAME_CAMERA]

COMPILED_TABLE_SUFFIX = "_Compiled.csv"

IMAGERY_CATEGORY_CHOICES = [
    "Nadir", "Oblique", "Horizontal", "360", "Inspection",
    "AerialFrameVideo", "TerrestrialFrameVideo", "Terrestrial360Video",
]

ELEVATION_SOURCE_NONE = "No Elevation Source"
ELEVATION_SOURCE_CONSTANT = "Constant Elevation"
ELEVATION_SOURCE_DEM = "DEM"

# Esri's own documented, stable oriented imagery dataset attribute fields
# (see https://doc.esri.com/en/arcgis-pro/latest/help/data/imagery/oriented-imagery-dataset.html) -
# insert_rows_into_oid() writes to these directly instead of going through
# AddImagesToOrientedImageryDataset's CSV schema auto-detection.
OID_STANDARD_FIELDS = [
    # (field name, field type, text length or None)
    ("Name", "TEXT", 255),
    ("ImagePath", "TEXT", 500),
    ("AcquisitionDate", "DATE", None),
    ("CameraHeading", "DOUBLE", None),
    ("CameraPitch", "DOUBLE", None),
    ("CameraRoll", "DOUBLE", None),
    ("CameraHeight", "DOUBLE", None),
    ("HorizontalFieldOfView", "DOUBLE", None),
    ("VerticalFieldOfView", "DOUBLE", None),
    ("NearDistance", "DOUBLE", None),
    ("FarDistance", "DOUBLE", None),
    ("OrientedImageryType", "TEXT", 50),
    ("SequenceOrder", "LONG", None),
]

# Esri's OID "SRS" field (per oriented-imagery-table.html) takes ONE
# coordinate system value (a WKID or WKT), not a combined pair - but Extract
# Video Frames To Images' own FCT "SRS" column is literally a "horizontal;
# vertical" pair (e.g. "3857;3855"), which would be misleading passed
# through unchanged as a single field named "SRS". Populated explicitly in
# insert_rows_into_oid() instead of through the generic extra-field loop.
_OID_SRS_FIELDS = [("SourceHorizontalSRS", "TEXT", 50), ("SourceVerticalSRS", "TEXT", 50)]

# Per-category defaults (pitch, roll, hfov, vfov, height_m, near_m, far_m),
# matching Esri's own oriented imagery category defaults table - used to
# fill in any of the fields above a row's compiled table doesn't provide.
IMAGERY_CATEGORY_DEFAULTS = {
    "Horizontal": (90, 0, 60, 40, 1.8, 1, 30),
    "Oblique": (45, 0, 60, 40, 200, 1, 500),
    "Nadir": (0, 0, 60, 40, 200, 1, 500),
    "360": (90, 0, 360, 180, 1.8, 1, 30),
    "Inspection": (90, 0, 60, 40, 1.8, 0, 5),
    "AerialFrameVideo": (70, 0, 60, 40, 100, 1, 500),
    "TerrestrialFrameVideo": (90, 0, 70, 40, 2, 1, 20),
    "Terrestrial360Video": (90, 0, 360, 180, 2, 1, 20),
}

# Compiled table columns already mapped explicitly by insert_rows_into_oid() -
# every other column becomes an extra text attribute field instead.
_OID_MAPPED_SOURCE_COLUMNS = {
    "Filename", "Raster", "CameraHeading", "CameraPitch", "CameraRoll",
    "HorizontalFieldOfView", "VerticalFieldOfView", "NearDistance", "FarDistance",
    "CameraHeight", "AcquisitionDate", "Precision Time Stamp", "SRS",
}

# Auto-detected iFDO columns (position/hash/identifier/orientation, computed
# by FCT_ImageFusion_core.py from the same PerspectiveX/Y/Z, CameraHeading/
# Pitch/Roll, and file bytes the OID already carries in its own standard
# fields) - excluded from the OID's extra attribute fields on user request,
# since they duplicate data the OID already has natively (in a different
# CRS/format) rather than adding new information. "ifdo_image_datetime" is
# deliberately NOT in this set - it stays as its own extra field alongside
# the manually-authored iFDO text fields (image-deployment, image-sensor,
# etc.), which were never excluded to begin with.
_OID_EXCLUDED_IFDO_COLUMNS = {
    "ifdo_image_latitude", "ifdo_image_longitude", "ifdo_image_altitude_meters",
    "ifdo_image_coordinate_reference_system", "ifdo_image_hash_sha256", "ifdo_image_uuid",
    "ifdo_image_camera_yaw_degrees", "ifdo_image_camera_pitch_degrees", "ifdo_image_camera_roll_degrees",
}

# CONFIRMED (2026-08-31): Extract Video Frames To Images' own FrameTable.csv
# already carries CenterX/CenterY/ZOrder columns - names that collide with
# Esri's own *mosaic dataset* footprint fields of the same name (the item's
# footprint centroid and draw order), but here they are computed from the
# same placeholder identity .tfw transform as the frame's non-georeferencing
# (pixel-space, not real ground coordinates) - passing them through to the
# OID as extra fields looks like real position data but is actually the
# frame's pixel width/height halved, and ZOrder is left blank by the source
# tool. The OID's real position already lives correctly in its own Shape
# field (from PerspectiveX/Y/Z), so these three add confusion, not data.
_OID_EXCLUDED_FCT_COLUMNS = {"CenterX", "CenterY", "ZOrder"}


def licensed_for_mosaic_and_oi():
    """Mosaic datasets and oriented imagery datasets require a Standard or
    Advanced ArcGIS Pro license; both tools are unavailable under Basic."""
    try:
        product = arcpy.ProductInfo()
    except Exception:
        return True  # fail open - let the GP tool itself report a license error
    return product in ("ArcEditor", "ArcInfo", "ArcServer")  # Standard, Advanced, Server


def _log_gp_messages(log=print, prefix=""):
    """Log every message the most recently run GP tool produced, not just
    exceptions - a tool can complete without raising while still reporting
    something useful and otherwise silently discarded, e.g. "0 of 76 rows
    matched a recognized schema" or "image is not georeferenced, skipped".
    Always call this right after a GP tool call, success or not.
    """
    messages = arcpy.GetMessages()
    if messages:
        log(f"{prefix}{messages}")


# Esri's Table/Raster Catalog raster type documents PixelType as a Long
# Integer code, NOT a text string like arcpy.Describe's "U8"/"U16" -
# https://doc.esri.com/en/arcgis-pro/latest/help/data/imagery/files-tables-and-web-raster-types.html
# ("Table / Raster Catalog" section) confirms the exact required mapping.
_PIXEL_TYPE_CODES = {
    "U1": 0, "U2": 1, "U4": 2,
    "U8": 3, "S8": 4,
    "U16": 5, "S16": 6,
    "U32": 7, "S32": 8,
    "F32": 9, "F64": 10,
    "C64": 11, "C128": 12,
    "CI16": 13, "CI32": 14,
}


def _pixel_type_to_code(pixel_type_str, log=print):
    """Map an arcpy.Describe raster pixelType string (e.g. "U8") to Esri's
    Table/Raster Catalog PixelType Long Integer code (e.g. 3). Falls back to
    -1 (Unknown) for anything unrecognized, logging a warning rather than
    raising - an unmapped pixel type is still better represented as -1 than
    left out of a field the raster type documents as required.
    """
    code = _PIXEL_TYPE_CODES.get(str(pixel_type_str).upper())
    if code is None:
        log(f"WARNING: unrecognized pixel type '{pixel_type_str}' from arcpy.Describe - "
            "using -1 (Unknown) for the Table raster type's required PixelType field.")
        return -1
    return code


def _describe_raster_schema(raster_path, log=print):
    """Read nRows/nCols/nBands/PixelType from one raster via arcpy.Raster.

    CONFIRMED (2026-08-31, via https://doc.esri.com/en/arcgis-pro/latest/help/data/imagery/files-tables-and-web-raster-types.html):
    the Table/Raster Catalog raster type's own field table marks nRows,
    nCols, nBands, and PixelType as Required = Yes (not merely recommended) -
    a very plausible cause of the earlier bare, detail-free "ERROR 999999"
    crashes, and PixelType specifically must be one of Esri's own Long
    Integer codes (see _PIXEL_TYPE_CODES), not a raw type-name string.

    CONFIRMED (2026-09-01, live run): arcpy.Describe() on a perfectly valid
    TIFF (verified by inspecting its raw bytes - a normal LZW-compressed
    640x480 RGB TIFF) raised "DescribeData: Method height does not exist" -
    i.e. Describe returned some non-raster description type for this path
    instead of a RasterDataset one, for reasons that could not be reproduced
    outside a live ArcGIS Pro session. Esri's OWN documented code sample on
    that same raster-type doc page for this exact purpose uses
    `arcpy.Raster(path)` instead, whose .height/.width/.bandCount/.pixelType
    are dedicated Raster object properties (not a Describe dispatch) -
    switched to match Esri's own reference pattern.

    Returns None if the raster still can't be opened at all (itself a
    useful diagnostic signal, logged here rather than raised, since this
    lookup is best-effort - the mosaic load is still attempted without
    these columns, though it will likely now fail given they are documented
    as required).
    """
    try:
        raster = arcpy.Raster(str(raster_path))
        schema = {
            "nRows": int(raster.height),
            "nCols": int(raster.width),
            "nBands": int(raster.bandCount),
            "PixelType": _pixel_type_to_code(raster.pixelType, log=log),
        }
        log(f"Read raster schema from {raster_path}: {schema} (raw pixelType={raster.pixelType!r})")
        return schema
    except Exception as exc:  # noqa: BLE001 - best-effort diagnostic lookup, never fatal
        log(f"WARNING: could not open raster {raster_path} for required nRows/nCols/nBands/PixelType "
            f"fields: {exc}")
        return None


def _build_mosaic_input_table(compiled_csv_path, gdb_path, imagery_category="Nadir", compute_heading=False,
                               heading_offset=0.0, spatial_reference=None, log=print):
    """Build a real geodatabase table (explicit typed fields, in gdb_path -
    not the "memory" workspace) for the mosaic's generic "Table" raster type
    from the richer FCT/iFDO compiled table. Also exports that exact table
    to a CSV alongside the compiled table for human review (via
    arcpy.management.CopyRows, so the CSV reflects the table's real field
    types/values, not an independently written copy) - that CSV itself is
    NOT what gets passed to AddRastersToMosaicDataset, only the table is.

    CONFIRMED (2026-08-31): Extract Video Frames To Images' own .tfw world
    files are NOT real georeferencing - every frame gets an identical
    placeholder identity transform (pixel size 1, origin 0.5/-0.5, no real
    coordinates or CRS). Esri's Table/Raster Catalog raster type reserves
    xMin/xMax/yMin/yMax/SRS field names so a row can supply its own
    real-world extent instead - computed here via compute_frame_footprints()
    from camera height/HFOV/VFOV/heading (own value or
    IMAGERY_CATEGORY_DEFAULTS fallback), same inputs as the OID insert.
    CONFIRMED (also 2026-08-31): supplying those extents via a CSV wrapped
    in arcpy.management.MakeTableView still produced an identical, bare
    "ERROR 999999" from AddRastersToMosaicDataset - a CSV-backed table view
    infers field types from the text data rather than guaranteeing real
    DOUBLE fields for xMin/xMax/yMin/yMax. A real table with AddField-
    declared DOUBLE/TEXT fields, populated via arcpy.da.InsertCursor (the
    same pattern already proven reliable for the OID insert), removes that
    ambiguity - built in gdb_path itself (not "memory") in case the "Table"
    raster type's own validation treats a non-persistent workspace
    differently. Also adds nRows/nCols/nBands/PixelType - CONFIRMED
    (2026-08-31) via Esri's own Table/Raster Catalog field table
    (files-tables-and-web-raster-types.html) as Required = Yes, not merely
    optional/recommended - read once from the first resolvable raster via
    _describe_raster_schema() (assumed uniform across a single video's
    extracted frames), with PixelType mapped to Esri's required Long
    Integer code via _pixel_type_to_code() (NOT the raw "U8"-style string
    arcpy.Describe returns). Also adds StdTime/StdZ (2026-09-01) - Esri's
    field table only marks these Required for multidimensional rasters,
    which this data is not, but they are added anyway as optional
    passthrough metadata (harmless if blank) for continuity with the
    source FCT's own timestamp/depth values.
    """
    with open(compiled_csv_path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    positions = _extract_positions(rows)
    headings = _compute_headings(rows, positions, compute_heading, heading_offset)
    footprints = compute_frame_footprints(rows, positions, headings, imagery_category)

    srs_code = None
    if spatial_reference is not None:
        try:
            srs_code = spatial_reference.factoryCode or None
        except AttributeError:
            srs_code = None

    raster_schema = None
    for row in rows:
        raster_path = row.get("Raster")
        if raster_path:
            raster_schema = _describe_raster_schema(raster_path, log=log)
            break

    table_name = f"mosaic_input_{uuid.uuid4().hex[:8]}"
    table_path = f"{gdb_path}/{table_name}"
    arcpy.management.CreateTable(gdb_path, table_name)
    arcpy.management.AddField(table_path, "Raster", "TEXT", field_length=500)
    arcpy.management.AddField(table_path, "Name", "TEXT", field_length=255)
    arcpy.management.AddField(table_path, "AcquisitionDate", "DATE")
    for fname in ("xMin", "xMax", "yMin", "yMax"):
        arcpy.management.AddField(table_path, fname, "DOUBLE")
    if srs_code:
        arcpy.management.AddField(table_path, "SRS", "LONG")
    if raster_schema:
        for fname in ("nRows", "nCols", "nBands", "PixelType"):
            arcpy.management.AddField(table_path, fname, "LONG")
    # StdTime/StdZ are only Required by Esri's own field table for
    # multidimensional rasters (NetCDF/HDF/GRIB subdatasets), which this
    # project's plain 2D TIFF frames are not - added anyway, best-effort, as
    # optional passthrough metadata (harmless if blank) for continuity with
    # the source FCT's own timestamp/depth values, both as Esri-documented
    # Text fields.
    arcpy.management.AddField(table_path, "StdTime", "TEXT", field_length=25)
    arcpy.management.AddField(table_path, "StdZ", "TEXT", field_length=50)

    insert_fields = ["Raster", "Name", "AcquisitionDate", "xMin", "xMax", "yMin", "yMax"] + \
        (["SRS"] if srs_code else [])
    if raster_schema:
        insert_fields += ["nRows", "nCols", "nBands", "PixelType"]
    insert_fields += ["StdTime", "StdZ"]
    with arcpy.da.InsertCursor(table_path, insert_fields) as cursor:
        for row, (xmin, xmax, ymin, ymax) in zip(rows, footprints):
            raster_path = row.get("Raster")
            name = row.get("Filename") or Path(str(raster_path)).name
            acquisition_date = _parse_oid_datetime(row.get("AcquisitionDate") or row.get("Precision Time Stamp"))
            values = [raster_path, name, acquisition_date, xmin, xmax, ymin, ymax]
            if srs_code:
                values.append(srs_code)
            if raster_schema:
                values.extend([raster_schema["nRows"], raster_schema["nCols"],
                                raster_schema["nBands"], raster_schema["PixelType"]])
            std_time = acquisition_date.strftime("%Y-%m-%dT%H:%M:%S") if acquisition_date else None
            std_z = fusion_core.to_float(row.get("PerspectiveZ"))
            values.extend([std_time, str(std_z) if std_z is not None else None])
            cursor.insertRow(values)
    log(f"Built mosaic Table raster type input table with {len(rows)} row(s) -> {table_path}")

    csv_path = Path(compiled_csv_path).with_name(Path(compiled_csv_path).stem + "_MosaicInput.csv")
    try:
        if arcpy.Exists(str(csv_path)):
            arcpy.management.Delete(str(csv_path))
        arcpy.management.CopyRows(table_path, str(csv_path))
        log(f"Exported the exact mosaic Table raster type input for review -> {csv_path}")
    except arcpy.ExecuteError as exc:  # noqa: BLE001 - this export is diagnostic only, never fatal
        log(f"WARNING: could not export mosaic input table to CSV for review: {exc}")

    return table_path


def _ensure_spatial_reference(spatial_reference, log=print):
    """Return a usable arcpy.SpatialReference, falling back to WGS 1984 Web
    Mercator (EPSG:3857) for None, a raw string that can't be parsed, and an
    'Unknown'/unset SpatialReference object - the latter is a real, truthy
    object (not None), so a plain `spatial_reference or default` check does
    not catch a GP tool parameter that came back blank/uninitialized, and GP
    tools that require a spatial reference (e.g. Create Oriented Imagery
    Dataset) reject it with 'ERROR 000840: The value is not a Spatial
    Reference.'

    CONFIRMED (2026-09-01, live run): calling this tool directly via
    `arcpy.<toolbox>.BuildMosaicAndOrientedImageryDatasets(output_srs="PROJCRS[...]")`
    (a raw WKT string, not a GPCoordinateSystem-wrapped object) triggered the
    'blank or Unknown' fallback even though a real, valid EPSG:3857 WKT was
    supplied - logged here with the actual type/name/factoryCode so this is
    diagnosable instead of a silent guess. Handles two real causes: (1) the
    value arriving as a plain str instead of an arcpy.SpatialReference, and
    (2) a WKT string arcpy accepts structurally but can't fully resolve to a
    known WKID (e.g. this project's WKT uses a WKT2 DYNAMIC/FRAMEEPOCH/MODEL
    datum clause, which older arcpy WKT parsing may not fully support) - in
    that second case, falls back to the ID["EPSG", <code>] pair embedded at
    the end of the WKT itself, if present, and builds the SpatialReference
    from that plain WKID instead (bypassing the unsupported datum clause).
    """
    default_sr = arcpy.SpatialReference(3857)
    if spatial_reference is None:
        return default_sr

    wkt_text = None
    if isinstance(spatial_reference, str):
        wkt_text = spatial_reference.strip()
        if not wkt_text:
            log("WARNING: Output Coordinate System was blank or 'Unknown' - defaulting to "
                "WGS 1984 Web Mercator Auxiliary Sphere (EPSG:3857).")
            return default_sr
        parsed = arcpy.SpatialReference()
        try:
            parsed.loadFromString(wkt_text)
        except Exception as exc:  # noqa: BLE001 - fall through to the WKID-extraction fallback below
            log(f"WARNING: could not parse Output Coordinate System text as a spatial reference ({exc}).")
            parsed = None
        spatial_reference = parsed

    try:
        is_unknown = spatial_reference is None or (
            not spatial_reference.factoryCode and spatial_reference.name in (None, "", "Unknown")
        )
    except AttributeError:
        is_unknown = True

    if is_unknown and wkt_text:
        # Last resort: pull a bare "ID["EPSG", <code>]" pair straight out of
        # the WKT text (present at the end of every Esri-exported WKT) and
        # build the SpatialReference from that WKID directly, sidestepping
        # whatever datum clause arcpy's WKT parser couldn't fully resolve.
        match = re.search(r'ID\[\s*"EPSG"\s*,\s*(\d+)\s*\]', wkt_text)
        if match:
            candidate = arcpy.SpatialReference(int(match.group(1)))
            if candidate.factoryCode:
                log(f"Recovered spatial reference EPSG:{match.group(1)} from the Output Coordinate System "
                    "WKT's embedded ID clause (the full WKT string - likely due to its WKT2 DYNAMIC/"
                    "FRAMEEPOCH/MODEL datum clause - did not resolve to a known WKID directly).")
                return candidate

    if is_unknown:
        name = getattr(spatial_reference, "name", None)
        code = getattr(spatial_reference, "factoryCode", None)
        log(f"WARNING: Output Coordinate System was blank or 'Unknown' (type={type(spatial_reference).__name__}, "
            f"name={name!r}, factoryCode={code!r}) - defaulting to WGS 1984 Web Mercator Auxiliary Sphere (EPSG:3857).")
        return default_sr
    return spatial_reference


_GP_TOOL_CACHE = {}


def _resolve_gp_tool(tool_name, preferred_modules=("oi", "management", "ia", "sa", "da")):
    """Find a geoprocessing tool function by name across arcpy's submodules.

    Esri's tool reference labels these "(Oriented Imagery Tools)" but does not
    consistently expose them under arcpy.management - confirmed to fail with
    AttributeError on a real ArcGIS Pro 3.7 install. Rather than hardcode a
    second guess that could also be wrong on some installs, every likely
    namespace, then every arcpy submodule, is probed once and the result is
    cached by tool name.
    """
    if tool_name in _GP_TOOL_CACHE:
        return _GP_TOOL_CACHE[tool_name]

    tool = getattr(arcpy, tool_name, None)
    if not callable(tool):
        for module_name in preferred_modules:
            module = getattr(arcpy, module_name, None)
            candidate = getattr(module, tool_name, None) if module else None
            if callable(candidate):
                tool = candidate
                break
        else:
            tool = None
            for name in dir(arcpy):
                module = getattr(arcpy, name, None)
                if isinstance(module, types.ModuleType):
                    candidate = getattr(module, tool_name, None)
                    if callable(candidate):
                        tool = candidate
                        break

    if not callable(tool):
        raise AttributeError(
            f"Could not locate the geoprocessing tool '{tool_name}' anywhere under arcpy on this "
            "ArcGIS Pro install (checked arcpy directly, "
            f"{', '.join('arcpy.' + m for m in preferred_modules)}, and every other arcpy submodule). "
            "Run `import arcpy; print(arcpy.ListToolboxes())` in this ArcGIS Pro's Python window to find "
            "the toolbox this tool belongs to on this install, then update FCT_MosaicOrientedImagery_core.py."
        )

    _GP_TOOL_CACHE[tool_name] = tool
    return tool


# ============================================================================
# Geodatabase / dataset setup
# ============================================================================
def ensure_geodatabase(gdb_path, log=print):
    """Return an existing geodatabase path as-is, or create a new file
    geodatabase if gdb_path doesn't exist yet and ends in .gdb."""
    gdb_path = str(gdb_path)
    if arcpy.Exists(gdb_path):
        return gdb_path
    if not gdb_path.lower().endswith(".gdb"):
        raise ValueError(
            f"Output geodatabase does not exist and is not a .gdb path: {gdb_path}"
        )
    parent = str(Path(gdb_path).parent)
    name = Path(gdb_path).name
    arcpy.management.CreateFileGDB(parent, name)
    log(f"Created file geodatabase: {gdb_path}")
    return gdb_path


def create_mosaic_dataset(gdb_path, name, spatial_reference, num_bands=3,
                           pixel_type="8_BIT_UNSIGNED", log=print):
    """Create an empty mosaic dataset. Returns its catalog path."""
    if arcpy.Exists(f"{gdb_path}/{name}"):
        log(f"Mosaic dataset already exists, reusing: {gdb_path}/{name} "
            "(its coordinate system/pixel type will NOT be changed to match this run's settings)")
        return f"{gdb_path}/{name}"
    arcpy.management.CreateMosaicDataset(
        gdb_path, name, spatial_reference, num_bands, pixel_type
    )
    mosaic_path = f"{gdb_path}/{name}"
    log(f"Created mosaic dataset: {mosaic_path}")
    return mosaic_path


def add_rasters_to_mosaic(mosaic_path, raster_type, input_data, camera_table_csv=None,
                           imagery_category="Nadir", compute_heading=False, heading_offset=0.0,
                           spatial_reference=None, gdb_path=None, log=print):
    """Load one raster source into an existing mosaic dataset.

    raster_type is MOSAIC_RASTER_TYPE_TABLE (input_data is the compiled
    Name/Path/FCT/iFDO table - real per-image extents are computed from the
    camera model, since each frame's own .tfw is a placeholder identity
    transform, not real georeferencing) or
    MOSAIC_RASTER_TYPE_FRAME_CAMERA (input_data is the original Frame Table
    CSV; footprints are computed photogrammetrically using camera_table_csv,
    supplied via that raster type's "CameraFile" auxiliary input).
    """
    # The "Table" raster type failed to recognize a bare compiled CSV path
    # live (crawled 0 items); a CSV-backed MakeTableView with computed
    # xMin/xMax/yMin/yMax also crashed with a bare ERROR 999999 (likely
    # CSV-inferred field types, not real DOUBLE, for those reserved names) -
    # a real geodatabase table with explicit typed fields is used instead.
    # "Frame Camera" is left as a raw CSV path - Esri's own doc explicitly
    # supports that for this raster type and it has not shown either problem.
    mosaic_input_table = None
    if raster_type == MOSAIC_RASTER_TYPE_TABLE:
        mosaic_input_table = _build_mosaic_input_table(
            input_data, gdb_path, imagery_category=imagery_category, compute_heading=compute_heading,
            heading_offset=heading_offset, spatial_reference=spatial_reference, log=log
        )
        gp_input_data = mosaic_input_table
    else:
        gp_input_data = str(input_data)

    # Esri's docs specify CameraFile as "a .cam or .csv file" (a raw file
    # path, unlike Input Data's Table option) - confirmed the true cause of
    # a live "Unable to load camera table." failure was NOT the file-path
    # format at all: Esri's Frames table schema requires an Omega/Phi/Kappa
    # triplet or a Matrix field for exterior orientation, and this project's
    # Frame Table has neither, so this will fail regardless of path format
    # unless the caller has added those columns.
    aux_inputs = f"CameraFile #{camera_table_csv}#" if camera_table_csv else None

    try:
        try:
            log(f"AddRastersToMosaicDataset auxiliary inputs: {aux_inputs!r}")
            arcpy.management.AddRastersToMosaicDataset(
                mosaic_path,
                raster_type,
                gp_input_data,
                True,   # Update Cell Size Ranges
                True,   # Update Boundary
                False,  # Update Overviews
                None, None, None,   # Maximum Levels, Maximum Cell Size, Minimum Rows or Columns
                spatial_reference if raster_type == MOSAIC_RASTER_TYPE_TABLE else None,   # Coordinate System for Input Data
                None,   # Input Data Filter
                True,   # Include Sub Folders
                # CONFIRMED (2026-09-01, via add-rasters-to-mosaic-dataset.html):
                # the documented duplicate_items_action keyword is
                # "EXCLUDE_DUPLICATES" (all caps, underscore) - this ran and
                # behaved correctly with the human-readable "Exclude
                # duplicates" live, but aligning it with the exact
                # documented keyword removes any reliance on arcpy's keyword
                # matching being lenient.
                "EXCLUDE_DUPLICATES",   # Add New Datasets Only - skips rows already added on a re-run
                False,  # Build Raster Pyramids
                False,  # Calculate Statistics
                False,  # Build Thumbnails
                f"Deep ocean video frame import ({raster_type})",   # Operation Description
                False,  # Force this Coordinate System for Input Data
                False,  # Estimate Mosaic Dataset Statistics
                aux_inputs,   # Auxiliary Inputs
            )
        except arcpy.ExecuteError:
            # Captured here, before the cleanup call below runs - Delete() is
            # itself a GP tool call and would otherwise reset arcpy's message
            # stack before the caller could ever read the real failure reason.
            # Unfiltered GetMessages() (not just severity 2) in case an
            # info/warning message logged just before the crash is useful.
            raise RuntimeError(arcpy.GetMessages()) from None
        _log_gp_messages(log, prefix="AddRastersToMosaicDataset messages: ")
    finally:
        if mosaic_input_table:
            arcpy.management.Delete(mosaic_input_table)

    item_count = arcpy.management.GetCount(mosaic_path)[0]
    log(f"Added rasters from {input_data} to mosaic dataset {mosaic_path} "
        f"(raster type: {raster_type}); mosaic dataset now has {item_count} item(s) total")

    if raster_type == MOSAIC_RASTER_TYPE_TABLE:
        _enable_mosaic_acquisition_time(mosaic_path, log=log)


def _enable_mosaic_acquisition_time(mosaic_path, log=print):
    """Best-effort: enable time on the mosaic dataset keyed by the
    per-item AcquisitionDate field written by _build_mosaic_input_table(),
    so each frame's capture timestamp is queryable/orderable the same way
    it already is in the OID's own AcquisitionDate field. Wrapped so a
    parameter mismatch or unsupported environment never fails the run -
    the mosaic dataset and its items are already loaded at this point.

    CONFIRMED (2026-09-01, via set-mosaic-dataset-properties.html):
    "If the field [order_field] is a numeric or date field, the order_base
    parameter must be set." AcquisitionDate is a DATE field and order_base
    was previously omitted entirely - this ran without raising, but ordering
    relative to an unset base is undefined per Esri's own doc, so an
    explicit epoch (matches order_base's own documented date-string formats)
    is passed now.
    """
    try:
        arcpy.management.SetMosaicDatasetProperties(
            mosaic_path,
            default_mosaic_method="ByAttribute",
            order_field="AcquisitionDate",
            order_base="1/1/1970",
            sorting_order="ASCENDING",
            use_time="ENABLED",
            start_time_field="AcquisitionDate",
        )
        log(f"Enabled time on {mosaic_path} using AcquisitionDate")
    except arcpy.ExecuteError:
        log(f"WARNING: could not enable time on {mosaic_path}: {arcpy.GetMessages(2)}")


def build_footprints_and_boundary(mosaic_path, build_footprints=True, build_boundary=True, log=print):
    """CONFIRMED (2026-09-01, live run): Build Footprints' reset_footprint
    (the Pro UI calls this "Computation Method", but arcpy's actual
    parameter name is reset_footprint) defaults to "RADIOMETRY" (Esri's own
    doc: "Exclude pixels with a value outside of a defined range... This is
    the default."), which re-derives each item's footprint from the
    raster's own valid-pixel extent, discarding the xMin/xMax/yMin/yMax
    real-world extent _build_mosaic_input_table() wrote via the Table
    raster type.

    "GEOMETRY" ("Restore the footprint to its original geometry") was tried
    first and is ALSO wrong for this data: it restores the footprint to the
    raster's own *native* georeferencing (undoing any radiometric trim), not
    to whatever custom extent was assigned during AddRastersToMosaicDataset.
    Since every frame's own .tfw is a placeholder identity transform (pixel
    size 1, origin ~0), that native geometry sits at/near the coordinate
    origin - confirmed live as the "null island" mosaic footprint. "NONE"
    ("Do not redefine the footprints") is the one that actually leaves the
    real xMin/xMax/yMin/yMax extent AddRastersToMosaicDataset already
    assigned from the Table raster type's input table untouched.
    """
    if build_footprints:
        arcpy.management.BuildFootprints(mosaic_path, reset_footprint="NONE")
        log(f"Built footprints for {mosaic_path} (reset_footprint=NONE, leaving the real "
            "extent already assigned from the Table raster type's input untouched)")
    if build_boundary:
        arcpy.management.BuildBoundary(mosaic_path)
        log(f"Built boundary for {mosaic_path}")


# ============================================================================
# Folder layout + iFDO-enriched table compilation
# ============================================================================
def resolve_export_layout(input_folder):
    """Detect whether input_folder is a flat export (images, Frame/Camera
    Tables, and optional sidecars all together - the original Extract Video
    Frames To Images layout, or that same folder fused in place) or the
    Extracted Frame Image Metadata Generation tool's structured "Export to Output
    Folder" layout (images/, metadata/, and *.csv side by side)."""
    root = Path(input_folder)
    images_dir = root / fusion_core.EXPORT_FOLDER_IMAGES_DIR
    metadata_dir = root / fusion_core.EXPORT_FOLDER_METADATA_DIR
    if images_dir.is_dir():
        return {
            "root": root,
            "images_dir": images_dir,
            "metadata_dir": metadata_dir if metadata_dir.is_dir() else images_dir,
            "structured": True,
        }
    return {"root": root, "images_dir": root, "metadata_dir": root, "structured": False}


def _index_consolidated_ifdo(layout, log=print):
    """Pre-index every *.ifdo.json's image-set-items by filename once per
    compile_frame_table_with_ifdo() call, so a large frame set doesn't re-read
    and re-parse the same consolidated file for every single row -
    _sidecar_ifdo_for_image() used to do exactly that.

    Handles the current filename-keyed object form (what the iFDO v2.2.1
    schema requires) and the legacy list-of-{"$filename": ...} form. The
    export root is searched too: the single set-level document is written
    there, beside the Frame/Camera Tables, not in metadata/."""
    index = {}
    search_dirs = list(dict.fromkeys([layout["root"], layout["metadata_dir"], layout["images_dir"]]))
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for candidate in directory.glob("*.ifdo.json"):
            try:
                doc = json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log(f"WARNING: could not read {candidate.name}: {exc}")
                continue
            items = doc.get("image-set-items")
            if isinstance(items, list):
                for item in items:
                    filename = item.get("$filename")
                    if filename and filename not in index:
                        index[filename] = {k: v for k, v in item.items() if k != "$filename"}
            elif isinstance(items, dict):
                for filename, fields in items.items():
                    if filename and filename not in index and isinstance(fields, dict):
                        index[filename] = fields
    return index


def _sidecar_ifdo_for_image(image_name, layout, consolidated_index=None, log=print):
    """Best-effort iFDO field lookup for one image: a per-image *.ifdo.json
    sidecar first, then the pre-built consolidated set-level index, then the
    *.aux.xml sidecar's "iFDO" metadata domain. Returns {} if none is found -
    iFDO enrichment is optional, never a reason to fail a row."""
    search_dirs = list(dict.fromkeys([layout["metadata_dir"], layout["images_dir"]]))

    for directory in search_dirs:
        per_image = directory / f"{image_name}.ifdo.json"
        if per_image.is_file():
            try:
                doc = json.loads(per_image.read_text(encoding="utf-8"))
                items = doc.get("image-set-items", {})
                if isinstance(items, dict):
                    return items.get(image_name, {})
            except (json.JSONDecodeError, OSError) as exc:
                log(f"WARNING: could not read {per_image.name}: {exc}")

    if consolidated_index and image_name in consolidated_index:
        return consolidated_index[image_name]

    for directory in search_dirs:
        aux_path = directory / f"{image_name}.aux.xml"
        if aux_path.is_file():
            try:
                root_elem = ET.parse(aux_path).getroot()
                for metadata_elem in root_elem.findall("./Metadata[@domain='iFDO']"):
                    return {
                        mdi.get("key"): mdi.text
                        for mdi in metadata_elem.findall("MDI")
                        if mdi.get("key")
                    }
            except ET.ParseError as exc:
                log(f"WARNING: could not parse {aux_path.name}: {exc}")

    return {}


def compile_frame_table_with_ifdo(frame_table_csv, layout, output_csv_path, log=print):
    """Build a single table for raster/OID ingestion that starts from the
    original Frame Table columns (still a valid FrameCamera-schema table),
    corrects the "Raster" path to wherever the image actually resolves to
    under layout, and appends each image's iFDO fields as extra ifdo_*
    columns. Returns the output CSV path."""
    rows = fusion_core.read_metadata_table(str(frame_table_csv))
    if not rows:
        raise ValueError(f"Frame table is empty: {frame_table_csv}")
    original_fields = list(rows[0].keys())
    consolidated_index = _index_consolidated_ifdo(layout, log=log)

    extra_fields = []
    rows_with_ifdo = 0
    for row in rows:
        raw_reference = row.get("Raster") or row.get("Filename") or ""
        resolved = fusion_core.find_image_path(raw_reference, layout["images_dir"])
        image_name = resolved.name if resolved is not None else Path(str(raw_reference)).name
        if resolved is not None:
            row["Raster"] = str(resolved)
        else:
            log(f"WARNING: could not resolve image path for '{raw_reference}' - keeping original value.")

        ifdo_fields = _sidecar_ifdo_for_image(image_name, layout, consolidated_index, log=log)
        if ifdo_fields:
            rows_with_ifdo += 1
        for key, value in ifdo_fields.items():
            column = "ifdo_" + key.replace("-", "_")
            row[column] = value
            if column not in original_fields and column not in extra_fields:
                extra_fields.append(column)

    fieldnames = original_fields + sorted(extra_fields)
    output_csv_path = Path(output_csv_path)
    output_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)

    log(f"Compiled {len(rows)} row(s) ({rows_with_ifdo} with iFDO metadata, "
        f"{len(extra_fields)} iFDO column(s)) -> {output_csv_path}")
    return str(output_csv_path)


# ============================================================================
# Oriented Imagery Dataset
# ============================================================================
def create_oriented_imagery_dataset(gdb_path, name, spatial_reference,
                                     elevation_source=ELEVATION_SOURCE_NONE,
                                     constant_elevation=0.0, dem_path=None,
                                     has_z=True, log=print):
    """Create an empty oriented imagery dataset. Returns its catalog path."""
    oid_path = f"{gdb_path}/{name}"
    if arcpy.Exists(oid_path):
        log(f"Oriented imagery dataset already exists, reusing: {oid_path} "
            "(its coordinate system/elevation source will NOT be changed to match this run's settings)")
        return oid_path

    elevation_kw = {
        ELEVATION_SOURCE_NONE: None,
        ELEVATION_SOURCE_CONSTANT: "CONSTANT_ELEVATION",
        ELEVATION_SOURCE_DEM: "DEM",
    }[elevation_source]

    spatial_reference = _ensure_spatial_reference(spatial_reference, log=log)
    create_oid = _resolve_gp_tool("CreateOrientedImageryDataset")
    try:
        log(f"CreateOrientedImageryDataset parameters on this install: "
            f"{list(inspect.signature(create_oid).parameters.keys())}")
    except (TypeError, ValueError):
        pass
    create_oid(
        gdb_path, name, spatial_reference,
        elevation_kw,
        constant_elevation if elevation_source == ELEVATION_SOURCE_CONSTANT else None,
        dem_path if elevation_source == ELEVATION_SOURCE_DEM else None,
        None,   # Level of Detail
        None,   # Raster Function
        None,   # Template Datasets
        None,   # Oriented Imagery Dataset Alias
        None,   # Configuration Keyword
        "YES" if has_z else "NO",
    )
    log(f"Created oriented imagery dataset: {oid_path}")
    return oid_path


def _ensure_fields(table_path, field_specs, log=print):
    """Add any (name, type, length) fields from field_specs that don't
    already exist on table_path."""
    existing = {f.name.lower() for f in arcpy.ListFields(table_path)}
    for name, ftype, length in field_specs:
        if name.lower() in existing:
            continue
        if length:
            arcpy.management.AddField(table_path, name, ftype, field_length=length)
        else:
            arcpy.management.AddField(table_path, name, ftype)
        log(f"Added field '{name}' ({ftype}) to {table_path}")


def _sanitize_field_name(name):
    """Turn an arbitrary compiled-table column name into a valid, unique-ish
    geodatabase field name (letters/digits/underscores, not starting with a
    digit, max 64 characters)."""
    sanitized = "".join(ch if ch.isalnum() else "_" for ch in str(name)).strip("_") or "field"
    if sanitized[0].isdigit():
        sanitized = f"f_{sanitized}"
    return sanitized[:64]


def _parse_oid_datetime(raw_value):
    """Best-effort parse of a compiled-table timestamp into a Python
    datetime for the OID's DATE field. Returns None (field left null)
    rather than raising - a missing/unparsable date is not worth failing a
    row over."""
    if fusion_core.is_null_like(raw_value):
        return None
    parsed = fusion_core.parse_datetime(raw_value)
    if parsed is not None:
        return parsed.replace(tzinfo=None)
    text = str(raw_value).strip()
    for candidate in (text, text.replace(" ", "T", 1), text.replace("Z", "")):
        try:
            return datetime.datetime.fromisoformat(candidate).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _compute_bearing(x1, y1, x2, y2):
    """Bearing in degrees clockwise from north, from point 1 to point 2 -
    a flat-plane approximation, adequate for consecutive-frame distances."""
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return None
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _extract_positions(rows):
    """Return a (x, y) list from each row's PerspectiveX/Y, aligned by index -
    shared by the OID insert and the mosaic Table footprint computation so
    both derive the same real-world position per frame."""
    return [(fusion_core.to_float(row.get("PerspectiveX")), fusion_core.to_float(row.get("PerspectiveY")))
            for row in rows]


def _compute_headings(rows, positions, compute_heading=False, heading_offset=0.0):
    """Return one heading (degrees) per row, aligned by index.

    Uses each row's own CameraHeading when present (unless compute_heading
    forces a recompute), otherwise the bearing to the next frame's position,
    otherwise the previous row's resolved heading, otherwise 0.0 - never
    None and never Esri's -999 "unknown" sentinel, since
    BuildOrientedImageryFootprint rejects -999 as invalid live.
    """
    headings = []
    last_valid_heading = None
    for i, row in enumerate(rows):
        x, y = positions[i]
        heading = fusion_core.to_float(row.get("CameraHeading"))
        if compute_heading or heading is None:
            next_x, next_y = positions[i + 1] if i + 1 < len(positions) else (None, None)
            computed = _compute_bearing(x, y, next_x, next_y) if (x is not None and next_x is not None) else None
            if computed is not None:
                heading = computed
        if heading is None:
            heading = last_valid_heading if last_valid_heading is not None else 0.0
        heading = (heading + heading_offset) % 360.0
        last_valid_heading = heading
        headings.append(heading)
    return headings


def _compute_frame_extent_xy(x, y, heading_deg, camera_height, hfov_deg, vfov_deg):
    """Axis-aligned bounding box (xmin, xmax, ymin, ymax), in ground units,
    of one frame's nadir footprint rectangle rotated by heading - the ground
    rectangle's half-width/half-height come from camera height and HFOV/VFOV
    (same trigonometry as a simple pinhole nadir camera footprint), centered
    at (x, y) and rotated so its "forward" (VFOV) edge points along heading.
    """
    half_w = abs(camera_height) * math.tan(math.radians(min(max(hfov_deg, 0.0), 179.0) / 2.0))
    half_h = abs(camera_height) * math.tan(math.radians(min(max(vfov_deg, 0.0), 179.0) / 2.0))
    theta = math.radians(heading_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    xs, ys = [], []
    for dx, dy in ((-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)):
        xs.append(x + dx * cos_t + dy * sin_t)
        ys.append(y - dx * sin_t + dy * cos_t)
    return min(xs), max(xs), min(ys), max(ys)


def _resolve_camera_height(row, default):
    """Camera height above the ground, in meters - CONFIRMED (2026-09-01)
    the raw FCT rarely has a literal "CameraHeight" column (Extract Video
    Frames To Images calls it "Sensor Altitude" instead), so this silently
    fell back to the IMAGERY_CATEGORY_DEFAULTS value (e.g. 200m for Nadir)
    even when the row's real altitude was available under a different name.
    Falls back through fusion_core's own CANDIDATE_ALTITUDE_FIELDS (Sensor
    Altitude/Platform Altitude/Altitude/PerspectiveZ) before the category
    default, same as this project's iFDO altitude resolution.
    """
    height = fusion_core.to_float(row.get("CameraHeight"))
    if height is None:
        height = fusion_core.get_altitude(row)
    return height if height is not None else default


def compute_frame_footprints(rows, positions, headings, imagery_category):
    """Return one (xmin, xmax, ymin, ymax) real-world extent per row, using
    each row's own CameraHeight/HorizontalFieldOfView/VerticalFieldOfView
    when present, else the per-category defaults (IMAGERY_CATEGORY_DEFAULTS) -
    the same fallback values insert_rows_into_oid() uses, so the mosaic's
    footprints and the OID's are computed from consistent inputs. A row with
    no resolvable position falls back to a zero-size point extent rather
    than raising, since a missing position is a per-row data problem, not a
    reason to fail loading the whole table.
    """
    pitch_d, roll_d, hfov_d, vfov_d, height_d, near_d, far_d = IMAGERY_CATEGORY_DEFAULTS.get(
        imagery_category, IMAGERY_CATEGORY_DEFAULTS["Nadir"]
    )
    footprints = []
    for row, (x, y), heading in zip(rows, positions, headings):
        if x is None or y is None:
            footprints.append((0.0, 0.0, 0.0, 0.0))
            continue
        height = _resolve_camera_height(row, height_d)
        hfov = fusion_core.to_float(row.get("HorizontalFieldOfView"))
        hfov = hfov if hfov is not None else hfov_d
        vfov = fusion_core.to_float(row.get("VerticalFieldOfView"))
        vfov = vfov if vfov is not None else vfov_d
        footprints.append(_compute_frame_extent_xy(x, y, heading, height, hfov, vfov))
    return footprints


def insert_rows_into_oid(oid_path, compiled_csv_path, imagery_category,
                          include_extra_fields=True, compute_heading=False,
                          heading_offset=0.0, log=print):
    """Populate an oriented imagery dataset directly via arcpy.da.InsertCursor
    against its own documented, stable attribute schema (OID_STANDARD_FIELDS),
    instead of AddImagesToOrientedImageryDataset's CSV/table schema
    auto-detection - confirmed live to silently add 0 rows for this
    project's compiled table. Returns the number of rows inserted.
    """
    _ensure_fields(oid_path, OID_STANDARD_FIELDS, log=log)
    _ensure_fields(oid_path, _OID_SRS_FIELDS, log=log)

    with open(compiled_csv_path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        log(f"WARNING: no rows to insert from {compiled_csv_path}")
        return 0

    pitch_d, roll_d, hfov_d, vfov_d, height_d, near_d, far_d = IMAGERY_CATEGORY_DEFAULTS.get(
        imagery_category, IMAGERY_CATEGORY_DEFAULTS["Nadir"]
    )

    extra_field_map = []  # (source_column, sanitized_field_name)
    if include_extra_fields:
        seen = set()
        used_names = set()
        for key in rows[0].keys():
            if (key in _OID_MAPPED_SOURCE_COLUMNS or key in _OID_EXCLUDED_IFDO_COLUMNS
                    or key in _OID_EXCLUDED_FCT_COLUMNS or key in seen):
                continue
            seen.add(key)
            fname = _sanitize_field_name(key)
            if fname in used_names:
                fname = f"{fname}_{len(used_names)}"[:64]
            used_names.add(fname)
            extra_field_map.append((key, fname))
        _ensure_fields(oid_path, [(fname, "TEXT", 255) for _, fname in extra_field_map], log=log)

    # Extract Video Frames To Images assigns an incrementing CameraID per
    # frame (1, 2, 3, ...) rather than a true camera identifier - this
    # project's own data is always a single physical camera for the whole
    # collection, so every OID row uses the first row's CameraID value
    # instead of passing the raw per-frame value through unchanged.
    canonical_camera_id = rows[0].get("CameraID") if rows else None

    has_z = bool(arcpy.Describe(oid_path).hasZ)
    shape_token = "SHAPE@XYZ" if has_z else "SHAPE@XY"
    srs_field_names = [name for name, _, _ in _OID_SRS_FIELDS]
    insert_fields = [shape_token] + [name for name, _, _ in OID_STANDARD_FIELDS] + \
        srs_field_names + [fname for _, fname in extra_field_map]

    positions = _extract_positions(rows)
    # Esri documents -999 as the "unknown heading" sentinel, but
    # BuildOrientedImageryFootprint rejects it live with "ERROR 003761:
    # Required property value is missing or invalid : Camera Heading" - so
    # _compute_headings() always resolves a real numeric value instead.
    headings = _compute_headings(rows, positions, compute_heading, heading_offset)

    inserted = 0
    skipped = 0
    with arcpy.da.InsertCursor(oid_path, insert_fields) as cursor:
        for i, row in enumerate(rows):
            x, y = positions[i]
            if x is None or y is None:
                skipped += 1
                log(f"WARNING: skipping row with no PerspectiveX/Y (Raster={row.get('Raster')})")
                continue
            z = fusion_core.to_float(row.get("PerspectiveZ")) or 0.0
            heading = headings[i]

            values = {
                "Name": row.get("Filename") or Path(str(row.get("Raster", ""))).name,
                "ImagePath": row.get("Raster"),
                "AcquisitionDate": _parse_oid_datetime(row.get("AcquisitionDate") or row.get("Precision Time Stamp")),
                "CameraHeading": heading,
                "CameraPitch": fusion_core.to_float(row.get("CameraPitch")) if fusion_core.to_float(row.get("CameraPitch")) is not None else pitch_d,
                "CameraRoll": fusion_core.to_float(row.get("CameraRoll")) if fusion_core.to_float(row.get("CameraRoll")) is not None else roll_d,
                "CameraHeight": _resolve_camera_height(row, height_d),
                "HorizontalFieldOfView": fusion_core.to_float(row.get("HorizontalFieldOfView")) if fusion_core.to_float(row.get("HorizontalFieldOfView")) is not None else hfov_d,
                "VerticalFieldOfView": fusion_core.to_float(row.get("VerticalFieldOfView")) if fusion_core.to_float(row.get("VerticalFieldOfView")) is not None else vfov_d,
                "NearDistance": fusion_core.to_float(row.get("NearDistance")) if fusion_core.to_float(row.get("NearDistance")) is not None else near_d,
                "FarDistance": fusion_core.to_float(row.get("FarDistance")) if fusion_core.to_float(row.get("FarDistance")) is not None else far_d,
                "OrientedImageryType": imagery_category,
                # Populated afterward, once, across the whole OID by
                # _assign_oid_sequence_order() - a per-pair insert can't
                # know its position relative to rows from other pairs.
                "SequenceOrder": None,
            }

            # Extract Video Frames To Images' raw "SRS" column is a
            # "horizontal;vertical" WKID pair (e.g. "3857;3855") - split
            # instead of passed through as a single field named "SRS",
            # since Esri's own OID "SRS" field takes only one CS value.
            srs_parts = str(row.get("SRS") or "").split(";")
            srs_values = [
                srs_parts[0].strip() if len(srs_parts) > 0 and srs_parts[0].strip() else None,
                srs_parts[1].strip() if len(srs_parts) > 1 and srs_parts[1].strip() else None,
            ]

            shape_value = (x, y, z) if has_z else (x, y)
            extra_values = [
                canonical_camera_id if orig == "CameraID" else row.get(orig, "")
                for orig, _ in extra_field_map
            ]
            row_values = [shape_value] + [values[name] for name, _, _ in OID_STANDARD_FIELDS] + \
                srs_values + extra_values
            cursor.insertRow(row_values)
            inserted += 1

    log(f"Inserted {inserted} row(s) directly into oriented imagery dataset {oid_path} "
        f"({skipped} row(s) skipped for missing position)")
    return inserted


def assign_oid_sequence_order(oid_path, log=print):
    """Number every row in an oriented imagery dataset's SequenceOrder field
    (1, 2, 3, ...) in AcquisitionDate order - run once across the whole OID
    after all Frame/Camera Table pairs have been inserted, since a per-pair
    insert has no way to know its position relative to rows from other
    pairs. Rows with a null AcquisitionDate sort last (SQL default) and
    still get numbered, just at the end. Best-effort: logs a WARNING rather
    than raising, since a missing SequenceOrder does not stop the OID from
    working, only sequential navigation ordering.

    Reads the sort order and writes SequenceOrder via two separate cursors
    (a plain SearchCursor for the ORDER BY, an unsorted UpdateCursor keyed
    by OID for the write) rather than one UpdateCursor with an ORDER BY
    sql_clause - confirmed live (2026-09-03) that combining ORDER BY with
    UpdateCursor.updateRow() against a file geodatabase OID fails with a
    generic "returned NULL without setting an exception" SWIG-layer error
    instead of a real Python exception; sorting and writing through
    separate cursors avoids that combination entirely.
    """
    try:
        sequence_by_oid = {}
        with arcpy.da.SearchCursor(
            oid_path, ["OID@"], sql_clause=(None, "ORDER BY AcquisitionDate ASC")
        ) as cursor:
            for i, row in enumerate(cursor, start=1):
                sequence_by_oid[row[0]] = i
        with arcpy.da.UpdateCursor(oid_path, ["OID@", "SequenceOrder"]) as cursor:
            for row in cursor:
                row[1] = sequence_by_oid.get(row[0])
                cursor.updateRow(row)
        log(f"Assigned SequenceOrder (1-{len(sequence_by_oid)}) by AcquisitionDate order on {oid_path}")
    except Exception as exc:  # noqa: BLE001 - sequential navigation ordering only, never fatal
        log(f"WARNING: could not assign SequenceOrder on {oid_path}: {exc}")


def _normalize_path_for_compare(path_str):
    """Normalize a path for equality comparison (slash direction, case, and
    '.'/'..' segments) - Esri's OID may store ImagePath in a different form
    than what we wrote, which would otherwise make a plain string comparison
    miss real duplicates."""
    return os.path.normcase(os.path.normpath(str(path_str))) if path_str else path_str


def _existing_oid_image_paths(oid_path, log=print):
    """Return the set of ImagePath values already present in an oriented
    imagery dataset, for de-duplicating re-runs against the same OID -
    AddImagesToOrientedImageryDataset has no built-in duplicate handling,
    unlike AddRastersToMosaicDataset's "Add New Datasets Only" option. Never
    raises - an empty set (checked, nothing found) or an unreadable table
    both mean "add everything", since de-duplication is a nice-to-have, not
    a reason to fail a run."""
    if not arcpy.Exists(oid_path):
        return set()
    try:
        field_names = [f.name for f in arcpy.ListFields(oid_path)]
        path_field = fusion_core.resolve_field(
            field_names, ["ImagePath", "Image_Path", "IMAGEPATH"], required=False
        )
        if not path_field:
            log(f"WARNING: no ImagePath-like field found on {oid_path} "
                f"(fields: {field_names}) - cannot de-duplicate, adding all rows.")
            return set()
        with arcpy.da.SearchCursor(oid_path, [path_field]) as cursor:
            existing = {_normalize_path_for_compare(row[0]) for row in cursor if row[0]}
        log(f"Found {len(existing)} existing '{path_field}' value(s) in {oid_path} for de-duplication.")
        return existing
    except Exception as exc:  # noqa: BLE001 - de-dup is best-effort, never fatal
        log(f"WARNING: could not read existing image paths from {oid_path} for de-duplication: {exc}")
        return set()


def filter_new_images_for_oid(compiled_csv_path, oid_path, log=print):
    """Write a copy of compiled_csv_path with rows already present in
    oid_path's ImagePath field removed.

    Returns (path_or_None, skipped_count): path_or_None is the original
    compiled_csv_path if nothing needed filtering, a new "<name>_New.csv"
    if some rows were removed, or None if every row was already present
    (nothing new to add for this pair).
    """
    existing_paths = _existing_oid_image_paths(oid_path, log=log)
    if not existing_paths:
        return str(compiled_csv_path), 0

    compiled_csv_path = Path(compiled_csv_path)
    with open(compiled_csv_path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        rows = list(reader)

    new_rows = [
        row for row in rows
        if _normalize_path_for_compare(row.get("Raster")) not in existing_paths
    ]
    skipped = len(rows) - len(new_rows)
    if skipped == 0:
        return str(compiled_csv_path), 0
    if not new_rows:
        return None, skipped

    output_path = compiled_csv_path.with_name(f"{compiled_csv_path.stem}_New.csv")
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(new_rows)
    log(f"Skipped {skipped} image(s) already present in {oid_path}; "
        f"{len(new_rows)} new row(s) written to {output_path}")
    return str(output_path), skipped


FOOTPRINT_OPTION_PER_IMAGE = "PER_IMAGE"
FOOTPRINT_OPTION_MERGE = "MERGE"
FOOTPRINT_OPTION_BUFFER = "BUFFER"
FOOTPRINT_OPTION_EXTENT = "EXTENT"
# CONFIRMED (2026-09-01, via https://doc.esri.com/en/arcgis-pro/latest/tool-reference/oriented-imagery/build-oriented-imagery-footprint.html):
# these ALL-CAPS keywords are the tool's actual footprint_option values, not
# their Pro UI display labels - this constant previously held "One footprint
# per image" (the display label), which ran without raising but was never a
# documented valid value.
FOOTPRINT_OPTION_CHOICES = [
    FOOTPRINT_OPTION_PER_IMAGE, FOOTPRINT_OPTION_MERGE, FOOTPRINT_OPTION_BUFFER, FOOTPRINT_OPTION_EXTENT,
]


def build_oi_footprint(oid_path, gdb_path, footprint_option=FOOTPRINT_OPTION_PER_IMAGE, log=print):
    """Build the footprint feature class for an oriented imagery dataset.

    Footprint Dataset Location, Footprint Dataset Name, and Footprint Options
    are all required by this tool (confirmed by a live ERROR 000735 when
    Footprint Options was omitted) - the footprint is written into the same
    geodatabase as the OID so it also gets registered as the OID's Footprint
    property (per Esri's usage notes for this tool). footprint_option is one
    of FOOTPRINT_OPTION_CHOICES: PER_IMAGE (one polygon per image - the
    previous, and still default, behavior), MERGE (individual polygons
    merged into one optimized polygon), BUFFER (each camera point buffered
    by the average far distance - meant for street-view-style imagery), or
    EXTENT (a single footprint from the OID's overall extent).
    """
    build_footprint = _resolve_gp_tool("BuildOrientedImageryFootprint")
    footprint_name = f"{Path(oid_path).name}_Footprint"
    footprint_path = f"{gdb_path}/{footprint_name}"
    if arcpy.Exists(footprint_path):
        arcpy.management.Delete(footprint_path)
        log(f"Deleted existing footprint feature class before rebuilding: {footprint_path}")
    try:
        log(f"BuildOrientedImageryFootprint parameters on this install: "
            f"{list(inspect.signature(build_footprint).parameters.keys())}")
    except (TypeError, ValueError):
        pass
    build_footprint(oid_path, gdb_path, footprint_name, footprint_option)
    log(f"Built oriented imagery footprint for {oid_path}: {footprint_path} (footprint_option={footprint_option})")


# ============================================================================
# Orchestration
# ============================================================================
def build_datasets_from_folder(
    input_folder,
    gdb_path,
    dataset_base_name,
    spatial_reference,
    build_mosaic=True,
    mosaic_raster_type=MOSAIC_RASTER_TYPE_TABLE,
    mosaic_num_bands=3,
    mosaic_pixel_type="8_BIT_UNSIGNED",
    build_footprints=True,
    build_boundary=True,
    build_oid=True,
    imagery_category="Nadir",
    elevation_source=ELEVATION_SOURCE_NONE,
    constant_elevation=0.0,
    dem_path=None,
    has_z=True,
    build_footprint_for_oid=True,
    oi_footprint_option=FOOTPRINT_OPTION_PER_IMAGE,
    include_all_fields=True,
    compute_heading=False,
    heading_offset=0.0,
    compiled_table_folder=None,
    log=print,
):
    """Discover every Frame/Camera Table pair in input_folder and load each
    one into a shared mosaic dataset and/or oriented imagery dataset.
    Returns a summary dict; a pair that fails to load is logged and skipped,
    it does not abort the rest of the run."""
    spatial_reference = _ensure_spatial_reference(spatial_reference, log=log)
    pairs = discover_table_pairs(input_folder)
    if not pairs:
        raise FileNotFoundError(
            f"No *_FrameTable.csv files found in {input_folder}. This tool expects the "
            "output of Extract Video Frames To Images or Generate Deep Ocean Video Metadata, "
            "either in place or exported by Extracted Frame Image Metadata Generation."
        )
    log(f"Found {len(pairs)} Frame/Camera Table pair(s) in {input_folder}")

    layout = resolve_export_layout(input_folder)
    log(f"Resolved folder layout: images in {layout['images_dir']}, metadata in "
        f"{layout['metadata_dir']} ({'structured export' if layout['structured'] else 'flat export'})")

    gdb_path = ensure_geodatabase(gdb_path, log=log)
    compiled_dir = Path(compiled_table_folder) if compiled_table_folder else \
        Path(gdb_path).parent / f"{dataset_base_name}_CompiledTables"

    mosaic_path = None
    oid_path = None
    loaded_mosaic_pairs = 0
    loaded_oid_pairs = 0
    skipped_oid_pairs = 0
    failed_pairs = []

    if build_mosaic:
        mosaic_path = create_mosaic_dataset(
            gdb_path, f"{dataset_base_name}_Mosaic", spatial_reference,
            mosaic_num_bands, mosaic_pixel_type, log=log
        )
    if build_oid:
        oid_path = create_oriented_imagery_dataset(
            gdb_path, f"{dataset_base_name}_OID", spatial_reference,
            elevation_source, constant_elevation, dem_path, has_z, log=log
        )

    # The compiled Name/Path/FCT/iFDO table is needed whenever the OID is
    # built (always, for its richer attributes) or the mosaic dataset uses
    # the "Table" raster type; the "Frame Camera" mosaic path uses the
    # original, unmodified Frame/Camera Table pair instead.
    need_compiled_table = build_oid or (build_mosaic and mosaic_raster_type == MOSAIC_RASTER_TYPE_TABLE)

    for frame_table, camera_table in pairs:
        pair_label = f"{frame_table.name} / {camera_table.name if camera_table else '(no camera table)'}"

        compiled_csv = None
        if need_compiled_table:
            try:
                compiled_csv = compile_frame_table_with_ifdo(
                    frame_table, layout, compiled_dir / f"{frame_table.stem}{COMPILED_TABLE_SUFFIX}",
                    log=log
                )
            except Exception as exc:  # noqa: BLE001 - one pair must never abort the whole folder
                log(f"ERROR compiling metadata table for {pair_label}: {type(exc).__name__}: {exc}")
                failed_pairs.append(f"{pair_label} (table compilation)")
                continue

        # Each destination is attempted independently so a failure loading into
        # one (e.g. the OID) doesn't get reported as a failure for a pair that
        # actually did load successfully into the other (e.g. the mosaic).
        if build_mosaic:
            try:
                if mosaic_raster_type == MOSAIC_RASTER_TYPE_TABLE:
                    add_rasters_to_mosaic(
                        mosaic_path, MOSAIC_RASTER_TYPE_TABLE, compiled_csv,
                        imagery_category=imagery_category, compute_heading=compute_heading,
                        heading_offset=heading_offset, spatial_reference=spatial_reference,
                        gdb_path=gdb_path, log=log
                    )
                else:
                    add_rasters_to_mosaic(
                        mosaic_path, MOSAIC_RASTER_TYPE_FRAME_CAMERA, frame_table,
                        camera_table_csv=camera_table, log=log
                    )
                loaded_mosaic_pairs += 1
            except arcpy.ExecuteError:
                log(f"ERROR loading {pair_label} into mosaic dataset: {arcpy.GetMessages(2)}")
                failed_pairs.append(f"{pair_label} (mosaic dataset)")
            except Exception as exc:  # noqa: BLE001 - one pair must never abort the whole folder
                log(f"ERROR loading {pair_label} into mosaic dataset: {type(exc).__name__}: {exc}")
                failed_pairs.append(f"{pair_label} (mosaic dataset)")

        if build_oid:
            try:
                oid_input_csv, _ = filter_new_images_for_oid(compiled_csv, oid_path, log=log)
                if oid_input_csv is None:
                    log(f"All images in {pair_label} already exist in the oriented imagery "
                        "dataset - nothing new to add.")
                    skipped_oid_pairs += 1
                else:
                    insert_rows_into_oid(
                        oid_path, oid_input_csv, imagery_category,
                        include_all_fields, compute_heading, heading_offset, log=log
                    )
                    loaded_oid_pairs += 1
            except arcpy.ExecuteError:
                log(f"ERROR loading {pair_label} into oriented imagery dataset: {arcpy.GetMessages(2)}")
                failed_pairs.append(f"{pair_label} (oriented imagery dataset)")
            except Exception as exc:  # noqa: BLE001 - one pair must never abort the whole folder
                log(f"ERROR loading {pair_label} into oriented imagery dataset: {type(exc).__name__}: {exc}")
                failed_pairs.append(f"{pair_label} (oriented imagery dataset)")

    if build_oid:
        assign_oid_sequence_order(oid_path, log=log)

    if build_mosaic and (build_footprints or build_boundary):
        try:
            build_footprints_and_boundary(mosaic_path, build_footprints, build_boundary, log=log)
        except arcpy.ExecuteError:
            log(f"WARNING: could not build mosaic footprints/boundary: {arcpy.GetMessages(2)}")
    if build_oid and build_footprint_for_oid:
        try:
            build_oi_footprint(oid_path, gdb_path, footprint_option=oi_footprint_option, log=log)
        except arcpy.ExecuteError:
            log(f"WARNING: could not build oriented imagery footprint: {arcpy.GetMessages(2)}")

    return {
        "mosaic_dataset": mosaic_path,
        "oriented_imagery_dataset": oid_path,
        "pairs_found": len(pairs),
        "loaded_mosaic_pairs": loaded_mosaic_pairs,
        "loaded_oid_pairs": loaded_oid_pairs,
        "skipped_oid_pairs": skipped_oid_pairs,
        "failed_pairs": failed_pairs,
        "compiled_table_folder": str(compiled_dir) if need_compiled_table else None,
    }
