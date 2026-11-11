"""The fixed GridSat nearest-point contract, without downloader side effects.

Numerical operations follow Gridsat/gridsat_interp_1deg.py. NumPy and netCDF4
are imported lazily so the downloader can inspect its configuration without
installing the optional processing dependencies.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "gridsat-nearest-v1"
CHANNELS = ("irwin_cdr", "irwvp", "vschn")
OUTPUT_SHAPE = (3, 181, 360)

_FILE_RE = re.compile(
    r"^GRIDSAT-B1\.(\d{4})\.(\d{2})\.(\d{2})\.(\d{2})\.v\w+\.nc(?:\.part)?$"
)
_PACKING_ATTRS = (
    "_FillValue", "missing_value", "scale_factor", "add_offset",
    "valid_range", "valid_min", "valid_max", "_Unsigned",
)


def dataset_metadata() -> dict[str, Any]:
    """Return the JSON-safe, dependency-free description of every output array."""
    return {
        "contract_version": CONTRACT_VERSION,
        "channels": list(CHANNELS),
        "shape": list(OUTPUT_SHAPE),
        "dtype": "float32",
        "dimensions": ["channel", "lat", "lon"],
        "coordinates": {
            "lat": {"values": list(range(90, -91, -1)), "units": "degrees_north"},
            "lon": {"values": list(range(360)), "units": "degrees_east"},
        },
        "units": {"irwin_cdr": "K", "irwvp": "K", "vschn": "1"},
        "method": {
            "name": "nearest",
            "source_coordinate_dtype": "float32",
            "distance": "planar Euclidean degrees",
            "radius_degrees": 0.500001,
            "radius_comparison": "strictly_less_than",
            "tie_break": "left",
            "shared_source_indices": True,
            "longitude": "convert target range to source range; no periodic neighbor search",
            "missing": "mask original _FillValue/missing_value before float32 scale+offset",
            "valid_range_filter": False,
            "alternate_valid_point": False,
            "temporal_average": False,
        },
    }


def _json_value(value: Any) -> Any:
    import numpy as np

    if isinstance(value, np.ndarray):
        return _json_value(value.tolist())
    if isinstance(value, np.generic):
        return _json_value(value.item())
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    return value


def _utc_time(value: str, *, internal: bool = False) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid ISO UTC time: {value!r}") from exc
    if parsed.tzinfo is None and internal:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"Time must specify UTC: {value!r}")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _filename_time(path: Path) -> str | None:
    match = _FILE_RE.fullmatch(path.name)
    if match is None:
        return None
    year, month, day, hour = match.groups()
    return _utc_time(f"{year}-{month}-{day}T{hour}:00:00Z")


def _source_time(ds: Any, path: Path, expected_time: str | None) -> dict[str, Any]:
    import numpy as np
    from netCDF4 import num2date

    expected = _utc_time(expected_time) if expected_time is not None else None
    filename_time = _filename_time(path)
    if "time" in ds.dimensions and len(ds.dimensions["time"]) != 1:
        raise ValueError("GridSat must contain exactly one timestep")

    decoded_time = None
    time_metadata: dict[str, Any] = {}
    fallback_reason = "time variable is absent"
    if "time" in ds.variables:
        var = ds.variables["time"]
        if var.dimensions not in ((), ("time",)) or var.size != 1:
            raise ValueError("time must be a scalar or a single-element time coordinate")
        # I/O failures must escape: a corrupt time array is not a filename fallback.
        values = np.ma.asarray(var[:])
        time_metadata = {
            "dimensions": list(var.dimensions),
            "units": _json_value(getattr(var, "units", None)),
            "calendar": _json_value(getattr(var, "calendar", "standard")),
        }
        if values.dtype.kind not in "iuf":
            fallback_reason = "time coordinate is not numeric"
        elif np.ma.getmaskarray(values).any():
            fallback_reason = "time coordinate is missing"
        elif not np.isfinite(values).all():
            raise ValueError("time coordinate must be finite")
        else:
            try:
                decoded = num2date(
                    values.reshape(-1)[0], var.units, getattr(var, "calendar", "standard")
                )
                decoded_time = _utc_time(decoded.isoformat(), internal=True)
            except (ValueError, TypeError, OverflowError, AttributeError) as exc:
                fallback_reason = f"time decode failed: {exc}"

    if decoded_time is not None and filename_time is not None and decoded_time != filename_time:
        raise ValueError(f"Internal time {decoded_time} disagrees with filename time {filename_time}")
    actual = decoded_time or filename_time
    if actual is None:
        raise ValueError(f"Cannot determine source time: {fallback_reason}; no GridSat filename time")
    if expected is not None and actual != expected:
        raise ValueError(f"Source time {actual} disagrees with expected time {expected}")
    return {
        "time": actual,
        "time_source": "internal" if decoded_time is not None else "filename",
        "filename_time": filename_time,
        "time_metadata": time_metadata,
        "time_fallback_reason": None if decoded_time is not None else fallback_reason,
    }


def _read_coordinate(ds: Any, name: str) -> tuple[Any, dict[str, Any]]:
    import numpy as np

    if name not in ds.variables or name not in ds.dimensions:
        raise ValueError(f"Missing {name} coordinate")
    var = ds.variables[name]
    if var.dimensions != (name,) or var.size == 0:
        raise ValueError(f"{name} must be a nonempty one-dimensional coordinate")
    units = getattr(var, "units", None)
    allowed_units = {
        "lat": {"degrees_north", "degree_north", "degrees_n", "degree_n"},
        "lon": {"degrees_east", "degree_east", "degrees_e", "degree_e"},
    }
    if not isinstance(units, str) or units.strip().lower() not in allowed_units[name]:
        raise ValueError(f"Invalid {name} coordinate units: {units!r}")
    values = np.ma.asarray(var[:])
    if values.dtype.kind not in "iuf" or np.ma.getmaskarray(values).any():
        raise ValueError(f"{name} coordinate must be numeric and unmasked")
    with np.errstate(over="ignore", invalid="ignore"):
        source = np.asarray(values, dtype=np.float32)
    if not np.isfinite(source).all() or not np.all(source[1:] > source[:-1]):
        raise ValueError(f"{name} coordinate must be finite and strictly increasing in float32")
    lower, upper = float(source[0]), float(source[-1])
    if name == "lat" and not (-90 <= lower <= upper <= 90):
        raise ValueError("Latitude coordinate is outside [-90, 90]")
    if name == "lon" and not (-180 <= lower <= upper <= 180 or 0 <= lower <= upper <= 360):
        raise ValueError("Longitude coordinate must use [-180, 180] or [0, 360]")
    return source, {
        "units": units,
        "size": int(source.size),
        "dtype": "float32",
        "source_dtype": str(var.dtype),
        "range": [lower, upper],
    }


def _nearest_indices_1d(source: Any, target: Any) -> tuple[Any, Any]:
    import numpy as np

    source = np.asarray(source, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    pos = np.searchsorted(source, target)
    right = np.clip(pos, 0, source.size - 1)
    left = np.clip(pos - 1, 0, source.size - 1)
    choose_left = np.abs(target - source[left]) <= np.abs(target - source[right])
    idx = np.where(choose_left, left, right).astype(np.int64)
    return idx, np.abs(target - source[idx]).astype(np.float32)


def _match_target_lon_to_source(source_lon: Any, target_lon: Any) -> Any:
    import numpy as np

    src_min = float(np.nanmin(source_lon))
    src_max = float(np.nanmax(source_lon))
    if src_min < 0.0 and src_max <= 180.0:
        return (((target_lon + 180.0) % 360.0) - 180.0).astype(np.float32)
    if src_min >= 0.0 and np.nanmax(target_lon) <= 180.0:
        return ((target_lon % 360.0) + 360.0) % 360.0
    return target_lon.astype(np.float32)


def _build_lookup(source_lat: Any, source_lon: Any, target_lat: Any, target_lon: Any) -> tuple[Any, Any, Any]:
    lat_idx, lat_dist = _nearest_indices_1d(source_lat, target_lat)
    lon_idx, lon_dist = _nearest_indices_1d(
        source_lon, _match_target_lon_to_source(source_lon, target_lon)
    )
    d2 = lat_dist[:, None] ** 2 + lon_dist[None, :] ** 2
    return lat_idx, lon_idx, d2 < (0.500001) ** 2


def _read_physical(var: Any) -> Any:
    import numpy as np

    auto_mask, auto_scale = getattr(var, "mask", True), getattr(var, "scale", True)
    var.set_auto_maskandscale(False)
    try:
        raw = np.asarray(var[:])
    finally:
        var.set_auto_mask(auto_mask)
        var.set_auto_scale(auto_scale)
    if raw.dtype.kind not in "iuf":
        raise ValueError(f"{var.name} must contain real numeric values")
    mask = np.zeros(raw.shape, dtype=bool)
    for attr in ("_FillValue", "missing_value"):
        if hasattr(var, attr):
            missing = np.asarray(getattr(var, attr), dtype=raw.dtype)
            for sentinel in missing.reshape(-1):
                mask |= raw == sentinel
    if np.any(np.isinf(raw) & ~mask):
        raise ValueError(f"{var.name} contains infinity")

    with np.errstate(over="ignore", invalid="ignore"):
        data = raw.astype(np.float32)
        if hasattr(var, "scale_factor") or hasattr(var, "add_offset"):
            scale = np.asarray(getattr(var, "scale_factor", 1.0), dtype=np.float32)
            offset = np.asarray(getattr(var, "add_offset", 0.0), dtype=np.float32)
            if scale.size != 1 or offset.size != 1 or not np.isfinite(scale).all() or not np.isfinite(offset).all():
                raise ValueError(f"{var.name} packing scale and offset must be finite scalars")
            data = data * scale.reshape(())[()] + offset.reshape(())[()]
        data[mask] = np.nan
    if np.isinf(data).any():
        raise ValueError(f"{var.name} contains infinity after float32 unpacking")
    return data


def _process_source(path: Path, expected_time: str | None, *, regrid: bool) -> tuple[dict[str, Any], Any]:
    import numpy as np
    from netCDF4 import Dataset

    output = None
    with Dataset(str(path), "r") as ds:
        source_lat, lat_metadata = _read_coordinate(ds, "lat")
        source_lon, lon_metadata = _read_coordinate(ds, "lon")
        metadata = {
            "path": str(path),
            "size": path.stat().st_size,
            **_source_time(ds, path, expected_time),
            "coordinates": {"lat": lat_metadata, "lon": lon_metadata},
            "channels": {},
            "finite_counts": {},
        }
        if regrid:
            lat_idx, lon_idx, valid = _build_lookup(
                source_lat, source_lon,
                np.arange(90, -91, -1, dtype=np.float32),
                np.arange(360, dtype=np.float32),
            )
            output = np.empty(OUTPUT_SHAPE, dtype=np.float32)
        for channel_idx, name in enumerate(CHANNELS):
            if name not in ds.variables:
                raise ValueError(f"Missing GridSat channel: {name}")
            var = ds.variables[name]
            if var.dimensions not in (("lat", "lon"), ("time", "lat", "lon")):
                raise ValueError(f"{name} has invalid dimensions: {var.dimensions}")
            expected_shape = (source_lat.size, source_lon.size)
            if var.dimensions[0] == "time":
                expected_shape = (1, *expected_shape)
            if var.shape != expected_shape:
                raise ValueError(f"{name} has invalid shape: {var.shape}")
            units = getattr(var, "units", None)
            allowed_units = {"1", "unitless", "dimensionless"} if name == "vschn" else {"k", "kelvin", "kelvins"}
            if not isinstance(units, str) or units.strip().lower() not in allowed_units:
                raise ValueError(f"Invalid {name} units: {units!r}")
            # Always read the entire array, including when no target cell is valid.
            data = _read_physical(var)
            finite_count = int(np.isfinite(data).sum())
            metadata["finite_counts"][name] = finite_count
            metadata["channels"][name] = {
                "dimensions": list(var.dimensions),
                "shape": list(var.shape),
                "dtype": str(var.dtype),
                "units": units,
                "packing": {
                    attr: _json_value(var.getncattr(attr))
                    for attr in _PACKING_ATTRS if attr in var.ncattrs()
                },
            }
            if regrid:
                grid = data[0] if data.ndim == 3 else data
                sampled = grid[np.ix_(lat_idx, lon_idx)]
                sampled[~valid] = np.nan
                output[channel_idx] = sampled
    return metadata, output


def validate_source(path: Path, expected_time: str | None = None) -> dict[str, Any]:
    """Read all three channels and validate coordinates, packing, units and time.

    Returns JSON-safe source metadata. Semantic errors raise ValueError; NetCDF
    read errors propagate. No source file is modified or removed.
    """
    metadata, _ = _process_source(Path(path), expected_time, regrid=False)
    return metadata


def validate_output(path: Path, expected_sha256: str | None = None) -> dict[str, Any]:
    """Validate one float32 NPY array and return its integrity metrics.

    finite_counts is keyed by CHANNELS. NPY carries no timestamp, so time is
    None here; convert_file supplies the verified source time in its receipt.
    """
    import numpy as np

    path = Path(path)
    if expected_sha256 is not None and re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None:
        raise ValueError("expected_sha256 must be a 64-character hexadecimal digest")
    with path.open("rb") as handle:
        data = np.load(handle, allow_pickle=False)
        if not isinstance(data, np.ndarray):
            data.close()
            raise ValueError("Output must be a single NPY array, not an archive")
        if data.shape != OUTPUT_SHAPE or data.dtype != np.dtype("float32"):
            raise ValueError(f"Invalid output shape/dtype: {data.shape}/{data.dtype}")
        if np.isinf(data).any():
            raise ValueError("Output contains infinity")
        if handle.read(1):
            raise ValueError("Output contains trailing data")
        finite_counts = {
            name: int(np.isfinite(data[index]).sum()) for index, name in enumerate(CHANNELS)
        }
        handle.seek(0)
        digest = hashlib.sha256()
        size = 0
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    sha256 = digest.hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256.lower():
        raise ValueError(f"Output SHA-256 mismatch: expected {expected_sha256}, got {sha256}")
    return {
        "contract_version": CONTRACT_VERSION,
        "path": str(path),
        "sha256": sha256,
        "size": size,
        "shape": list(OUTPUT_SHAPE),
        "dtype": "float32",
        "finite_counts": finite_counts,
        "time": None,
    }


def convert_file(source: Path, dest: Path, expected_time: str | None = None) -> dict[str, Any]:
    """Validate, sample, fsync and atomically publish an NPY; return its receipt.

    Only this invocation's unique temporary file is cleaned on failure. Existing
    outputs remain untouched until validation succeeds. Manifest/receipt storage
    and source lifecycle management belong to the caller.
    """
    import numpy as np

    source, dest = Path(source), Path(dest)
    if source.resolve() == dest.resolve() or (dest.exists() and source.samefile(dest)):
        raise ValueError("Output must not replace the source file")
    source_metadata, output = _process_source(source, expected_time, regrid=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            np.save(handle, output, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        receipt = {
            **validate_output(temporary),
            "path": str(dest),
            "time": source_metadata["time"],
            "source": source_metadata,
        }
        os.replace(temporary, dest)
        temporary = None
        return receipt
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
