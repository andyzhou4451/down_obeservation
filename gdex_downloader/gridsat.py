"""One-shot, resumable GridSat inventory -> quarterly cache -> verified NPY."""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import logging
import math
import multiprocessing
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .cli import DEFAULT_USER_AGENT, HrefParser, URLClient

BASE_URL = "https://www.ncei.noaa.gov/data/geostationary-ir-channel-brightness-temperature-gridsat-b1/access/"
HOURS = tuple(range(0, 24, 3))
FILE_RE = re.compile(r"GRIDSAT-B1\.(\d{4})\.(\d{2})\.(\d{2})\.(\d{2})\.v02r01\.nc")
QUARTER_RE = re.compile(r"\d{4}Q[1-4]")
CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
LOG = logging.getLogger("gridsat")
DEFAULT_LAUNCHER = "/fs6/home/yangjh18/gdas_healda_20260916/runtime/science-python.sh"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    """Reject traversal and symlinks before touching managed data or state."""
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Unsafe managed path: {relative}")
    result = root
    for component in rel.parts:
        result = result / component
        if result.is_symlink():
            raise ValueError(f"Managed path is a symlink: {result}")
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Managed path escapes data root: {result}")
    return result


class RunActiveError(RuntimeError):
    pass


class RunLock:
    """Kernel-owned locks release on exit; never break a lock based on age."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b" ")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RunActiveError(f"Another GridSat process holds {self.path}") from exc
        return self

    def __exit__(self, *args):
        if self.handle is not None:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
            self.handle = None


def quarter_for(day: date) -> str:
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"


def expected_times(start: date, end: date):
    day = start
    while day <= end:
        for hour in HOURS:
            yield f"{day.isoformat()}T{hour:02d}:00:00Z"
        day += timedelta(days=1)


def file_item(name: str) -> dict:
    match = FILE_RE.fullmatch(name)
    if match is None:
        raise ValueError(f"Unexpected GridSat filename: {name}")
    year, month, day, hour = map(int, match.groups())
    stamp = datetime(year, month, day, hour, tzinfo=timezone.utc)
    if hour not in HOURS:
        raise ValueError(f"Unexpected GridSat hour: {hour}")
    quarter = quarter_for(stamp.date())
    return {
        "name": name,
        "url": f"{BASE_URL}{year}/{name}",
        "time": stamp.strftime("%Y-%m-%dT%H:00:00Z"),
        "quarter": quarter,
        "raw": f"_cache/{quarter}/{name}",
        "npy": f"npy/{year}/{stamp:%Y-%m-%d}/gridsat_{stamp:%Y%m%d}_t{hour:02d}.npy",
    }


def read_index(client, url: str, *, retries: int, retry_sleep: float) -> bytes:
    limit = 8 * 1024 * 1024
    for attempt in range(retries + 1):
        try:
            with client.request(url) as response:
                expected = response.headers.get("Content-Length")
                if expected is not None and not 0 < int(expected) <= limit:
                    raise ValueError(f"Invalid inventory Content-Length: {url}")
                body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError(f"Inventory exceeds size limit: {url}")
            if expected is not None and len(body) != int(expected):
                raise ValueError(f"Truncated inventory: {len(body)}/{expected} bytes: {url}")
            return body
        except Exception:
            if attempt == retries:
                raise
            LOG.warning("Request retry %d/%d: %s", attempt + 1, retries, url)
            time.sleep(retry_sleep * (attempt + 1))
    raise AssertionError("unreachable")


def discover_snapshot(client, start: date, end: date, *, retries=3, retry_sleep=5.0) -> dict:
    if start > end:
        raise ValueError("Start date must not follow end date")
    files = {}
    latest_by_year = {}
    for year in range(start.year, end.year + 1):
        url = f"{BASE_URL}{year}/"
        LOG.info("Reading official inventory: %s", url)
        body = read_index(client, url, retries=retries, retry_sleep=retry_sleep)
        parser = HrefParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        year_items = {}
        for href in parser.links:
            parsed = urllib.parse.urlsplit(urllib.parse.urljoin(url, href))
            if parsed.query or parsed.fragment:
                continue
            name = parsed.path.rsplit("/", 1)[-1]
            if not FILE_RE.fullmatch(name):
                continue
            item = file_item(name)
            if item["url"] != urllib.parse.urlunsplit(parsed) or not name.startswith(f"GRIDSAT-B1.{year}."):
                continue
            year_items[name] = item
        if not year_items:
            raise ValueError(f"No valid GridSat links in inventory; not treating this as an empty year: {url}")
        latest_by_year[str(year)] = max(item["time"] for item in year_items.values())
        for name, item in year_items.items():
            if start <= date.fromisoformat(item["time"][:10]) <= end:
                files[name] = item
    if not files:
        raise ValueError("No published files in the requested date range")
    ordered = sorted(files.values(), key=lambda item: item["time"])
    available = {item["time"] for item in ordered}
    coverage = {}
    for stamp in expected_times(start, end):
        quarter = quarter_for(date.fromisoformat(stamp[:10]))
        row = coverage.setdefault(quarter, {"expected_slots": 0, "published": 0, "not_listed": []})
        row["expected_slots"] += 1
        if stamp in available:
            row["published"] += 1
        else:
            row["not_listed"].append(stamp)
    return {
        "schema": 1, "snapshot_id": uuid.uuid4().hex, "created_at": utcnow(),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "source": BASE_URL, "latest_by_year": latest_by_year,
        "coverage": coverage, "files": ordered,
    }


def validate_snapshot(snapshot: dict) -> None:
    if snapshot.get("schema") != 1 or snapshot.get("source") != BASE_URL:
        raise ValueError("Unsupported or untrusted snapshot")
    seen = set()
    for item in snapshot["files"]:
        if item != file_item(item["name"]) or item["name"] in seen:
            raise ValueError("Snapshot contains a duplicate or altered file entry")
        seen.add(item["name"])
    if not seen:
        raise ValueError("Snapshot contains no files")


def receipt_path(root: Path, item: dict) -> Path:
    return safe_path(root, f"_state/receipts/{item['time'][:4]}/{item['name']}.json")


def transfer_path(root: Path, item: dict) -> Path:
    return safe_path(root, f"_state/downloads/{item['name']}.json")


def output_valid(root: Path, item: dict, *, verify=True) -> bool:
    from .gridsat_processing import CONTRACT_VERSION, validate_output
    try:
        receipt = read_json(receipt_path(root, item))
        if not receipt or receipt.get("contract_version") != CONTRACT_VERSION:
            return False
        if receipt.get("source_url") != item["url"] or receipt.get("time") != item["time"]:
            return False
        dest = safe_path(root, item["npy"])
        if not dest.is_file() or dest.stat().st_size != receipt["size"]:
            return False
        if verify:
            validate_output(dest, expected_sha256=receipt["sha256"])
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _check_owner(record: dict | None, snapshot: dict, item: dict) -> None:
    if not record or record.get("snapshot_id") != snapshot["snapshot_id"] or record.get("url") != item["url"] or record.get("raw") != item["raw"]:
        raise ValueError(f"Refusing to alter an unmanaged cache file: {item['raw']}")


def download_one(root: Path, snapshot: dict, item: dict, client, *, retries=3, retry_sleep=5.0, progress=None) -> None:
    from .gridsat_processing import validate_source
    dest = safe_path(root, item["raw"])
    part = safe_path(root, item["raw"] + ".part")
    record_path = transfer_path(root, item)
    record = read_json(record_path)
    if dest.exists() or part.exists():
        _check_owner(record, snapshot, item)
    if dest.exists():
        size = dest.stat().st_size
        if record.get("size") is not None and size != record["size"]:
            raise ValueError(f"Cached source size changed; retained for inspection: {dest}")
        sha256 = digest_file(dest)
        if record.get("sha256") and record["sha256"] != sha256:
            raise ValueError(f"Cached source checksum changed; retained for inspection: {dest}")
        validate_source(dest, expected_time=item["time"])
        record.update(status="downloaded", size=size, sha256=sha256, updated_at=utcnow())
        atomic_json(record_path, record)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if record is None:
        record = {"snapshot_id": snapshot["snapshot_id"], "url": item["url"], "raw": item["raw"], "status": "partial"}
        atomic_json(record_path, record)
    else:
        _check_owner(record, snapshot, item)
    for attempt in range(retries + 1):
        try:
            with client.request(item["url"], method="HEAD") as response:
                total = int(response.headers.get("Content-Length", "0"))
                etag = response.headers.get("ETag", "")
                validator = etag if etag and not etag.startswith("W/") else response.headers.get("Last-Modified", "")
            if total <= 0:
                raise ValueError("NOAA did not supply a positive Content-Length")
            offset = part.stat().st_size if part.exists() else 0
            if offset and (not validator or record.get("total") != total or record.get("validator") != validator or offset > total):
                part.unlink()
                offset = 0
            record.update(total=total, validator=validator, updated_at=utcnow())
            atomic_json(record_path, record)
            if shutil.disk_usage(dest.parent).free < total - offset + 256 * 1024 * 1024:
                raise OSError("Insufficient free space for next GridSat file plus safety margin")
            if offset < total:
                headers = {"Accept-Encoding": "identity"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                    if validator:
                        headers["If-Range"] = validator
                with client.request(item["url"], headers=headers) as response:
                    status = getattr(response, "status", 200)
                    if "text/html" in response.headers.get("Content-Type", "").lower():
                        raise ValueError("Download returned HTML instead of NetCDF")
                    if status == 206:
                        match = CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                        if match is None or tuple(map(int, match.groups())) != (offset, total - 1, total):
                            raise ValueError("Invalid or mismatched Content-Range")
                        mode = "ab" if offset else "wb"
                    elif status == 200:
                        offset = 0
                        mode = "wb"
                    else:
                        raise ValueError(f"Unexpected download HTTP status {status}")
                    length = int(response.headers.get("Content-Length", "0"))
                    if length != total - offset:
                        raise ValueError("GET and HEAD content lengths disagree")
                    written = offset
                    last_progress = 0.0
                    with part.open(mode) as handle:
                        while True:
                            block = response.read(1024 * 1024)
                            if not block:
                                break
                            handle.write(block)
                            written += len(block)
                            if written > total:
                                raise ValueError("Download exceeds expected length")
                            if progress and time.monotonic() - last_progress >= 5:
                                progress(written, total)
                                last_progress = time.monotonic()
                        handle.flush()
                        os.fsync(handle.fileno())
                    if written != total:
                        raise ValueError(f"Truncated transfer: {written}/{total} bytes")
            if part.stat().st_size != total:
                raise ValueError("Partial file has incorrect length")
            try:
                validate_source(part, expected_time=item["time"])
            except Exception:
                # A complete but invalid transfer cannot be repaired by Range.
                record["validator"] = None
                atomic_json(record_path, record)
                raise
            sha256 = digest_file(part)
            os.replace(part, dest)
            record.update(status="downloaded", size=total, sha256=sha256, updated_at=utcnow())
            atomic_json(record_path, record)
            if progress:
                progress(total, total)
            LOG.info("Downloaded and validated %s (%d bytes)", item["name"], total)
            return
        except Exception as exc:
            if attempt >= retries:
                raise
            LOG.warning("Download attempt %d failed for %s: %s", attempt + 1, item["name"], exc)
            time.sleep(retry_sleep * (attempt + 1))


def _convert_task(source: str, dest: str, stamp: str) -> dict:
    from .gridsat_processing import convert_file
    return convert_file(Path(source), Path(dest), expected_time=stamp)


def process_quarter(root: Path, quarter: str, workers=2) -> None:
    from .gridsat_processing import CONTRACT_VERSION
    if not QUARTER_RE.fullmatch(quarter):
        raise ValueError("Invalid quarter")
    snapshot = read_json(safe_path(root, "_state/snapshot.json"))
    validate_snapshot(snapshot)
    items = [item for item in snapshot["files"] if item["quarter"] == quarter]
    if not items:
        raise ValueError(f"Quarter {quarter} has no published files in this snapshot")
    state_path = safe_path(root, f"_state/process/{quarter}.json")
    state = {"quarter": quarter, "pid": os.getpid(), "host": socket.gethostname(), "started_at": utcnow(), "total": len(items), "processed": 0, "skipped": 0, "errors": [], "stage": "processing"}
    with RunLock(safe_path(root, f"_state/process-{quarter}.lock")):
        atomic_json(state_path, state)
        pending = []
        try:
            for item in items:
                if output_valid(root, item):
                    state["skipped"] += 1
                    continue
                record = read_json(transfer_path(root, item))
                _check_owner(record, snapshot, item)
                source = safe_path(root, item["raw"])
                if record.get("status") != "downloaded" or not source.is_file() or digest_file(source) != record.get("sha256"):
                    raise ValueError(f"Source not verified or changed: {source}")
                pending.append(item)
        except Exception as exc:
            state.update(stage="failed", error=str(exc), finished_at=utcnow())
            atomic_json(state_path, state)
            raise
        atomic_json(state_path, state)

        def save_result(item, result):
            result.update(contract_version=CONTRACT_VERSION, source_url=item["url"], time=item["time"], snapshot_id=snapshot["snapshot_id"], verified_at=utcnow())
            atomic_json(receipt_path(root, item), result)
            if not output_valid(root, item):
                raise ValueError(f"Output receipt verification failed: {item['npy']}")
            state["processed"] += 1
            state["last_file"] = item["name"]
            atomic_json(state_path, state)
            LOG.info("Processed %s (%d/%d)", item["name"], state["processed"] + state["skipped"], len(items))

        try:
            if workers == 1:
                for item in pending:
                    result = _convert_task(str(safe_path(root, item["raw"])), str(safe_path(root, item["npy"])), item["time"])
                    save_result(item, result)
            else:
                with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as pool:
                    futures = {pool.submit(_convert_task, str(safe_path(root, item["raw"])), str(safe_path(root, item["npy"])), item["time"]): item for item in pending}
                    for future in concurrent.futures.as_completed(futures):
                        item = futures[future]
                        try:
                            save_result(item, future.result())
                        except Exception as exc:
                            state["errors"].append({"name": item["name"], "error": str(exc)})
                            atomic_json(state_path, state)
            if state["errors"]:
                raise RuntimeError(f"{len(state['errors'])} conversion(s) failed; raw cache retained")
            state.update(stage="complete", finished_at=utcnow())
        except Exception as exc:
            state.update(stage="failed", error=str(exc), finished_at=utcnow())
            raise
        finally:
            atomic_json(state_path, state)


def cleanup_quarter(root: Path, snapshot: dict, items: list[dict]) -> int:
    # Verify the WHOLE batch before unlinking even one owned raw file.
    for item in items:
        if not output_valid(root, item):
            raise ValueError(f"Cleanup blocked by unverified output: {item['npy']}")
    owned = []
    for item in items:
        source = safe_path(root, item["raw"])
        part = safe_path(root, item["raw"] + ".part")
        if source.exists() or part.exists():
            record = read_json(transfer_path(root, item))
            _check_owner(record, snapshot, item)
            if source.exists():
                if source.stat().st_size != record.get("size") or digest_file(source) != record.get("sha256"):
                    raise ValueError(f"Cleanup blocked by modified source: {source}")
                owned.append(source)
            if part.exists():
                owned.append(part)
    for path in owned:
        path.unlink()
    for quarter in {item["quarter"] for item in items}:
        cache = safe_path(root, f"_cache/{quarter}")
        if cache.exists():
            with contextlib.suppress(OSError):
                cache.rmdir()
    return len(owned)


def make_client(timeout: float):
    return URLClient(timeout=timeout, retries=0, retry_sleep=0, headers=[], use_netrc=False, user_agent=DEFAULT_USER_AGENT, insecure_tls=False)


def run_pipeline(args, *, client=None, scheduler=None) -> dict:
    from .gridsat_processing import CONTRACT_VERSION, dataset_metadata
    from .gridsat_slurm import JobFailedError, SlurmScheduler
    root = args.data_root.expanduser().resolve()
    start = date.fromisoformat(args.start_date) if args.start_date else date(args.start_year, 1, 1)
    end = date.fromisoformat(args.end_date) if args.end_date else date(args.end_year, 12, 31)
    if start > end:
        raise ValueError("Start date follows end date")
    client = client or make_client(args.timeout)
    snapshot_path = safe_path(root, "_state/snapshot.json")
    existing = read_json(snapshot_path)
    if args.dry_run:
        snapshot = existing or discover_snapshot(client, start, end, retries=args.retries, retry_sleep=args.retry_sleep)
        validate_snapshot(snapshot)
        if snapshot["start_date"] != start.isoformat() or snapshot["end_date"] != end.isoformat():
            raise ValueError("Existing snapshot date range differs; use its original range or another data root")
        return {"dry_run": True, "files": len(snapshot["files"]), "quarters": {q: {"published": row["published"], "expected_slots": row["expected_slots"], "not_listed": len(row["not_listed"])} for q, row in snapshot["coverage"].items()}, "latest_by_year": snapshot["latest_by_year"]}
    root.mkdir(parents=True, exist_ok=True)
    with RunLock(safe_path(root, "_state/controller.lock")):
        snapshot = read_json(snapshot_path)
        if snapshot is None:
            snapshot = discover_snapshot(client, start, end, retries=args.retries, retry_sleep=args.retry_sleep)
            atomic_json(snapshot_path, snapshot)
        validate_snapshot(snapshot)
        if snapshot["start_date"] != start.isoformat() or snapshot["end_date"] != end.isoformat():
            raise ValueError("Existing snapshot date range differs; resume with original dates or use another data root")
        metadata = dataset_metadata()
        metadata["contract_version"] = CONTRACT_VERSION
        atomic_json(safe_path(root, "dataset.json"), metadata)
        state = {"snapshot_id": snapshot["snapshot_id"], "pid": os.getpid(), "host": socket.gethostname(), "started_at": utcnow(), "stage": "starting", "total": len(snapshot["files"]), "completed_quarters": 0}

        def report(**changes):
            state.update(changes, updated_at=utcnow())
            atomic_json(safe_path(root, "_state/controller.json"), state)

        report()
        if args.executor == "slurm" and scheduler is None:
            scheduler = SlurmScheduler(app_dir=Path(__file__).resolve().parents[1], data_root=root, python_launcher=args.python_launcher, poll_seconds=args.poll_seconds, workers=args.workers)
        try:
            quarters = sorted({item["quarter"] for item in snapshot["files"]})
            for quarter in quarters:
                items = [item for item in snapshot["files"] if item["quarter"] == quarter]
                batch_path = safe_path(root, f"_state/batches/{quarter}.json")
                batch = read_json(batch_path, {"quarter": quarter, "stage": "pending"})
                report(quarter=quarter, batch_total=len(items), current_file=None, job_id=batch.get("job_id"), stage="checking")
                LOG.info("Quarter %s: %d published files", quarter, len(items))
                # An orphaned worker must finish before cache validation/cleanup.
                if batch["stage"] in {"submitting", "processing"} and args.executor != "slurm":
                    raise RuntimeError("An unresolved Slurm submission exists; resume with --executor slurm before switching executors")
                if batch["stage"] in {"submitting", "processing"} and args.executor == "slurm":
                    job_id = batch.get("job_id") or scheduler.find_job(batch["job_name"])
                    if not job_id:
                        raise RuntimeError(f"Unresolved submission {batch['job_name']}; refusing to submit a duplicate. Check squeue/sacct and the batch record.")
                    batch.update(job_id=job_id, stage="processing")
                    atomic_json(batch_path, batch)
                    report(stage="processing", job_id=job_id)
                    try:
                        scheduler.wait(job_id, heartbeat=lambda: report())
                    except JobFailedError:
                        batch["stage"] = "processing_failed"
                        atomic_json(batch_path, batch)
                        raise
                    batch["stage"] = "processed"
                    atomic_json(batch_path, batch)
                pending = [item for item in items if not output_valid(root, item)]
                report(stage="downloading", batch_verified=len(items) - len(pending), batch_pending=len(pending))
                for number, item in enumerate(pending, 1):
                    report(current_file=item["name"], download_index=number, current_bytes=0, total_bytes=None)
                    download_one(root, snapshot, item, client, retries=args.retries, retry_sleep=args.retry_sleep, progress=lambda done, total: report(current_bytes=done, total_bytes=total))
                if pending:
                    report(stage="processing", current_file=None, current_bytes=None, total_bytes=None)
                    if args.executor == "local":
                        process_quarter(root, quarter, workers=args.workers)
                    else:
                        tag = hashlib.sha256(str(root).encode()).hexdigest()[:8]
                        batch.update(stage="submitting", job_name=f"gridsat_{tag}_{quarter}_{uuid.uuid4().hex[:8]}", job_id=None, submitted_at=utcnow())
                        atomic_json(batch_path, batch)
                        job_id = scheduler.submit(quarter, batch["job_name"])
                        batch.update(stage="processing", job_id=job_id)
                        atomic_json(batch_path, batch)
                        report(job_id=job_id)
                        try:
                            scheduler.wait(job_id, heartbeat=lambda: report())
                        except JobFailedError:
                            batch["stage"] = "processing_failed"
                            atomic_json(batch_path, batch)
                            raise
                report(stage="validating", current_file=None)
                removed = cleanup_quarter(root, snapshot, items)
                batch.update(stage="complete", published=len(items), raw_files_removed=removed, completed_at=utcnow())
                atomic_json(batch_path, batch)
                report(completed_quarters=state["completed_quarters"] + 1, stage="quarter_complete", batch_verified=len(items))
                LOG.info("Quarter %s verified: %d NPY files, removed %d managed cache files", quarter, len(items), removed)
            not_listed = sum(len(row["not_listed"]) for row in snapshot["coverage"].values())
            report(stage="complete", job_id=None, finished_at=utcnow(), verified=len(snapshot["files"]), not_listed=not_listed)
            LOG.info("Published snapshot complete: %d NPY files; %d requested slots not listed by source (not claimed complete)", len(snapshot["files"]), not_listed)
        except BaseException as exc:
            report(stage="failed", error=str(exc), failed_at=utcnow())
            LOG.exception("Quarter pipeline stopped; unverified raw data retained")
            raise
        return state


def pipeline_status(root: Path, *, verify=False) -> dict:
    root = root.expanduser().resolve()
    snapshot = read_json(safe_path(root, "_state/snapshot.json"))
    if snapshot is None:
        return {"data_root": str(root), "stage": "not_started"}
    validate_snapshot(snapshot)
    rows = []
    for quarter, coverage in sorted(snapshot["coverage"].items()):
        items = [item for item in snapshot["files"] if item["quarter"] == quarter]
        batch = read_json(safe_path(root, f"_state/batches/{quarter}.json"), {})
        rows.append({"quarter": quarter, "stage": batch.get("stage", "not_published" if not items else "pending"), "published": len(items), "verified_npy": sum(output_valid(root, item, verify=verify) for item in items), "cached_nc": sum(safe_path(root, item["raw"]).is_file() for item in items), "not_listed": len(coverage["not_listed"]), "job_id": batch.get("job_id")})
    current = read_json(safe_path(root, "_state/controller.json"), {})
    worker = read_json(safe_path(root, f"_state/process/{current['quarter']}.json"), {}) if current.get("quarter") else {}
    return {"data_root": str(root), "snapshot_id": snapshot["snapshot_id"], "snapshot_created_at": snapshot["created_at"], "latest_by_year": snapshot["latest_by_year"], "controller": current, "worker": worker, "quarters": rows, "checksums_rechecked": verify, "verified_npy": sum(row["verified_npy"] for row in rows), "published": len(snapshot["files"])}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    default_root = Path(__file__).resolve().parents[2] / "data" / "gridsat-b1"
    run = sub.add_parser("run", help="Run or resume this immutable published snapshot once")
    run.add_argument("--data-root", type=Path, default=default_root)
    run.add_argument("--start-year", type=int, default=2016)
    run.add_argument("--end-year", type=int, default=2026)
    run.add_argument("--start-date", help="Inclusive ISO date, for isolated validation runs")
    run.add_argument("--end-date", help="Inclusive ISO date, for isolated validation runs")
    run.add_argument("--dry-run", action="store_true", help="Read inventory without writing state or downloading files")
    run.add_argument("--executor", choices=("slurm", "local"), default="slurm")
    run.add_argument("--workers", type=int, default=2)
    run.add_argument("--python-launcher", default=DEFAULT_LAUNCHER)
    run.add_argument("--poll-seconds", type=float, default=30.0)
    run.add_argument("--timeout", type=float, default=120.0)
    run.add_argument("--retries", type=int, default=3)
    run.add_argument("--retry-sleep", type=float, default=5.0)
    process = sub.add_parser("process", help="Internal compute-node quarterly worker")
    process.add_argument("--data-root", type=Path, required=True)
    process.add_argument("--quarter", required=True)
    process.add_argument("--workers", type=int, default=2)
    status = sub.add_parser("status", help="Report recorded progress without starting work")
    status.add_argument("--data-root", type=Path, default=default_root)
    status.add_argument("--verify", action="store_true", help="Also rehash and validate all recorded outputs")
    args = parser.parse_args(argv)
    if getattr(args, "workers", 1) < 1 or getattr(args, "workers", 1) > 2:
        parser.error("--workers must be 1 or 2 for the configured 2-CPU worker")
    if getattr(args, "retries", 0) < 0 or getattr(args, "retry_sleep", 0) < 0 or getattr(args, "timeout", 1) <= 0 or getattr(args, "poll_seconds", 1) <= 0:
        parser.error("Retry counts/delays must be nonnegative and timeouts positive")
    if any(not math.isfinite(getattr(args, key, 1.0)) for key in ("retry_sleep", "timeout", "poll_seconds")):
        parser.error("Timeouts and delays must be finite")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "status":
            result = pipeline_status(args.data_root, verify=args.verify)
        elif args.command == "process":
            process_quarter(args.data_root.expanduser().resolve(), args.quarter, args.workers)
            result = {"quarter": args.quarter, "stage": "processed"}
        else:
            result = run_pipeline(args)
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        return 0
    except RunActiveError as exc:
        LOG.error("%s", exc)
        return 3
    except (Exception, KeyboardInterrupt) as exc:
        LOG.error("%s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
