"""Read RealityScan's standard GUI camera CSV without assuming a pose convention.

The CSV does not establish the yaw/pitch/roll order, camera axes, principal-point
convention, or distortion model. Consumers must determine those from image
reprojection before creating Blender cameras. Diagnostics here are review flags,
not calibration decisions.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
import re
import struct

from ..errors import InvalidParamsError


HEADER = (
    "#name", "x", "y", "alt", "yaw", "pitch", "roll", "f_35mm",
    "px_norm", "py_norm", "k1", "k2", "k3", "k4", "t1", "t2",
)
MAX_CSV_BYTES = 32 * 1024 * 1024
MAX_CAMERAS = 100_000
MAX_ABS_POSITION = 1_000_000_000.0
MAX_ABS_ANGLE = 1_000_000.0
MAX_FOCAL_35MM = 10_000.0
MAX_ABS_PRINCIPAL = 10.0
MAX_ABS_DISTORTION = 1_000_000_000.0

# Conservative raw-coefficient flags. The CSV supplies no distortion-model
# convention, so these thresholds never reject a row or imply pixel error.
LARGE_DISTORTION_LIMITS = {
    "k1": 0.5, "k2": 1.0, "k3": 2.0, "k4": 4.0,
    "t1": 0.1, "t2": 0.1,
}
# A >30% relative focal change within two seconds is worth image review.
# Zoom is valid in the Owner's game footage; this is deliberately a warning.
FOCAL_JUMP_FRACTION = 0.30
FOCAL_NEIGHBOR_SECONDS = 2.0

_TIMESTAMP = re.compile(r"_(\d{2})-(\d{2})-(\d{2})-(\d{3})\.png$", re.IGNORECASE)
_FRAME_INDEX = re.compile(r"_\d{6}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def read_png_dimensions(path: str | Path) -> tuple[int, int]:
    """Read PNG dimensions from the signature and IHDR, without image decoding."""
    image_path = Path(path)
    try:
        with image_path.open("rb") as stream:
            head = stream.read(33)
    except OSError as exc:
        raise InvalidParamsError(f"Cannot read PNG {image_path}: {exc}") from exc
    if (len(head) != 33 or head[:8] != _PNG_SIGNATURE or
            struct.unpack(">I", head[8:12])[0] != 13 or head[12:16] != b"IHDR"):
        raise InvalidParamsError(f"Invalid PNG signature or IHDR: {image_path}")
    width, height = struct.unpack(">II", head[16:24])
    if not 1 <= width <= 100_000 or not 1 <= height <= 100_000:
        raise InvalidParamsError(f"PNG dimensions out of bounds: {image_path}")
    return width, height


def timestamp_seconds(name: str) -> float | None:
    """Extract the terminal HH-MM-SS-mmm game-frame timestamp, if present."""
    match = _TIMESTAMP.search(name)
    if match is None:
        return None
    hours, minutes, seconds, milliseconds = map(int, match.groups())
    if minutes >= 60 or seconds >= 60 or milliseconds >= 1000:
        return None
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def _timestamp_group(name: str) -> str | None:
    """Keep independently timed clips/segments out of neighbour diagnostics."""
    match = _TIMESTAMP.search(name)
    if match is None:
        return None
    return _FRAME_INDEX.sub("", name[:match.start()])


def _number(raw: str, field: str, line: int) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidParamsError(f"CSV line {line}: {field} must be numeric") from exc
    if not math.isfinite(value):
        raise InvalidParamsError(f"CSV line {line}: {field} must be finite")
    if field in ("x", "y", "alt"):
        valid = abs(value) <= MAX_ABS_POSITION
    elif field in ("yaw", "pitch", "roll"):
        valid = abs(value) <= MAX_ABS_ANGLE
    elif field == "f_35mm":
        valid = 0 < value <= MAX_FOCAL_35MM
    elif field in ("px_norm", "py_norm"):
        valid = abs(value) <= MAX_ABS_PRINCIPAL
    else:
        valid = abs(value) <= MAX_ABS_DISTORTION
    if not valid:
        raise InvalidParamsError(f"CSV line {line}: {field} is out of bounds")
    return value


def parse_realityscan_csv(
    source_path: str | Path, images_dir: str | Path | None = None,
) -> dict:
    """Parse a standard GUI export and report suspect intrinsics.

    All raw per-image fields remain intact. Image dimensions are included when
    ``images_dir`` is given and the matching PNG exists. No rotation, focal
    conversion, point-cloud transform, or undistortion is inferred here.
    """
    if not isinstance(source_path, (str, Path)):
        raise InvalidParamsError("source_path must be an absolute CSV path")
    path = Path(source_path)
    if not path.is_absolute() or path.suffix.lower() != ".csv":
        raise InvalidParamsError("source_path must be an absolute CSV path")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise InvalidParamsError(f"Cannot read source_path {path}: {exc}") from exc
    if not 0 < size <= MAX_CSV_BYTES:
        raise InvalidParamsError("source_path CSV is empty or exceeds 32 MiB")
    images = None
    if images_dir is not None:
        if not isinstance(images_dir, (str, Path)):
            raise InvalidParamsError("images_dir must be an existing absolute directory")
        images = Path(images_dir)
        if not images.is_absolute() or not images.is_dir():
            raise InvalidParamsError("images_dir must be an existing absolute directory")

    cameras: list[dict] = []
    warnings: list[dict] = []
    seen: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            if tuple(next(reader, ())) != HEADER:
                raise InvalidParamsError("RealityScan CSV header does not match the standard GUI export")
            for row in reader:
                line = reader.line_num
                if len(row) != len(HEADER):
                    raise InvalidParamsError(f"CSV line {line}: expected {len(HEADER)} fields")
                name = row[0]
                if (not name or name != name.strip() or Path(name).name != name or
                        any(character in name for character in '/\\<>:"|?*\0') or
                        Path(name).suffix.lower() != ".png"):
                    raise InvalidParamsError(f"CSV line {line}: #name must be a PNG filename")
                key = name.casefold()
                if key in seen:
                    raise InvalidParamsError(f"CSV line {line}: duplicate camera name {name}")
                seen.add(key)
                if len(cameras) >= MAX_CAMERAS:
                    raise InvalidParamsError(f"CSV line {line}: too many cameras")
                camera = {"name": name, "source_line": line}
                camera.update({field: _number(raw, field, line)
                               for field, raw in zip(HEADER[1:], row[1:])})
                camera["timestamp_seconds"] = timestamp_seconds(name)
                camera["timestamp_group"] = _timestamp_group(name)
                if images is not None:
                    image_path = images / name
                    if image_path.is_file():
                        camera["image_size"] = list(read_png_dimensions(image_path))
                    else:
                        camera["image_size"] = None
                        warnings.append({"code": "image_missing", "name": name})
                excess = {field: camera[field] for field, limit in LARGE_DISTORTION_LIMITS.items()
                          if abs(camera[field]) > limit}
                if excess:
                    warnings.append({"code": "large_distortion", "name": name,
                                     "coefficients": excess})
                cameras.append(camera)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InvalidParamsError(f"Cannot parse RealityScan CSV {path}: {exc}") from exc
    if not cameras:
        raise InvalidParamsError("RealityScan CSV has no cameras")

    # Compare consecutive timestamped images only within the same clip/segment.
    # The leading video timestamp is part of the group; the terminal frame
    # timestamp is the comparable time within that group.
    timed = sorted((camera for camera in cameras if camera["timestamp_seconds"] is not None),
                   key=lambda camera: (camera["timestamp_group"],
                                       camera["timestamp_seconds"], camera["name"]))
    for before, after in zip(timed, timed[1:]):
        if before["timestamp_group"] != after["timestamp_group"]:
            continue
        gap = after["timestamp_seconds"] - before["timestamp_seconds"]
        if not 0 < gap <= FOCAL_NEIGHBOR_SECONDS:
            continue
        ratio = abs(after["f_35mm"] - before["f_35mm"]) / before["f_35mm"]
        if ratio > FOCAL_JUMP_FRACTION:
            warnings.append({"code": "abrupt_focal_jump", "name": after["name"],
                             "previous_name": before["name"], "gap_seconds": gap,
                             "relative_change": ratio})
    warning_counts = {code: sum(item["code"] == code for item in warnings)
                      for code in {item["code"] for item in warnings}}
    return {
        "source_path": str(path.resolve()), "camera_count": len(cameras),
        "cameras": cameras, "warnings": warnings, "warning_counts": warning_counts,
        "diagnostic_thresholds": {
            "large_distortion_abs": dict(LARGE_DISTORTION_LIMITS),
            "focal_jump_fraction": FOCAL_JUMP_FRACTION,
            "focal_neighbor_seconds": FOCAL_NEIGHBOR_SECONDS,
        },
    }
