"""Compare real files against an explicitly supplied, trusted legacy script."""
from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
from pathlib import Path

import numpy as np
from netCDF4 import Dataset

from gdex_downloader.gridsat_processing import CHANNELS, convert_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--sources", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("legacy_gridsat_reference", args.reference)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load reference script")
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    lat, lon, _ = reference.load_target_grid(None, 1.0)
    reports = []
    with tempfile.TemporaryDirectory(prefix="gridsat-reference-") as tmp:
        for source in args.sources:
            dest = Path(tmp) / (source.name + ".npy")
            receipt = convert_file(source, dest)
            actual = np.load(dest, allow_pickle=False)
            with Dataset(source) as ds:
                lookup = reference.build_source_to_target_lookup(
                    np.asarray(ds.variables["lat"][:], dtype=np.float32),
                    np.asarray(ds.variables["lon"][:], dtype=np.float32), lat, lon, 1.0,
                )
                for channel, name in enumerate(CHANNELS):
                    grid, _ = reference.variable_to_source_grid(ds, name)
                    expected = reference.sample_nearest_to_target(grid, *lookup)
                    if not np.array_equal(actual[channel], expected, equal_nan=True):
                        raise AssertionError(f"Reference mismatch for {source.name}: {name}")
            reports.append({"source": str(source), "equal_including_nan": True, "output": receipt})
    print(json.dumps(reports, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
