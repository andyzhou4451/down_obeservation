#!/usr/bin/env python3
"""Download NOAA IBTrACS and preserve all columns in UTC calendar-year subsets."""

import argparse
import csv
import getpass
import hashlib
import json
import logging
import os
import tempfile
import time
import urllib.request
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path


VERSION = "v04r01"
SOURCE_NAME = "ibtracs.since1980.list.v04r01.csv"
SOURCE_URL = (
    "https://www.ncei.noaa.gov/data/"
    "international-best-track-archive-for-climate-stewardship-ibtracs/"
    "v04r01/access/csv/" + SOURCE_NAME
)
LOG = logging.getLogger("ibtracs")


def current_user():
    if os.name == "posix":
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    return getpass.getuser()


@contextmanager
def run_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".download.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b" ")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise OSError("Another IBTrACS run is active in " + str(root)) from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def download_source(root, retries=3):
    """Freeze one complete HTTPS response; never append across source revisions."""
    source_dir = root / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    target = source_dir / SOURCE_NAME
    receipt = source_dir / "source.json"
    if target.exists() and receipt.exists():
        record = json.loads(receipt.read_text(encoding="utf-8"))
        if record["sha256"] != sha256(target):
            raise ValueError("Cached official source checksum changed; use a new data root")
        return target, record
    if target.exists() or receipt.exists():
        raise ValueError("Incomplete source receipt; use --source to validate an existing CSV")
    for attempt in range(retries + 1):
        try:
            with tempfile.TemporaryDirectory(prefix=".download-", dir=source_dir) as tmp:
                stage = Path(tmp)
                request = urllib.request.Request(
                    SOURCE_URL,
                    headers={"User-Agent": "down-obeservation/ibtracs", "Accept-Encoding": "identity"},
                )
                with urllib.request.urlopen(request, timeout=120) as response:
                    if response.status != 200:
                        raise ValueError("Expected a complete HTTP 200 response")
                    expected = int(response.headers.get("Content-Length", "0"))
                    if expected <= 0:
                        raise ValueError("Missing source Content-Length; cannot check transfer completeness")
                    record = {"url": SOURCE_URL, "retrieved_at": datetime.now(timezone.utc).isoformat(),
                              "last_modified": response.headers.get("Last-Modified"),
                              "etag": response.headers.get("ETag")}
                    with (stage / SOURCE_NAME).open("xb") as handle:
                        received = 0
                        while True:
                            block = response.read(1024 * 1024)
                            if not block:
                                break
                            handle.write(block)
                            received += len(block)
                            if received % (16 * 1024 * 1024) == 0 or received == expected:
                                LOG.info("Source download: %d/%d bytes (%.1f%%)",
                                         received, expected, 100 * received / expected)
                        handle.flush()
                        os.fsync(handle.fileno())
                if (stage / SOURCE_NAME).stat().st_size != expected:
                    raise ValueError("Truncated source download")
                with (stage / SOURCE_NAME).open(encoding="utf-8-sig", newline="") as handle:
                    read_header(csv.reader(handle, strict=True))
                record.update(bytes=expected, sha256=sha256(stage / SOURCE_NAME))
                write_json(stage / "source.json", record)
                os.replace(stage / SOURCE_NAME, target)
                os.replace(stage / "source.json", receipt)
            return target, record
        except (OSError, ValueError) as exc:
            if attempt == retries:
                raise
            LOG.warning("Download attempt %d failed: %s", attempt + 1, exc)
            time.sleep(5 * (attempt + 1))


def read_header(reader):
    header = next(reader, None)
    units = next(reader, None)
    required = {"SID", "SEASON", "ISO_TIME", "BASIN", "LAT", "LON", "TRACK_TYPE"}
    if not header or not required.issubset(header) or len(header) != len(set(header)):
        raise ValueError("Not an IBTrACS CSV: required unique columns are missing")
    if units is None or len(units) != len(header):
        raise ValueError("Missing or malformed IBTrACS units row")
    if units[header.index("SID")].strip():
        raise ValueError("Expected a units row, not the first observation")
    return header, units


def verify_existing(output, source_hash, start_year, end_year):
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if (manifest["source"]["sha256"] != source_hash
            or manifest["start_year"] != start_year or manifest["end_year"] != end_year):
        raise ValueError("Existing output belongs to a different source or date range")
    expected = {"ibtracs_global_%d_%d.csv" % (start_year, end_year)}
    expected.update("ibtracs_global_%d.csv" % year for year in range(start_year, end_year + 1))
    if set(manifest["files"]) != expected:
        raise ValueError("Existing output manifest is incomplete")
    for name, record in manifest["files"].items():
        path = output / name
        if path.is_symlink() or sha256(path) != record["sha256"]:
            raise ValueError("Existing output failed checksum verification: " + name)
    return manifest


def subset_source(source, root, start_year=2016, end_year=2023, provenance=None):
    if not 1980 <= start_year <= end_year <= 9998:
        raise ValueError("Expected 1980 <= start year <= end year <= 9998")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    output = root / ("%d-%d" % (start_year, end_year))
    source_hash = sha256(source)
    if provenance and provenance.get("sha256") != source_hash:
        raise ValueError("Source no longer matches its download receipt")
    if output.is_symlink():
        raise ValueError("Output directory must not be a symlink")
    if output.exists():
        return verify_existing(output, source_hash, start_year, end_year)
    records = dict(provenance or {"url": SOURCE_URL, "acquisition": "operator-supplied CSV"})
    records.update(sha256=source_hash, bytes=source.stat().st_size)
    manifest = {"version": VERSION, "start_year": start_year, "end_year": end_year,
                "selection": "start_year-01-01 <= ISO_TIME < (end_year+1)-01-01, UTC; not SEASON",
                "source": records, "created_at": datetime.now(timezone.utc).isoformat(),
                "notes": ["All source columns and the units row are preserved.",
                          "No wind conversion, agency averaging, intensity threshold or time interpolation.",
                          "All TRACK_TYPE and NATURE values are retained; SID counts are not typhoon counts.",
                          "Cross-year storms may occur in more than one yearly file.",
                          "Time-window boundaries can clip a storm's full lifetime."],
                "files": {}, "years": {}}
    with tempfile.TemporaryDirectory(prefix=".subset-", dir=root) as tmp:
        stage = Path(tmp)
        with source.open(encoding="utf-8-sig", newline="") as handle, ExitStack() as stack:
            reader = csv.reader(handle, strict=True)
            header, units = read_header(reader)
            indexes = {name: header.index(name) for name in ("SID", "ISO_TIME", "BASIN", "TRACK_TYPE")}
            writers, file_handles = {}, {}
            years = list(range(start_year, end_year + 1))
            for year in [None] + years:
                name = ("ibtracs_global_%d_%d.csv" % (start_year, end_year) if year is None
                        else "ibtracs_global_%d.csv" % year)
                dest = stack.enter_context((stage / name).open("x", encoding="utf-8", newline=""))
                writer = csv.writer(dest, lineterminator="\n")
                writer.writerows([header, units])
                writers[year] = writer
                file_handles[name] = dest
            stats = {year: {"rows": 0, "sids": set(), "first_time": None, "last_time": None}
                     for year in [None] + years}
            track_types, basins = Counter(), Counter()
            source_rows = 0
            for row in reader:
                if len(row) != len(header):
                    raise ValueError("Malformed source row at line %d" % reader.line_num)
                stamp = datetime.strptime(row[indexes["ISO_TIME"]].strip(), "%Y-%m-%d %H:%M:%S")
                sid = row[indexes["SID"]].strip()
                if not sid:
                    raise ValueError("Missing SID at line %d" % reader.line_num)
                source_rows += 1
                if not start_year <= stamp.year <= end_year:
                    continue
                iso_time = stamp.isoformat() + "Z"
                for year in (None, stamp.year):
                    writers[year].writerow(row)
                    stat = stats[year]
                    stat["rows"] += 1
                    stat["sids"].add(sid)
                    stat["first_time"] = min(stat["first_time"] or iso_time, iso_time)
                    stat["last_time"] = max(stat["last_time"] or iso_time, iso_time)
                track_types[row[indexes["TRACK_TYPE"]]] += 1
                basins[row[indexes["BASIN"]]] += 1
            if any(stats[year]["rows"] == 0 for year in years):
                raise ValueError("At least one requested year has no records; refusing incomplete output")
            for dest in file_handles.values():
                dest.flush()
                os.fsync(dest.fileno())
        for year, stat in stats.items():
            stat["unique_sids"] = len(stat.pop("sids"))
            if year is None:
                manifest.update(stat)
            else:
                manifest["years"][str(year)] = stat
        manifest.update(source_rows=source_rows, columns=len(header), track_types=dict(track_types), basins=dict(basins))
        for name in file_handles:
            path = stage / name
            manifest["files"][name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
        if sha256(source) != source_hash:
            raise ValueError("Source changed during extraction")
        write_json(stage / "manifest.json", manifest)
        os.replace(stage, output)
    return verify_existing(output, source_hash, start_year, end_year)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path.home() / "data" / "ibtracs")
    parser.add_argument("--source", type=Path, help="Use an already downloaded official CSV")
    parser.add_argument("--start-year", type=int, default=2016)
    parser.add_argument("--end-year", type=int, default=2023)
    parser.add_argument("--require-user", help="Refuse to write under a different login account")
    args = parser.parse_args(argv)
    if args.require_user and current_user() != args.require_user:
        parser.error("Wrong login account: expected " + args.require_user)
    if not 1980 <= args.start_year <= args.end_year <= 9998:
        parser.error("Invalid year range")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        root = args.data_root.expanduser().resolve()
        with run_lock(root):
            if args.source:
                source, provenance = args.source.expanduser().resolve(), None
            else:
                LOG.info("Downloading or validating official global source: %s", SOURCE_URL)
                source, provenance = download_source(root)
            LOG.info("Extracting UTC calendar years %d-%d", args.start_year, args.end_year)
            result = subset_source(source, root, args.start_year, args.end_year, provenance)
        print(json.dumps({"data_root": str(root), "rows": result["rows"],
                          "unique_sids": result["unique_sids"], "years": result["years"]}, indent=2))
        return 0
    except (OSError, ValueError, csv.Error) as exc:
        LOG.error("IBTrACS download/subset failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
