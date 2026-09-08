# -*- coding: utf-8 -*-
"""
FCT_VideoSensorInspection_core.py

Reference implementation for "Inspect Video and Sensor Data" - a lasinfo-
style probe tool that reports a video's and an optional sensor table's own
facts (time extent, frame rate/resolution, internal discontinuities/gaps)
and validates whether the two actually line up (temporal overlap, coverage
percentage), writing a plain-text + JSON report.

Exists because a value table column's pick-list is fixed at construction and
cannot be repopulated at runtime, so no dialog can offer these values back as
choices once it has worked them out. The report sidesteps that: it is produced
first, and the operator reads the detected video start and end and the
suggested sensor mappings out of it and enters them where they are needed,
rather than a dialog trying to fill itself in.

Only depends on `av` (PyAV), and only for reading a video - the table-only
path needs nothing beyond a default ArcGIS Pro install. is_av_available()
checks that narrow dependency so callers can gate on it precisely.
"""
import bisect
import importlib.util
import json
import math
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).parent.absolute()


def _load_sibling(module_name):
    path = _THIS_DIR / f"{module_name}.py"
    if not path.exists():
        raise FileNotFoundError(
            f"{module_name}.py not found in {_THIS_DIR}. "
            "All FCT_*.py modules must be shipped together in the same folder."
        )
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


video_metadata_core = _load_sibling("FCT_DeepOceanVideoMetadata_core")

# Standard field vocabulary a downstream frame-extraction step maps its
# source columns onto (timestamp/latitude/longitude/depth/altitude/heading/
# pitch/roll), so a suggested mapping produced here is directly usable.
MAPPING_TARGET_CHOICES = (
    "timestamp", "latitude", "longitude", "depth", "altitude", "heading", "pitch", "roll",
)

# Translates video_metadata_core's generic telemetry keys into that
# vocabulary. "z" is suggested as "depth" - if a table's Z column is
# actually an above-ground altitude rather than a below-surface depth, use
# "altitude" instead when transcribing it.
_TELEMETRY_KEY_TO_MAPPING_TARGET = {
    "timestamp": "timestamp", "x": "longitude", "y": "latitude",
    "z": "depth", "heading": "heading", "pitch": "pitch", "roll": "roll",
}


def is_av_available():
    """True if the `av` (PyAV) package can be imported in the ACTIVE Python
    environment. Never raises - used to gate isLicensed()."""
    try:
        import av  # noqa: F401
    except Exception:
        return False
    return True


# MISB ST 0601 UAS Datalink Local Set 16-byte Universal Key (SMPTE 336M KLV) -
# every Esri Video Multiplexer output embeds telemetry per-frame under this
# key per Esri's own doc ("MISB parameters...will be encoded into the final
# video"), so a video's OWN telemetry (Precision Time Stamp plus position/
# heading/pitch/roll/altitude) can be read back directly - no container
# tag/external table/override needed at all.
_MISB0601_UL_KEY = bytes.fromhex("060e2b34020b01010e01030101000000")
_MISB0601_PRECISION_TIMESTAMP_TAG = 2


def _read_ber_oid(data, offset):
    """BER-OID (tag) decode: 7 payload bits per byte, MSB=1 means "more
    bytes follow" - used for KLV local-set tags, most of which fit in one
    byte for MISB ST 0601 but not guaranteed."""
    value = 0
    while True:
        byte = data[offset]
        value = (value << 7) | (byte & 0x7F)
        offset += 1
        if not (byte & 0x80):
            break
    return value, offset


def _read_ber_length(data, offset):
    """BER length decode: short form (<0x80) is the length itself; long
    form's low 7 bits count the following big-endian length bytes."""
    first = data[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    num_bytes = first & 0x7F
    length = int.from_bytes(data[offset:offset + num_bytes], "big")
    return length, offset + num_bytes


def _walk_misb0601_local_set(packet_bytes):
    """Yields every (tag, raw_bytes) element of one MISB ST 0601 UAS
    Datalink Local Set KLV packet. Returns [] for anything that doesn't
    match the Universal Key or is truncated/corrupt."""
    if len(packet_bytes) < 17 or packet_bytes[:16] != _MISB0601_UL_KEY:
        return []
    elements = []
    try:
        offset = 16
        set_length, offset = _read_ber_length(packet_bytes, offset)
        end = offset + set_length
        while offset < end:
            tag, offset = _read_ber_oid(packet_bytes, offset)
            length, offset = _read_ber_length(packet_bytes, offset)
            elements.append((tag, packet_bytes[offset:offset + length]))
            offset += length
    except (IndexError, ValueError):
        return []
    return elements


def _decode_misb_symmetric(raw_bytes, out_max):
    """MISB ST 0601 IMAPB decode for a signed field linearly mapped to
    +/-out_max (e.g. Platform Pitch/Roll Angle, Sensor Latitude/Longitude)."""
    n_bits = len(raw_bytes) * 8
    raw = int.from_bytes(raw_bytes, "big", signed=True)
    max_raw = 2 ** (n_bits - 1) - 1
    return raw * out_max / max_raw


def _decode_misb_unsigned_range(raw_bytes, out_min, out_max):
    """MISB ST 0601 IMAPB decode for an unsigned field linearly mapped to
    [out_min, out_max] (e.g. Platform Heading Angle, Sensor True Altitude)."""
    n_bits = len(raw_bytes) * 8
    raw = int.from_bytes(raw_bytes, "big", signed=False)
    max_raw = 2 ** n_bits - 1
    return out_min + raw * (out_max - out_min) / max_raw


# Local tag -> (video_metadata_core telemetry key, decode function) for the
# standard MISB ST 0601 fields this tool also reports value statistics for
# (beyond Precision Time Stamp) - lets a video's OWN embedded telemetry
# populate the same X/Y/Z/heading/pitch/roll stats table an external
# Sensor Data Table would, with no separate file needed. Ranges/tag numbers
# are the publicly documented ST 0601 values - NOT live-verified against a
# real KLV stream in this dev session.
_MISB0601_TELEMETRY_TAG_DECODERS = {
    5: ("heading", lambda b: _decode_misb_unsigned_range(b, 0.0, 360.0)),
    6: ("pitch", lambda b: _decode_misb_symmetric(b, 20.0)),
    7: ("roll", lambda b: _decode_misb_symmetric(b, 50.0)),
    13: ("y", lambda b: _decode_misb_symmetric(b, 90.0)),
    14: ("x", lambda b: _decode_misb_symmetric(b, 180.0)),
    15: ("z", lambda b: _decode_misb_unsigned_range(b, -900.0, 19000.0)),
}


def _describe_stream(stream):
    codec_name = (getattr(stream.codec_context, "name", "") or "").lower()
    return f"#{stream.index} type={stream.type} codec={codec_name or 'unknown'}"


def probe_embedded_klv_metadata(video_path, log=print):
    """Reads the video's OWN embedded MISB ST 0601 KLV metadata stream (if
    present) directly, extracting every frame's Precision Time Stamp - the
    most authoritative possible source for a multiplexed video's start/end
    time, since it requires no container tag, override, or external sensor
    table at all. Also decodes whichever of Platform Heading/Pitch/Roll
    Angle and Sensor Latitude/Longitude/True Altitude are present into
    `result["field_statistics"]` (same shape/keys as
    compute_telemetry_value_statistics() - x/y/z/heading/pitch/roll - so
    the SAME report renderer works for either source), meaning telemetry
    value statistics appear even with no separate Sensor Data Table
    supplied at all. Demux-only (no video decode). Never raises - returns
    available=False with a log NOTE if no KLV stream is found or nothing
    parses.

    Detection is CONTENT-based, not codec-name-based: every non-video/audio
    stream (data/subtitle/attachment/unknown - FFmpeg's codec-name label for
    a private MPEG-TS metadata PID varies by build/version, e.g. "klv",
    "smpte_klv", "bin_data", or even "none" if its registration descriptor
    isn't recognized) is demuxed and each packet is tested against the
    known MISB ST 0601 Universal Key - a stream only counts once a packet
    actually decodes. This is more robust than trusting a codec name.
    """
    import av

    result = {
        "available": False, "packet_count": 0, "start_utc": None, "end_utc": None,
        "field_statistics": {},
    }
    video_path = Path(video_path)
    with av.open(str(video_path)) as container:
        candidate_streams = [s for s in container.streams if s.type not in ("video", "audio")]
        stream_summary = ", ".join(_describe_stream(s) for s in container.streams) or "(none)"

        if not candidate_streams:
            log(f"NOTE: {video_path} has no non-video/audio stream at all (streams found: "
                f"{stream_summary}) - it was likely never multiplexed with sensor metadata, or "
                "an intermediate re-encode step stripped the metadata track.")
            return result

        first_micros, last_micros, count = None, None, 0
        matched_stream_index = None
        telemetry_values = {key: [] for key, _decoder in _MISB0601_TELEMETRY_TAG_DECODERS.values()}
        try:
            for packet in container.demux(*candidate_streams):
                data = bytes(packet)
                if not data:
                    continue
                elements = _walk_misb0601_local_set(data)
                if not elements:
                    continue
                packet_micros = None
                for tag, raw in elements:
                    if tag == _MISB0601_PRECISION_TIMESTAMP_TAG and len(raw) == 8:
                        packet_micros = int.from_bytes(raw, "big")
                if packet_micros is None:
                    continue
                if matched_stream_index is None:
                    matched_stream_index = packet.stream.index
                elif packet.stream.index != matched_stream_index:
                    continue  # a second stream coincidentally matched once - ignore, keep the first
                count += 1
                if first_micros is None:
                    first_micros = packet_micros
                last_micros = packet_micros
                for tag, raw in elements:
                    decoder_entry = _MISB0601_TELEMETRY_TAG_DECODERS.get(tag)
                    if decoder_entry is None:
                        continue
                    key, decoder = decoder_entry
                    try:
                        telemetry_values[key].append(decoder(raw))
                    except (ValueError, ZeroDivisionError):
                        continue
        except Exception as exc:
            log(f"WARNING: KLV metadata parsing stopped early ({exc}) - reporting only "
                "what was found before this point.")

        if first_micros is None:
            log(f"NOTE: {video_path} has {len(candidate_streams)} non-video/audio stream(s) but "
                f"none decoded as MISB ST 0601 KLV (streams found: {stream_summary}).")
            return result

    result["available"] = True
    result["packet_count"] = count
    start_dt = datetime.fromtimestamp(first_micros / 1_000_000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(last_micros / 1_000_000, tz=timezone.utc)
    result["start_utc"] = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    result["end_utc"] = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    for key, values in telemetry_values.items():
        stats = compute_value_statistics(values)
        if stats:
            stats["column"] = "Embedded KLV Metadata (MISB ST 0601)"
            result["field_statistics"][key] = stats
    return result


# Internal decode key -> the exact MISB target field name this project's
# own telemetry candidate lists already recognize (see
# FCT_DeepOceanVideoMetadata_core.py's CANDIDATE_HEADING_FIELDS/etc.,
# already built around these MISB names) - used as CSV column headers so
# an exported embedded-telemetry CSV auto-detects downstream exactly like
# any other sensor table, with no separate detection logic needed.
_TELEMETRY_KEY_TO_CSV_COLUMN = {
    "heading": "Platform Heading Angle",
    "pitch": "Platform Pitch Angle (Full)",
    "roll": "Platform Roll Angle (Full)",
    "y": "Sensor Latitude",
    "x": "Sensor Longitude",
    "z": "Sensor True Altitude",
}


def export_embedded_klv_to_csv(video_path, output_path, log=print):
    """Re-demuxes the video's own embedded MISB ST 0601 KLV metadata stream
    (same underlying decode as probe_embedded_klv_metadata(), run again
    here to capture every packet's values rather than just summary
    statistics) and writes ONE ROW PER PACKET to a CSV - a real timestamp
    column plus whichever of Platform Heading/Pitch/Roll Angle and Sensor
    Latitude/Longitude/True Altitude were present.

    This is what lets a video's OWN embedded telemetry be used exactly like
    any external sensor table downstream: once written out as a real CSV it
    is an ordinary table, so anything that accepts a sensor table accepts
    this too. It also recovers the telemetry when the original navigation
    log is no longer available.

    Returns the CSV path, or None if no KLV metadata is found (never raises).
    """
    import av
    import csv as csv_module

    video_path = Path(video_path)
    rows = []
    with av.open(str(video_path)) as container:
        candidate_streams = [s for s in container.streams if s.type not in ("video", "audio")]
        if not candidate_streams:
            log(f"NOTE: {video_path} has no non-video/audio stream - cannot export embedded "
                "KLV telemetry.")
            return None

        matched_stream_index = None
        try:
            for packet in container.demux(*candidate_streams):
                data = bytes(packet)
                if not data:
                    continue
                elements = _walk_misb0601_local_set(data)
                if not elements:
                    continue
                packet_micros = None
                values = {}
                for tag, raw in elements:
                    if tag == _MISB0601_PRECISION_TIMESTAMP_TAG and len(raw) == 8:
                        packet_micros = int.from_bytes(raw, "big")
                        continue
                    decoder_entry = _MISB0601_TELEMETRY_TAG_DECODERS.get(tag)
                    if decoder_entry is None:
                        continue
                    key, decoder = decoder_entry
                    try:
                        values[_TELEMETRY_KEY_TO_CSV_COLUMN[key]] = decoder(raw)
                    except (ValueError, ZeroDivisionError):
                        continue
                if packet_micros is None:
                    continue
                if matched_stream_index is None:
                    matched_stream_index = packet.stream.index
                elif packet.stream.index != matched_stream_index:
                    continue
                row = {"Precision Time Stamp": datetime.fromtimestamp(
                    packet_micros / 1_000_000, tz=timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
                row.update(values)
                rows.append(row)
        except Exception as exc:
            log(f"WARNING: embedded KLV telemetry export stopped early ({exc}) - writing only "
                "what was found before this point.")

    if not rows:
        log(f"NOTE: {video_path} has no decodable MISB ST 0601 KLV metadata - no telemetry "
            "CSV exported.")
        return None

    fieldnames = ["Precision Time Stamp"] + sorted(
        {key for row in rows for key in row if key != "Precision Time Stamp"}
    )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv_module.DictWriter(handle, fieldnames=fieldnames, restval="")
        writer.writeheader()
        writer.writerows(rows)
    log(f"Exported {len(rows)} embedded KLV telemetry row(s) -> {output_path}")
    return str(output_path)


def _percentile(sorted_values, pct):
    """Linear-interpolation percentile (numpy's default method) - a stdlib
    stand-in since this project has no pandas/numpy dependency."""
    k = (len(sorted_values) - 1) * (pct / 100)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_values[int(k)]
    return sorted_values[lo] * (hi - k) + sorted_values[hi] * (k - lo)


def compute_value_statistics(values):
    """pandas-.describe()-style summary (count/mean/std/min/25%/50%/75%/max)
    plus mode, computed with stdlib `statistics` only (this project has no
    pandas/numpy dependency - see repo conventions). Returns None if fewer
    than 2 values are supplied."""
    clean = sorted(v for v in values if v is not None)
    if len(clean) < 2:
        return None
    try:
        mode = statistics.mode(clean)
    except statistics.StatisticsError:
        mode = None
    return {
        "count": len(clean),
        "min": round(clean[0], 4),
        "max": round(clean[-1], 4),
        "range": round(clean[-1] - clean[0], 4),
        "mean": round(statistics.fmean(clean), 4),
        "median": round(statistics.median(clean), 4),
        "mode": round(mode, 4) if mode is not None else None,
        "stdev": round(statistics.stdev(clean), 4),
        "q1": round(_percentile(clean, 25), 4),
        "q3": round(_percentile(clean, 75), 4),
    }


def probe_video_info(video_path, video_start_time_override=None,
                      break_threshold_multiplier=3.0, sensor_fallback_start_utc=None,
                      log=print):
    """Reads a video's container-level facts directly via PyAV: start/end
    UTC, duration, frame rate, resolution, codec, and "breaks" - gaps
    between consecutive packet timestamps significantly larger (by
    break_threshold_multiplier) than the expected 1/frame_rate interval,
    a cheap way (demux only, no full decode) to spot dropped-frame/
    encoding discontinuities. `info["interval_statistics"]` summarizes the
    SIZE of those flagged break gaps only (not every packet-to-packet
    interval), so its count always matches `len(info["breaks"])`. Never
    raises for a probing failure partway through - returns whatever was
    determined so far, logging a WARNING.

    Start/end time resolution order (info["start_source"] records which one
    was used): (1) video_start_time_override, (2) the video's OWN embedded
    MISB KLV metadata stream (see probe_embedded_klv_metadata() - the most
    authoritative source, since it's read directly from the video itself,
    no external input needed), (3) the container's own 'creation_time' tag,
    (4) sensor_fallback_start_utc - the EARLIEST timestamp from a
    sensor/telemetry table, if the caller supplies one (deterministic ONLY
    when that table is the same one the video was multiplexed FROM). When
    KLV metadata resolved both ends directly, end_utc is that value;
    otherwise end_utc is start + duration.
    """
    import av

    video_path = Path(video_path)
    info = {
        "path": str(video_path), "start_utc": None, "end_utc": None, "start_source": None,
        "duration_seconds": None, "frame_rate": None, "width": None, "height": None,
        "codec_name": None, "frame_count_estimate": None, "breaks": [], "embedded_klv": None,
        "interval_statistics": None,
    }
    creation_time = None
    klv_info = probe_embedded_klv_metadata(video_path, log=log)
    info["embedded_klv"] = klv_info
    with av.open(str(video_path)) as container:
        info["duration_seconds"] = container.duration / 1_000_000 if container.duration else None
        creation_time = container.metadata.get("creation_time")

        video_streams = container.streams.video
        if video_streams:
            stream = video_streams[0]
            info["width"] = stream.codec_context.width
            info["height"] = stream.codec_context.height
            info["codec_name"] = stream.codec_context.name
            rate = stream.average_rate or stream.base_rate
            info["frame_rate"] = float(rate) if rate else None
            if info["duration_seconds"] and info["frame_rate"]:
                info["frame_count_estimate"] = int(info["duration_seconds"] * info["frame_rate"])

            if info["frame_rate"]:
                expected_interval = 1.0 / info["frame_rate"]
                threshold = expected_interval * break_threshold_multiplier
                last_pts_seconds = None
                break_gap_values = []
                try:
                    for packet in container.demux(stream):
                        if packet.pts is None:
                            continue
                        pts_seconds = float(packet.pts * packet.time_base)
                        if last_pts_seconds is not None:
                            gap = pts_seconds - last_pts_seconds
                            if gap > threshold:
                                info["breaks"].append({
                                    "at_seconds": round(last_pts_seconds, 3),
                                    "gap_seconds": round(gap, 3),
                                })
                                break_gap_values.append(gap)
                        last_pts_seconds = pts_seconds
                except Exception as exc:
                    log(f"WARNING: break detection stopped early ({exc}) - reporting only "
                        "what was found before this point.")
                info["interval_statistics"] = compute_value_statistics(break_gap_values)

    start_dt = None
    if video_start_time_override:
        start_dt = video_metadata_core.parse_any_timestamp(video_start_time_override)
        info["start_source"] = "override"
    elif klv_info.get("available"):
        start_dt = video_metadata_core.parse_any_timestamp(klv_info["start_utc"])
        info["start_source"] = "embedded_klv_metadata"
        info["end_utc"] = klv_info["end_utc"]
    elif creation_time:
        start_dt = video_metadata_core.parse_any_timestamp(creation_time)
        info["start_source"] = "container_creation_time"
    elif sensor_fallback_start_utc:
        start_dt = video_metadata_core.parse_any_timestamp(sensor_fallback_start_utc)
        info["start_source"] = "sensor_table_earliest_timestamp"
        log(f"NOTE: {video_path} has no container 'creation_time' tag and no Video Start Time "
            f"Override was given - falling back to the sensor table's earliest timestamp "
            f"({sensor_fallback_start_utc}) as the video start time. This is only accurate if "
            "the supplied sensor table is the SAME one this video was multiplexed from.")
    else:
        log(f"WARNING: {video_path} has no embedded KLV metadata, no container 'creation_time' "
            "tag, no Video Start Time Override was given, and no sensor table was supplied to "
            "fall back on - cannot determine an absolute start/end time.")

    if start_dt is not None:
        info["start_utc"] = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        if info["end_utc"] is None and info["duration_seconds"] is not None:
            info["end_utc"] = (start_dt + timedelta(seconds=info["duration_seconds"])) \
                .strftime("%Y-%m-%dT%H:%M:%SZ")
    return info


# Telemetry value columns worth summarizing beyond timestamp/gaps - the
# ones Rule-Based Frame Extraction's Sensor Mappings/Extraction Rules
# constraints actually act on. Excludes "timestamp" (covered by start/end +
# gap statistics above) and "filename" (not numeric).
_TELEMETRY_VALUE_STAT_KEYS = ("x", "y", "z", "heading", "pitch", "roll")

# This project's own Generate Deep Ocean Video Metadata output always
# writes BOTH altitude columns (VIDEO_METADATA_TABLE_FIELDS) but only
# populates ONE per run depending on Z Value Type - the other stays blank
# for every row. resolve_field()'s fixed name-priority pick might land on
# the all-blank one; retried against the other name below if so.
_ALTERNATE_Z_COLUMN_NAMES = ("Sensor True Altitude", "Sensor Ellipsoid Height Extended")


def _numeric_column_values(rows, column):
    values = [video_metadata_core.to_float(row.get(column)) for row in rows]
    return [v for v in values if v is not None]


def compute_telemetry_value_statistics(rows, resolved_fields):
    """Per-column VALUE statistics (min/max/range/mean/median/mode/stdev -
    not gaps) for whichever of X/Y/Z/heading/pitch/roll were auto-detected,
    via the same compute_value_statistics() used for gap sizes. Useful for
    spotting bad/constant/out-of-range sensor readings before building
    Rule-Based Frame Extraction's sensor constraints. Skips any key that
    wasn't auto-detected or has fewer than 2 numeric values."""
    stats = {}
    for key in _TELEMETRY_VALUE_STAT_KEYS:
        column = resolved_fields.get(key)
        if not column:
            continue
        values = _numeric_column_values(rows, column)
        if key == "z" and not values and column in _ALTERNATE_Z_COLUMN_NAMES and rows:
            other = next((c for c in _ALTERNATE_Z_COLUMN_NAMES if c != column and c in rows[0]), None)
            if other:
                other_values = _numeric_column_values(rows, other)
                if other_values:
                    column, values = other, other_values
        column_stats = compute_value_statistics(values)
        if column_stats:
            column_stats["column"] = column
            stats[key] = column_stats
    return stats


def probe_sensor_table_info(table_path, gap_threshold_multiplier=5.0,
                             absolute_gap_threshold_seconds=None, log=print):
    """Reads a sensor table's own facts: row count, auto-detected
    timestamp/lat/lon/depth/altitude/heading/pitch/roll columns (reusing
    video_metadata_core's existing candidate-name resolution, not
    reimplemented), detected time range, median sampling interval, and
    gaps (consecutive-timestamp deltas exceeding the gap threshold).

    The gap threshold is `median_interval * gap_threshold_multiplier`
    (default 5x, matching this tool's video-side Break Sensitivity
    convention) unless `absolute_gap_threshold_seconds` is given, which
    OVERRIDES the multiplier entirely with a fixed seconds value - useful
    for a table whose sampling rate is too irregular for a median-based
    threshold to mean anything. The resolved value is recorded in
    `info["gap_threshold_seconds"]`.

    `info["interval_statistics"]` summarizes the SIZE of those flagged gaps
    only (not every consecutive sample interval), so its count always
    matches `len(info["gaps"])`. `info["field_statistics"]` summarizes the
    VALUES (min/max/range/mean/etc., not gaps) of whichever X/Y/Z/heading/
    pitch/roll columns were auto-detected - see
    compute_telemetry_value_statistics().
    """
    rows = video_metadata_core.read_telemetry_table(table_path)
    header = video_metadata_core.read_telemetry_header(table_path)
    resolved = video_metadata_core.resolve_telemetry_fields(header)

    info = {
        "path": str(table_path), "row_count": len(rows), "resolved_fields": resolved,
        "start_utc": None, "end_utc": None, "median_interval_seconds": None,
        "gap_threshold_seconds": None, "gaps": [],
        "interval_statistics": None,
        "field_statistics": compute_telemetry_value_statistics(rows, resolved),
    }

    timestamp_field = resolved.get("timestamp")
    if not timestamp_field:
        log("WARNING: could not auto-detect a timestamp column in the sensor table - "
            "time-range/coverage validation will be skipped.")
        return info

    timestamps = sorted(
        dt for dt in (video_metadata_core.parse_any_timestamp(row.get(timestamp_field))
                      for row in rows)
        if dt is not None
    )
    if not timestamps:
        log("WARNING: no parseable timestamps found in the sensor table's detected "
            f"timestamp column ('{timestamp_field}').")
        return info

    info["start_utc"] = timestamps[0].strftime("%Y-%m-%dT%H:%M:%SZ")
    info["end_utc"] = timestamps[-1].strftime("%Y-%m-%dT%H:%M:%SZ")

    if len(timestamps) >= 2:
        intervals = sorted((b - a).total_seconds() for a, b in zip(timestamps, timestamps[1:]))
        mid = len(intervals) // 2
        median_interval = (
            intervals[mid] if len(intervals) % 2 else (intervals[mid - 1] + intervals[mid]) / 2
        )
        info["median_interval_seconds"] = round(median_interval, 3)
        gap_threshold = (
            absolute_gap_threshold_seconds if absolute_gap_threshold_seconds is not None
            else median_interval * gap_threshold_multiplier
        )
        info["gap_threshold_seconds"] = round(gap_threshold, 3)
        gap_values = []
        for a, b in zip(timestamps, timestamps[1:]):
            gap = (b - a).total_seconds()
            if gap > gap_threshold:
                info["gaps"].append({
                    "after": a.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "before": b.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "gap_seconds": round(gap, 3),
                })
                gap_values.append(gap)
        info["interval_statistics"] = compute_value_statistics(gap_values)
    return info


def compute_coverage(video_info, sensor_info):
    """Validates whether the sensor table's detected time range actually
    overlaps the video's - the specific check this tool exists to make
    explicit rather than discovered only after a failed extraction run.
    """
    result = {"overlap_start": None, "overlap_end": None, "coverage_percentage": None, "warnings": []}
    v_start, v_end = video_info.get("start_utc"), video_info.get("end_utc")
    s_start, s_end = sensor_info.get("start_utc"), sensor_info.get("end_utc")

    if not video_info:
        result["warnings"].append("No video supplied - skipping coverage validation.")
        return result
    if not sensor_info:
        result["warnings"].append("No sensor table supplied - skipping coverage validation.")
        return result
    if not v_start or not v_end:
        result["warnings"].append(
            "Video start/end time could not be determined - skipping coverage validation."
        )
        return result
    if not s_start or not s_end:
        result["warnings"].append(
            "No sensor table time range available - skipping coverage validation."
        )
        return result

    if video_info.get("start_source") == "sensor_table_earliest_timestamp":
        result["warnings"].append(
            "Video start time was derived from this same sensor table's earliest timestamp "
            "(no container creation_time tag was available) - coverage below reflects that "
            "assumption rather than an independent cross-check between two separate sources."
        )

    v_start_dt = video_metadata_core.parse_any_timestamp(v_start)
    v_end_dt = video_metadata_core.parse_any_timestamp(v_end)
    s_start_dt = video_metadata_core.parse_any_timestamp(s_start)
    s_end_dt = video_metadata_core.parse_any_timestamp(s_end)

    overlap_start = max(v_start_dt, s_start_dt)
    overlap_end = min(v_end_dt, s_end_dt)
    if overlap_start >= overlap_end:
        result["warnings"].append(
            f"NO temporal overlap between the sensor table ({s_start} to {s_end}) and the "
            f"video ({v_start} to {v_end}) - check for a clock offset or a mismatched file pairing."
        )
        return result

    result["overlap_start"] = overlap_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    result["overlap_end"] = overlap_end.strftime("%Y-%m-%dT%H:%M:%SZ")
    video_duration = (v_end_dt - v_start_dt).total_seconds()
    overlap_duration = (overlap_end - overlap_start).total_seconds()
    coverage_percentage = (overlap_duration / video_duration * 100) if video_duration else None
    result["coverage_percentage"] = round(coverage_percentage, 1) if coverage_percentage is not None else None
    if coverage_percentage is not None and coverage_percentage < 99.9:
        result["warnings"].append(
            f"Sensor data covers only {result['coverage_percentage']}% of the video's "
            "duration - frames outside this window will have no matched sensor value."
        )
    if sensor_info.get("gaps"):
        result["warnings"].append(
            f"Sensor table has {len(sensor_info['gaps'])} gap(s) exceeding 5x its typical "
            "sampling interval - frames landing in these windows will have no matched sensor value."
        )
    return result


def build_suggested_mapping(resolved_fields):
    """Ready-to-copy (Target Field, Source Field) pairs, translating
    video_metadata_core's generic telemetry keys into the standard field
    vocabulary a downstream frame-extraction step expects."""
    mapping = []
    for key, target in _TELEMETRY_KEY_TO_MAPPING_TARGET.items():
        source = resolved_fields.get(key)
        if source:
            mapping.append((target, source))
    return mapping


def _append_interval_statistics(lines, stats, indent="  "):
    """Appends a pandas-.describe()-style statistics table (count/min/25%/
    median/75%/max/mean/mode/stdev) summarizing the SIZE of each
    already-flagged gap (not every normal frame-to-frame/sample-to-sample
    interval) - its Count always matches the "Breaks detected"/"Gaps
    detected" line printed just above it. Replaces an itemized per-gap
    dump with a proper summary, computed with stdlib `statistics` only
    (see compute_value_statistics()). Prints a placeholder if there are
    fewer than 2 flagged gaps to summarize."""
    if not stats:
        lines.append(f"{indent}Gap size statistics: (fewer than 2 gaps found)")
        return
    lines.append(f"{indent}Gap Size Statistics (seconds):")
    lines.append(f"{indent}  Count:    {stats['count']}")
    lines.append(f"{indent}  Min:      {stats['min']}")
    lines.append(f"{indent}  25% (Q1): {stats['q1']}")
    lines.append(f"{indent}  Median:   {stats['median']}")
    lines.append(f"{indent}  75% (Q3): {stats['q3']}")
    lines.append(f"{indent}  Max:      {stats['max']}")
    lines.append(f"{indent}  Mean:     {stats['mean']}")
    mode_text = stats["mode"] if stats["mode"] is not None else "n/a (no repeated value)"
    lines.append(f"{indent}  Mode:     {mode_text}")
    lines.append(f"{indent}  Std Dev:  {stats['stdev']}")


_TELEMETRY_VALUE_STAT_LABELS = {
    "z": "Z (Depth/Altitude)", "heading": "Heading", "pitch": "Pitch", "roll": "Roll",
    "x": "X", "y": "Y",
}


def _append_field_value_statistics(lines, field_statistics, indent="  "):
    """Appends a pandas-.describe()-style value table (min/max/range/mean/
    median/mode/stdev) for each auto-detected Z/heading/pitch/roll/X/Y
    column - the key telemetry fields Rule-Based Frame Extraction's Sensor
    Mappings/Extraction Rules constraints act on. No-ops if none of these
    columns were auto-detected (see compute_telemetry_value_statistics())."""
    if not field_statistics:
        return
    lines.append(f"{indent}Telemetry Value Statistics:")
    for key in _TELEMETRY_VALUE_STAT_KEYS:
        stats = field_statistics.get(key)
        if not stats:
            continue
        label = _TELEMETRY_VALUE_STAT_LABELS.get(key, key)
        lines.append(f"{indent}  {label} ('{stats['column']}'):")
        lines.append(f"{indent}    Count:  {stats['count']}")
        lines.append(f"{indent}    Min:    {stats['min']}")
        lines.append(f"{indent}    Max:    {stats['max']}")
        lines.append(f"{indent}    Range:  {stats['range']}")
        lines.append(f"{indent}    Mean:   {stats['mean']}")
        lines.append(f"{indent}    Median: {stats['median']}")
        mode_text = stats["mode"] if stats["mode"] is not None else "n/a (no repeated value)"
        lines.append(f"{indent}    Mode:   {mode_text}")
        lines.append(f"{indent}    StdDev: {stats['stdev']}")


# Combined variable table: one table holding both the video metadata and the
# sensor metadata for a deployment, so downstream steps need carry no join
# logic. A user who already has a single table holding both can skip this
# entirely and supply that table directly.
COLUMN_MAP_TABLE_SUFFIX = "_ColumnMap"
_MAX_TEXT_FIELD_LENGTH = 512
_MIN_JOIN_TOLERANCE_SECONDS = 0.05


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _detect_timestamp_column(header):
    return video_metadata_core.preview_resolve_telemetry_fields(header).get("timestamp")


def _auto_join_tolerance_seconds(timestamps):
    """Half the median sampling interval - the tightest tolerance that still
    lets every primary row pair with its genuinely nearest secondary sample."""
    ordered = sorted(dt for dt in timestamps if dt is not None)
    intervals = [
        (b - a).total_seconds() for a, b in zip(ordered, ordered[1:])
        if (b - a).total_seconds() > 0
    ]
    median = _median(intervals)
    if median is None:
        return None
    return max(median / 2.0, _MIN_JOIN_TOLERANCE_SECONDS)


def _unique_column_name(name, taken):
    candidate, suffix = name, 2
    while candidate in taken:
        candidate = f"{name}_{suffix}"
        suffix += 1
    return candidate


def _join_secondary_rows(primary_rows, primary_timestamps, secondary_rows,
                          secondary_timestamps, secondary_header, primary_header,
                          tolerance_seconds, log):
    """Nearest-timestamp join. Rows with no match inside the tolerance keep
    blank secondary values rather than being dropped."""
    paired = sorted(
        ((ts, row) for ts, row in zip(secondary_timestamps, secondary_rows) if ts is not None),
        key=lambda item: item[0],
    )
    if not paired:
        log("WARNING: no parseable timestamps in the secondary table - skipping the join.")
        return [], {}

    taken = set(primary_header)
    renamed = {}
    for column in secondary_header:
        renamed[column] = _unique_column_name(column, taken)
        taken.add(renamed[column])

    sorted_times = [ts for ts, _row in paired]
    added_columns = [renamed[column] for column in secondary_header]
    matched = 0
    for row, ts in zip(primary_rows, primary_timestamps):
        for column in added_columns:
            row[column] = None
        if ts is None:
            continue
        index = bisect.bisect_left(sorted_times, ts)
        best, best_delta = None, None
        for candidate in (index - 1, index):
            if 0 <= candidate < len(paired):
                delta = abs((sorted_times[candidate] - ts).total_seconds())
                if best_delta is None or delta < best_delta:
                    best, best_delta = paired[candidate][1], delta
        if best is None or best_delta > tolerance_seconds:
            continue
        matched += 1
        for column in secondary_header:
            row[renamed[column]] = best.get(column)

    log(f"Joined secondary table: {matched} of {len(primary_rows)} row(s) matched within "
        f"{round(tolerance_seconds, 3)}s.")
    return added_columns, renamed


def _infer_column_types(rows, columns, timestamp_column):
    """DATE for the timestamp column (so the Query Builder offers a date
    picker), DOUBLE where every populated value parses as a number, else TEXT."""
    types = {}
    for column in columns:
        if column == timestamp_column:
            types[column] = ("DATE", None)
            continue
        numeric, populated, width = True, False, 1
        for row in rows:
            value = row.get(column)
            if video_metadata_core.is_null_like(value):
                continue
            populated = True
            width = max(width, len(str(value)))
            if numeric and video_metadata_core.to_float(value) is None:
                numeric = False
                if width >= _MAX_TEXT_FIELD_LENGTH:
                    break
        if populated and numeric:
            types[column] = ("DOUBLE", None)
        else:
            types[column] = ("TEXT", min(max(width, 1), _MAX_TEXT_FIELD_LENGTH))
    return types


def _write_column_map_table(workspace, table_name, gdb_to_raw, log):
    import arcpy

    map_path = os.path.join(workspace, table_name)
    if arcpy.Exists(map_path):
        arcpy.management.Delete(map_path)
    arcpy.management.CreateTable(workspace, table_name)
    arcpy.management.AddField(map_path, "GdbField", "TEXT", field_length=255)
    arcpy.management.AddField(map_path, "RawField", "TEXT", field_length=_MAX_TEXT_FIELD_LENGTH)
    with arcpy.da.InsertCursor(map_path, ["GdbField", "RawField"]) as cursor:
        for gdb_name, raw_name in sorted(gdb_to_raw.items()):
            cursor.insertRow((gdb_name, raw_name))
    log(f"Wrote column map table: {map_path}")
    return map_path


def read_column_map_table(map_path):
    """Reads a `<name>_ColumnMap` table back into {gdb_field: raw_field}.
    Returns {} for a missing/unreadable table."""
    import arcpy

    try:
        if not arcpy.Exists(map_path):
            return {}
        with arcpy.da.SearchCursor(map_path, ["GdbField", "RawField"]) as cursor:
            return {row[0]: row[1] for row in cursor if row[0]}
    except Exception:
        return {}


def build_variable_table(primary_table, secondary_table=None, join_tolerance_seconds=None,
                          workspace=None, name=None, log=print):
    """Builds the combined variable table from a primary video metadata
    table plus an OPTIONAL secondary sensor table, nearest-timestamp joined.

    A geodatabase sanitizes column names ("Sensor Longitude" ->
    "Sensor_Longitude"), and consumers may need either the sanitized form or
    the table's raw header - so the gdb-to-raw map is persisted alongside as
    a `<name>_ColumnMap` table rather than left to be re-derived by
    guesswork later.
    """
    import arcpy

    if not workspace or not name:
        raise ValueError("Variable table workspace and name are both required.")

    primary_rows = video_metadata_core.read_telemetry_table(primary_table)
    primary_header = video_metadata_core.read_telemetry_header(primary_table)
    timestamp_column = _detect_timestamp_column(primary_header)
    if not timestamp_column:
        raise ValueError(
            f"Could not auto-detect a timestamp column in {primary_table} - a variable table "
            "must have one so rules can be time-aligned to video frames."
        )
    primary_timestamps = [
        video_metadata_core.parse_any_timestamp(row.get(timestamp_column)) for row in primary_rows
    ]

    columns = list(primary_header)
    if secondary_table:
        secondary_rows = video_metadata_core.read_telemetry_table(secondary_table)
        secondary_header = video_metadata_core.read_telemetry_header(secondary_table)
        secondary_timestamp_column = _detect_timestamp_column(secondary_header)
        if not secondary_timestamp_column:
            raise ValueError(
                f"Could not auto-detect a timestamp column in {secondary_table} - it cannot be "
                "joined to the primary table without one."
            )
        secondary_timestamps = [
            video_metadata_core.parse_any_timestamp(row.get(secondary_timestamp_column))
            for row in secondary_rows
        ]
        tolerance = join_tolerance_seconds
        if tolerance is None:
            tolerance = _auto_join_tolerance_seconds(secondary_timestamps)
            if tolerance is None:
                raise ValueError(
                    f"Could not compute an automatic join tolerance from {secondary_table} - "
                    "supply one explicitly."
                )
            log(f"Auto join tolerance: {round(tolerance, 3)}s "
                "(half the secondary table's median sampling interval).")
        added_columns, _renamed = _join_secondary_rows(
            primary_rows, primary_timestamps, secondary_rows, secondary_timestamps,
            secondary_header, primary_header, tolerance, log,
        )
        columns.extend(added_columns)

    table_name = arcpy.ValidateTableName(name, workspace)
    table_path = os.path.join(workspace, table_name)
    if arcpy.Exists(table_path):
        arcpy.management.Delete(table_path)
    arcpy.management.CreateTable(workspace, table_name)

    column_types = _infer_column_types(primary_rows, columns, timestamp_column)
    raw_to_gdb, gdb_to_raw = {}, {}
    for column in columns:
        gdb_name = _unique_column_name(
            arcpy.ValidateFieldName(column, workspace), set(gdb_to_raw)
        )
        raw_to_gdb[column] = gdb_name
        gdb_to_raw[gdb_name] = column
        field_type, field_length = column_types[column]
        if field_type == "TEXT":
            arcpy.management.AddField(table_path, gdb_name, "TEXT", field_length=field_length)
        else:
            arcpy.management.AddField(table_path, gdb_name, field_type)

    gdb_fields = [raw_to_gdb[column] for column in columns]
    with arcpy.da.InsertCursor(table_path, gdb_fields) as cursor:
        for row, timestamp in zip(primary_rows, primary_timestamps):
            values = []
            for column in columns:
                field_type, _length = column_types[column]
                if column == timestamp_column:
                    # arcpy DATE fields reject tz-aware datetimes.
                    values.append(timestamp.replace(tzinfo=None) if timestamp else None)
                elif field_type == "DOUBLE":
                    values.append(video_metadata_core.to_float(row.get(column)))
                else:
                    value = row.get(column)
                    values.append(None if video_metadata_core.is_null_like(value) else str(value))
            cursor.insertRow(values)

    log(f"Wrote variable table: {table_path} ({len(primary_rows)} row(s), "
        f"{len(columns)} column(s), timestamp column '{timestamp_column}').")
    column_map_path = _write_column_map_table(
        workspace, f"{table_name}{COLUMN_MAP_TABLE_SUFFIX}", gdb_to_raw, log,
    )
    return {
        "path": table_path,
        "column_map_path": column_map_path,
        "raw_to_gdb": raw_to_gdb,
        "gdb_to_raw": gdb_to_raw,
        "timestamp_column": timestamp_column,
        "row_count": len(primary_rows),
    }


def write_description_report(video_info, sensor_info, coverage, suggested_mapping,
                              output_folder, output_name, variable_table=None, log=print):
    """Writes a lasinfo-style plain-text report plus a companion JSON with
    the same facts. Define Frame Extraction Rule and Extract Video Frames by
    Rules both read the JSON to pre-fill their own parameters - including the
    combined variable table path, when one was built."""
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)
    txt_path = output_folder / f"{output_name}_DescriptionReport.txt"
    json_path = output_folder / f"{output_name}_DescriptionReport.json"

    lines = ["=" * 70, "VIDEO / SENSOR DATA DESCRIPTION REPORT", "=" * 70, ""]

    if video_info:
        lines.append(f"Video: {video_info.get('path')}")
        lines.append(f"  Start (UTC):      {video_info.get('start_utc') or 'UNKNOWN'}")
        lines.append(f"  End (UTC):        {video_info.get('end_utc') or 'UNKNOWN'}")
        if video_info.get("start_source"):
            lines.append(f"  Start time from:  {video_info['start_source']}")
        klv = video_info.get("embedded_klv") or {}
        if klv.get("available"):
            lines.append(f"  Embedded KLV:     {klv['packet_count']} MISB timestamp(s) found "
                          f"({klv['start_utc']} to {klv['end_utc']})")
            if klv.get("csv_path"):
                lines.append(f"  Embedded telemetry CSV: {klv['csv_path']} (usable directly as "
                              "Rule-Based Frame Extraction's Sensor Data Table)")
            _append_field_value_statistics(lines, klv.get("field_statistics"))
        else:
            lines.append("  Embedded KLV:     not detected")
        lines.append(f"  Duration:         {video_info.get('duration_seconds')} s")
        lines.append(f"  Frame rate:       {video_info.get('frame_rate')} fps")
        lines.append(f"  Resolution:       {video_info.get('width')} x {video_info.get('height')}")
        lines.append(f"  Codec:            {video_info.get('codec_name')}")
        lines.append(f"  Est. frame count: {video_info.get('frame_count_estimate')}")
        breaks = video_info.get("breaks") or []
        lines.append(f"  Breaks detected:  {len(breaks)}")
        _append_interval_statistics(lines, video_info.get("interval_statistics"))
        lines.append("")
    else:
        lines.append("Video: (none supplied)")
        lines.append("")

    if sensor_info:
        lines.append(f"Sensor Table: {sensor_info.get('path')}")
        lines.append(f"  Row count:        {sensor_info.get('row_count')}")
        lines.append(f"  Start (UTC):      {sensor_info.get('start_utc') or 'UNKNOWN'}")
        lines.append(f"  End (UTC):        {sensor_info.get('end_utc') or 'UNKNOWN'}")
        lines.append(f"  Median interval:  {sensor_info.get('median_interval_seconds')} s")
        lines.append(f"  Gap threshold:    {sensor_info.get('gap_threshold_seconds')} s")
        gaps = sensor_info.get("gaps") or []
        lines.append(f"  Gaps detected:    {len(gaps)}")
        _append_interval_statistics(lines, sensor_info.get("interval_statistics"))
        lines.append("  Detected columns:")
        for key, column in (sensor_info.get("resolved_fields") or {}).items():
            lines.append(f"    {key:>10}: {column or '(not detected)'}")
        _append_field_value_statistics(lines, sensor_info.get("field_statistics"))
        lines.append("")
    else:
        lines.append("Sensor Table: (none supplied)")
        lines.append("")

    lines.append("Coverage Validation:")
    if coverage.get("overlap_start"):
        lines.append(f"  Overlap window:   {coverage['overlap_start']} to {coverage['overlap_end']}")
        lines.append(f"  Coverage:         {coverage.get('coverage_percentage')}% of video duration")
    for warning in coverage.get("warnings", []):
        lines.append(f"  WARNING: {warning}")
    lines.append("")

    lines.append("Suggested Field Mappings (detected source column for each standard field):")
    if suggested_mapping:
        for target, source in suggested_mapping:
            lines.append(f"  Target Field: {target:<12} Source Field: {source}")
    else:
        lines.append("  (none - no sensor table supplied, or no fields auto-detected)")
    lines.append("")
    if video_info:
        lines.append("Video time window (use these bounds when selecting a range of frames):")
        lines.append(f"  Period Start UTC: {video_info.get('start_utc') or 'UNKNOWN'}")
        lines.append(f"  Period End UTC:   {video_info.get('end_utc') or 'UNKNOWN'}")

    if variable_table:
        lines.append("")
        lines.append("Combined Variable Table:")
        lines.append(f"  Table:      {variable_table.get('path')}")
        lines.append(f"  Column map: {variable_table.get('column_map_path')}")
        lines.append(f"  Rows:       {variable_table.get('row_count')}")
        lines.append(f"  Timestamp:  {variable_table.get('timestamp_column')}")

    with open(txt_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))

    payload = {
        "video": video_info, "sensor_table": sensor_info,
        "coverage": coverage, "suggested_mapping": suggested_mapping,
        "variable_table": variable_table,
    }
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)

    log(f"Wrote description report: {txt_path}")
    return str(txt_path), str(json_path)
