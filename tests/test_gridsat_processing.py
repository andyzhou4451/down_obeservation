from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gdex_downloader import gridsat_processing as processing

try:
    import numpy as np
except (ImportError, OSError):
    np = None

try:
    from netCDF4 import Dataset
except (ImportError, OSError):
    Dataset = None


STAMP = "2016-01-01T00:00:00Z"
FILENAME = "GRIDSAT-B1.2016.01.01.00.v02r01.nc"


class MetadataTests(unittest.TestCase):
    def test_fixed_json_safe_contract(self) -> None:
        self.assertEqual(processing.CONTRACT_VERSION, "gridsat-nearest-v1")
        self.assertEqual(processing.CHANNELS, ("irwin_cdr", "irwvp", "vschn"))
        self.assertEqual(processing.OUTPUT_SHAPE, (3, 181, 360))
        metadata = processing.dataset_metadata()
        json.dumps(metadata, allow_nan=False)
        self.assertEqual(metadata["coordinates"]["lat"]["values"], list(range(90, -91, -1)))
        self.assertEqual(metadata["coordinates"]["lon"]["values"], list(range(360)))
        self.assertEqual(metadata["units"], {"irwin_cdr": "K", "irwvp": "K", "vschn": "1"})
        self.assertFalse(metadata["method"]["temporal_average"])
        self.assertFalse(metadata["method"]["valid_range_filter"])
        metadata["channels"].clear()
        self.assertEqual(len(processing.dataset_metadata()["channels"]), 3)

    def test_metadata_does_not_require_numerical_dependencies(self) -> None:
        with mock.patch.dict("sys.modules", {"numpy": None, "netCDF4": None}):
            self.assertEqual(processing.dataset_metadata()["dtype"], "float32")


@unittest.skipIf(np is None, "numpy is unavailable")
class LookupTests(unittest.TestCase):
    def test_searchsorted_nearest_left_ties_and_endpoints(self) -> None:
        indices, distances = processing._nearest_indices_1d(
            np.array([-2, 0, 2], dtype=np.float32),
            np.array([-3, -1, 0, 1, 1.1, 3], dtype=np.float32),
        )
        np.testing.assert_array_equal(indices, [0, 0, 1, 1, 2, 2])
        np.testing.assert_allclose(distances, [1, 1, 0, 1, 0.9, 1])
        self.assertEqual(distances.dtype, np.dtype("float32"))

    def test_euclidean_radius_and_strict_float32_boundary(self) -> None:
        radius = np.float32(0.500001)
        for lat, lon, expected in (
            (0.5, 0, True),
            (np.nextafter(radius, np.float32(0)), 0, True),
            (radius, 0, False),
            (np.nextafter(radius, np.float32(1)), 0, False),
            (0.3, 0.4, True),
            (0.4, 0.4, False),
        ):
            with self.subTest(lat=lat, lon=lon):
                _, _, valid = processing._build_lookup(
                    np.array([lat], dtype=np.float32), np.array([lon], dtype=np.float32),
                    np.array([0], dtype=np.float32), np.array([0], dtype=np.float32),
                )
                self.assertEqual(bool(valid[0, 0]), expected)

    def test_longitude_range_conversion(self) -> None:
        signed = processing._match_target_lon_to_source(
            np.array([-180, 180], dtype=np.float32),
            np.array([0, 179, 180, 359], dtype=np.float32),
        )
        np.testing.assert_array_equal(signed, [0, 179, -180, -1])
        unsigned = processing._match_target_lon_to_source(
            np.array([0, 359], dtype=np.float32),
            np.array([-180, -1, 0, 180], dtype=np.float32),
        )
        np.testing.assert_array_equal(unsigned, [180, 359, 0, 180])


@unittest.skipIf(np is None, "numpy is unavailable")
class OutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "output.npy"

    def save(self, array) -> None:
        with self.path.open("wb") as handle:
            np.save(handle, array, allow_pickle=False)

    def test_valid_output_metrics_hash_and_all_nan_channel(self) -> None:
        array = np.zeros(processing.OUTPUT_SHAPE, dtype=np.float32)
        array[1] = np.nan
        array[2, 0, 0] = np.nan
        self.save(array)
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = processing.validate_output(self.path, digest.upper())
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["shape"], [3, 181, 360])
        self.assertEqual(result["dtype"], "float32")
        self.assertEqual(result["finite_counts"], {"irwin_cdr": 65160, "irwvp": 0, "vschn": 65159})
        self.assertEqual(result["sha256"], digest)
        self.assertEqual(result["size"], self.path.stat().st_size)
        self.assertIsNone(result["time"])

    def test_wrong_shape_and_dtype(self) -> None:
        for array in (
            np.zeros((181, 360, 3), dtype=np.float32),
            np.zeros((3, 180, 360), dtype=np.float32),
            np.zeros(processing.OUTPUT_SHAPE, dtype=np.float64),
            np.zeros(processing.OUTPUT_SHAPE, dtype=np.int32),
        ):
            with self.subTest(shape=array.shape, dtype=array.dtype):
                self.save(array)
                with self.assertRaisesRegex(ValueError, "shape/dtype"):
                    processing.validate_output(self.path)

    def test_infinity_is_invalid(self) -> None:
        for value in (np.inf, -np.inf):
            with self.subTest(value=value):
                array = np.zeros(processing.OUTPUT_SHAPE, dtype=np.float32)
                array[2, 180, 359] = value
                self.save(array)
                with self.assertRaisesRegex(ValueError, "infinity"):
                    processing.validate_output(self.path)

    def test_hash_mismatch_and_invalid_expected_hash(self) -> None:
        self.save(np.zeros(processing.OUTPUT_SHAPE, dtype=np.float32))
        for digest in ("0" * 64, "not-a-digest"):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                processing.validate_output(self.path, digest)

    def test_truncated_non_npy_object_archive_and_trailing_bytes(self) -> None:
        self.save(np.zeros(processing.OUTPUT_SHAPE, dtype=np.float32))
        with self.path.open("r+b") as handle:
            handle.truncate(self.path.stat().st_size - 7)
        with self.assertRaises((ValueError, OSError)):
            processing.validate_output(self.path)
        self.path.write_bytes(b"not an array")
        with self.assertRaises(ValueError):
            processing.validate_output(self.path)
        with self.path.open("wb") as handle:
            np.save(handle, np.array([object()], dtype=object), allow_pickle=True)
        with self.assertRaises(ValueError):
            processing.validate_output(self.path)
        with self.path.open("wb") as handle:
            np.savez(handle, data=np.zeros(processing.OUTPUT_SHAPE, dtype=np.float32))
        with self.assertRaisesRegex(ValueError, "archive"):
            processing.validate_output(self.path)
        self.save(np.zeros(processing.OUTPUT_SHAPE, dtype=np.float32))
        with self.path.open("ab") as handle:
            handle.write(b"trailing bytes")
        with self.assertRaisesRegex(ValueError, "trailing"):
            processing.validate_output(self.path)


@unittest.skipIf(np is None or Dataset is None, "numpy/netCDF4 are unavailable")
class SourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / FILENAME
        self.dest = self.root / "output.npy"

    def make_source(
        self, *, lat=(0,), lon=(0,), arrays=None, dtype="i2", attrs=None,
        fill=-31999, time_value=0, time_units="hours since 2016-01-01 00:00:00",
        include_time=True, time_count=1, channel_dims=("time", "lat", "lon"),
        filename=FILENAME, units=None, coord_dtype="f8",
    ) -> Path:
        self.source = self.root / filename
        with Dataset(str(self.source), "w", format="NETCDF4") as ds:
            ds.createDimension("lat", len(lat))
            ds.createDimension("lon", len(lon))
            ds.createDimension("time", time_count)
            for name, values, coordinate_units in (
                ("lat", lat, "degrees_north"), ("lon", lon, "degrees_east")
            ):
                var = ds.createVariable(name, coord_dtype, (name,))
                var.units = coordinate_units
                var[:] = values
            if include_time:
                var = ds.createVariable("time", "f8", ("time",))
                if time_units is not None:
                    var.units = time_units
                var.calendar = "standard"
                var[:] = time_value
            for index, name in enumerate(processing.CHANNELS):
                var = ds.createVariable(
                    name, dtype, channel_dims, fill_value=fill, zlib=True, fletcher32=True
                )
                var.units = (units or {}).get(name, "1" if name == "vschn" else "Kelvin")
                for key, value in (attrs or {}).get(name, {}).items():
                    var.setncattr(key, value)
                var.set_auto_maskandscale(False)
                var[:] = arrays[index] if arrays is not None else index + 1
        return self.source

    def convert(self):
        receipt = processing.convert_file(self.source, self.dest, expected_time=STAMP)
        with self.dest.open("rb") as handle:
            array = np.load(handle, allow_pickle=False)
        return array, receipt

    def test_poles_axis_order_and_receipt(self) -> None:
        grids = [np.array([[1, 2], [3, 4], [5, 6]]) + 10 * index for index in range(3)]
        self.make_source(lat=(-90, 0, 90), lon=(0, 359), arrays=grids)
        original = self.source.read_bytes()
        array, receipt = self.convert()
        np.testing.assert_array_equal(array[:, 0, 0], [5, 15, 25])
        np.testing.assert_array_equal(array[:, 180, 359], [2, 12, 22])
        np.testing.assert_array_equal(array[:, 90, 0], [3, 13, 23])
        self.assertTrue(np.isnan(array[:, 0, 180]).all())
        self.assertEqual(array.shape, processing.OUTPUT_SHAPE)
        self.assertEqual(array.dtype, np.dtype("float32"))
        self.assertEqual(receipt["finite_counts"], dict.fromkeys(processing.CHANNELS, 6))
        self.assertEqual(receipt["time"], STAMP)
        self.assertEqual(receipt["source"]["time_source"], "internal")
        self.assertEqual(receipt["source"]["coordinates"]["lat"]["range"], [-90, 90])
        self.assertEqual(receipt["source"]["channels"]["irwin_cdr"]["units"], "Kelvin")
        self.assertEqual(receipt["source"]["channels"]["irwin_cdr"]["packing"]["_FillValue"], -31999)
        self.assertEqual(receipt["sha256"], hashlib.sha256(self.dest.read_bytes()).hexdigest())
        json.dumps(receipt, allow_nan=False)
        self.assertEqual(self.source.read_bytes(), original)

    def test_source_coordinates_cast_to_float32_before_nearest_search(self) -> None:
        self.make_source(lat=(-0.250000001, 0.25), arrays=[[[10], [20]]] * 3)
        array, _ = self.convert()
        np.testing.assert_array_equal(array[:, 90, 0], [10, 10, 10])

    def test_common_lookup_keeps_independent_masks_and_does_not_seek_valid_neighbor(self) -> None:
        self.make_source(lon=(-0.25, 0.25), arrays=[[-31999, 100], [20, 200], [30, -31999]])
        array, _ = self.convert()
        self.assertTrue(np.isnan(array[0, 90, 0]))
        self.assertEqual(array[1, 90, 0], 20)
        self.assertEqual(array[2, 90, 0], 30)

    def test_dateline_conversion_uses_negative_180_and_not_positive_duplicate(self) -> None:
        self.make_source(lon=(-180, -1, 0, 1, 179, 180), arrays=[[10, 11, 12, 13, 14, 15]] * 3)
        array, _ = self.convert()
        np.testing.assert_array_equal(array[0, 90, [0, 1, 179, 180, 359]], [12, 13, 14, 10, 11])

    def test_no_periodic_alternate_neighbor_or_polar_extrapolation(self) -> None:
        self.make_source(lon=(359.9,))
        array, _ = self.convert()
        self.assertTrue(np.isnan(array[:, 90, 0]).all())
        self.assertTrue(np.isnan(array[:, (0, 180), :]).all())

    def test_diagonal_radius_rejection(self) -> None:
        self.make_source(lat=(0.4,), lon=(0.4,))
        array, _ = self.convert()
        self.assertTrue(np.isnan(array).all())

    def test_original_integer_fill_mask_is_applied_before_float32_conversion(self) -> None:
        self.make_source(lon=(0, 1), dtype="i8", fill=16777216, arrays=[[16777216, 16777217]] * 3)
        array, _ = self.convert()
        self.assertTrue(np.isnan(array[:, 90, 0]).all())
        np.testing.assert_array_equal(array[:, 90, 1], [16777216] * 3)

    def test_float32_packing_and_no_valid_range_filtering(self) -> None:
        raw = np.array([-30000, -31999, 100, -12345], dtype=np.int16)
        packing = {
            "scale_factor": 0.010000000123, "add_offset": 200.000000123,
            "valid_range": np.array([140, 375], dtype=np.float32),
            "missing_value": np.int16(-12345),
        }
        self.make_source(lon=(0, 1, 2, 3), arrays=[raw] * 3,
                         attrs={name: packing for name in processing.CHANNELS})
        array, receipt = self.convert()
        expected = raw.astype(np.float32) * np.float32(packing["scale_factor"]) + np.float32(packing["add_offset"])
        expected[[1, 3]] = np.nan
        np.testing.assert_array_equal(array[0, 90, :4], expected)
        self.assertLess(array[0, 90, 0], 140)
        self.assertEqual(array[0, 90, 2], 201)
        self.assertEqual(receipt["source"]["channels"]["irwin_cdr"]["packing"]["valid_range"], [140, 375])

    def test_multiple_missing_values_and_unsigned_attribute_does_not_reinterpret_raw(self) -> None:
        self.make_source(
            lon=(0, 1, 2), arrays=[[-10, -11, -12]] * 3,
            attrs={name: {"missing_value": np.array([-11, -12], dtype=np.int16), "_Unsigned": "true"}
                   for name in processing.CHANNELS},
        )
        array, _ = self.convert()
        np.testing.assert_array_equal(array[:, 90, 0], [-10] * 3)
        self.assertTrue(np.isnan(array[:, 90, 1:3]).all())

    def test_all_nan_channel_and_json_safe_nan_fill(self) -> None:
        self.make_source(dtype="f4", fill=np.nan, arrays=[np.nan, 2, 3])
        source_info = processing.validate_source(self.source, STAMP)
        self.assertEqual(source_info["finite_counts"]["irwin_cdr"], 0)
        array, receipt = self.convert()
        self.assertTrue(np.isnan(array[0]).all())
        self.assertEqual(array[1, 90, 0], 2)
        self.assertEqual(receipt["source"]["channels"]["irwin_cdr"]["packing"]["_FillValue"], "NaN")
        json.dumps(source_info, allow_nan=False)
        json.dumps(receipt, allow_nan=False)

    def test_infinity_and_float32_overflow_are_rejected_even_outside_target_radius(self) -> None:
        for value in (np.inf, -np.inf, 1e40):
            with self.subTest(value=value):
                self.make_source(lat=(0.4,), lon=(0.4,), dtype="f8", arrays=[1, 2, value])
                with self.assertRaisesRegex(ValueError, "infinity"):
                    processing.validate_source(self.source, STAMP)

    def test_invalid_packing_scalars_are_rejected(self) -> None:
        for value in (np.inf, np.nan, np.array([0.01, 0.02])):
            with self.subTest(value=value):
                self.make_source(attrs={"irwin_cdr": {"scale_factor": value}})
                with self.assertRaisesRegex(ValueError, "finite scalars"):
                    processing.validate_source(self.source)

    def test_full_channel_reads_are_not_skipped_for_empty_target_coverage(self) -> None:
        self.make_source(lat=(0.4,), lon=(0.4,))
        read = processing._read_physical
        names = []

        def track(var):
            names.append(var.name)
            return read(var)

        with mock.patch.object(processing, "_read_physical", side_effect=track):
            processing.validate_source(self.source, STAMP)
        self.assertEqual(names, list(processing.CHANNELS))

        def fail_last(var):
            if var.name == "vschn":
                raise RuntimeError("NetCDF: corrupt compressed chunk")
            return read(var)

        with mock.patch.object(processing, "_read_physical", side_effect=fail_last):
            with self.assertRaisesRegex(RuntimeError, "corrupt compressed chunk"):
                processing.validate_source(self.source, STAMP)

    def test_non_netcdf_and_truncated_netcdf_are_rejected(self) -> None:
        self.source.write_bytes(b"not netcdf")
        with self.assertRaises((OSError, RuntimeError, ValueError)):
            processing.validate_source(self.source, STAMP)
        self.make_source()
        with self.source.open("r+b") as handle:
            handle.truncate(self.source.stat().st_size // 2)
        with self.assertRaises((OSError, RuntimeError, ValueError)):
            processing.validate_source(self.source, STAMP)

    def test_monotonic_finite_and_nonempty_source_coordinates(self) -> None:
        cases = (
            {"lat": (1, 0)}, {"lon": (0, 1, 0.5)}, {"lat": (0, 0)},
            {"lon": (np.nan,)}, {"lat": (np.inf,)}, {"lat": ()},
            {"lat": (91,)}, {"lon": (-181,)},
            {"lat": (1, 1.000000001)},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                self.make_source(**kwargs)
                with self.assertRaises(ValueError):
                    processing.validate_source(self.source, STAMP)

    def test_missing_channel_invalid_dimensions_and_units(self) -> None:
        self.make_source()
        with Dataset(str(self.source), "a") as ds:
            ds.renameVariable("vschn", "other")
        with self.assertRaisesRegex(ValueError, "Missing GridSat channel"):
            processing.validate_source(self.source)
        self.make_source(channel_dims=("time", "lon", "lat"))
        with self.assertRaisesRegex(ValueError, "invalid dimensions"):
            processing.validate_source(self.source)
        for name, units in (("irwin_cdr", "Celsius"), ("vschn", "%"), ("lat", "radians")):
            with self.subTest(name=name):
                self.make_source()
                with Dataset(str(self.source), "a") as ds:
                    ds.variables[name].units = units
                with self.assertRaisesRegex(ValueError, "units"):
                    processing.validate_source(self.source)

    def test_missing_units_are_rejected_and_archive_unit_aliases_allowed(self) -> None:
        for name in ("lat", "lon", *processing.CHANNELS):
            with self.subTest(name=name):
                self.make_source()
                with Dataset(str(self.source), "a") as ds:
                    ds.variables[name].delncattr("units")
                with self.assertRaisesRegex(ValueError, "units"):
                    processing.validate_source(self.source)
        for units in ("1", "unitless", "dimensionless"):
            with self.subTest(units=units):
                self.make_source(units={"irwin_cdr": "K", "irwvp": "kelvin", "vschn": units})
                self.assertEqual(processing.validate_source(self.source)["time"], STAMP)

    def test_valid_time_is_compared_with_both_filename_and_expected(self) -> None:
        self.make_source(time_value=3)
        for expected in (None, STAMP, "2016-01-01T03:00:00Z"):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "filename time"):
                processing.validate_source(self.source, expected)
        self.make_source()
        with self.assertRaisesRegex(ValueError, "expected time"):
            processing.validate_source(self.source, "2016-01-01T03:00:00Z")
        with self.assertRaisesRegex(ValueError, "UTC"):
            processing.validate_source(self.source, "2016-01-01T00:00:00")
        self.assertEqual(processing.validate_source(self.source, "2016-01-01T00:00:00+00:00")["time"], STAMP)

    def test_filename_fallback_only_for_absent_or_undecodable_time(self) -> None:
        for kwargs in ({"include_time": False}, {"time_units": None}, {"time_units": "nonsense"}):
            with self.subTest(kwargs=kwargs):
                self.make_source(**kwargs)
                result = processing.validate_source(self.source, STAMP)
                self.assertEqual(result["time"], STAMP)
                self.assertEqual(result["time_source"], "filename")
                self.assertTrue(result["time_fallback_reason"])
                with self.assertRaisesRegex(ValueError, "expected time"):
                    processing.validate_source(self.source, "2016-01-01T03:00:00Z")

    def test_partial_download_filename_supports_fallback_and_time_consistency(self) -> None:
        self.make_source(filename=FILENAME + ".part", include_time=False)
        self.assertEqual(processing.validate_source(self.source, STAMP)["time_source"], "filename")
        self.make_source(filename=FILENAME + ".part", time_value=3)
        with self.assertRaisesRegex(ValueError, "filename time"):
            processing.validate_source(self.source, "2016-01-01T03:00:00Z")

    def test_internal_time_can_identify_generic_filename_but_expected_is_not_a_fallback(self) -> None:
        self.make_source(filename="source.nc")
        self.assertEqual(processing.validate_source(self.source, STAMP)["time_source"], "internal")
        self.make_source(filename="source.nc", include_time=False)
        with self.assertRaisesRegex(ValueError, "Cannot determine source time"):
            processing.validate_source(self.source, STAMP)

    def test_multiple_timesteps_rejected_instead_of_first_selection_or_averaging(self) -> None:
        self.make_source(time_count=2, time_value=[0, 3])
        with self.assertRaisesRegex(ValueError, "exactly one timestep"):
            processing.validate_source(self.source)
        with self.assertRaisesRegex(ValueError, "exactly one timestep"):
            self.convert()
        self.assertFalse(self.dest.exists())

    def test_two_dimensional_channel_without_time_variable(self) -> None:
        self.make_source(channel_dims=("lat", "lon"), include_time=False)
        array, receipt = self.convert()
        np.testing.assert_array_equal(array[:, 90, 0], [1, 2, 3])
        self.assertEqual(receipt["time"], STAMP)

    def test_nonfinite_time_rejected_and_time_io_failure_not_swallowed(self) -> None:
        for value in (np.inf, np.nan):
            with self.subTest(value=value):
                self.make_source(time_value=value)
                with self.assertRaisesRegex(ValueError, "time coordinate must be finite"):
                    processing.validate_source(self.source)
        var = mock.MagicMock()
        var.dimensions = ("time",)
        var.size = 1
        var.__getitem__.side_effect = RuntimeError("time chunk unreadable")
        ds = mock.Mock(dimensions={"time": [0]}, variables={"time": var})
        with self.assertRaisesRegex(RuntimeError, "time chunk unreadable"):
            processing._source_time(ds, self.source, STAMP)

    def test_conversion_uses_unique_sibling_filehandle_and_verifies_before_replace(self) -> None:
        self.make_source()
        self.dest = self.root / "nested" / "output.custom"
        calls = []
        save, fsync, validate, replace = np.save, processing.os.fsync, processing.validate_output, processing.os.replace

        def record_save(handle, data, **kwargs):
            self.assertTrue(hasattr(handle, "write"))
            self.assertFalse(kwargs["allow_pickle"])
            self.assertEqual(Path(handle.name).parent, self.dest.parent)
            calls.append("save")
            return save(handle, data, **kwargs)

        def record_fsync(fd):
            calls.append("fsync")
            return fsync(fd)

        def record_validate(path, *args, **kwargs):
            calls.append("validate")
            self.assertNotEqual(path, self.dest)
            self.assertTrue(path.exists())
            return validate(path, *args, **kwargs)

        def record_replace(source, dest):
            calls.append("replace")
            self.assertEqual(source.parent, dest.parent)
            return replace(source, dest)

        with mock.patch.object(np, "save", side_effect=record_save), \
             mock.patch.object(processing.os, "fsync", side_effect=record_fsync), \
             mock.patch.object(processing, "validate_output", side_effect=record_validate), \
             mock.patch.object(processing.os, "replace", side_effect=record_replace):
            _, receipt = self.convert()
        self.assertEqual(calls, ["save", "fsync", "validate", "replace"])
        self.assertEqual(receipt["path"], str(self.dest))
        self.assertEqual(list(self.dest.parent.iterdir()), [self.dest])

    def test_atomic_failures_preserve_existing_output_source_and_unrelated_temps(self) -> None:
        self.make_source()
        original = self.source.read_bytes()
        self.dest.write_bytes(b"previous output")
        unrelated = self.root / ".output.npy.not-ours.tmp"
        unrelated.write_bytes(b"another process owns this")
        before = set(self.root.iterdir())

        def partial_save(handle, *args, **kwargs):
            handle.write(b"partial array")
            raise OSError("injected write failure")

        failures = (
            (np, "save", partial_save),
            (processing.os, "fsync", OSError("injected fsync failure")),
            (processing, "validate_output", ValueError("injected validation failure")),
            (processing.os, "replace", OSError("injected replace failure")),
        )
        for owner, name, failure in failures:
            with self.subTest(stage=name), mock.patch.object(owner, name, side_effect=failure):
                with self.assertRaises((OSError, ValueError)):
                    self.convert()
            self.assertEqual(self.dest.read_bytes(), b"previous output")
            self.assertEqual(self.source.read_bytes(), original)
            self.assertEqual(unrelated.read_bytes(), b"another process owns this")
            self.assertEqual(set(self.root.iterdir()), before)

    def test_reload_detects_invalid_saved_array_and_leaves_no_output(self) -> None:
        self.make_source()
        save = np.save

        def save_wrong_dtype(handle, data, **kwargs):
            return save(handle, data.astype(np.float64), **kwargs)

        before = set(self.root.iterdir())
        with mock.patch.object(np, "save", side_effect=save_wrong_dtype):
            with self.assertRaisesRegex(ValueError, "shape/dtype"):
                self.convert()
        self.assertEqual(set(self.root.iterdir()), before)

    def test_source_validation_failure_never_replaces_output(self) -> None:
        self.make_source(time_value=3)
        self.dest.write_bytes(b"previous output")
        before = set(self.root.iterdir())
        with self.assertRaisesRegex(ValueError, "filename time"):
            self.convert()
        self.assertEqual(self.dest.read_bytes(), b"previous output")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_source_cannot_be_replaced(self) -> None:
        self.make_source()
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "must not replace the source"):
            processing.convert_file(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
