from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from gdex_downloader.gridsat import (
    BASE_URL, RunActiveError, RunLock, atomic_json, cleanup_quarter,
    digest_file, discover_snapshot, download_one, expected_times, file_item,
    parse_args, pipeline_status, quarter_for, read_json, run_pipeline,
    safe_path, transfer_path, validate_snapshot,
)


class Response(io.BytesIO):
    def __init__(self, body=b"", headers=None, status=200):
        super().__init__(body)
        self.headers = headers or {}
        self.status = status


def snapshot_for(*names):
    items = [file_item(name) for name in names]
    coverage = {}
    for item in items:
        row = coverage.setdefault(item["quarter"], {"published": 0, "expected_slots": 0, "not_listed": []})
        row["published"] += 1
        row["expected_slots"] += 1
    return {"schema": 1, "snapshot_id": "test-snapshot", "source": BASE_URL,
            "created_at": "2026-11-11T00:00:00Z", "start_date": "2016-01-01",
            "end_date": "2026-12-31", "coverage": coverage, "latest_by_year": {}, "files": items}


NAME = "GRIDSAT-B1.2016.01.01.00.v02r01.nc"


class InventoryTests(unittest.TestCase):
    def test_leap_year_and_quarter_boundaries(self):
        stamps = list(expected_times(date(2016, 1, 1), date(2016, 3, 31)))
        self.assertEqual(len(stamps), 91 * 8)
        self.assertIn("2016-02-29T21:00:00Z", stamps)
        self.assertEqual(quarter_for(date(2016, 4, 1)), "2016Q2")
        self.assertEqual(stamps[-1], "2016-03-31T21:00:00Z")

    def test_inventory_filters_untrusted_links_and_records_missing(self):
        body = f'<a href="{NAME}">x</a><a href="https://evil.invalid/{NAME}">x</a><a href="{NAME}">duplicate</a><a href="GRIDSAT-B1.2016.01.01.01.v02r02.nc">bad version</a>'
        client = Mock()
        client.request.return_value = Response(body.encode())
        result = discover_snapshot(client, date(2016, 1, 1), date(2016, 1, 1), retries=0)
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(len(result["coverage"]["2016Q1"]["not_listed"]), 7)
        validate_snapshot(result)

    def test_empty_or_failed_index_is_not_success(self):
        client = Mock()
        client.request.return_value = Response(b"<html>Login required</html>")
        with self.assertRaisesRegex(ValueError, "No valid GridSat"):
            discover_snapshot(client, date(2016, 1, 1), date(2016, 1, 1), retries=0)

    def test_truncated_index_is_retried_not_frozen(self):
        body = f'<a href="{NAME}">file</a>'.encode()
        client = Mock()
        client.request.side_effect = [Response(body, {"Content-Length": str(len(body) * 2)}), Response(body, {"Content-Length": str(len(body))})]
        result = discover_snapshot(client, date(2016, 1, 1), date(2016, 1, 1), retries=1, retry_sleep=0)
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(client.request.call_count, 2)
        client.request.side_effect = None
        client.request.return_value = Response(body, {"Content-Length": str(len(body) * 2)})
        with self.assertRaisesRegex(ValueError, "Truncated inventory"):
            discover_snapshot(client, date(2016, 1, 1), date(2016, 1, 1), retries=0)
        client.request.side_effect = OSError("network unavailable")
        with self.assertRaises(OSError):
            discover_snapshot(client, date(2016, 1, 1), date(2016, 1, 1), retries=0)

    def test_snapshot_cannot_redirect_or_delete_arbitrary_paths(self):
        snap = snapshot_for(NAME)
        snap["files"][0]["raw"] = "../important.nc"
        with self.assertRaises(ValueError):
            validate_snapshot(snap)


class StateTests(unittest.TestCase):
    def test_atomic_state_and_kernel_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            atomic_json(root / "state.json", {"a": 1})
            atomic_json(root / "state.json", {"a": 2})
            self.assertEqual(read_json(root / "state.json"), {"a": 2})
            with RunLock(root / "lock"):
                with self.assertRaises(RunActiveError):
                    with RunLock(root / "lock"):
                        pass
            with RunLock(root / "lock"):
                pass

    def test_safe_path_and_status_do_not_create_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "not-created"
            self.assertEqual(pipeline_status(root)["stage"], "not_started")
            self.assertFalse(root.exists())
            with self.assertRaises(ValueError):
                safe_path(root, "../outside")

    def test_symlink_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            try:
                (root / "_cache").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Symlink privileges unavailable")
            with self.assertRaises(ValueError):
                safe_path(root, "_cache/file.nc")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot = snapshot_for(NAME)
        self.item = self.snapshot["files"][0]
        self.payload = b"synthetic-netcdf-payload"
        self.headers = {"Content-Length": str(len(self.payload)), "ETag": '"v1"', "Content-Type": "application/x-netcdf"}
        self.validation = patch("gdex_downloader.gridsat_processing.validate_source", return_value={})
        self.validation.start()

    def tearDown(self):
        self.validation.stop()
        self.temp.cleanup()

    def client(self):
        client = Mock()
        client.request.side_effect = lambda url, method="GET", headers=None: Response(b"" if method == "HEAD" else self.payload, self.headers)
        return client

    def mark_partial(self, content):
        part = self.root / (self.item["raw"] + ".part")
        part.parent.mkdir(parents=True)
        part.write_bytes(content)
        atomic_json(transfer_path(self.root, self.item), {"snapshot_id": self.snapshot["snapshot_id"], "url": self.item["url"], "raw": self.item["raw"], "total": len(self.payload), "validator": '"v1"', "status": "partial"})
        return part

    def test_complete_download_and_verified_cache_skip_network(self):
        client = self.client()
        download_one(self.root, self.snapshot, self.item, client, retries=0)
        self.assertEqual((self.root / self.item["raw"]).read_bytes(), self.payload)
        self.assertEqual(read_json(transfer_path(self.root, self.item))["sha256"], digest_file(self.root / self.item["raw"]))
        client.reset_mock()
        download_one(self.root, self.snapshot, self.item, client, retries=0)
        client.request.assert_not_called()

    def test_truncated_transfer_is_retained_then_resumed(self):
        client = self.client()
        client.request.side_effect = lambda url, method="GET", headers=None: Response(b"" if method == "HEAD" else self.payload[:5], self.headers)
        with self.assertRaisesRegex(ValueError, "Truncated"):
            download_one(self.root, self.snapshot, self.item, client, retries=0)
        self.assertFalse((self.root / self.item["raw"]).exists())
        self.assertEqual((self.root / (self.item["raw"] + ".part")).read_bytes(), self.payload[:5])

        def request(url, method="GET", headers=None):
            if method == "HEAD":
                return Response(headers=self.headers)
            self.assertEqual(headers["Range"], "bytes=5-")
            self.assertEqual(headers["If-Range"], '"v1"')
            return Response(self.payload[5:], {**self.headers, "Content-Length": str(len(self.payload) - 5), "Content-Range": f"bytes 5-{len(self.payload)-1}/{len(self.payload)}"}, 206)

        client.request.side_effect = request
        download_one(self.root, self.snapshot, self.item, client, retries=0)
        self.assertEqual((self.root / self.item["raw"]).read_bytes(), self.payload)

    def test_server_ignoring_range_restarts_without_appending(self):
        self.mark_partial(self.payload[:5])
        download_one(self.root, self.snapshot, self.item, self.client(), retries=0)
        self.assertEqual((self.root / self.item["raw"]).read_bytes(), self.payload)

    def test_partial_without_strong_validator_restarts(self):
        for etag in (None, 'W/"weak"'):
            with self.subTest(etag=etag), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                part = root / (self.item["raw"] + ".part")
                part.parent.mkdir(parents=True)
                part.write_bytes(b"old-version")
                record = {"snapshot_id": self.snapshot["snapshot_id"], "url": self.item["url"], "raw": self.item["raw"], "total": len(self.payload), "validator": "", "status": "partial"}
                atomic_json(transfer_path(root, self.item), record)
                response_headers = {"Content-Length": str(len(self.payload))}
                if etag:
                    response_headers["ETag"] = etag
                client = Mock()

                def request(url, method="GET", headers=None):
                    if method == "GET":
                        self.assertNotIn("Range", headers)
                    return Response(b"" if method == "HEAD" else self.payload, response_headers)

                client.request.side_effect = request
                download_one(root, self.snapshot, self.item, client, retries=0)
                self.assertEqual((root / self.item["raw"]).read_bytes(), self.payload)

    def test_invalid_content_range_never_promotes_partial(self):
        self.mark_partial(self.payload[:5])
        client = self.client()
        client.request.side_effect = [Response(headers=self.headers), Response(self.payload[5:], {**self.headers, "Content-Range": "bytes 0-1/2"}, 206)]
        with self.assertRaisesRegex(ValueError, "Content-Range"):
            download_one(self.root, self.snapshot, self.item, client, retries=0)
        self.assertFalse((self.root / self.item["raw"]).exists())

    def test_complete_partial_is_validated_without_get(self):
        self.mark_partial(self.payload)
        client = self.client()
        download_one(self.root, self.snapshot, self.item, client, retries=0)
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual((self.root / self.item["raw"]).read_bytes(), self.payload)

    def test_corrupt_nc_is_not_promoted(self):
        with patch("gdex_downloader.gridsat_processing.validate_source", side_effect=ValueError("bad NetCDF")):
            with self.assertRaisesRegex(ValueError, "bad NetCDF"):
                download_one(self.root, self.snapshot, self.item, self.client(), retries=0)
        self.assertFalse((self.root / self.item["raw"]).exists())
        self.assertTrue((self.root / (self.item["raw"] + ".part")).exists())

    def test_unmanaged_cache_is_never_adopted_or_removed(self):
        source = self.root / self.item["raw"]
        source.parent.mkdir(parents=True)
        source.write_bytes(b"user data")
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            download_one(self.root, self.snapshot, self.item, self.client(), retries=0)
        self.assertEqual(source.read_bytes(), b"user data")

    def test_failure_before_cleanup_retains_entire_quarter(self):
        download_one(self.root, self.snapshot, self.item, self.client(), retries=0)
        with patch("gdex_downloader.gridsat.output_valid", return_value=False):
            with self.assertRaisesRegex(ValueError, "Cleanup blocked"):
                cleanup_quarter(self.root, self.snapshot, [self.item])
        self.assertTrue((self.root / self.item["raw"]).exists())

    def test_success_cleanup_only_removes_owned_files(self):
        download_one(self.root, self.snapshot, self.item, self.client(), retries=0)
        foreign = (self.root / self.item["raw"]).parent / "unrelated.nc"
        foreign.write_bytes(b"keep")
        with patch("gdex_downloader.gridsat.output_valid", return_value=True):
            self.assertEqual(cleanup_quarter(self.root, self.snapshot, [self.item]), 1)
            self.assertEqual(cleanup_quarter(self.root, self.snapshot, [self.item]), 0)
        self.assertEqual(foreign.read_bytes(), b"keep")

    def test_cleanup_modified_source_fails_before_unlink(self):
        download_one(self.root, self.snapshot, self.item, self.client(), retries=0)
        (self.root / self.item["raw"]).write_bytes(b"modified")
        with patch("gdex_downloader.gridsat.output_valid", return_value=True):
            with self.assertRaisesRegex(ValueError, "modified source"):
                cleanup_quarter(self.root, self.snapshot, [self.item])
        self.assertTrue((self.root / self.item["raw"]).exists())


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "data"
        self.args = parse_args(["run", "--data-root", str(self.root), "--retries", "0"])
        self.snapshot = snapshot_for(NAME)

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_has_no_filesystem_side_effects(self):
        self.args.dry_run = True
        with patch("gdex_downloader.gridsat.discover_snapshot", return_value=self.snapshot):
            result = run_pipeline(self.args, client=Mock())
        self.assertEqual(result["files"], 1)
        self.assertFalse(self.root.exists())

    def test_all_verified_outputs_skip_redownload_after_raw_cleanup(self):
        atomic_json(self.root / "_state/snapshot.json", self.snapshot)
        client = Mock()
        scheduler = Mock()
        with patch("gdex_downloader.gridsat.output_valid", return_value=True):
            result = run_pipeline(self.args, client=client, scheduler=scheduler)
        self.assertEqual(result["stage"], "complete")
        client.request.assert_not_called()
        scheduler.submit.assert_not_called()

    def test_resume_waits_for_existing_job_before_inspecting_outputs(self):
        atomic_json(self.root / "_state/snapshot.json", self.snapshot)
        atomic_json(self.root / "_state/batches/2016Q1.json", {"stage": "processing", "job_id": "123", "job_name": "owned-job"})
        scheduler = Mock()
        with patch("gdex_downloader.gridsat.output_valid", return_value=True):
            run_pipeline(self.args, client=Mock(), scheduler=scheduler)
        scheduler.wait.assert_called_once()
        self.assertEqual(scheduler.wait.call_args.args, ("123",))
        scheduler.submit.assert_not_called()

    def test_unresolved_submission_never_submits_duplicate(self):
        atomic_json(self.root / "_state/snapshot.json", self.snapshot)
        atomic_json(self.root / "_state/batches/2016Q1.json", {"stage": "submitting", "job_name": "uncertain"})
        scheduler = Mock()
        scheduler.find_job.return_value = None
        with self.assertRaisesRegex(RuntimeError, "refusing to submit a duplicate"):
            run_pipeline(self.args, client=Mock(), scheduler=scheduler)
        scheduler.submit.assert_not_called()

    def test_unresolved_slurm_cannot_be_bypassed_with_local_executor(self):
        atomic_json(self.root / "_state/snapshot.json", self.snapshot)
        atomic_json(self.root / "_state/batches/2016Q1.json", {"stage": "processing", "job_id": "123", "job_name": "active-job"})
        self.args.executor = "local"
        with patch("gdex_downloader.gridsat.process_quarter") as process, patch("gdex_downloader.gridsat.cleanup_quarter") as cleanup:
            with self.assertRaisesRegex(RuntimeError, "unresolved Slurm"):
                run_pipeline(self.args, client=Mock())
            process.assert_not_called()
            cleanup.assert_not_called()

    def test_failed_slurm_keeps_cache_and_stops_before_cleanup(self):
        from gdex_downloader.gridsat_slurm import JobFailedError
        atomic_json(self.root / "_state/snapshot.json", self.snapshot)
        scheduler = Mock()
        scheduler.submit.return_value = "123"
        scheduler.wait.side_effect = JobFailedError("FAILED")
        with patch("gdex_downloader.gridsat.output_valid", return_value=False), patch("gdex_downloader.gridsat.download_one"), patch("gdex_downloader.gridsat.cleanup_quarter") as cleanup:
            with self.assertRaises(JobFailedError):
                run_pipeline(self.args, client=Mock(), scheduler=scheduler)
            cleanup.assert_not_called()
        batch = read_json(self.root / "_state/batches/2016Q1.json")
        self.assertEqual(batch["stage"], "processing_failed")


class EndToEndTests(unittest.TestCase):
    def test_two_quarters_convert_cleanup_then_restart_without_network(self):
        import numpy as np
        from netCDF4 import Dataset, date2num
        from datetime import datetime

        names = [NAME, "GRIDSAT-B1.2016.04.01.00.v02r01.nc"]
        snap = snapshot_for(*names)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "data"
            payloads = {}
            for item in snap["files"]:
                source = base / item["name"]
                with Dataset(source, "w") as ds:
                    ds.createDimension("time", 1)
                    ds.createDimension("lat", 3)
                    ds.createDimension("lon", 3)
                    for name in ("lat", "lon"):
                        var = ds.createVariable(name, "f4", (name,))
                        var.units = "degrees_north" if name == "lat" else "degrees_east"
                        var[:] = [-0.07, 0, 0.07]
                    clock = ds.createVariable("time", "f8", ("time",))
                    clock.units = "hours since 1970-01-01 00:00:00"
                    clock[:] = [date2num(datetime.fromisoformat(item["time"].replace("Z", "+00:00")), clock.units)]
                    for name in ("irwin_cdr", "irwvp", "vschn"):
                        var = ds.createVariable(name, "i2", ("time", "lat", "lon"), fill_value=-31999)
                        var.units = "1" if name == "vschn" else "Kelvin"
                        var.scale_factor = np.float32(0.00004 if name == "vschn" else 0.01)
                        var.add_offset = np.float32(1 if name == "vschn" else 200)
                        var.set_auto_maskandscale(False)
                        var[:] = np.full((1, 3, 3), 10000, dtype=np.int16)
                payloads[item["url"]] = source.read_bytes()
            client = Mock()

            def request(url, method="GET", headers=None):
                if "2016.04.01" in url:
                    self.assertFalse((root / "_cache/2016Q1").exists(), "Q1 must be cleaned before Q2 download")
                payload = payloads[url]
                return Response(b"" if method == "HEAD" else payload, {"Content-Length": str(len(payload)), "ETag": '"fixture"', "Content-Type": "application/x-netcdf"})

            client.request.side_effect = request
            args = parse_args(["run", "--data-root", str(root), "--executor", "local", "--workers", "1", "--retries", "0"])
            with patch("gdex_downloader.gridsat.discover_snapshot", return_value=snap):
                result = run_pipeline(args, client=client)
            self.assertEqual(result["stage"], "complete")
            self.assertEqual(result["completed_quarters"], 2)
            for item in snap["files"]:
                output = np.load(root / item["npy"], allow_pickle=False)
                self.assertEqual(output.shape, (3, 181, 360))
                np.testing.assert_allclose(output[:, 90, 0], [300, 300, 1.4])
                self.assertFalse((root / item["raw"]).exists())
            self.assertEqual(pipeline_status(root, verify=True)["verified_npy"], 2)
            client.reset_mock()
            result = run_pipeline(args, client=client)
            self.assertEqual(result["stage"], "complete")
            client.request.assert_not_called()
            # A corrupted output invalidates its receipt and downloads only that file.
            (root / snap["files"][1]["npy"]).write_bytes(b"corrupt npy")
            self.assertEqual(pipeline_status(root, verify=True)["verified_npy"], 1)
            result = run_pipeline(args, client=client)
            self.assertEqual(result["stage"], "complete")
            self.assertEqual(client.request.call_count, 2)
            self.assertEqual(pipeline_status(root, verify=True)["verified_npy"], 2)


if __name__ == "__main__":
    unittest.main()
