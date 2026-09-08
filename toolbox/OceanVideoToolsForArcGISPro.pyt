# -*- coding: utf-8 -*-
"""
Python toolbox for ArcGIS Pro 3.7+.

Turns deep ocean video into documented still imagery. Reads the Frame and
Camera tables produced upstream, writes their values into the frame images,
and generates the metadata files the downstream annotation and imagery
platforms expect - GDAL PAM (.aux.xml), XMP, EXIF/PNG tEXt, iFDO v2.2.1 JSON
and BIIGLE volume CSV.

The tool classes here stay thin. Anything substantive lives in the matching
FCT_*_core.py modules, so it can be read and tested without opening a dialog.
Those modules must sit in the same folder as this file; they ship together.

Two conventions to keep if you edit this, both of which cost time to learn.
Parameters are looked up by name rather than by index, because ArcGIS caches
a tool's parameter list and index access breaks silently the moment one is
inserted or reordered. And input is checked in updateMessages() rather than
in execute(), so a problem appears in the dialog instead of after the user
has waited for a run to start.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import arcpy


# ============================================================================
# CONSTANTS
# ============================================================================
# Kept in sync with FCT_ImageFusion_core.py; behavior is resolved there by
# resolve_ifdo_format(), so older saved scripts passing a previous label work.
IFDO_FORMAT_SET_LEVEL = "Single image-set JSON file (iFDO standard) - DEFAULT"
IFDO_FORMAT_PER_IMAGE = "Per-image JSON sidecars (one JSON file per image)"

EXPORT_EXISTS_OVERWRITE = "Overwrite"
EXPORT_EXISTS_SKIP = "Skip existing files"
EXPORT_EXISTS_FAIL = "Fail"

SIDECAR_FORMAT_AUX = "*.aux.xml"
SIDECAR_FORMAT_XMP = "*.xmp"

# iFDO metadata category toggles: (toggle parameter name, label, category).
_IFDO_CATEGORY_TOGGLES = [
    ("include_context", "Include Project Context", "Project Context"),
    ("include_deployment", "Include Deployment Information", "Deployment Information"),
    ("include_sensor", "Include Sensor / Camera Information", "Sensor / Camera Information"),
    ("include_licensing", "Include Licensing, Credit & Identifiers", "Licensing & Identifiers"),
    ("include_study", "Include Study Design & Classification", "Study Design & Classification"),
    ("include_curation", "Include Curation & Quality", "Curation & Quality"),
]

# Every iFDO image-set-header field this tool can meaningfully carry, as
# (parameter name, iFDO key, label, category, gating toggle, kind). Existing
# parameter names are preserved so saved scripts and models keep working.
#   text   - one GPString
#   vocab  - GPString restricted to the schema's own allowed values
#   object - a name GPString plus a "<param>_uri" GPString -> {"name", "uri"}
#   people - semicolon-separated names -> [{"name"}, ...]
#   double - GPDouble
_IFDO_FIELD_SPECS = [
    ("image_context", "image-context", "image-context (overarching programme)",
     "Project Context", "include_context", "object"),
    ("image_project", "image-project", "image-project (cruise / expedition)",
     "Project Context", "include_context", "object"),
    ("image_event", "image-event", "image-event (dive / station / deployment)",
     "Project Context", "include_context", "object"),

    ("image_deployment", "image-deployment", "image-deployment",
     "Deployment Information", "include_deployment", "vocab"),
    ("image_navigation", "image-navigation", "image-navigation",
     "Deployment Information", "include_deployment", "vocab"),
    ("image_illumination", "image-illumination", "image-illumination",
     "Deployment Information", "include_deployment", "vocab"),
    ("image_scale_reference", "image-scale-reference", "image-scale-reference",
     "Deployment Information", "include_deployment", "vocab"),
    ("image_fauna_attraction", "image-fauna-attraction", "image-fauna-attraction",
     "Deployment Information", "include_deployment", "vocab"),
    ("image_capture_mode", "image-capture-mode", "image-capture-mode",
     "Deployment Information", "include_deployment", "vocab"),

    ("image_platform", "image-platform", "image-platform (vehicle / ROV)",
     "Sensor / Camera Information", "include_sensor", "object"),
    ("image_sensor", "image-sensor", "image-sensor (camera system)",
     "Sensor / Camera Information", "include_sensor", "object"),
    ("image_acquisition", "image-acquisition", "image-acquisition",
     "Sensor / Camera Information", "include_sensor", "vocab"),
    ("image_quality", "image-quality", "image-quality",
     "Sensor / Camera Information", "include_sensor", "vocab"),
    ("image_spectral_resolution", "image-spectral-resolution", "image-spectral-resolution",
     "Sensor / Camera Information", "include_sensor", "vocab"),
    ("image_pixel_magnitude", "image-pixel-magnitude", "image-pixel-magnitude (size of one pixel)",
     "Sensor / Camera Information", "include_sensor", "vocab"),

    ("image_license", "image-license", "image-license (e.g. CC-BY, CC-0)",
     "Licensing & Identifiers", "include_licensing", "object"),
    ("image_set_handle", "image-set-handle", "image-set-handle (Handle URL or DOI)",
     "Licensing & Identifiers", "include_licensing", "text"),
    ("image_copyright", "image-copyright", "image-copyright (statement or contact)",
     "Licensing & Identifiers", "include_licensing", "text"),
    ("image_pi", "image-pi", "image-pi (principal investigator)",
     "Licensing & Identifiers", "include_licensing", "object"),
    ("image_creators", "image-creators", "image-creators (names, separated by ';')",
     "Licensing & Identifiers", "include_licensing", "people"),

    ("image_objective", "image-objective", "image-objective (aims of the study)",
     "Study Design & Classification", "include_study", "text"),
    ("image_target_environment", "image-target-environment", "image-target-environment",
     "Study Design & Classification", "include_study", "text"),
    ("image_marine_zone", "image-marine-zone", "image-marine-zone",
     "Study Design & Classification", "include_study", "vocab"),
    ("image_target_timescale", "image-target-timescale", "image-target-timescale",
     "Study Design & Classification", "include_study", "text"),
    ("image_spatial_constraints", "image-spatial-constraints", "image-spatial-constraints",
     "Study Design & Classification", "include_study", "text"),
    ("image_temporal_constraints", "image-temporal-constraints", "image-temporal-constraints",
     "Study Design & Classification", "include_study", "text"),
    ("image_abstract", "image-abstract", "image-abstract (500-2000 characters)",
     "Study Design & Classification", "include_study", "text"),

    ("image_time_synchronisation", "image-time-synchronisation", "image-time-synchronisation",
     "Curation & Quality", "include_curation", "text"),
    ("image_item_identification_scheme", "image-item-identification-scheme",
     "image-item-identification-scheme (how filenames are built)",
     "Curation & Quality", "include_curation", "text"),
    ("image_curation_protocol", "image-curation-protocol", "image-curation-protocol",
     "Curation & Quality", "include_curation", "text"),
    ("image_visual_constraints", "image-visual-constraints",
     "image-visual-constraints (turbidity, blocked view, ...)",
     "Curation & Quality", "include_curation", "text"),
    ("image_coordinate_uncertainty_meters", "image-coordinate-uncertainty-meters",
     "image-coordinate-uncertainty-meters", "Curation & Quality", "include_curation", "double"),
]


def _ifdo_param_names(spec):
    """Every parameter name one field spec owns (object fields own two)."""
    name, _, _, _, _, kind = spec
    return [name, f"{name}_uri"] if kind == "object" else [name]

_UNSET = object()  # sentinel distinct from any real parameter value, including None


# ============================================================================
# SAFE MODULE IMPORT - No sys.path manipulation
# ============================================================================
# Import using the same directory as this .pyt file. This avoids sys.path
# pollution and works reliably across different machines/environments. All
# three FCT_*_core.py modules must ship together with this .pyt in the same
# folder; the metadata-generator and mosaic/OID core modules also import
# FCT_ImageFusion_core.py themselves (for shared table/value helpers) using
# this same technique.
def _load_core_module(module_name):
    module_path = _TOOLBOX_DIR / f"{module_name}.py"
    if not module_path.exists():
        raise FileNotFoundError(
            f"{module_name}.py not found in {_TOOLBOX_DIR}. "
            "All FCT_*.py modules must be shipped together in the same folder."
        )
    import importlib.util
    spec = importlib.util.spec_from_file_location(module_name, str(module_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    _TOOLBOX_DIR = Path(__file__).parent.absolute()
    fct_core = _load_core_module("FCT_ImageFusion_core")
    video_metadata_core = _load_core_module("FCT_DeepOceanVideoMetadata_core")
    mosaic_oi_core = _load_core_module("FCT_MosaicOrientedImagery_core")
    frame_xref_core = _load_core_module("FCT_VideoPlayerFrameCrossReference_core")
    inspection_core = _load_core_module("FCT_VideoSensorInspection_core")
except Exception as import_error:
    raise RuntimeError(
        f"Failed to import FCT_*.py core modules: {import_error}"
    ) from import_error


def _reload_core_modules(log=None):
    """Re-read every FCT_*_core.py module from disk right before a tool runs.

    ArcGIS Pro can keep this .pyt's module (and everything it loaded) alive
    across multiple tool runs within the same session/Python window, so
    saving an edit to a core module is not always enough by itself to affect
    the next run - each Tool.execute() calls this first so a run always
    reflects the current on-disk code, not whatever was cached from an
    earlier run in the same session.
    """
    global fct_core, video_metadata_core, mosaic_oi_core, frame_xref_core, inspection_core
    fct_core = _load_core_module("FCT_ImageFusion_core")
    video_metadata_core = _load_core_module("FCT_DeepOceanVideoMetadata_core")
    mosaic_oi_core = _load_core_module("FCT_MosaicOrientedImagery_core")
    frame_xref_core = _load_core_module("FCT_VideoPlayerFrameCrossReference_core")
    inspection_core = _load_core_module("FCT_VideoSensorInspection_core")
    if log:
        loaded = [fct_core, video_metadata_core, mosaic_oi_core, frame_xref_core,
                  inspection_core]
        for module in loaded:
            mtime = datetime.fromtimestamp(Path(module.__file__).stat().st_mtime)
            log(f"Loaded {Path(module.__file__).name} (last modified {mtime:%Y-%m-%d %H:%M:%S})")


def _parse_multivalue_text(value_as_text):
    """Parse a multiValue parameter's valueAsText into a list of items.

    Items are ";"-separated and are quoted ONLY when they contain a space -
    e.g. "*.aux.xml;*.xmp" arrives entirely unquoted (confirmed from a live
    GP run), so a quoted-only regex silently returns [] and every space-free
    selection is lost. Same conditional-quoting behavior already documented
    for GPValueTable getRow() elsewhere in this file. Returns [] for blank.
    """
    if not value_as_text:
        return []
    items = []
    for token in str(value_as_text).split(";"):
        token = token.strip()
        if len(token) >= 2 and token[0] == token[-1] == "'":
            token = token[1:-1]
        if token:
            items.append(token)
    return items


def _number_or(param, default):
    """Parameter value with an explicit None check - a real 0 must not fall
    back to the default the way `param.value or default` would."""
    if param is None or param.value is None:
        return default
    return param.value


class Toolbox(object):
    def __init__(self):
        self.label = "Deep Ocean Video Metadata & Imagery Tools"
        self.alias = "OceanVideoTools"
        self.tools = [
            ExtractedFrameImageMetadataGeneration,
            GenerateDeepOceanVideoMetadata,
            BuildMosaicAndOrientedImageryDatasets,
            CrossReferenceVideoPlayerFrames,
            InspectVideoAndSensorData,
        ]


class ExtractedFrameImageMetadataGeneration(object):
    """GUI geoprocessing tool for embedding Frame/Camera metadata into images."""

    def __init__(self):
        self.label = "Extracted Frame Image Metadata Generation"
        self.description = (
            "Writes the per-frame metadata held in a Frame Table and Camera Table into the "
            "frame images themselves, and into standard sidecar files beside them, so each "
            "image carries its own time, position and camera information wherever it goes. "
            "Input is a folder of extracted frames plus the Frame/Camera Table CSV pair "
            "produced by Esri's Extract Video Frames To Images (or by Cross-Reference Video "
            "Player Frame Exports).\n\n"
            "Outputs, each optional: GDAL PAM sidecars (*.aux.xml, the format ArcGIS reads); "
            "Adobe XMP sidecars (*.xmp, read by Lightroom/Bridge/Photoshop); metadata "
            "embedded inside the image file itself (EXIF tags for JPEG and TIFF, tEXt chunks "
            "for PNG); image FAIR Digital Object metadata (iFDO v2.2.1 JSON, the marine "
            "imaging community standard); a BIIGLE file-metadata CSV for upload to the BIIGLE "
            "annotation platform; and a JSON manifest recording where every image came from "
            "and which settings produced it. Can also copy everything into a self-contained "
            "deliverable folder, and save the metadata you type as a reusable template."
        )
        # canRunInBackground = False: fuse_folder() performs synchronous I/O
        # operations (file reads/writes, CSV parsing, EXIF embedding). Running in
        # background would block the foreground UI thread, creating poor user
        # experience. Running in foreground allows arcpy.AddMessage() to report
        # live progress to the Geoprocessing pane.
        self.canRunInBackground = False

    def getParameterInfo(self):
        """Define all tool parameters with proper organization."""
        params = []

        # ====================================================================
        # INPUT VIDEO FRAME FOLDER
        # ====================================================================
        export_folder = arcpy.Parameter(
            displayName="Input video frame folder with Frame & Camera tables",
            name="export_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input")
        export_folder.description = (
            "Folder holding the extracted frame images together with their Frame Table and "
            "Camera Table CSVs - the output of Esri's Extract Video Frames To Images, or of "
            "Cross-Reference Video Player Frame Exports. The tables are found automatically by "
            "their '*_FrameTable.csv' / '*_CameraTable.csv' names, so leave those names alone. "
            "Both a flat folder and an already-exported images/ + metadata/ layout are "
            "accepted. More than one Frame Table in the same folder is supported and each is "
            "processed in turn."
        )
        params.append(export_folder)

        rename_images_base_name = arcpy.Parameter(
            displayName="Rename Image Frames (new base name, optional)",
            name="rename_images_base_name",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        rename_images_base_name.description = (
            "Renames every frame image in the input folder to this new base name, before "
            "any other processing below runs. The trailing '_<digits>' sequence each frame "
            "already carries (its sequential index or elapsed-time suffix, e.g. "
            "'_0000000') and its file extension are always preserved - only the text before "
            "that suffix is replaced. Example: 'Squid_0000000.tif' with a new base name of "
            "'ROV1' becomes 'ROV1_0000000.tif'. The Frame Table's image-path column, and "
            "every table/sidecar/BIIGLE CSV/manifest this tool generates afterward "
            "(including when written into this same input folder), automatically reflect "
            "the new names - no further action is needed. Leave blank to keep the existing "
            "file names unchanged."
        )
        params.append(rename_images_base_name)

        # ====================================================================
        # METADATA EXPORT FORMATS - each checkbox enables its own detail
        # fields elsewhere in the dialog (BIIGLE overrides here; the iFDO
        # Output Options/iFDO Metadata Categories categories for Export iFDO
        # JSON, which is also the sole on/off switch for writing iFDO JSON)
        # ====================================================================
        export_json_manifest = arcpy.Parameter(
            displayName="Export JSON Manifest (records source images and run settings)",
            name="export_json_manifest",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
            category="Metadata Export Formats")
        export_json_manifest.value = False
        export_json_manifest.description = (
            "Writes a SourceManifest.json listing every image with the absolute path it came "
            "from, plus the settings this run used. It answers 'where did this image come "
            "from and how was it made' months later, which matters once a deliverable folder "
            "has been copied away from the source data."
        )
        params.append(export_json_manifest)

        export_biigle_metadata = arcpy.Parameter(
            displayName="Export BIIGLE File Metadata CSV (biigle.de volume metadata upload)",
            name="export_biigle_metadata",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
            category="Metadata Export Formats")
        export_biigle_metadata.value = False
        export_biigle_metadata.description = (
            "Writes one BiigleMetadata.csv covering every processed image, in the column "
            "format BIIGLE's volume file-metadata upload expects "
            "(biigle.de/manual/tutorials/volumes/file-metadata): filename (mandatory), "
            "taken_at, lng, lat, gps_altitude, distance_to_ground, area, SUB_heading. "
            "filename/taken_at/lng/lat/gps_altitude/SUB_heading are filled in automatically "
            "from the same resolved iFDO fields (image-datetime/image-latitude/"
            "image-longitude/image-altitude-meters/image-camera-yaw-degrees) already used "
            "elsewhere in this tool. distance_to_ground and area have no equivalent "
            "elsewhere in this project - they are auto-detected from a matching Frame "
            "Table column if one exists, otherwise left blank unless a constant override "
            "below is provided."
        )
        params.append(export_biigle_metadata)

        biigle_distance_to_ground_override = arcpy.Parameter(
            displayName="BIIGLE distance_to_ground override (meters, applied to every image)",
            name="biigle_distance_to_ground_override",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Metadata Export Formats")
        biigle_distance_to_ground_override.enabled = False
        biigle_distance_to_ground_override.description = (
            "Single camera-to-seafloor distance in meters, written to every image's BIIGLE "
            "row. Only used when no matching Frame Table column is found. Supply it when the "
            "vehicle flew at a roughly constant altitude and you want BIIGLE to be able to "
            "scale annotations."
        )
        params.append(biigle_distance_to_ground_override)

        biigle_area_override = arcpy.Parameter(
            displayName="BIIGLE area override (square meters, applied to every image)",
            name="biigle_area_override",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Metadata Export Formats")
        biigle_area_override.enabled = False
        biigle_area_override.description = (
            "Single ground area covered by one frame, in square meters, written to every "
            "image's BIIGLE row. Only used when no matching Frame Table column is found."
        )
        params.append(biigle_area_override)

        export_ifdo_json = arcpy.Parameter(
            displayName="Export iFDO JSON",
            name="export_ifdo_json",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
            category="Metadata Export Formats")
        export_ifdo_json.value = False
        export_ifdo_json.description = (
            "Writes image FAIR Digital Object (iFDO) metadata - the marine imaging community "
            "standard used by repositories such as BIIGLE and PANGAEA. This is the single "
            "on/off switch for iFDO: checking it reveals the iFDO Output Options and iFDO "
            "Metadata Categories sections below, where you supply the deployment facts that "
            "cannot be derived from the imagery itself."
        )
        params.append(export_ifdo_json)

        # ====================================================================
        # SIDECAR METADATA
        # ====================================================================
        metadata_sidecar_formats = arcpy.Parameter(
            displayName="Write Metadata Sidecar File(s)",
            name="metadata_sidecar_formats",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            multiValue=True,
            category="Sidecar Metadata")
        metadata_sidecar_formats.filter.list = [SIDECAR_FORMAT_AUX, SIDECAR_FORMAT_XMP]
        metadata_sidecar_formats.value = [SIDECAR_FORMAT_AUX]
        metadata_sidecar_formats.description = (
            "Writes metadata to files sitting NEXT TO each image, leaving the image untouched. "
            "'*.aux.xml' is the GDAL/Esri format ArcGIS itself understands. '*.xmp' is the "
            "Adobe sidecar format read by Lightroom, Bridge and Photoshop - additive, not a "
            "replacement, since ArcGIS Pro is not confirmed to read it. Select both, either, "
            "or neither."
        )
        params.append(metadata_sidecar_formats)

        write_native_metadata = arcpy.Parameter(
            displayName="Embed metadata directly into image files (EXIF for JPEG/TIFF, tEXt for PNG)",
            name="write_native_metadata",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input")
        write_native_metadata.value = True
        write_native_metadata.description = (
            "Writes the same values already carried in the *.aux.xml/*.xmp sidecars "
            "directly into the image file itself, as a single JSON blob (EXIF UserComment "
            "for JPEG/TIFF, a 'FrameCameraMetadata' tEXt chunk for PNG), plus a handful of "
            "standard tags readers outside this project's own tools recognize:<br/><br/>"
            "&#8226; Every raw Frame Table column (Frame_*) and, when a Camera Table pairs "
            "with it, every Camera Table column (Camera_*)<br/>"
            "&#8226; Every resolved iFDO field (image-latitude/longitude/altitude, "
            "image-datetime, image-uuid, image-camera-yaw/pitch/roll-degrees, plus any "
            "manually-entered or template-imported iFDO Metadata Category fields such as "
            "image-license/image-platform/image-deployment)<br/>"
            "&#8226; Standard ImageDescription/DateTime tags and GPS latitude/longitude/"
            "altitude (EXIF GPS IFD for JPEG/TIFF; plain-text GPS tEXt fields for PNG)"
        )
        params.append(write_native_metadata)

        ifdo_template_input = arcpy.Parameter(
            displayName="Import iFDO Metadata Template from Previous Run (optional)",
            name="ifdo_template_input",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input")
        ifdo_template_input.filter.list = ["json"]
        ifdo_template_input.description = (
            "A JSON file of iFDO values saved by a previous run's 'Export iFDO Metadata "
            "Template' option, supplying the deployment facts that cannot be derived from the "
            "imagery - platform, sensor, licence, project, principal investigator. Fill these "
            "in once per deployment and reuse the file for every dive. A '*.ifdo.json' written "
            "by an earlier run also works directly; set-identity values (the set's own UUID "
            "and name) are never carried over."
        )
        params.append(ifdo_template_input)

        # ====================================================================
        # OUTPUT OPTIONS
        # ====================================================================
        generate_in_input_folder = arcpy.Parameter(
            displayName="Generate metadata output files within the Input Folder",
            name="generate_in_input_folder",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Output Options")
        generate_in_input_folder.value = False
        generate_in_input_folder.description = (
            "When checked, all metadata output is written directly into the input video "
            "frame folder and 'Export to Output Folder' below is disabled. Leave unchecked "
            "to keep using 'Export to Output Folder' (or leave that blank to still write "
            "into the input folder)."
        )
        params.append(generate_in_input_folder)

        export_output_folder = arcpy.Parameter(
            displayName="Export to Output Folder (optional - creates complete deliverable)",
            name="export_output_folder",
            datatype="DEFolder",
            parameterType="Optional",
            direction="Input",
            category="Output Options")
        export_output_folder.description = (
            "Copies the images, their sidecars and the Frame/Camera Tables into a new folder "
            "arranged as images/ + metadata/ with the tables at the root - a self-contained "
            "deliverable that can be handed off without the original working folder. World "
            "files travel with their images, and the copied Frame Table's image paths are "
            "rewritten to point at the copies. Leave blank to write metadata in place."
        )
        params.append(export_output_folder)

        if_output_exists = arcpy.Parameter(
            displayName="If Output Folder Exists",
            name="if_output_exists",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Output Options")
        if_output_exists.filter.list = [
            EXPORT_EXISTS_OVERWRITE,
            EXPORT_EXISTS_SKIP,
            EXPORT_EXISTS_FAIL
        ]
        if_output_exists.value = EXPORT_EXISTS_OVERWRITE
        if_output_exists.enabled = False
        if_output_exists.description = (
            "What to do when the export folder already exists: overwrite its contents, skip "
            "the export and keep what is there, or stop with an error. Enabled only when an "
            "Export to Output Folder is set."
        )
        params.append(if_output_exists)

        export_ifdo_template = arcpy.Parameter(
            displayName="Export iFDO Metadata Template for Future Runs",
            name="export_ifdo_template",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
            category="Output Options")
        export_ifdo_template.value = False
        export_ifdo_template.description = (
            "Saves the iFDO values used by this run to a JSON file you can import into later "
            "runs. Check this after filling in the iFDO Metadata Categories once, then reuse "
            "the file across the rest of the deployment instead of retyping them."
        )
        params.append(export_ifdo_template)

        ifdo_template_output = arcpy.Parameter(
            displayName="iFDO Template Output File (.json)",
            name="ifdo_template_output",
            datatype="DEFile",
            parameterType="Optional",
            direction="Output",
            category="Output Options")
        ifdo_template_output.filter.list = ["json"]
        ifdo_template_output.enabled = False
        ifdo_template_output.description = (
            "Where to save the iFDO template JSON. Enabled only when Export iFDO Metadata "
            "Template is checked."
        )
        params.append(ifdo_template_output)

        # ====================================================================
        # iFDO OUTPUT OPTIONS - hidden unless "Export iFDO JSON" is enabled
        # ====================================================================
        ifdo_output_format = arcpy.Parameter(
            displayName="iFDO Output Format (default: single image-set JSON file)",
            name="ifdo_output_format",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="iFDO Output Options")
        ifdo_output_format.filter.list = [
            IFDO_FORMAT_SET_LEVEL,
            IFDO_FORMAT_PER_IMAGE
        ]
        ifdo_output_format.value = IFDO_FORMAT_SET_LEVEL
        ifdo_output_format.description = (
            "Single image-set JSON file (default, and the iFDO standard's own layout): one "
            "'&lt;Image Set Name&gt;.ifdo.json' describing every image, written to the root of the "
            "output folder beside the Frame/Camera Tables and BiigleMetadata.csv. Per-image JSON "
            "sidecars: one '&lt;image&gt;.ifdo.json' next to each image instead."
        )
        params.append(ifdo_output_format)

        image_set_name = arcpy.Parameter(
            displayName="Image Set Name override",
            name="image_set_name",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="iFDO Output Options")
        image_set_name.description = (
            "Name identifying this collection of images in the iFDO metadata, and the file "
            "name used for the single image-set JSON document. Defaults to the ArcGIS Pro "
            "project title if one is set, otherwise the Frame Table's own name. Use something "
            "meaningful to the deployment, such as the dive or transect identifier."
        )
        params.append(image_set_name)

        # ====================================================================
        # iFDO METADATA CATEGORIES - hidden unless "Export iFDO JSON" is enabled
        # ====================================================================
        for toggle_name, toggle_label, _category in _IFDO_CATEGORY_TOGGLES:
            toggle = arcpy.Parameter(
                displayName=toggle_label,
                name=toggle_name,
                datatype="GPBoolean",
                parameterType="Optional",
                direction="Input",
                category="iFDO Metadata Categories")
            toggle.value = False
            toggle.description = (
                "Reveals this group of iFDO fields for editing. These are facts a person "
                "decides once per deployment - they cannot be derived from the imagery or "
                "from ArcGIS - so they stay hidden until you ask for them. Anything left "
                "blank is simply omitted from the iFDO document."
            )
            params.append(toggle)

        # ====================================================================
        # iFDO HEADER FIELDS - generated from _IFDO_FIELD_SPECS so the dialog,
        # the enable/disable logic and execute() can never drift apart.
        # ====================================================================
        for spec in _IFDO_FIELD_SPECS:
            name, ifdo_key, label, category, _toggle, kind = spec

            if kind == "double":
                field = arcpy.Parameter(
                    displayName=label, name=name, datatype="GPDouble",
                    parameterType="Optional", direction="Input", category=category)
            else:
                field = arcpy.Parameter(
                    displayName=label, name=name, datatype="GPString",
                    parameterType="Optional", direction="Input", category=category)

            if kind == "vocab":
                # The iFDO schema defines a closed value list for these; free
                # text here is simply invalid metadata.
                field.filter.list = list(fct_core.IFDO_CONTROLLED_VOCABULARIES[ifdo_key])
                field.description = (
                    f"{ifdo_key} - iFDO defines a fixed set of allowed values for this field. "
                    "Values from an imported template are used when this is left blank."
                )
            elif kind == "object":
                field.description = (
                    f"{ifdo_key} - the NAME of the {label.split('(')[0].strip()}. iFDO stores this "
                    "as an object; use the matching 'uri' parameter to add a resolvable link."
                )
            elif kind == "people":
                field.description = (
                    f"{ifdo_key} - one or more names separated by ';'. Overrides the creators "
                    "detected from the signed-in ArcGIS portal account."
                )
            else:
                field.description = (
                    f"{ifdo_key} - populated from an imported template if available; "
                    "manual entry overrides the template value."
                )
            field.enabled = False
            params.append(field)

            if kind == "object":
                uri = arcpy.Parameter(
                    displayName=f"{label.split('(')[0].strip()} - uri (optional)",
                    name=f"{name}_uri", datatype="GPString",
                    parameterType="Optional", direction="Input", category=category)
                uri.description = (
                    f"Optional URI pointing to details of this {ifdo_key} "
                    "(equipment page, cruise report, ORCID, licence deed, ...)."
                )
                uri.enabled = False
                params.append(uri)

        # ====================================================================
        # DERIVED OUTPUT
        # ====================================================================
        out_folder = arcpy.Parameter(
            displayName="Output Folder (where metadata was written)",
            name="out_folder",
            datatype="DEFolder",
            parameterType="Derived",
            direction="Output")
        params.append(out_folder)

        out_json_manifest = arcpy.Parameter(
            displayName="JSON Manifest File",
            name="out_json_manifest",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output")
        params.append(out_json_manifest)

        out_biigle_metadata = arcpy.Parameter(
            displayName="BIIGLE File Metadata CSV",
            name="out_biigle_metadata",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output")
        params.append(out_biigle_metadata)

        out_ifdo_json = arcpy.Parameter(
            displayName="iFDO JSON File (single image-set document)",
            name="out_ifdo_json",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output")
        params.append(out_ifdo_json)

        return params

    def isLicensed(self):
        """All ArcGIS Pro installations can run this tool."""
        return True

    def _set_enabled(self, params_by_name, field_names, enabled):
        """Enable or disable a set of dependent parameters.

        Missing names are skipped: ArcGIS Pro caches getParameterInfo() for the
        life of a loaded toolbox, so a dialog opened before new parameters were
        added would otherwise raise a bare KeyError here.
        """
        for field_name in field_names:
            param = params_by_name.get(field_name)
            if param is not None:
                param.enabled = enabled

    def updateParameters(self, parameters):
        """Enable/disable dependent parameters based on checkbox state."""
        # Build parameter lookup by name for safe access
        params_by_name = {p.name: p for p in parameters}

        # "Export iFDO JSON" is the sole iFDO on/off switch - a separate
        # "Include iFDO metadata" checkbox was removed as redundant.
        write_ifdo_enabled = bool(params_by_name["export_ifdo_json"].value)

        # iFDO Output Options and iFDO Metadata Categories only matter when
        # iFDO JSON sidecars are actually being written.
        self._set_enabled(params_by_name, [
            "ifdo_template_input",
            "ifdo_output_format",
            "image_set_name",
        ] + [toggle for toggle, _, _ in _IFDO_CATEGORY_TOGGLES], write_ifdo_enabled)

        # Each category's fields follow their own "Include ..." checkbox.
        for spec in _IFDO_FIELD_SPECS:
            toggle_param = params_by_name.get(spec[4])
            self._set_enabled(
                params_by_name, _ifdo_param_names(spec),
                write_ifdo_enabled and bool(toggle_param.value if toggle_param else False))

        # "Generate metadata output files within the Input Folder" forces
        # output in place - Export to Output Folder is only meaningful when
        # that checkbox is off.
        generate_in_input_folder = bool(params_by_name["generate_in_input_folder"].value)
        export_output_folder_param = params_by_name["export_output_folder"]
        export_output_folder_param.enabled = not generate_in_input_folder

        # Enable if_output_exists only if export_output_folder has a value
        if_exists_param = params_by_name["if_output_exists"]
        if_exists_param.enabled = (
            not generate_in_input_folder and bool(export_output_folder_param.valueAsText)
        )

        # Enable ifdo_template_output only if export_ifdo_template is checked
        export_template_param = params_by_name["export_ifdo_template"]
        template_output_param = params_by_name["ifdo_template_output"]
        template_output_param.enabled = bool(export_template_param.value)

        # Enable the BIIGLE per-run overrides only if export_biigle_metadata is checked
        self._set_enabled(params_by_name, [
            "biigle_distance_to_ground_override",
            "biigle_area_override",
        ], bool(params_by_name["export_biigle_metadata"].value))

    def updateMessages(self, parameters):
        """Validate all inputs upfront - fail-fast with clear messages.

        Uses parameter name mapping for safe access across tool versions.
        """
        params_by_name = {p.name: p for p in parameters}

        # === Warn if no metadata output is selected at all ===
        sidecar_formats_param = params_by_name["metadata_sidecar_formats"]
        write_native_param = params_by_name["write_native_metadata"]
        export_ifdo_json_param = params_by_name["export_ifdo_json"]
        if not (sidecar_formats_param.valueAsText or write_native_param.value or export_ifdo_json_param.value):
            export_ifdo_json_param.setWarningMessage(
                "No metadata output is selected - the tool will run but will not write any "
                "*.aux.xml/*.xmp sidecars, embedded EXIF/PNG metadata, or iFDO JSON files."
            )

        # === Export folder validation ===
        export_folder_param = params_by_name["export_folder"]
        if export_folder_param.altered and export_folder_param.value:
            folder_path = export_folder_param.valueAsText
            folder = Path(folder_path)

            # Check folder exists
            if not folder.is_dir():
                export_folder_param.setErrorMessage(
                    f"Export folder does not exist: {folder_path}"
                )
            else:
                # Check for Frame/Camera Table CSVs
                frame_tables = list(folder.glob("*FrameTable.csv"))
                if not frame_tables:
                    export_folder_param.setWarningMessage(
                        "No *_FrameTable.csv files found in this folder. "
                        "Extract Video Frames To Images writes Frame/Camera Table CSVs "
                        "alongside the extracted images. Verify this is the correct output folder."
                    )

        # === Rename base name validation ===
        rename_param = params_by_name["rename_images_base_name"]
        if rename_param.altered and rename_param.valueAsText:
            invalid_chars = set(rename_param.valueAsText) & set('\\/:*?"<>|')
            if invalid_chars:
                rename_param.setErrorMessage(
                    f"Base name contains character(s) not allowed in file names: {''.join(sorted(invalid_chars))}"
                )

        # === iFDO template input validation ===
        template_input_param = params_by_name["ifdo_template_input"]
        if template_input_param.altered and template_input_param.value:
            template_path = template_input_param.valueAsText
            template_file = Path(template_path)

            # Check file exists
            if not template_file.is_file():
                template_input_param.setErrorMessage(
                    f"Template file does not exist: {template_path}"
                )
            else:
                # Validate template structure
                try:
                    with open(template_path, "r", encoding="utf-8") as f:
                        template = json.load(f)

                    # Check for required iFDO template structure
                    if "metadata" not in template:
                        template_input_param.setErrorMessage(
                            "Invalid iFDO template: missing 'metadata' section. "
                            "Template must contain 'metadata' key with iFDO field values."
                        )
                    elif not isinstance(template.get("metadata"), dict):
                        template_input_param.setErrorMessage(
                            "Invalid iFDO template: 'metadata' must be a dictionary/object."
                        )
                    else:
                        # Template is valid - show what was loaded with category coverage
                        metadata_keys = list(template["metadata"].keys())
                        coverage = fct_core.preview_template_coverage(template_path)
                        coverage_text = fct_core.format_template_preview(coverage)
                        template_input_param.setWarningMessage(
                            f"Template loaded successfully with {len(metadata_keys)} value(s). "
                            f"Edit fields below to override. Details:\n{coverage_text}"
                        )
                except json.JSONDecodeError as exc:
                    template_input_param.setErrorMessage(
                        f"Invalid JSON in template file: {exc}"
                    )
                except Exception as exc:
                    template_input_param.setErrorMessage(
                        f"Error reading template file: {exc}"
                    )

        # === Export output folder validation (if specified) ===
        export_output_folder_param = params_by_name["export_output_folder"]
        if export_output_folder_param.altered and export_output_folder_param.value:
            output_folder_path = export_output_folder_param.valueAsText
            output_folder = Path(output_folder_path)

            # Validation order: (1) parent directory exists, (2) parent is writable
            # The output folder itself doesn't need to exist yet - it will be created
            if not output_folder.parent.is_dir():
                export_output_folder_param.setErrorMessage(
                    f"Parent directory does not exist: {output_folder.parent}"
                )
            elif not os.access(str(output_folder.parent), os.W_OK):
                export_output_folder_param.setErrorMessage(
                    f"No write permission in parent directory: {output_folder.parent}"
                )

        # === iFDO template output validation (if export_ifdo_template is checked) ===
        export_template_param = params_by_name["export_ifdo_template"]
        template_output_param = params_by_name["ifdo_template_output"]
        if export_template_param.value and template_output_param.value:
            template_path = template_output_param.valueAsText
            template_file = Path(template_path)

            # Check parent directory exists and is writable
            if not template_file.parent.is_dir():
                template_output_param.setErrorMessage(
                    f"Parent directory does not exist: {template_file.parent}"
                )
            elif not os.access(str(template_file.parent), os.W_OK):
                template_output_param.setErrorMessage(
                    f"No write permission in parent directory: {template_file.parent}"
                )
            # Warn if file already exists
            elif template_file.exists():
                template_output_param.setWarningMessage(
                    f"File already exists: {template_path} (will be overwritten)"
                )

    def execute(self, parameters, messages):
        """Execute the tool: extract parameters and call core fusion logic.

        Uses parameter name mapping for safe, reorderable parameter access.
        """
        _reload_core_modules(log=_gp_log)
        params_by_name = {p.name: p for p in parameters}

        # Extract primary parameters
        export_folder = params_by_name["export_folder"].valueAsText

        # Rename frame images (if requested) BEFORE anything else reads the
        # Frame Table - every table/sidecar/BIIGLE CSV/manifest generated
        # below then sees the new names with no special-casing needed.
        rename_images_base_name = params_by_name["rename_images_base_name"].valueAsText or None
        if rename_images_base_name:
            try:
                # rename_frame_images() reports its own totals through log.
                fct_core.rename_frame_images(export_folder, rename_images_base_name, log=_gp_log)
            except Exception as exc:
                arcpy.AddError(f"Could not rename frame images: {type(exc).__name__}: {exc}")
                raise arcpy.ExecuteError from exc

        sidecar_formats = _parse_multivalue_text(params_by_name["metadata_sidecar_formats"].valueAsText)
        write_aux = SIDECAR_FORMAT_AUX in sidecar_formats
        write_xmp = SIDECAR_FORMAT_XMP in sidecar_formats
        write_native_metadata = params_by_name["write_native_metadata"].value
        # "Export iFDO JSON" is the sole iFDO on/off switch - a separate
        # "Include iFDO metadata" checkbox was removed as redundant.
        write_ifdo = bool(params_by_name["export_ifdo_json"].value)
        image_set_name = params_by_name["image_set_name"].valueAsText or None

        # Import iFDO template if provided (merges with user-specified values)
        ifdo_template_input = params_by_name["ifdo_template_input"].valueAsText or None
        ifdo_metadata = {}
        if ifdo_template_input:
            try:
                # Load template and merge with any user-provided values
                template_data = fct_core.import_ifdo_template(ifdo_template_input)
                if template_data:
                    ifdo_metadata.update(template_data)
                    # Show template coverage by category
                    coverage = fct_core.preview_template_coverage(ifdo_template_input)
                    coverage_text = fct_core.format_template_preview(coverage)
                    arcpy.AddMessage(
                        f"Imported iFDO template: {len(template_data)} value(s) loaded"
                    )
                    arcpy.AddMessage(coverage_text)
                else:
                    arcpy.AddWarning(
                        f"Template file exists but contains no metadata values"
                    )
            except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError) as exc:
                arcpy.AddWarning(
                    f"Could not import template {ifdo_template_input}: {exc}"
                )

        # Collect iFDO metadata fields from user input (override template values).
        # Only categories the user actually enabled contribute; template values
        # are unconditional, since importing one is itself the opt-in.
        manual_overrides = {}
        for spec in _IFDO_FIELD_SPECS:
            name, ifdo_key, _label, _category, toggle_name, kind = spec
            toggle = params_by_name.get(toggle_name)
            if not (toggle is not None and toggle.value):
                continue
            param = params_by_name.get(name)
            if param is None:
                continue

            if kind == "double":
                value = param.value
                if value is None:
                    continue
                resolved = float(value)
            else:
                text = param.valueAsText
                if not text:
                    continue
                if kind == "object":
                    resolved = {"name": text}
                    uri_param = params_by_name.get(f"{name}_uri")
                    uri_text = uri_param.valueAsText if uri_param is not None else None
                    if uri_text:
                        resolved["uri"] = uri_text
                elif kind == "people":
                    resolved = [{"name": part.strip()} for part in text.split(";") if part.strip()]
                    if not resolved:
                        continue
                else:
                    resolved = text

            ifdo_metadata[ifdo_key] = resolved
            manual_overrides[ifdo_key] = resolved

        # Show which fields were overridden by user input
        if manual_overrides:
            arcpy.AddMessage(
                f"User input overrides: {len(manual_overrides)} value(s) updated"
            )
            for key in sorted(manual_overrides.keys()):
                arcpy.AddMessage(f"  - {key}: {manual_overrides[key]}")

        # Extract phase 2-4 parameters
        # NOTE: The parameter is named 'should_' to allow flexibility in naming
        # if future changes to the underlying core functions are needed.
        ifdo_output_format = (
            params_by_name["ifdo_output_format"].valueAsText or
            IFDO_FORMAT_SET_LEVEL
        )
        generate_in_input_folder = params_by_name["generate_in_input_folder"].value
        export_output_folder = (
            None if generate_in_input_folder
            else params_by_name["export_output_folder"].valueAsText or None
        )
        if_output_exists = (
            params_by_name["if_output_exists"].valueAsText or EXPORT_EXISTS_OVERWRITE
        )
        should_export_ifdo_template = params_by_name["export_ifdo_template"].value
        ifdo_template_output = params_by_name["ifdo_template_output"].valueAsText or None
        should_export_json_manifest = params_by_name["export_json_manifest"].value
        should_export_biigle_metadata = params_by_name["export_biigle_metadata"].value
        biigle_distance_to_ground_override = params_by_name["biigle_distance_to_ground_override"].value
        biigle_area_override = params_by_name["biigle_area_override"].value

        # If exporting to output folder, disable sidecar generation in input folder
        # (they will only be generated in the output folder to keep input clean)
        if export_output_folder:
            write_aux_input = False
            write_xmp_input = False
            write_ifdo_input = False
            write_biigle_input = False
        else:
            write_aux_input = write_aux
            write_xmp_input = write_xmp
            write_ifdo_input = write_ifdo
            write_biigle_input = should_export_biigle_metadata

        # Two-pass strategy for export workflow:
        #   Pass 1 (input folder):
        #     • Embeds native metadata (EXIF/PNG tEXt) into frame images
        #     • Skips sidecar generation if exporting (sidecars go to output only)
        #   Pass 2 (output folder - only runs if export folder specified):
        #     • Generates sidecars in output folder with complete metadata
        #     • Skips native re-embedding (already done in Pass 1)
        # This keeps the input folder clean while giving users a complete,
        # self-contained output folder with all images and metadata sidecars.

        arcpy.SetProgressor("default", "Fusing video frame metadata into images...")
        try:
            # Create output folder if it doesn't exist
            if export_output_folder:
                output_path = Path(export_output_folder)
                output_path.mkdir(parents=True, exist_ok=True)
                arcpy.AddMessage(f"Output folder ready: {export_output_folder}")

            # Process input folder: embed native metadata (EXIF/PNG tEXt) but skip sidecars if exporting
            pass1_result = fct_core.fuse_folder(
                export_folder,
                write_aux=write_aux_input,
                write_xmp=write_xmp_input,
                write_native_metadata=write_native_metadata,
                write_ifdo=write_ifdo_input,
                write_biigle=write_biigle_input,
                distance_to_ground_override=biigle_distance_to_ground_override,
                area_override=biigle_area_override,
                image_set_name=image_set_name,
                ifdo_metadata=ifdo_metadata,
                ifdo_output_format=ifdo_output_format,
                export_output_folder=export_output_folder,
                if_output_exists=if_output_exists,
                export_ifdo_template=should_export_ifdo_template,
                ifdo_template_output=ifdo_template_output,
                ifdo_template_input=ifdo_template_input,
                log=_gp_log,
            )

            # If exporting to output folder and sidecars were disabled for input,
            # regenerate them in the output folder to create a complete, clean deliverable.
            #
            # This ensures users get a self-contained output folder with all images and
            # metadata sidecars (.aux.xml, .ifdo.json), separate from their input folder.
            # Note: this must NOT depend on ifdo_metadata being non-empty - aux.xml/iFDO
            # sidecars also carry auto-detected geolocation/timestamp values and the raw
            # Frame_/Camera_ passthrough fields, which exist regardless of whether the user
            # filled in any manual iFDO fields or imported a template.
            if export_output_folder and (write_aux or write_xmp or write_ifdo or should_export_biigle_metadata):
                arcpy.AddMessage(
                    f"Generating metadata sidecars in output folder "
                    f"(aux={write_aux}, xmp={write_xmp}, ifdo={write_ifdo}, "
                    f"biigle={should_export_biigle_metadata}) with {len(ifdo_metadata)} metadata value(s)"
                )
                arcpy.AddMessage(
                    f"Processing structured export: images in {export_output_folder}/images/, "
                    f"sidecars will be written to {export_output_folder}/metadata/"
                )
                pass2_result = fct_core.fuse_folder(
                    export_output_folder,
                    write_aux=write_aux,
                    write_xmp=write_xmp,
                    write_native_metadata=False,  # Already embedded; don't re-embed
                    write_ifdo=write_ifdo,
                    write_biigle=should_export_biigle_metadata,
                    distance_to_ground_override=biigle_distance_to_ground_override,
                    area_override=biigle_area_override,
                    image_set_name=image_set_name,
                    ifdo_metadata=ifdo_metadata,
                    ifdo_output_format=ifdo_output_format,
                    export_output_folder=None,  # Don't export again
                    if_output_exists=if_output_exists,
                    export_ifdo_template=False,  # Template already exported in first call
                    ifdo_template_output=None,  # Don't re-export template
                    ifdo_template_input=ifdo_template_input,
                    log=_gp_log,
                )
            else:
                pass2_result = None
                if export_output_folder:
                    arcpy.AddWarning(
                        "Output folder specified but no sidecars were generated because no sidecar "
                        "format is selected, 'Write iFDO JSON sidecars' is unchecked, and BIIGLE export "
                        "is unchecked."
                    )

            # After processing, show what was created
            set_level_ifdo = fct_core.resolve_ifdo_format(ifdo_output_format) == fct_core.IFDO_FORMAT_SET_LEVEL
            ifdo_setlevel_path = (pass2_result or pass1_result or {}).get("ifdo_setlevel_path")
            if export_output_folder:
                metadata_path = Path(export_output_folder) / "metadata"
                aux_count = xmp_count = per_image_ifdo_count = 0
                if metadata_path.is_dir():
                    aux_count = len(list(metadata_path.glob("*.aux.xml")))
                    xmp_count = len(list(metadata_path.glob("*.xmp")))
                    per_image_ifdo_count = len(list(metadata_path.glob("*.ifdo.json")))
                # The set-level document lives at the output root, not in
                # metadata/, so it is reported from its own returned path.
                if set_level_ifdo and ifdo_setlevel_path:
                    ifdo_summary = f"  • 1 single image-set iFDO JSON file ({Path(ifdo_setlevel_path).name})"
                else:
                    ifdo_summary = f"  • {per_image_ifdo_count} per-image iFDO JSON file(s)"
                arcpy.AddMessage(
                    f"Metadata written to output folder:\n"
                    f"{ifdo_summary}\n"
                    f"  • {aux_count} .aux.xml file(s)\n"
                    f"  • {xmp_count} .xmp file(s)"
                )

            # Set meaningful output: the folder where metadata was written
            # If export_output_folder was specified, that's the result; otherwise
            # metadata was written to export_folder itself.
            output_location = export_output_folder or export_folder
            params_by_name["out_folder"].value = output_location

            biigle_metadata_path = (pass2_result or pass1_result or {}).get("biigle_metadata_path")
            if biigle_metadata_path:
                params_by_name["out_biigle_metadata"].value = biigle_metadata_path

            if ifdo_setlevel_path:
                params_by_name["out_ifdo_json"].value = ifdo_setlevel_path

            if should_export_json_manifest:
                run_settings = {
                    "write_aux": write_aux,
                    "write_xmp": write_xmp,
                    "write_native_metadata": write_native_metadata,
                    "write_ifdo": write_ifdo,
                    "export_biigle_metadata": should_export_biigle_metadata,
                    "ifdo_output_format": ifdo_output_format,
                    "image_set_name": image_set_name,
                    "generate_in_input_folder": generate_in_input_folder,
                    "if_output_exists": if_output_exists,
                }
                manifest_path = fct_core.write_source_manifest(
                    export_folder,
                    output_folder=export_output_folder,
                    run_settings=run_settings,
                    log=_gp_log,
                )
                params_by_name["out_json_manifest"].value = manifest_path

        except FileNotFoundError as exc:
            arcpy.AddError(f"File not found: {exc}")
            raise arcpy.ExecuteError from exc
        except ValueError as exc:
            arcpy.AddError(f"Invalid value: {exc}")
            raise arcpy.ExecuteError from exc
        except PermissionError as exc:
            arcpy.AddError(f"Permission denied: {exc}")
            raise arcpy.ExecuteError from exc
        except IOError as exc:
            arcpy.AddError(f"I/O error: {exc}")
            raise arcpy.ExecuteError from exc
        except Exception as exc:
            arcpy.AddError(
                f"Unexpected error during metadata fusion: {type(exc).__name__}: {exc}"
            )
            raise arcpy.ExecuteError from exc
        finally:
            arcpy.ResetProgressor()

    def postExecute(self, parameters):
        """Post-execution cleanup (called after messages are reported to user)."""
        # Placeholder for future cleanup logic (logging, resource cleanup, etc.)
        pass


# (param_name, base displayName, profile dict key) - Sensor Relative
# Azimuth/Elevation/Roll Angle (camera-to-platform angle); always
# profile-driven, never telemetry-sourced.
_PROFILE_OVERRIDE_SPECS = [
    ("camera_pitch_override", "Sensor Relative Elevation Angle (degrees, 0 = straight down)", "camera_pitch"),
    ("camera_roll_override", "Sensor Relative Roll Angle (degrees)", "camera_roll"),
    ("heading_constant_override", "Constant Platform Heading (degrees, used if no track/field)", "heading_constant"),
    ("hfov_override", "Sensor Horizontal Field of View (degrees)", "hfov"),
    ("vfov_override", "Sensor Vertical Field of View (degrees)", "vfov"),
    ("camera_height_override", "Camera Height Above Seafloor (meters)", "camera_height"),
    ("near_distance_override", "Near Distance (meters)", "near_distance"),
    ("far_distance_override", "Far Distance (meters)", "far_distance"),
]


class GenerateDeepOceanVideoMetadata(object):
    """Generates synthetic/estimated FMV metadata for deep ocean video from a
    moving platform's telemetry log (X/Y/Timestamp required), filling gaps a
    raw nav log lacks from a selectable Video Acquisition Profile. Combines
    Generate Video Metadata (Stationary)'s gap-filling role with Convert
    Video Metadata's field-matching/MISB-format role (delegated directly,
    not reimplemented) - requires Image Analyst.
    """

    def __init__(self):
        self.label = "Generate Deep Ocean Video Metadata"
        self.description = (
            "Builds a metadata CSV ready for Esri's Video Multiplexer from a vehicle's "
            "navigation/telemetry log, so deep ocean video can be turned into geospatially "
            "aware full motion video (FMV).\n\n"
            "Only X, Y and a timestamp are required from the log. The camera facts a "
            "navigation log almost never records - camera pitch and roll, heading, field of "
            "view, near and far distance, height above the seafloor - are supplied instead by "
            "a selectable Video Acquisition Profile (remotely operated vehicle, autonomous "
            "underwater vehicle, towed sled, drop camera), which you can inspect and adjust "
            "before the run. Heading can also be computed from the track itself. Positions are "
            "reprojected to WGS84 longitude/latitude, which the MISB ST 0601 standard behind "
            "full motion video requires.\n\n"
            "REQUIREMENT: the Image Analyst extension - the final field-naming and formatting "
            "pass is delegated to Esri's own Convert Video Metadata tool rather than "
            "reimplemented here."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        params = []

        telemetry_table = arcpy.Parameter(
            displayName="Input Navigation / Telemetry Table (raw vehicle log)",
            name="telemetry_table",
            datatype="GPTableView",
            parameterType="Required",
            direction="Input")
        telemetry_table.description = (
            "The vehicle's navigation/telemetry log - a CSV, Excel worksheet, or geodatabase "
            "table with one row per time step. Must contain an X/longitude column, a "
            "Y/latitude column, and a timestamp column; rows missing any of those three are "
            "skipped. Everything else the multiplexer needs that the log does not have "
            "(camera angles, field of view, near/far distance, camera height) is supplied by "
            "the Video Acquisition Profile below."
        )
        params.append(telemetry_table)

        # Top-level (not collapsed in a category) - the profile selection
        # drives defaults everywhere else, so it stays immediately visible.
        video_acquisition_profile = arcpy.Parameter(
            displayName="Video Acquisition Profile",
            name="video_acquisition_profile",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        video_acquisition_profile.filter.list = list(
            video_metadata_core.VIDEO_ACQUISITION_PROFILES.keys()
        )
        video_acquisition_profile.value = "ROV - Down-looking (Nadir) Camera"
        video_acquisition_profile.description = (
            "How the camera was mounted and pointed. This is the core of the tool: it supplies "
            "realistic estimates for the camera facts a navigation log almost never records - "
            "camera pitch and roll relative to the vehicle, horizontal and vertical field of "
            "view, near and far distance, and height above the seafloor - so a usable video "
            "metadata file can still be produced from an incomplete source. Selecting a "
            "profile immediately pre-fills the override fields below with its values, where "
            "you can inspect and adjust them. Switching profiles overwrites those fields, "
            "including any manual edits."
        )
        params.append(video_acquisition_profile)

        # --- Output (top-level, not collapsed - Convert Video Metadata and
        # Video Multiplexer only accept CSV/JSON/GPX, never a gdb table) ---
        output_folder = arcpy.Parameter(
            displayName="Output Folder for Video Metadata File",
            name="output_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input")
        output_folder.description = (
            "Folder the output CSV files are written into. A folder is used rather than a "
            "geodatabase because the downstream Esri tools (Convert Video Metadata, Video "
            "Multiplexer) only accept CSV, JSON or GPX files."
        )
        params.append(output_folder)

        output_name = arcpy.Parameter(
            displayName="Output Table Name",
            name="output_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        output_name.description = (
            "Base name for the outputs, without an extension. Three files are produced: the "
            "final multiplexer-ready metadata CSV, this tool's own pre-conversion table, and "
            "the field mapping file. Existing files of the same name are overwritten."
        )
        params.append(output_name)

        create_track_fc = arcpy.Parameter(
            displayName="Create Sensor Track Point Feature Class (QA layer)",
            name="create_track_fc",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input")
        create_track_fc.value = False
        create_track_fc.description = (
            "Also writes a point feature class of the resolved track, one point per output "
            "row, so you can put it on a map and confirm the positions look right BEFORE "
            "multiplexing. Purely for quality control - nothing downstream consumes it. This "
            "is the quickest way to catch a wrong Coordinate System of Telemetry X/Y, which "
            "otherwise puts the whole track in the wrong part of the world."
        )
        params.append(create_track_fc)

        output_gdb = arcpy.Parameter(
            displayName="Output Geodatabase for Sensor Track Feature Class",
            name="output_gdb",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input")
        output_gdb.filter.list = ["Local Database"]
        output_gdb.description = (
            "Geodatabase the QA track feature class is written into. Required only when "
            "Create Sensor Track Point Feature Class is checked."
        )
        params.append(output_gdb)

        # --- Key Sensor Information ---
        input_srs = arcpy.Parameter(
            displayName="Coordinate System of Telemetry X/Y",
            name="input_srs",
            datatype="GPCoordinateSystem",
            parameterType="Optional",
            direction="Input",
            category="Key Sensor Information")
        input_srs.value = arcpy.SpatialReference(4326)
        input_srs.description = (
            "The coordinate system the telemetry log's X/Y values are ALREADY in - not a "
            "system to convert them to. This tool always reprojects them to WGS84 longitude "
            "and latitude, because the MISB standard the video multiplexer follows requires "
            "geographic degrees. Getting this wrong is the single most common cause of a track "
            "appearing in the wrong part of the world: leave it at WGS84 only if the log "
            "genuinely holds degrees, and set the correct projected system if it holds "
            "eastings and northings in meters."
        )
        params.append(input_srs)

        # X/Y/Timestamp all get an explicit field designation dropdown (not
        # just an auto-detect override) - filter.list is populated from the
        # loaded table's fields in updateParameters(). X/Y specifically also
        # need this tool's own reprojection into WGS84 lon/lat, which Convert
        # Video Metadata does not do - it only renames/unit-converts already-
        # appropriately-typed fields, it doesn't reproject coordinates.
        x_field = arcpy.Parameter(
            displayName="X / Longitude / Easting Field",
            name="x_field",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Key Sensor Information")
        x_field.description = (
            "Column holding the east-west position, in the units of the Coordinate System of "
            "Telemetry X/Y above. Leave blank to auto-detect by name. Required data: a row "
            "with no usable X is skipped entirely."
        )
        params.append(x_field)

        y_field = arcpy.Parameter(
            displayName="Y / Latitude / Northing Field",
            name="y_field",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Key Sensor Information")
        y_field.description = (
            "Column holding the north-south position, in the units of the Coordinate System of "
            "Telemetry X/Y above. Leave blank to auto-detect by name. Required data: a row "
            "with no usable Y is skipped entirely."
        )
        params.append(y_field)

        time_field = arcpy.Parameter(
            displayName="Timestamp Field (for multiplexing order)",
            name="time_field",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Key Sensor Information")
        time_field.description = (
            "Column holding each row's date and time. Leave blank to auto-detect by name. "
            "Required data: a row whose timestamp is missing or unparseable is skipped, "
            "because the multiplexer aligns metadata to video frames by time. Epoch numbers, "
            "ISO 8601, and common US M/D/YYYY formats are all understood; times with no zone "
            "are treated as UTC."
        )
        params.append(time_field)

        # --- Video Acquisition Profile Overrides (pre-filled per-profile) ---
        for param_name, display_name, _key in _PROFILE_OVERRIDE_SPECS:
            p = arcpy.Parameter(
                displayName=display_name,
                name=param_name,
                datatype="GPDouble",
                parameterType="Optional",
                direction="Input",
                category="Video Acquisition Profile Overrides")
            p.description = (
                "Pre-filled from the Video Acquisition Profile selected above. Edit it to "
                "describe your actual camera rig; the value here is written to every output "
                "row. Changing the profile selection overwrites this field again."
            )
            params.append(p)

        # --- Profile Template Import/Export (mirrors the iFDO template UX) ---
        import_profile_template = arcpy.Parameter(
            displayName="Import Video Acquisition Profile Template (optional)",
            name="import_profile_template",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input",
            category="Profile Template")
        import_profile_template.filter.list = ["json"]
        import_profile_template.description = (
            "A JSON file of camera settings saved by a previous run's Export Resolved Profile "
            "option. Use it to apply one vehicle's measured rig settings across every dive of "
            "a cruise instead of retyping them. Values loaded here fill the override fields "
            "above."
        )
        params.append(import_profile_template)

        export_profile_template = arcpy.Parameter(
            displayName="Export Resolved Profile as Template for Future Runs",
            name="export_profile_template",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
            category="Profile Template")
        export_profile_template.value = False
        export_profile_template.description = (
            "Saves the camera settings actually used by this run - the selected profile plus "
            "any edits you made to the override fields - as a JSON file you can import into "
            "later runs. Check this once you have the rig's settings right, then reuse the "
            "file for every subsequent dive with the same vehicle."
        )
        params.append(export_profile_template)

        profile_template_output = arcpy.Parameter(
            displayName="Profile Template Output File (.json)",
            name="profile_template_output",
            datatype="DEFile",
            parameterType="Optional",
            direction="Output",
            category="Profile Template")
        profile_template_output.filter.list = ["json"]
        profile_template_output.enabled = False
        profile_template_output.description = (
            "Where to save the profile template JSON. Enabled only when Export Resolved "
            "Profile as Template is checked."
        )
        params.append(profile_template_output)

        # --- Camera Model ---
        resample_interval = arcpy.Parameter(
            displayName="Resample Telemetry to Fixed Time Interval (seconds, optional)",
            name="resample_interval",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Camera Model")
        resample_interval.description = (
            "Thins the telemetry to one row per this many seconds. Leave blank to keep every "
            "row. Useful when the log is sampled far faster than the video needs - a 10 Hz "
            "log over a long dive produces a very large metadata file, and 1 second per row "
            "is usually ample for multiplexing."
        )
        params.append(resample_interval)

        camera_id = arcpy.Parameter(
            displayName="Camera ID",
            name="camera_id",
            datatype="GPLong",
            parameterType="Required",
            direction="Input",
            category="Camera Model")
        camera_id.value = 1
        camera_id.description = (
            "Identifier for the camera these settings describe. One physical camera per run, "
            "so the default of 1 is correct unless you are keeping several cameras' outputs "
            "distinct."
        )
        params.append(camera_id)

        for param_name, display_name, help_text in [
            ("camera_ncols", "Camera Sensor Columns (pixels)",
             "Video frame width in pixels, e.g. 1920 for HD. Optional."),
            ("camera_nrows", "Camera Sensor Rows (pixels)",
             "Video frame height in pixels, e.g. 1080 for HD. Optional."),
        ]:
            p = arcpy.Parameter(
                displayName=display_name,
                name=param_name,
                datatype="GPLong",
                parameterType="Optional",
                direction="Input",
                category="Camera Model")
            p.description = help_text
            params.append(p)

        for param_name, display_name, help_text in [
            ("camera_focal_length", "Camera Focal Length",
             "Lens focal length, in the same units as Camera Pixel Size (Esri's Cameras table "
             "schema documents both in microns). Optional - leave blank if unknown."),
            ("camera_pixel_size", "Camera Pixel Size",
             "Physical size of one sensor pixel, in the same units as Camera Focal Length. "
             "Optional - leave blank if unknown."),
        ]:
            p = arcpy.Parameter(
                displayName=display_name,
                name=param_name,
                datatype="GPDouble",
                parameterType="Optional",
                direction="Input",
                category="Camera Model")
            p.description = help_text
            params.append(p)

        out_video_metadata_table = arcpy.Parameter(
            displayName="Output Video Metadata Table (Multiplexer-ready)",
            name="out_video_metadata_table",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output")
        params.append(out_video_metadata_table)

        out_metadata_mapping_file = arcpy.Parameter(
            displayName="Output Metadata Mapping File",
            name="out_metadata_mapping_file",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output")
        params.append(out_metadata_mapping_file)

        out_track_fc = arcpy.Parameter(
            displayName="Output Sensor Track Feature Class",
            name="out_track_fc",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output")
        params.append(out_track_fc)

        return params

    def isLicensed(self):
        # Requires Image Analyst - delegates final field-matching/format
        # pass to Convert Video Metadata (see run_convert_video_metadata()).
        return arcpy.CheckExtension("ImageAnalyst") == "Available"

    def updateParameters(self, parameters):
        params_by_name = {p.name: p for p in parameters}

        export_param = params_by_name["export_profile_template"]
        template_output_param = params_by_name["profile_template_output"]
        template_output_param.enabled = bool(export_param.value)

        create_track_param = params_by_name["create_track_fc"]
        output_gdb_param = params_by_name["output_gdb"]
        output_gdb_param.enabled = bool(create_track_param.value)

        self._update_field_overrides(params_by_name)
        self._prefill_profile_overrides(params_by_name)

    def _update_field_overrides(self, params_by_name):
        """Populate the X/Y/Timestamp field pick lists from the loaded
        table's fields. No-op unless the telemetry table itself changed."""
        x_param = params_by_name.get("x_field")
        y_param = params_by_name.get("y_field")
        time_param = params_by_name.get("time_field")
        if x_param is None or y_param is None or time_param is None:
            return  # stale cached parameter list - refresh the toolbox in Pro
        telemetry_param = params_by_name["telemetry_table"]
        header_fields = self._read_telemetry_header(telemetry_param)
        state = tuple(header_fields)
        if state == getattr(self, "_telemetry_header_state", None):
            return
        self._telemetry_header_state = state
        x_param.filter.list = header_fields
        y_param.filter.list = header_fields
        time_param.filter.list = header_fields

    def _prefill_profile_overrides(self, params_by_name):
        """Pre-fill the 8 override fields with the selected profile's
        defaults on profile change (last selection wins, overwrites edits)."""
        profile_name = params_by_name["video_acquisition_profile"].valueAsText
        if profile_name == getattr(self, "_profile_override_state", _UNSET):
            return
        self._profile_override_state = profile_name

        try:
            profile_defaults = video_metadata_core.build_profile(profile_name) if profile_name else {}
            self._profile_override_error = None
        except ValueError as exc:
            profile_defaults = {}
            # Surfaced by updateMessages - a dialog callback cannot log.
            self._profile_override_error = str(exc)
        for param_name, _base_display, key in _PROFILE_OVERRIDE_SPECS:
            params_by_name[param_name].value = profile_defaults.get(key)

    def _read_telemetry_header(self, telemetry_param):
        """Read+cache the telemetry table's fields (keyed on path, plus mtime
        when the path is a plain file) so repeated updateParameters() calls
        don't re-read it. Never raises - returns [] on failure;
        updateMessages reports the error."""
        if not telemetry_param.value:
            return []
        path = telemetry_param.valueAsText
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None  # not a plain file - gdb table, Excel worksheet, etc.
        cache = getattr(self, "_telemetry_header_cache", None)
        if cache and cache[0] == path and cache[1] == mtime:
            return cache[2]
        try:
            fields = video_metadata_core.read_telemetry_header(path)
            self._telemetry_header_error = None
        except Exception as exc:
            fields = []
            self._telemetry_header_error = str(exc)
        self._telemetry_header_cache = (path, mtime, fields)
        return fields

    def updateMessages(self, parameters):
        params_by_name = {p.name: p for p in parameters}

        telemetry_param = params_by_name["telemetry_table"]
        if telemetry_param.altered and telemetry_param.value:
            path = telemetry_param.valueAsText
            # is_file() only applies to plain files (csv/txt/dbf) - gdb
            # tables/Excel worksheets aren't real files, and GPTableView
            # already validates those exist before allowing selection.
            if path.lower().endswith((".csv", ".txt", ".dbf")) and not Path(path).is_file():
                telemetry_param.setErrorMessage(f"Telemetry table does not exist: {path}")

        header_error = getattr(self, "_telemetry_header_error", None)
        if header_error and telemetry_param.value:
            telemetry_param.setWarningMessage(
                f"Could not read this table's field names ({header_error}). Field pick-lists "
                "below will be empty and auto-detection cannot be previewed."
            )

        profile_error = getattr(self, "_profile_override_error", None)
        profile_param = params_by_name.get("video_acquisition_profile")
        if profile_error and profile_param is not None:
            profile_param.setWarningMessage(
                f"Could not load this profile's default values ({profile_error}). The override "
                "fields below were left blank - fill them in manually."
            )

        create_track_param = params_by_name["create_track_fc"]
        output_gdb_param = params_by_name["output_gdb"]
        if create_track_param.value and not output_gdb_param.value:
            output_gdb_param.setErrorMessage(
                "An output geodatabase is required when creating the sensor track feature class."
            )

        import_template_param = params_by_name["import_profile_template"]
        if import_template_param.altered and import_template_param.value:
            if not Path(import_template_param.valueAsText).is_file():
                import_template_param.setErrorMessage(
                    f"Profile template file does not exist: {import_template_param.valueAsText}"
                )

        # X/Y/Timestamp each get an explicit field dropdown; auto-detect is
        # the fallback when a dropdown is left blank.
        x_param = params_by_name.get("x_field")
        y_param = params_by_name.get("y_field")
        time_param = params_by_name.get("time_field")
        header_fields = self._read_telemetry_header(telemetry_param)
        if header_fields:
            field_overrides = {
                "x": (x_param.valueAsText or None) if x_param else None,
                "y": (y_param.valueAsText or None) if y_param else None,
                "timestamp": (time_param.valueAsText or None) if time_param else None,
            }
            preview = video_metadata_core.preview_resolve_telemetry_fields(header_fields, field_overrides)
            for key, param in (("x", x_param), ("y", y_param), ("timestamp", time_param)):
                if param is None:
                    continue
                override_value = field_overrides[key]
                if override_value and not preview.get(key):
                    param.setErrorMessage(
                        f"'{override_value}' was not found in the telemetry table. "
                        f"Available fields: {header_fields}"
                    )
            missing_required = []
            if not field_overrides["x"] and not preview.get("x"):
                missing_required.append(f"X (looked for {video_metadata_core.CANDIDATE_X_FIELDS})")
            if not field_overrides["y"] and not preview.get("y"):
                missing_required.append(f"Y (looked for {video_metadata_core.CANDIDATE_Y_FIELDS})")
            if not field_overrides["timestamp"] and not preview.get("timestamp"):
                missing_required.append(
                    f"Timestamp (looked for {video_metadata_core.CANDIDATE_TIMESTAMP_FIELDS})"
                )
            if missing_required:
                telemetry_param.setErrorMessage(
                    "Could not auto-detect required field(s): " + "; ".join(missing_required) +
                    f". Available columns: {header_fields}. Select the correct field above."
                )

    def execute(self, parameters, messages):
        _reload_core_modules(log=_gp_log)
        params_by_name = {p.name: p for p in parameters}

        field_overrides = {
            "x": params_by_name["x_field"].valueAsText or None,
            "y": params_by_name["y_field"].valueAsText or None,
            "timestamp": params_by_name["time_field"].valueAsText or None,
        }

        manual_overrides = {
            "camera_pitch": params_by_name["camera_pitch_override"].value,
            "camera_roll": params_by_name["camera_roll_override"].value,
            "heading_constant": params_by_name["heading_constant_override"].value,
            "hfov": params_by_name["hfov_override"].value,
            "vfov": params_by_name["vfov_override"].value,
            "camera_height": params_by_name["camera_height_override"].value,
            "near_distance": params_by_name["near_distance_override"].value,
            "far_distance": params_by_name["far_distance_override"].value,
        }

        profile_name = params_by_name["video_acquisition_profile"].valueAsText
        import_template = params_by_name["import_profile_template"].valueAsText or None
        if import_template:
            try:
                imported_profile = video_metadata_core.import_profile_template(import_template)
                manual_overrides = {**imported_profile, **{
                    k: v for k, v in manual_overrides.items() if v is not None
                }}
                arcpy.AddMessage(f"Imported Video Acquisition Profile template: {import_template}")
            except (FileNotFoundError, ValueError, OSError) as exc:
                arcpy.AddWarning(f"Could not import profile template {import_template}: {exc}")

        arcpy.SetProgressor("default", "Generating deep ocean video metadata...")
        arcpy.CheckOutExtension("ImageAnalyst")
        try:
            result = video_metadata_core.build_video_metadata_table(
                telemetry_table_path=params_by_name["telemetry_table"].valueAsText,
                output_folder=params_by_name["output_folder"].valueAsText,
                output_name=params_by_name["output_name"].valueAsText,
                profile_name=profile_name,
                field_overrides=field_overrides,
                manual_overrides=manual_overrides,
                # .value, not valueAsText - a real SpatialReference's .factoryCode is a fast,
                # reliable path; _coerce_spatial_reference() falls back to text/regex parsing
                # if .value turns out to be an opaque object instead (rare, direct-call only).
                input_srs=params_by_name["input_srs"].value,
                resample_interval_seconds=params_by_name["resample_interval"].value,
                camera_id=params_by_name["camera_id"].value,
                camera_ncols=params_by_name["camera_ncols"].value,
                camera_nrows=params_by_name["camera_nrows"].value,
                camera_focal_length=params_by_name["camera_focal_length"].value,
                camera_pixel_size=params_by_name["camera_pixel_size"].value,
                log=_gp_log,
            )

            params_by_name["out_video_metadata_table"].value = result["video_metadata_table"]
            params_by_name["out_metadata_mapping_file"].value = result["metadata_mapping_file"]
            arcpy.AddMessage(f"Pre-conversion intermediate table: {result['intermediate_table']}")

            if params_by_name["export_profile_template"].value:
                template_output = params_by_name["profile_template_output"].valueAsText
                if template_output:
                    video_metadata_core.export_profile_template(result["profile"], template_output)
                    arcpy.AddMessage(f"Exported resolved profile template: {template_output}")

            if params_by_name["create_track_fc"].value:
                output_gdb = params_by_name["output_gdb"].valueAsText
                fc_name = f"{params_by_name['output_name'].valueAsText}_Track"
                out_fc_path = video_metadata_core.build_track_feature_class(
                    result["metadata_rows"], f"{output_gdb}/{fc_name}", log=_gp_log
                )
                params_by_name["out_track_fc"].value = out_fc_path

            arcpy.AddMessage(
                f"Generated {result['row_count']} video metadata record(s) "
                f"({result['skipped_row_count']} skipped for missing X/Y/Timestamp)."
            )
        except (FileNotFoundError, ValueError, KeyError) as exc:
            arcpy.AddError(f"Invalid input: {exc}")
            raise arcpy.ExecuteError from exc
        except arcpy.ExecuteError as exc:
            arcpy.AddError(f"Geoprocessing error: {arcpy.GetMessages(2)}")
            raise arcpy.ExecuteError from exc
        except Exception as exc:
            arcpy.AddError(f"Unexpected error generating video metadata: {type(exc).__name__}: {exc}")
            raise arcpy.ExecuteError from exc
        finally:
            arcpy.CheckInExtension("ImageAnalyst")
            arcpy.ResetProgressor()

    def postExecute(self, parameters):
        pass


class BuildMosaicAndOrientedImageryDatasets(object):
    """GUI geoprocessing tool that loads extracted video frames plus their
    Frame/Camera Tables into a mosaic dataset and/or an oriented imagery
    dataset - the "manage product data" step after frame extraction and
    (optionally) metadata fusion.
    """

    def __init__(self):
        self.label = "Build Mosaic and Oriented Imagery Datasets"
        self.description = (
            "Loads extracted video frame images and their Frame/Camera Table CSVs into a "
            "mosaic dataset and/or an oriented imagery dataset in a geodatabase. A mosaic "
            "dataset presents the frames as continuous imagery on a map; an oriented imagery "
            "dataset preserves each frame's real viewing geometry so it can be inspected "
            "individually in the Oriented Imagery viewer. This is how the frames and metadata "
            "produced earlier in the workflow are turned into managed, shareable datasets.\n\n"
            "Accepts either the original Extract Video Frames To Images folder (optionally "
            "with *.aux.xml / *.ifdo.json sidecars already added alongside the images) or the "
            "structured images/ + metadata/ folder exported by Extracted Frame Image Metadata "
            "Generation. Each image's name, resolved file path, Frame Table fields and any "
            "iFDO metadata are compiled into one table, which feeds the mosaic dataset and is "
            "written directly into the oriented imagery dataset's own attribute schema, so "
            "the metadata carries into both.\n\n"
            "REQUIREMENT: a Standard or Advanced ArcGIS Pro license - mosaic datasets are not "
            "available under Basic."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        params = []

        input_folder = arcpy.Parameter(
            displayName="Input Video Frame Folder with Frame/Camera Tables",
            name="input_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input")
        input_folder.description = (
            "Either the original Extract Video Frames To Images output folder (images plus "
            "*_FrameTable.csv/*_CameraTable.csv, optionally fused in place with "
            "*.aux.xml/*.ifdo.json sidecars alongside the images), or the folder produced by "
            "Extracted Frame Image Metadata Generation's 'Export to Output Folder' option (images/, "
            "metadata/, and *.csv at the folder root). Both layouts are detected automatically."
        )
        params.append(input_folder)

        output_gdb = arcpy.Parameter(
            displayName="Output Geodatabase (existing, or a new .gdb path)",
            name="output_gdb",
            datatype="DEWorkspace",
            parameterType="Required",
            direction="Input")
        output_gdb.description = (
            "File geodatabase the mosaic dataset and/or oriented imagery dataset are created "
            "in. Point at an existing .gdb, or type a new .gdb path and it will be created. "
            "Re-running against the same geodatabase REUSES an existing dataset of the same "
            "name and adds only new images to it - it does not rebuild it, so the existing "
            "dataset's coordinate system and settings are kept even if you change them here."
        )
        params.append(output_gdb)

        dataset_base_name = arcpy.Parameter(
            displayName="Output Dataset Base Name",
            name="dataset_base_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        dataset_base_name.description = (
            "Base name for the outputs. The mosaic dataset is created as '<base name>' and "
            "the oriented imagery dataset as '<base name>_OI', with its footprint feature "
            "class as '<base name>_OI_Footprint'. Use a geodatabase-safe name: letters, "
            "digits and underscores, starting with a letter."
        )
        params.append(dataset_base_name)

        output_srs = arcpy.Parameter(
            displayName="Output Coordinate System",
            name="output_srs",
            datatype="GPCoordinateSystem",
            parameterType="Optional",
            direction="Input")
        output_srs.value = arcpy.SpatialReference(3857)
        output_srs.description = (
            "Coordinate system the mosaic dataset and oriented imagery dataset are created "
            "in, and the system the Frame Table's PerspectiveX/PerspectiveY values are "
            "assumed to already be in. This MUST match the Output Coordinate System used by "
            "whichever tool produced the Frame Table (Cross-Reference Video Player Frame "
            "Exports writes PerspectiveX/Y in the system chosen there) - a mismatch places "
            "every footprint in the wrong location. Defaults to WGS 1984 Web Mercator "
            "(EPSG:3857)."
        )
        params.append(output_srs)

        # ====================================================================
        # MOSAIC DATASET OPTIONS
        # ====================================================================
        build_mosaic = arcpy.Parameter(
            displayName="Build Mosaic Dataset",
            name="build_mosaic",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Mosaic Dataset")
        build_mosaic.value = True
        build_mosaic.description = (
            "Creates a mosaic dataset from the frame images. A mosaic dataset is the right "
            "output for viewing the frames as continuous imagery on a map. Uncheck to build "
            "only the oriented imagery dataset. At least one of the two must be checked."
        )
        params.append(build_mosaic)

        mosaic_raster_type = arcpy.Parameter(
            displayName="Mosaic Raster Type",
            name="mosaic_raster_type",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
            category="Mosaic Dataset")
        mosaic_raster_type.filter.list = mosaic_oi_core.MOSAIC_RASTER_TYPE_CHOICES
        mosaic_raster_type.value = mosaic_oi_core.MOSAIC_RASTER_TYPE_TABLE
        mosaic_raster_type.description = (
            "'Table' (recommended, and required for this data) loads a compiled table "
            "carrying each image's name, resolved file path, FCT fields, and any iFDO "
            "metadata; per-image real-world extent (xMin/xMax/yMin/yMax), nRows/nCols/"
            "nBands, and PixelType are computed/read automatically (Extract Video Frames "
            "To Images' own .tfw world files are placeholder identity transforms, not real "
            "georeferencing, so per-image georeferencing on disk is NOT required). "
            "'Frame Camera' computes footprints photogrammetrically from the camera model "
            "instead, but requires an Omega/Phi/Kappa or Matrix exterior-orientation field "
            "in the Frame Table - Extract Video Frames To Images' Frame/Camera Table pair "
            "does not include these, so this option will fail with 'Unable to load camera "
            "table.' unless you add them yourself."
        )
        params.append(mosaic_raster_type)

        mosaic_num_bands = arcpy.Parameter(
            displayName="Number of Bands",
            name="mosaic_num_bands",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input",
            category="Mosaic Dataset")
        mosaic_num_bands.value = 3
        mosaic_num_bands.description = (
            "Number of raster bands in the frame images. 3 for ordinary RGB video frames; "
            "1 for greyscale. Must match the actual images or the mosaic dataset will "
            "refuse to add them."
        )
        params.append(mosaic_num_bands)

        mosaic_pixel_type = arcpy.Parameter(
            displayName="Pixel Type",
            name="mosaic_pixel_type",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Mosaic Dataset")
        mosaic_pixel_type.filter.list = ["8_BIT_UNSIGNED", "16_BIT_UNSIGNED", "32_BIT_FLOAT"]
        mosaic_pixel_type.value = "8_BIT_UNSIGNED"
        mosaic_pixel_type.description = (
            "Bit depth of the frame images. 8_BIT_UNSIGNED covers ordinary 8-bit-per-channel "
            "JPEG/TIFF/PNG video frames, which is what frame extraction produces."
        )
        params.append(mosaic_pixel_type)

        build_footprints = arcpy.Parameter(
            displayName="Build Footprints",
            name="build_footprints",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Mosaic Dataset")
        build_footprints.value = True
        build_footprints.description = (
            "Computes each mosaic dataset item's footprint - the polygon describing where "
            "that frame actually sits on the ground. Left on, this preserves the real-world "
            "extent computed from each frame's camera position, heading and field of view. "
            "This step is best-effort: if it fails the run still completes and the mosaic "
            "dataset is still usable, with a warning in the messages."
        )
        params.append(build_footprints)

        build_boundary = arcpy.Parameter(
            displayName="Build Boundary",
            name="build_boundary",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Mosaic Dataset")
        build_boundary.value = True
        build_boundary.description = (
            "Computes the single polygon enclosing every item in the mosaic dataset, which "
            "is what controls the dataset's overall display extent. Best-effort, same as "
            "Build Footprints - a failure here warns rather than aborting the run."
        )
        params.append(build_boundary)

        # ====================================================================
        # ORIENTED IMAGERY DATASET OPTIONS
        # ====================================================================
        build_oid = arcpy.Parameter(
            displayName="Build Oriented Imagery Dataset",
            name="build_oid",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Oriented Imagery Dataset")
        build_oid.value = True
        build_oid.description = (
            "Creates an oriented imagery dataset (OID) - the right output for inspecting "
            "individual frames in their real viewing geometry with the Oriented Imagery "
            "viewer, rather than as a flattened map layer. Rows are written directly to the "
            "OID's own schema. Uncheck to build only the mosaic dataset. At least one of "
            "the two must be checked."
        )
        params.append(build_oid)

        imagery_category = arcpy.Parameter(
            displayName="Imagery Category",
            name="imagery_category",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
            category="Oriented Imagery Dataset")
        imagery_category.filter.list = mosaic_oi_core.IMAGERY_CATEGORY_CHOICES
        imagery_category.value = "Nadir"
        imagery_category.description = (
            "How the camera was pointed. This selects Esri's own default camera pitch, roll, "
            "horizontal/vertical field of view, camera height and near/far distance, used to "
            "fill in ONLY those values a row does not already supply - a real per-frame value "
            "in the Frame Table always wins. 'Nadir' means looking straight down, the usual "
            "case for downward-facing seafloor survey video; choose an oblique or horizontal "
            "category for a forward- or side-looking camera, otherwise footprints are placed "
            "as if the camera were pointing at the seafloor directly beneath it."
        )
        params.append(imagery_category)

        elevation_source = arcpy.Parameter(
            displayName="Elevation Source",
            name="elevation_source",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
            category="Oriented Imagery Dataset")
        elevation_source.filter.list = [
            mosaic_oi_core.ELEVATION_SOURCE_NONE,
            mosaic_oi_core.ELEVATION_SOURCE_CONSTANT,
            mosaic_oi_core.ELEVATION_SOURCE_DEM,
        ]
        elevation_source.value = mosaic_oi_core.ELEVATION_SOURCE_NONE
        elevation_source.description = (
            "Ground elevation the oriented imagery viewer uses when projecting a frame onto "
            "the surface. 'None' uses no elevation surface at all, which is the usual choice "
            "for deep ocean video where no seafloor terrain model is available. 'Constant' "
            "uses the single Constant Elevation value below. 'DEM' uses the Digital "
            "Elevation Model below."
        )
        params.append(elevation_source)

        constant_elevation = arcpy.Parameter(
            displayName="Constant Elevation (meters)",
            name="constant_elevation",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Oriented Imagery Dataset")
        constant_elevation.enabled = False
        constant_elevation.description = (
            "Single ground elevation in meters, applied to every frame. Only used when "
            "Elevation Source is 'Constant'. For seafloor imagery this is a depth, so it is "
            "normally NEGATIVE (e.g. -1200 for a 1200 m deep site)."
        )
        params.append(constant_elevation)

        dem_input = arcpy.Parameter(
            displayName="Digital Elevation Model",
            name="dem_input",
            datatype="GPRasterLayer",
            parameterType="Optional",
            direction="Input",
            category="Oriented Imagery Dataset")
        dem_input.enabled = False
        dem_input.description = (
            "Raster elevation surface (e.g. a bathymetry grid) sampled per frame. Only used "
            "when Elevation Source is 'DEM'. It must cover the full extent of the frame "
            "positions and use the same vertical units (meters) as the rest of the data."
        )
        params.append(dem_input)

        has_z = arcpy.Parameter(
            displayName="Dataset Has Z Values",
            name="has_z",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Oriented Imagery Dataset")
        has_z.value = True
        has_z.description = (
            "Stores each camera position as a 3D point carrying its depth/altitude, rather "
            "than a flat 2D point. Leave checked for deep ocean video, where the camera's "
            "vertical position is meaningful and is what separates one pass over a site from "
            "another at a different depth."
        )
        params.append(has_z)

        build_oi_footprint = arcpy.Parameter(
            displayName="Build Oriented Imagery Footprint",
            name="build_oi_footprint",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Oriented Imagery Dataset")
        build_oi_footprint.value = True
        build_oi_footprint.description = (
            "Generates the footprint feature class that makes the oriented imagery dataset "
            "selectable on a map - without it, clicking the map will not find any image. "
            "Written as '<Output Dataset Base Name>_OI_Footprint' in the same geodatabase, "
            "which is where the dataset expects to find it. Requires a valid camera heading "
            "on every row; the tool always computes one when the data does not supply it."
        )
        params.append(build_oi_footprint)

        oi_footprint_option = arcpy.Parameter(
            displayName="Oriented Imagery Footprint Generation Method",
            name="oi_footprint_option",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
            category="Oriented Imagery Dataset")
        oi_footprint_option.filter.list = mosaic_oi_core.FOOTPRINT_OPTION_CHOICES
        oi_footprint_option.value = mosaic_oi_core.FOOTPRINT_OPTION_PER_IMAGE
        oi_footprint_option.description = (
            "PER_IMAGE - one polygon footprint per image, computed from each image's camera "
            "parameters (CameraHeading/Pitch/Roll, HorizontalFieldOfView/VerticalFieldOfView, "
            "CameraHeight, NearDistance/FarDistance). Use when frame positions are spread over "
            "a large area. "
            "MERGE - the same per-image polygons, merged into a single, more optimized "
            "footprint polygon for the whole dataset. "
            "BUFFER - each camera point buffered by the dataset's average Far Distance and "
            "merged into one polygon; intended for street-view-style imagery, not nadir/oblique "
            "frame video. "
            "EXTENT - a single rectangular footprint from the oriented imagery dataset's "
            "overall extent; use when many camera points are packed into a small area."
        )
        params.append(oi_footprint_option)

        include_all_fields = arcpy.Parameter(
            displayName="Include All Frame Table Fields in Attribute Table",
            name="include_all_fields",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Oriented Imagery Dataset")
        include_all_fields.value = True
        include_all_fields.description = (
            "Carries every remaining Frame Table column through into the oriented imagery "
            "dataset's attribute table as extra fields, so per-frame source metadata stays "
            "queryable alongside the imagery. Columns that duplicate what the dataset already "
            "stores natively (position, acquisition date, camera angles) are excluded "
            "automatically. Uncheck for a minimal attribute table."
        )
        params.append(include_all_fields)

        compute_heading = arcpy.Parameter(
            displayName="Recompute Camera Heading from Positions",
            name="compute_heading",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Oriented Imagery Dataset")
        compute_heading.value = False
        compute_heading.description = (
            "Leave unchecked to keep CameraHeading values already present in the Frame "
            "Table when available (e.g. from Generate Deep Ocean Video Metadata) - heading "
            "is still computed from consecutive frame positions for any row missing a value, "
            "since a valid heading is required for footprint computation. Checking this "
            "recomputes heading for every row instead, overriding any existing values."
        )
        params.append(compute_heading)

        heading_offset = arcpy.Parameter(
            displayName="Heading Offset (degrees, for off-axis camera mounts)",
            name="heading_offset",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Oriented Imagery Dataset")
        heading_offset.value = 0.0
        heading_offset.description = (
            "Degrees added to every camera heading, clockwise. Use this when the camera is "
            "mounted at a fixed angle to the vehicle's direction of travel: 90 for a camera "
            "looking off the starboard side, 270 (or -90) for port, 180 for aft-facing. "
            "Leave at 0 for a forward-facing or downward-facing camera. Applies both to "
            "headings read from the Frame Table and to headings computed from positions."
        )
        params.append(heading_offset)

        # ====================================================================
        # COMPILED TABLE (FCT + iFDO)
        # ====================================================================
        compiled_table_folder = arcpy.Parameter(
            displayName="Compiled Table Output Folder (optional)",
            name="compiled_table_folder",
            datatype="DEFolder",
            parameterType="Optional",
            direction="Input",
            category="Compiled Table (FCT + iFDO)")
        compiled_table_folder.description = (
            "Where the per-pair compiled table (image name, resolved file path, FCT fields, "
            "and iFDO fields as ifdo_* columns) is written. Defaults to a "
            "'<Output Dataset Base Name>_CompiledTables' folder next to the output geodatabase."
        )
        params.append(compiled_table_folder)

        out_mosaic_dataset = arcpy.Parameter(
            displayName="Output Mosaic Dataset",
            name="out_mosaic_dataset",
            datatype="DEMosaicDataset",
            parameterType="Derived",
            direction="Output")
        params.append(out_mosaic_dataset)

        out_oid = arcpy.Parameter(
            displayName="Output Oriented Imagery Dataset",
            name="out_oid",
            datatype="GPString",
            parameterType="Derived",
            direction="Output")
        params.append(out_oid)

        return params

    def isLicensed(self):
        if not mosaic_oi_core.licensed_for_mosaic_and_oi():
            return False
        return True

    def updateParameters(self, parameters):
        params_by_name = {p.name: p for p in parameters}

        build_mosaic = params_by_name["build_mosaic"]
        for name in ("mosaic_raster_type", "mosaic_num_bands", "mosaic_pixel_type",
                     "build_footprints", "build_boundary"):
            params_by_name[name].enabled = bool(build_mosaic.value)

        build_oid = params_by_name["build_oid"]
        for name in ("imagery_category", "elevation_source", "has_z", "build_oi_footprint",
                     "include_all_fields", "compute_heading", "heading_offset"):
            params_by_name[name].enabled = bool(build_oid.value)

        build_oi_footprint = params_by_name["build_oi_footprint"]
        # .get(), not direct indexing: a toolbox that hasn't been refreshed
        # since this parameter was added would otherwise raise KeyError here
        # and surface as a generic "Error 000001" on unrelated parameters.
        oi_footprint_option = params_by_name.get("oi_footprint_option")
        if oi_footprint_option is not None:
            oi_footprint_option.enabled = bool(build_oid.value and build_oi_footprint.value)

        elevation_source = params_by_name["elevation_source"]
        params_by_name["constant_elevation"].enabled = bool(
            build_oid.value and elevation_source.value == mosaic_oi_core.ELEVATION_SOURCE_CONSTANT
        )
        params_by_name["dem_input"].enabled = bool(
            build_oid.value and elevation_source.value == mosaic_oi_core.ELEVATION_SOURCE_DEM
        )

    def updateMessages(self, parameters):
        params_by_name = {p.name: p for p in parameters}

        build_mosaic = params_by_name["build_mosaic"].value
        build_oid = params_by_name["build_oid"].value
        if not build_mosaic and not build_oid:
            params_by_name["build_mosaic"].setErrorMessage(
                "Enable at least one of 'Build Mosaic Dataset' or 'Build Oriented Imagery Dataset'."
            )

        input_folder_param = params_by_name["input_folder"]
        if input_folder_param.altered and input_folder_param.value:
            pairs = fct_core.discover_table_pairs(input_folder_param.valueAsText)
            if not pairs:
                input_folder_param.setWarningMessage(
                    "No *_FrameTable.csv files found in this folder. Extract Video Frames "
                    "To Images and Generate Deep Ocean Video Metadata both write Frame Table "
                    "CSVs alongside frame images or on their own."
                )

    def execute(self, parameters, messages):
        _reload_core_modules(log=_gp_log)
        params_by_name = {p.name: p for p in parameters}

        arcpy.SetProgressor("default", "Building mosaic and oriented imagery datasets...")
        try:
            result = mosaic_oi_core.build_datasets_from_folder(
                input_folder=params_by_name["input_folder"].valueAsText,
                gdb_path=params_by_name["output_gdb"].valueAsText,
                dataset_base_name=params_by_name["dataset_base_name"].valueAsText,
                spatial_reference=params_by_name["output_srs"].value,
                build_mosaic=params_by_name["build_mosaic"].value,
                mosaic_raster_type=params_by_name["mosaic_raster_type"].valueAsText,
                mosaic_num_bands=params_by_name["mosaic_num_bands"].value,
                mosaic_pixel_type=params_by_name["mosaic_pixel_type"].valueAsText,
                build_footprints=params_by_name["build_footprints"].value,
                build_boundary=params_by_name["build_boundary"].value,
                build_oid=params_by_name["build_oid"].value,
                imagery_category=params_by_name["imagery_category"].valueAsText,
                elevation_source=params_by_name["elevation_source"].valueAsText,
                constant_elevation=_number_or(params_by_name.get("constant_elevation"), 0.0),
                dem_path=params_by_name["dem_input"].valueAsText,
                has_z=params_by_name["has_z"].value,
                build_footprint_for_oid=params_by_name["build_oi_footprint"].value,
                oi_footprint_option=(
                    params_by_name["oi_footprint_option"].valueAsText
                    if "oi_footprint_option" in params_by_name
                    else mosaic_oi_core.FOOTPRINT_OPTION_PER_IMAGE
                ),
                include_all_fields=params_by_name["include_all_fields"].value,
                compute_heading=params_by_name["compute_heading"].value,
                heading_offset=_number_or(params_by_name.get("heading_offset"), 0.0),
                compiled_table_folder=params_by_name["compiled_table_folder"].valueAsText,
                log=_gp_log,
            )

            if result["mosaic_dataset"]:
                params_by_name["out_mosaic_dataset"].value = result["mosaic_dataset"]
            if result["oriented_imagery_dataset"]:
                params_by_name["out_oid"].value = result["oriented_imagery_dataset"]

            arcpy.AddMessage(
                f"Found {result['pairs_found']} Frame/Camera Table pair(s); "
                f"loaded {result['loaded_mosaic_pairs']} into the mosaic dataset and "
                f"{result['loaded_oid_pairs']} into the oriented imagery dataset."
            )
            if result["skipped_oid_pairs"]:
                arcpy.AddMessage(
                    f"{result['skipped_oid_pairs']} pair(s) were already fully present in the "
                    "oriented imagery dataset and were not re-added."
                )
            if result["compiled_table_folder"]:
                arcpy.AddMessage(f"Compiled FCT + iFDO table(s) written to {result['compiled_table_folder']}")
            if result["failed_pairs"]:
                arcpy.AddWarning(
                    f"{len(result['failed_pairs'])} pair(s) failed to load: "
                    f"{', '.join(result['failed_pairs'])}"
                )
        except FileNotFoundError as exc:
            arcpy.AddError(f"File not found: {exc}")
            raise arcpy.ExecuteError from exc
        except ValueError as exc:
            arcpy.AddError(f"Invalid value: {exc}")
            raise arcpy.ExecuteError from exc
        except arcpy.ExecuteError as exc:
            arcpy.AddError(f"Geoprocessing error: {arcpy.GetMessages(2)}")
            raise arcpy.ExecuteError from exc
        except Exception as exc:
            arcpy.AddError(f"Unexpected error building datasets: {type(exc).__name__}: {exc}")
            raise arcpy.ExecuteError from exc
        finally:
            arcpy.ResetProgressor()

    def postExecute(self, parameters):
        pass


class CrossReferenceVideoPlayerFrames(object):
    """GUI geoprocessing tool that builds a Frame/Camera Table CSV pair for
    images exported one-at-a-time from the ArcGIS Pro FMV video player
    (filenames ending in "..._<elapsed milliseconds>.<ext>", e.g.
    "..._1767.tif" for a frame grabbed at 000:01.767), by cross-referencing
    each image's elapsed time against an input video metadata table. The
    resulting Frame/Camera Table pair is a drop-in for Video Frame Image
    Metadata Fusion and Build Mosaic and Oriented Imagery Datasets, and this
    tool can also invoke the same aux.xml/iFDO sidecar generation directly.
    """

    def __init__(self):
        self.label = "Cross-Reference Video Player Frame Exports"
        self.description = (
            "Builds a Frame Table and Camera Table for frames grabbed one at a time from the "
            "ArcGIS Pro video player. Those frames arrive with no table of their own - only a "
            "filename ending in the frame's elapsed video time in milliseconds. This tool "
            "converts that elapsed time to an absolute timestamp and matches each frame to "
            "the nearest row of a video metadata table, producing the same Frame/Camera Table "
            "pair that Extract Video Frames To Images produces automatically.\n\n"
            "The result is a drop-in input for Extracted Frame Image Metadata Generation and "
            "Build Mosaic and Oriented Imagery Datasets, so manually grabbed frames rejoin "
            "the same workflow as automatically extracted ones. Frames with no match inside "
            "the tolerance are kept and flagged, never silently dropped. Can also write "
            "sidecar metadata directly, and re-running only processes newly added frames."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        params = []

        image_folder = arcpy.Parameter(
            displayName="Video Player Frame Export Folder",
            name="image_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input")
        image_folder.description = (
            "Folder holding frames exported one at a time from the ArcGIS Pro video player. "
            "Those files have NO Frame Table of their own - only a filename ending in the "
            "frame's elapsed video time in milliseconds, e.g. a grab at 00:01.767 is saved as "
            "'..._1767.tif'. That suffix is what this tool matches on, so do not rename the "
            "files beforehand. Output tables are written back into this same folder. Re-running "
            "after exporting more frames only processes the new ones."
        )
        params.append(image_folder)

        metadata_table = arcpy.Parameter(
            displayName="Input Video Metadata Table (MISB metadata for the same video)",
            name="metadata_table",
            datatype="GPTableView",
            parameterType="Required",
            direction="Input")
        metadata_table.description = (
            "Per-second (or similar) telemetry for the SAME video the frames were exported "
            "from - a CSV, Excel worksheet, or geodatabase table. Each frame's elapsed time is "
            "converted to an absolute timestamp and matched to the nearest row here. Longitude "
            "and latitude columns must be geographic WGS84 degrees. Every other column "
            "(altitude, heading, pitch, roll, field of view, near/far distance) is "
            "auto-detected by name; only the timestamp column can be designated manually."
        )
        params.append(metadata_table)

        time_field = arcpy.Parameter(
            displayName="Metadata Timestamp Field",
            name="time_field",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        time_field.description = (
            "Column holding each row's absolute timestamp. Leave blank to auto-detect by name "
            "(e.g. 'Precision Time Stamp'). Set it explicitly when the table has more than one "
            "time-like column, or when auto-detection reports a miss."
        )
        params.append(time_field)

        time_tolerance_seconds = arcpy.Parameter(
            displayName="Match Tolerance (seconds, optional - blank auto-computes from the "
                        "metadata table's own sampling interval)",
            name="time_tolerance_seconds",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input")
        time_tolerance_seconds.description = (
            "How far apart a frame and a metadata row may be in time and still be considered a "
            "match. Blank uses half the table's own median sampling interval - the tightest "
            "value that still lets every frame pair with its genuinely nearest sample. Do not "
            "set this to 0: frames land at arbitrary elapsed times while the table is sampled "
            "at discrete instants, so an exact match almost never exists. Frames outside the "
            "tolerance are KEPT, flagged unmatched with blank metadata, never dropped."
        )
        params.append(time_tolerance_seconds)

        output_name = arcpy.Parameter(
            displayName="Output Table Name",
            name="output_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        output_name.description = (
            "Base filename only, not a location - output table(s) are always written into the "
            "Video Player Frame Export Folder above, next to the images, as "
            "'<name>_FrameTable.csv' / '<name>_CameraTable.csv' / "
            "'<name>_MatchedMetadataTable.csv'. This is required: Video Frame Image Metadata "
            "Fusion and Build Mosaic and Oriented Imagery Datasets both auto-discover "
            "Frame/Camera Table pairs by scanning that same folder, so the tables must live "
            "there to be found - there is no separate output-location parameter."
        )
        params.append(output_name)

        output_srs = arcpy.Parameter(
            displayName="Output Coordinate System (for PerspectiveX/Y)",
            name="output_srs",
            datatype="GPCoordinateSystem",
            parameterType="Optional",
            direction="Input")
        output_srs.value = arcpy.SpatialReference(3857)
        output_srs.description = (
            "Only affects the Frame Table's PerspectiveX/Y columns, which Build Mosaic and "
            "Oriented Imagery Datasets uses for placement - must match that tool's own Output "
            "Coordinate System if it will be run against this output afterward. Unrelated to "
            "the Video Multiplexer: this tool passes Platform Longitude/Latitude through from "
            "the input metadata table's own WGS84 values untouched (never reprojected), and per "
            "Esri's Video Multiplexer documentation, Sensor Latitude/Longitude must always be "
            "plain geographic WGS84 degrees (MISB ST 0601) regardless of this parameter - the "
            "Multiplexer's own 'Input Coordinate System' only applies to optional frame-corner "
            "ground coordinates supplied separately in its metadata file, not to Sensor Lat/Lon, "
            "and Esri's docs do not require WKID 3857 specifically for that or any other field."
        )
        params.append(output_srs)

        write_frame_camera_table = arcpy.Parameter(
            displayName="Write Frame/Camera Table (required for Video Frame Image Metadata "
                        "Fusion / Build Mosaic and Oriented Imagery Datasets)",
            name="write_frame_camera_table",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input")
        write_frame_camera_table.value = True
        write_frame_camera_table.description = (
            "Writes this project's canonical Frame Table and Camera Table CSV pair - the "
            "schema the downstream metadata-generation and mosaic/oriented-imagery tools "
            "read. Leave checked unless you only want the matched-metadata passthrough below. "
            "Note the Frame Table is also what lets a re-run skip frames already processed, so "
            "unchecking it means every re-run reprocesses the whole folder."
        )
        params.append(write_frame_camera_table)

        write_matched_metadata_table = arcpy.Parameter(
            displayName="Write Matched Metadata Table (MISB-compatible passthrough of matched "
                        "input rows, e.g. to re-feed the Video Multiplexer)",
            name="write_matched_metadata_table",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input")
        write_matched_metadata_table.value = False
        write_matched_metadata_table.description = (
            "Writes a copy of the matched rows from the input metadata table, unchanged - same "
            "columns, same order, one row per matched frame. Use this to feed another "
            "MISB-consuming tool such as the Video Multiplexer. This is a different schema "
            "from the Frame/Camera Table above, which exists for the imagery pipeline; the two "
            "are independent and can both be written in one run."
        )
        params.append(write_matched_metadata_table)

        camera_id = arcpy.Parameter(
            displayName="Camera ID (fallback if not in the metadata table)",
            name="camera_id",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input",
            category="Camera Model")
        camera_id.value = 1
        camera_id.description = (
            "Identifier written to the Frame Table's CameraID column and used to key the "
            "Camera Table. Only used when the metadata table has no camera identifier of its "
            "own. One physical camera per run, so a single constant is correct."
        )
        params.append(camera_id)

        for param_name, display_name, help_text in [
            ("camera_ncols", "Camera Sensor Columns (pixels)",
             "Image width in pixels. Written to the Camera Table; leave blank if unknown."),
            ("camera_nrows", "Camera Sensor Rows (pixels)",
             "Image height in pixels. Written to the Camera Table; leave blank if unknown."),
        ]:
            p = arcpy.Parameter(
                displayName=display_name,
                name=param_name,
                datatype="GPLong",
                parameterType="Optional",
                direction="Input",
                category="Camera Model")
            p.description = help_text
            params.append(p)

        for param_name, display_name, help_text in [
            ("camera_focal_length", "Camera Focal Length",
             "Lens focal length in the same units as Camera Pixel Size (Esri's Cameras table "
             "schema documents both in microns). Optional - leave blank if unknown."),
            ("camera_pixel_size", "Camera Pixel Size",
             "Physical size of one sensor pixel, in the same units as Camera Focal Length. "
             "Optional - leave blank if unknown."),
        ]:
            p = arcpy.Parameter(
                displayName=display_name,
                name=param_name,
                datatype="GPDouble",
                parameterType="Optional",
                direction="Input",
                category="Camera Model")
            p.description = help_text
            params.append(p)

        write_aux = arcpy.Parameter(
            displayName="Write *.aux.xml Sidecars",
            name="write_aux",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Metadata Output")
        write_aux.value = True
        write_aux.description = (
            "Writes a GDAL/Esri-style '<image>.aux.xml' sidecar next to each matched frame, "
            "carrying its matched metadata. Never modifies image pixels, and works for every "
            "image format. This is the safest option and is on by default."
        )
        params.append(write_aux)

        write_native_metadata = arcpy.Parameter(
            displayName="Embed Native Metadata (EXIF/PNG tEXt)",
            name="write_native_metadata",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Metadata Output")
        write_native_metadata.value = False
        write_native_metadata.description = (
            "Writes metadata INSIDE each image file (EXIF tags for JPEG/TIFF, tEXt chunks for "
            "PNG), so it travels with the file outside ArcGIS. This REWRITES the image, so "
            "keep an unmodified copy if the originals matter. Requires the Pillow package, "
            "which ships with ArcGIS Pro; the step is skipped with a warning if it is absent."
        )
        params.append(write_native_metadata)

        write_ifdo = arcpy.Parameter(
            displayName="Write iFDO JSON Sidecars",
            name="write_ifdo",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input",
            category="Metadata Output")
        write_ifdo.value = True
        write_ifdo.description = (
            "Writes image FAIR Digital Object (iFDO) metadata - the marine imaging community "
            "standard used by repositories such as BIIGLE - describing each frame's time, "
            "position and camera orientation."
        )
        params.append(write_ifdo)

        ifdo_template_input = arcpy.Parameter(
            displayName="Import iFDO Template (optional)",
            name="ifdo_template_input",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input",
            category="Metadata Output")
        ifdo_template_input.filter.list = ["json"]
        ifdo_template_input.description = (
            "A JSON file of iFDO values saved from a previous run, supplying the facts that "
            "cannot be derived from the data itself (platform, sensor, licence, project). "
            "Reuse one per deployment instead of retyping them. An existing '*.ifdo.json' "
            "written by an earlier run can be used directly."
        )
        params.append(ifdo_template_input)

        image_set_name = arcpy.Parameter(
            displayName="Image Set Name (optional)",
            name="image_set_name",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Metadata Output")
        image_set_name.description = (
            "Name identifying this collection of frames in the iFDO metadata. Defaults to the "
            "output table name. Use something meaningful to the deployment, e.g. the dive or "
            "transect identifier."
        )
        params.append(image_set_name)

        out_frame_table = arcpy.Parameter(
            displayName="Output Frame Table",
            name="out_frame_table",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output")
        params.append(out_frame_table)

        out_camera_table = arcpy.Parameter(
            displayName="Output Camera Table",
            name="out_camera_table",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output")
        params.append(out_camera_table)

        out_matched_metadata_table = arcpy.Parameter(
            displayName="Output Matched Metadata Table",
            name="out_matched_metadata_table",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output")
        params.append(out_matched_metadata_table)

        return params

    def isLicensed(self):
        # No extension dependency - plain CSV cross-referencing plus the
        # same aux.xml/iFDO embedding Extracted Frame Image Metadata Generation uses.
        return True

    def _read_metadata_header(self, metadata_param):
        """Read+cache the metadata table's fields (keyed on path, plus mtime
        when the path is a plain file). Never raises - returns [] on
        failure; updateMessages reports the error."""
        if not metadata_param.value:
            return []
        path = metadata_param.valueAsText
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None  # not a plain file - gdb table, Excel worksheet, etc.
        cache = getattr(self, "_metadata_header_cache", None)
        if cache and cache[0] == path and cache[1] == mtime:
            return cache[2]
        try:
            fields = video_metadata_core.read_telemetry_header(path)
            self._metadata_header_error = None
        except Exception as exc:
            fields = []
            self._metadata_header_error = str(exc)
        self._metadata_header_cache = (path, mtime, fields)
        return fields

    def updateParameters(self, parameters):
        params_by_name = {p.name: p for p in parameters}

        time_param = params_by_name.get("time_field")
        if time_param is not None:
            header_fields = self._read_metadata_header(params_by_name["metadata_table"])
            state = tuple(header_fields)
            if state != getattr(self, "_metadata_header_state", None):
                self._metadata_header_state = state
                time_param.filter.list = header_fields

        output_name_param = params_by_name["output_name"]
        image_folder_param = params_by_name["image_folder"]
        if not output_name_param.value and image_folder_param.value:
            output_name_param.value = Path(image_folder_param.valueAsText).name

    def updateMessages(self, parameters):
        params_by_name = {p.name: p for p in parameters}

        image_folder_param = params_by_name["image_folder"]
        if image_folder_param.altered and image_folder_param.value:
            images = frame_xref_core.list_frame_images(image_folder_param.valueAsText)
            if not images:
                image_folder_param.setErrorMessage(
                    f"No images found in {image_folder_param.valueAsText} matching "
                    f"{fct_core.EXPORT_IMAGE_EXTENSIONS}."
                )
            elif not any(frame_xref_core.parse_elapsed_milliseconds(p.name) is not None for p in images):
                image_folder_param.setWarningMessage(
                    "No filenames in this folder matched the expected "
                    "'..._<elapsed milliseconds>.<ext>' video-player export pattern - "
                    "every image will be written unmatched."
                )

        metadata_param = params_by_name["metadata_table"]
        if metadata_param.altered and metadata_param.value:
            path = metadata_param.valueAsText
            if path.lower().endswith((".csv", ".txt", ".dbf")) and not Path(path).is_file():
                metadata_param.setErrorMessage(f"Metadata table does not exist: {path}")

        metadata_header_error = getattr(self, "_metadata_header_error", None)
        if metadata_header_error and metadata_param.value:
            metadata_param.setWarningMessage(
                f"Could not read this table's field names ({metadata_header_error}). The "
                "timestamp field pick-list will be empty and matching cannot be previewed."
            )

        time_param = params_by_name.get("time_field")
        header_fields = self._read_metadata_header(metadata_param)
        if header_fields:
            time_override = (time_param.valueAsText or None) if time_param else None
            resolved = frame_xref_core.resolve_cross_reference_fields(header_fields, time_override)
            if time_override and not resolved.get("timestamp") and time_param is not None:
                time_param.setErrorMessage(
                    f"'{time_override}' was not found in the metadata table. "
                    f"Available fields: {header_fields}"
                )
            missing_required = []
            if not resolved.get("x"):
                missing_required.append(f"X/Longitude (looked for {frame_xref_core.CANDIDATE_LON_FIELDS})")
            if not resolved.get("y"):
                missing_required.append(f"Y/Latitude (looked for {frame_xref_core.CANDIDATE_LAT_FIELDS})")
            if not time_override and not resolved.get("timestamp"):
                missing_required.append(
                    f"Timestamp (looked for {frame_xref_core.CANDIDATE_TIMESTAMP_FIELDS})"
                )
            if missing_required:
                metadata_param.setErrorMessage(
                    "Could not auto-detect required field(s): " + "; ".join(missing_required) +
                    f". Available columns: {header_fields}. Select the correct field above if needed."
                )

    def execute(self, parameters, messages):
        _reload_core_modules(log=_gp_log)
        params_by_name = {p.name: p for p in parameters}

        image_folder = params_by_name["image_folder"].valueAsText
        metadata_table = params_by_name["metadata_table"].valueAsText
        time_field = params_by_name["time_field"].valueAsText or None
        tolerance_seconds = params_by_name["time_tolerance_seconds"].value
        output_name = params_by_name["output_name"].valueAsText
        output_srs_text = params_by_name["output_srs"].value or None
        write_frame_camera_table = params_by_name["write_frame_camera_table"].value
        write_matched_metadata_table = params_by_name["write_matched_metadata_table"].value

        camera_model_defaults = {
            "camera_id": params_by_name["camera_id"].value,
            "camera_ncols": params_by_name["camera_ncols"].value,
            "camera_nrows": params_by_name["camera_nrows"].value,
            "camera_focal_length": params_by_name["camera_focal_length"].value,
            "camera_pixel_size": params_by_name["camera_pixel_size"].value,
        }

        arcpy.SetProgressor("default", "Cross-referencing video player frame exports...")
        try:
            result = frame_xref_core.cross_reference_frames(
                image_folder=image_folder,
                metadata_table_path=metadata_table,
                output_name=output_name,
                time_field_override=time_field,
                tolerance_seconds=tolerance_seconds,
                output_srs_text=output_srs_text,
                camera_model_defaults=camera_model_defaults,
                write_frame_camera_table=write_frame_camera_table,
                write_matched_metadata_table=write_matched_metadata_table,
                log=_gp_log,
            )

            if result["frame_table_path"]:
                params_by_name["out_frame_table"].value = result["frame_table_path"]
            if result["camera_table_path"]:
                params_by_name["out_camera_table"].value = result["camera_table_path"]
            if result["matched_metadata_table_path"]:
                params_by_name["out_matched_metadata_table"].value = result["matched_metadata_table_path"]
            arcpy.AddMessage(
                f"{result['new_images']} new image(s) ({result['already_processed_count']} already "
                f"processed in a prior run, {result['total_images']} total in folder): "
                f"{result['matched_count']} matched, {result['unmatched_count']} unmatched (outside "
                f"tolerance), {result['no_timestamp_count']} with no elapsed-time suffix."
            )

            write_aux = params_by_name["write_aux"].value
            write_native_metadata = params_by_name["write_native_metadata"].value
            write_ifdo = params_by_name["write_ifdo"].value
            if result["new_images"] == 0:
                arcpy.AddMessage("No new images this run - skipping metadata sidecar fusion "
                                 "(nothing changed since the last run).")
            elif write_aux or write_native_metadata or write_ifdo:
                ifdo_template_input = params_by_name["ifdo_template_input"].valueAsText or None
                image_set_name = params_by_name["image_set_name"].valueAsText or None
                ifdo_metadata = {}
                if ifdo_template_input:
                    try:
                        ifdo_metadata = fct_core.import_ifdo_template(ifdo_template_input) or {}
                        arcpy.AddMessage(f"Imported iFDO template: {ifdo_template_input}")
                    except (FileNotFoundError, ValueError, OSError) as exc:
                        arcpy.AddWarning(f"Could not import iFDO template {ifdo_template_input}: {exc}")

                fct_core.fuse_folder(
                    image_folder,
                    write_aux=write_aux,
                    write_native_metadata=write_native_metadata,
                    write_ifdo=write_ifdo,
                    image_set_name=image_set_name,
                    ifdo_metadata=ifdo_metadata,
                    ifdo_template_input=ifdo_template_input,
                    log=_gp_log,
                )
        except (FileNotFoundError, ValueError, KeyError) as exc:
            arcpy.AddError(f"Invalid input: {exc}")
            raise arcpy.ExecuteError from exc
        except arcpy.ExecuteError as exc:
            arcpy.AddError(f"Geoprocessing error: {arcpy.GetMessages(2)}")
            raise arcpy.ExecuteError from exc
        except Exception as exc:
            arcpy.AddError(f"Unexpected error cross-referencing frames: {type(exc).__name__}: {exc}")
            raise arcpy.ExecuteError from exc
        finally:
            arcpy.ResetProgressor()

    def postExecute(self, parameters):
        pass


class InspectVideoAndSensorData(object):
    """GUI geoprocessing tool that probes an input video and/or a sensor
    data table (at least one is required, either works alone) and writes a
    lasinfo-style plain-text + JSON report: detected time extent, frame
    rate/resolution, internal discontinuities, sensor sampling gaps (as
    pandas-.describe()-style statistics, not an itemized dump), and whether
    the sensor table's time range even overlaps the video's.

    Run this before any frame-extraction step to confirm the video and the
    sensor table actually line up in time, and to read off the values that
    step needs (video start/end, suggested field mappings). Supplying only a
    table (no video) also works as a standalone table statistics/gap-
    inspection utility, and in that mode requires no third-party packages.
    """

    def __init__(self):
        self.label = "Inspect Video and Sensor Data"
        self.description = (
            "Probes an input video and/or a sensor data table (CSV, Excel worksheet, or "
            "geodatabase table - at least one is required, either works alone) and writes a "
            "plain-text + JSON report: detected UTC time extent, frame rate, resolution, "
            "internal discontinuities ('breaks'), sensor sampling interval statistics, and "
            "whether the sensor table's time range actually overlaps the video's. Run it "
            "before extracting frames to confirm the two inputs line up in time, or "
            "standalone against just a table for its interval and value statistics. "
            "REQUIREMENTS: reading a VIDEO needs the 'av' (PyAV) package installed in a "
            "cloned ArcGIS Pro Python environment; the table-only path needs nothing beyond "
            "a default ArcGIS Pro install."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        params = []

        input_video = arcpy.Parameter(
            displayName="Input Video File",
            name="input_video",
            datatype="DEFile",
            parameterType="Optional",
            direction="Input")
        input_video.filter.list = [
            "avi", "m2ts", "mkv", "mov", "mp2", "mp4", "mpeg", "mpeg2", "mpeg4",
            "mpg", "mpg2", "mpg4", "ps", "ts", "vob", "wmv",
        ]
        input_video.description = (
            "Optional if a Sensor Data Table is supplied instead - at least one of the two "
            "inputs is required. Supplying only a table runs this tool purely as a table "
            "statistics/gap-inspection utility (CSV, Excel worksheet, or a File Geodatabase "
            "table, via the Sensor Data Table parameter below)."
        )
        params.append(input_video)

        sensor_table = arcpy.Parameter(
            displayName="Input Sensor Data Table (raw telemetry or video metadata, optional)",
            name="sensor_table",
            datatype="GPTableView",
            parameterType="Optional",
            direction="Input")
        sensor_table.description = (
            "Optional if an Input Video File is supplied instead - at least one of the two "
            "inputs is required. Can be supplied alone (no video) to inspect a table's own "
            "timestamp-interval statistics/gaps without a video at all."
        )
        params.append(sensor_table)

        secondary_sensor_table = arcpy.Parameter(
            displayName="Secondary Sensor Table (joined to the Sensor Data Table on timestamp)",
            name="secondary_sensor_table",
            datatype="GPTableView",
            parameterType="Optional",
            direction="Input",
            category="Combined Variable Table")
        secondary_sensor_table.description = (
            "Optional second table (e.g. a CTD/chemistry log) whose columns are joined onto "
            "the Sensor Data Table by nearest timestamp when Build Combined Variable Table is "
            "checked. Rows with no match inside the join tolerance keep blank values rather "
            "than being dropped. A column name already present in the Sensor Data Table is "
            "suffixed (_2) rather than overwriting it."
        )
        params.append(secondary_sensor_table)

        build_variable_table = arcpy.Parameter(
            displayName="Build Combined Variable Table",
            name="build_variable_table",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
            category="Combined Variable Table")
        build_variable_table.value = False
        build_variable_table.description = (
            "Writes the Sensor Data Table (optionally joined with the Secondary Sensor Table) "
            "into a single geodatabase table, so one table holds both the video metadata and "
            "the sensor metadata for a deployment. Skip this if you already have a single "
            "table holding both."
        )
        params.append(build_variable_table)

        variable_table_workspace = arcpy.Parameter(
            displayName="Variable Table Geodatabase",
            name="variable_table_workspace",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input",
            category="Combined Variable Table")
        variable_table_workspace.filter.list = ["Local Database"]
        variable_table_workspace.description = (
            "File geodatabase the combined variable table is written into. Required when "
            "Build Combined Variable Table is checked."
        )
        params.append(variable_table_workspace)

        variable_table_name = arcpy.Parameter(
            displayName="Variable Table Name",
            name="variable_table_name",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
            category="Combined Variable Table")
        variable_table_name.description = (
            "Name of the combined variable table inside the geodatabase above. Defaults to "
            "'<Output Description Report Name>_VariableTable'. An existing table of the same "
            "name is overwritten."
        )
        params.append(variable_table_name)

        join_tolerance_seconds = arcpy.Parameter(
            displayName="Join Tolerance (seconds, blank = auto)",
            name="join_tolerance_seconds",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Combined Variable Table")
        join_tolerance_seconds.description = (
            "Maximum timestamp difference for a Secondary Sensor Table row to be joined onto "
            "a Sensor Data Table row. Blank computes half the secondary table's own median "
            "sampling interval - the tightest tolerance that still lets every row pair with "
            "its genuinely nearest sample."
        )
        params.append(join_tolerance_seconds)

        video_start_time_override = arcpy.Parameter(
            displayName="Video Start Time Override (ISO 8601 UTC, if the video's own "
                        "creation_time tag is missing or wrong)",
            name="video_start_time_override",
            datatype="GPString",
            parameterType="Optional",
            direction="Input")
        video_start_time_override.description = (
            "Absolute UTC start time of the video, e.g. '2024-06-01T14:30:00Z'. Only needed "
            "when the video carries no embedded MISB KLV metadata AND no container "
            "'creation_time' tag - without one of those three the report cannot place the "
            "video on an absolute timeline, and overlap with the sensor table cannot be "
            "checked. Resolution order is: this override, then embedded KLV, then the "
            "container tag, then the sensor table's earliest timestamp."
        )
        params.append(video_start_time_override)

        output_folder = arcpy.Parameter(
            displayName="Output Folder",
            name="output_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input")
        output_folder.description = (
            "Folder the description report is written into. Both the plain-text and JSON "
            "versions of the report are written here, plus the embedded-telemetry CSV if "
            "that option is checked."
        )
        params.append(output_folder)

        output_name = arcpy.Parameter(
            displayName="Output Description Report Name",
            name="output_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        output_name.description = (
            "Base name for the outputs, without an extension - the tool appends "
            "'_DescriptionReport.txt' and '_DescriptionReport.json'. Defaults to the input "
            "video's file name, or the sensor table's name when no video is supplied. An "
            "existing report of the same name is overwritten."
        )
        params.append(output_name)

        break_threshold_multiplier = arcpy.Parameter(
            displayName="Break Sensitivity (multiple of the expected frame interval)",
            name="break_threshold_multiplier",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Advanced")
        break_threshold_multiplier.value = 3.0
        break_threshold_multiplier.description = (
            "A gap between consecutive video frame timestamps larger than this many times the "
            "expected 1/frame-rate interval is reported as a 'break' (dropped frame/encoding "
            "discontinuity). Lower = more sensitive (more breaks reported); higher = only "
            "larger gaps are flagged."
        )
        params.append(break_threshold_multiplier)

        sensor_gap_threshold_multiplier = arcpy.Parameter(
            displayName="Sensor Gap Sensitivity (multiple of the median sampling interval)",
            name="sensor_gap_threshold_multiplier",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Advanced")
        sensor_gap_threshold_multiplier.value = 5.0
        sensor_gap_threshold_multiplier.description = (
            "A gap between consecutive sensor table timestamps larger than this many times "
            "the table's own median sampling interval is reported as a 'gap'. Lower = more "
            "sensitive (more gaps reported); higher = only larger gaps are flagged. Ignored "
            "if Sensor Gap Threshold Override (seconds) below is set."
        )
        params.append(sensor_gap_threshold_multiplier)

        sensor_gap_threshold_seconds = arcpy.Parameter(
            displayName="Sensor Gap Threshold Override (seconds, optional)",
            name="sensor_gap_threshold_seconds",
            datatype="GPDouble",
            parameterType="Optional",
            direction="Input",
            category="Advanced")
        sensor_gap_threshold_seconds.description = (
            "If set, a gap between consecutive sensor table timestamps larger than this "
            "FIXED number of seconds is reported as a 'gap' - overrides Sensor Gap "
            "Sensitivity above entirely. Useful when the table's sampling rate is too "
            "irregular for a median-based threshold to mean anything."
        )
        params.append(sensor_gap_threshold_seconds)

        export_embedded_telemetry_csv = arcpy.Parameter(
            displayName="Export Embedded KLV Telemetry as CSV (if present)",
            name="export_embedded_telemetry_csv",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input")
        export_embedded_telemetry_csv.value = True
        export_embedded_telemetry_csv.description = (
            "When the video carries its own embedded MISB ST 0601 KLV metadata, write it out "
            "as a CSV - one row per packet, holding the timestamp plus whichever of Platform "
            "Heading/Pitch/Roll Angle and Sensor Latitude/Longitude/True Altitude are present. "
            "This recovers the telemetry from a multiplexed video when the original navigation "
            "log is no longer to hand, and produces an ordinary sensor table you can inspect, "
            "map, or feed to any tool in this toolbox that accepts one."
        )
        params.append(export_embedded_telemetry_csv)

        out_report_txt = arcpy.Parameter(
            displayName="Output Description Report (Text)",
            name="out_description_report_txt",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output")
        params.append(out_report_txt)

        out_report_json = arcpy.Parameter(
            displayName="Output Description Report (JSON)",
            name="out_description_report_json",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output")
        params.append(out_report_json)

        out_embedded_telemetry_csv = arcpy.Parameter(
            displayName="Output Embedded KLV Telemetry CSV",
            name="out_embedded_telemetry_csv",
            datatype="DEFile",
            parameterType="Derived",
            direction="Output")
        params.append(out_embedded_telemetry_csv)

        out_variable_table = arcpy.Parameter(
            displayName="Output Combined Variable Table",
            name="out_variable_table",
            datatype="DETable",
            parameterType="Derived",
            direction="Output")
        params.append(out_variable_table)

        return params

    def isLicensed(self):
        # Not gated on PyAV: the table-only path needs no video decoding at all.
        # updateMessages() raises the PyAV requirement only when a video is supplied.
        return True

    def updateParameters(self, parameters):
        params_by_name = {p.name: p for p in parameters}
        output_name_param = params_by_name["output_name"]
        input_video_param = params_by_name["input_video"]
        sensor_table_param = params_by_name["sensor_table"]
        if not output_name_param.value:
            if input_video_param.value:
                output_name_param.value = Path(input_video_param.valueAsText).stem
            elif sensor_table_param.value:
                output_name_param.value = Path(sensor_table_param.valueAsText).stem

        build_variable_table = bool(params_by_name["build_variable_table"].value)
        for name in ("variable_table_workspace", "variable_table_name",
                     "secondary_sensor_table", "join_tolerance_seconds"):
            params_by_name[name].enabled = build_variable_table
        variable_table_name_param = params_by_name["variable_table_name"]
        if build_variable_table and not variable_table_name_param.value and output_name_param.value:
            variable_table_name_param.value = f"{output_name_param.value}_VariableTable"

    def updateMessages(self, parameters):
        params_by_name = {p.name: p for p in parameters}
        input_video_param = params_by_name["input_video"]
        sensor_table_param = params_by_name["sensor_table"]

        if not inspection_core.is_av_available() and input_video_param.value:
            input_video_param.setErrorMessage(
                "Reading a video file requires the 'av' (PyAV) package, which is not installed "
                "in the active ArcGIS Pro Python environment. Install it into a CLONED conda "
                "environment (never the default arcgispro-py3). Alternatively, clear this "
                "parameter and run the tool against a Sensor Data Table alone, which needs no "
                "extra packages."
            )

        if not input_video_param.value and not sensor_table_param.value:
            sensor_table_param.setErrorMessage(
                "Provide an Input Video File and/or a Sensor Data Table - at least one input "
                "is required."
            )
        elif sensor_table_param.altered and sensor_table_param.value:
            path = sensor_table_param.valueAsText
            if path.lower().endswith((".csv", ".txt", ".dbf")) and not Path(path).is_file():
                sensor_table_param.setErrorMessage(f"Sensor Data Table does not exist: {path}")

        if params_by_name["build_variable_table"].value:
            if not sensor_table_param.value:
                params_by_name["build_variable_table"].setErrorMessage(
                    "A Sensor Data Table is required to build a Combined Variable Table - it "
                    "is the primary table the Secondary Sensor Table is joined onto."
                )
            for name, label in (("variable_table_workspace", "Variable Table Geodatabase"),
                                ("variable_table_name", "Variable Table Name")):
                if not params_by_name[name].value:
                    params_by_name[name].setErrorMessage(
                        f"{label} is required when Build Combined Variable Table is checked."
                    )

    def execute(self, parameters, messages):
        _reload_core_modules(log=_gp_log)
        params_by_name = {p.name: p for p in parameters}

        input_video = params_by_name["input_video"].valueAsText or None
        sensor_table = params_by_name["sensor_table"].valueAsText or None
        video_start_time_override = params_by_name["video_start_time_override"].valueAsText or None
        output_folder = params_by_name["output_folder"].valueAsText
        output_name = params_by_name["output_name"].valueAsText
        break_threshold_multiplier = params_by_name["break_threshold_multiplier"].value or 3.0
        sensor_gap_threshold_multiplier = (
            params_by_name["sensor_gap_threshold_multiplier"].value or 5.0
        )
        sensor_gap_threshold_seconds = params_by_name["sensor_gap_threshold_seconds"].value
        export_embedded_telemetry_csv = params_by_name["export_embedded_telemetry_csv"].value
        build_variable_table = bool(params_by_name["build_variable_table"].value)
        secondary_sensor_table = params_by_name["secondary_sensor_table"].valueAsText or None
        variable_table_workspace = params_by_name["variable_table_workspace"].valueAsText or None
        variable_table_name = params_by_name["variable_table_name"].valueAsText or None
        join_tolerance_seconds = params_by_name["join_tolerance_seconds"].value

        try:
            sensor_info = {}
            if sensor_table:
                arcpy.SetProgressor("default", "Probing sensor data table...")
                sensor_info = inspection_core.probe_sensor_table_info(
                    sensor_table, sensor_gap_threshold_multiplier,
                    sensor_gap_threshold_seconds, log=_gp_log,
                )

            video_info = {}
            if input_video:
                arcpy.SetProgressor("default", "Probing input video...")
                video_info = inspection_core.probe_video_info(
                    input_video, video_start_time_override, break_threshold_multiplier,
                    sensor_fallback_start_utc=sensor_info.get("start_utc"), log=_gp_log,
                )

            telemetry_csv_path = None
            if input_video and export_embedded_telemetry_csv and (video_info.get("embedded_klv") or {}).get("available"):
                arcpy.SetProgressor("default", "Exporting embedded KLV telemetry...")
                telemetry_csv_path = inspection_core.export_embedded_klv_to_csv(
                    input_video, Path(output_folder) / f"{output_name}_EmbeddedTelemetry.csv", log=_gp_log
                )
                if telemetry_csv_path:
                    video_info["embedded_klv"]["csv_path"] = telemetry_csv_path
                    params_by_name["out_embedded_telemetry_csv"].value = telemetry_csv_path

            coverage = inspection_core.compute_coverage(video_info, sensor_info)
            suggested_mapping = inspection_core.build_suggested_mapping(
                sensor_info.get("resolved_fields", {})
            )

            variable_table_result = None
            if build_variable_table:
                arcpy.SetProgressor("default", "Building combined variable table...")
                variable_table_result = inspection_core.build_variable_table(
                    sensor_table, secondary_sensor_table, join_tolerance_seconds,
                    variable_table_workspace, variable_table_name, log=_gp_log,
                )
                params_by_name["out_variable_table"].value = variable_table_result["path"]

            txt_path, json_path = inspection_core.write_description_report(
                video_info, sensor_info, coverage, suggested_mapping,
                output_folder, output_name, variable_table=variable_table_result, log=_gp_log,
            )
            params_by_name["out_description_report_txt"].value = txt_path
            params_by_name["out_description_report_json"].value = json_path

            if video_info:
                arcpy.AddMessage(
                    f"Video: {video_info.get('start_utc')} to {video_info.get('end_utc')} "
                    f"({video_info.get('frame_rate')} fps, {len(video_info.get('breaks', []))} "
                    f"break(s) detected)."
                )
            if sensor_info:
                arcpy.AddMessage(
                    f"Sensor table: {sensor_info.get('row_count')} row(s), "
                    f"{sensor_info.get('start_utc')} to {sensor_info.get('end_utc')}."
                )
            for warning in coverage.get("warnings", []):
                arcpy.AddWarning(warning)
        except (FileNotFoundError, ValueError, KeyError) as exc:
            arcpy.AddError(f"Invalid input: {exc}")
            raise arcpy.ExecuteError from exc
        except RuntimeError as exc:
            arcpy.AddError(str(exc))
            raise arcpy.ExecuteError from exc
        except arcpy.ExecuteError as exc:
            arcpy.AddError(f"Geoprocessing error: {arcpy.GetMessages(2)}")
            raise arcpy.ExecuteError from exc
        except Exception as exc:
            arcpy.AddError(f"Unexpected error inspecting video/sensor data: "
                           f"{type(exc).__name__}: {exc}")
            raise arcpy.ExecuteError from exc
        finally:
            arcpy.ResetProgressor()

    def postExecute(self, parameters):
        pass


def _gp_log(message):
    """Route log messages to ArcGIS Pro Geoprocessing pane.

    Routed by the message's own leading prefix ("ERROR"/"WARNING"), never by
    whether that word merely appears somewhere inside the text. A WARNING
    that quotes an underlying GP error's text (e.g. "WARNING: could not build
    ... ERROR 999999 ...") must stay a warning - arcpy.AddError() marks the
    whole tool run as Failed even when execute() completes normally and
    returns a full summary, which is exactly what was happening here before
    this was fixed to check the prefix instead of doing a substring search.
    """
    text = str(message)
    stripped = text.lstrip()
    if stripped.startswith("ERROR"):
        arcpy.AddError(text)
    elif stripped.startswith("WARNING"):
        arcpy.AddWarning(text)
    else:
        arcpy.AddMessage(text)
