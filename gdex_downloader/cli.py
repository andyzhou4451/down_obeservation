from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import dataclasses
import hashlib
import html.parser
import json
import logging
import netrc
import os
import posixpath
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


LOG = logging.getLogger("gdex_downloader")
DEFAULT_USER_AGENT = "down-obeservation/0.1 (+https://github.com/andyzhou4451/down_obeservation)"
HTML_EXTENSIONS = {"", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp"}
SKIP_SCHEMES = {"javascript", "mailto", "tel", "data"}
TEXT_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/xml",
    "text/xml",
    "application/octet-stream",
)
KEY_PATTERN = re.compile(r"<Key>([^<]+)</Key>", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
STANDALONE_YEAR_PATTERN = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
RELEVANT_LINK_KEYWORDS = (
    "api",
    "dataaccess",
    "download",
    "file",
    "d337000",
    "d735000",
    "ds337",
    "ds735",
)


@dataclasses.dataclass(frozen=True)
class DatasetConfig:
    id: str
    label: str
    dataaccess_url: str
    seeds: tuple[str, ...]
    scope_markers: tuple[str, ...]
    products: dict[str, tuple[str, ...]]
    file_extensions: tuple[str, ...]
    max_depth: int


@dataclasses.dataclass(frozen=True)
class Candidate:
    dataset_id: str
    product: str
    url: str
    local_path: Path


@dataclasses.dataclass(frozen=True)
class DownloadResult:
    candidate: Candidate
    status: str
    bytes_written: int = 0
    detail: str = ""


class HrefParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() in {"href", "src"} and value:
                self.links.append(value)


class JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **payload: object) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


class SingleRunLock:
    def __init__(self, path: Path, stale_hours: float) -> None:
        self.path = path
        self.stale_seconds = stale_hours * 3600
        self.fd: int | None = None

    def __enter__(self) -> "SingleRunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self._is_stale():
            LOG.warning("Removing stale lock: %s", self.path)
            self.path.unlink()
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        try:
            self.fd = os.open(self.path, flags)
        except FileExistsError as exc:
            raise SystemExit(f"Another downloader run is active: {self.path}") from exc
        os.write(self.fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def _is_stale(self) -> bool:
        if self.stale_seconds <= 0:
            return False
        age = time.time() - self.path.stat().st_mtime
        return age > self.stale_seconds


class URLClient:
    def __init__(
        self,
        *,
        timeout: float,
        retries: int,
        retry_sleep: float,
        headers: Iterable[str],
        use_netrc: bool,
        user_agent: str,
        insecure_tls: bool,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.retry_sleep = retry_sleep
        self.ssl_context = ssl._create_unverified_context() if insecure_tls else None
        self.base_headers = {"User-Agent": user_agent}
        for raw in headers:
            name, sep, value = raw.partition(":")
            if not sep or not name.strip() or not value.strip():
                raise SystemExit(f"Invalid --header value, expected 'Name: value': {raw!r}")
            self.base_headers[name.strip()] = value.strip()
        self.netrc_auth = load_netrc_auth() if use_netrc else {}

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
    ):
        request_headers = dict(self.base_headers)
        request_headers.update(self._auth_header_for(url))
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers, method=method)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                sleep_for = self.retry_sleep * (attempt + 1)
                LOG.warning("Request failed (%s), retrying in %.1fs: %s", exc, sleep_for, url)
                time.sleep(sleep_for)
        assert last_error is not None
        raise last_error

    def _auth_header_for(self, url: str) -> dict[str, str]:
        host = urllib.parse.urlparse(url).hostname
        if not host or host not in self.netrc_auth:
            return {}
        login, password = self.netrc_auth[host]
        token = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}"}


def load_netrc_auth() -> dict[str, tuple[str, str]]:
    try:
        auth = netrc.netrc()
    except (FileNotFoundError, netrc.NetrcParseError):
        return {}
    result: dict[str, tuple[str, str]] = {}
    for host in auth.hosts:
        entry = auth.authenticators(host)
        if not entry:
            continue
        login, _, password = entry
        if login and password:
            result[host] = (login, password)
    return result


def configure_logging(log_dir: Path, verbose: bool) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.setLevel(level)

    file_handler = logging.FileHandler(log_dir / "gdex_downloader.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    logging.basicConfig(level=level, handlers=[console, file_handler], force=True)


def load_config(path: Path) -> tuple[int, set[str], list[DatasetConfig]]:
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    default_year = int(raw.get("year", datetime.now(timezone.utc).year))
    allowed_hosts = set(raw.get("allowed_hosts", []))
    datasets = []
    for item in raw["datasets"]:
        products = {
            str(product): tuple(str(pattern).lower() for pattern in patterns)
            for product, patterns in item.get("products", {}).items()
        }
        datasets.append(
            DatasetConfig(
                id=str(item["id"]),
                label=str(item.get("label", item["id"])),
                dataaccess_url=str(item.get("dataaccess_url", "")),
                seeds=tuple(str(seed) for seed in item.get("seeds", [])),
                scope_markers=tuple(str(marker).lower() for marker in item.get("scope_markers", [])),
                products=products,
                file_extensions=tuple(str(ext).lower() for ext in item.get("file_extensions", [])),
                max_depth=int(item.get("max_depth", 6)),
            )
        )
    return default_year, allowed_hosts, datasets


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return url
    path = posixpath.normpath(parsed.path or "/")
    if parsed.path.endswith("/") and not path.endswith("/"):
        path += "/"
    return urllib.parse.urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            parsed.query,
            "",
        )
    )


def join_link(base_url: str, link: str) -> str | None:
    parsed = urllib.parse.urlparse(link)
    if parsed.scheme.lower() in SKIP_SCHEMES:
        return None
    if link.startswith("#"):
        return None
    return normalize_url(urllib.parse.urljoin(base_url, link))


def path_text(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.unquote(f"{parsed.path}?{parsed.query}").lower()


def matches_year(url: str, year: int) -> bool:
    return str(year) in path_text(url)


def is_out_of_year_scope(url: str, year: int) -> bool:
    years = set(STANDALONE_YEAR_PATTERN.findall(path_text(url)))
    return bool(years) and str(year) not in years


def matching_product(url: str, dataset: DatasetConfig) -> str | None:
    text = path_text(url)
    if not dataset.products:
        return "all"
    for product, patterns in dataset.products.items():
        if any(pattern in text for pattern in patterns):
            return product
    return None


def extension_for_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for ext in (".tar.gz", ".bufr.gz", ".nc.gz"):
        if path.endswith(ext):
            return ext
    return Path(path).suffix.lower()


def has_allowed_extension(url: str, dataset: DatasetConfig) -> bool:
    if not dataset.file_extensions:
        return True
    path = urllib.parse.urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in dataset.file_extensions)


def is_probably_index_page(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.path.endswith("/"):
        return True
    if parsed.query:
        return True
    return extension_for_url(url) in HTML_EXTENSIONS


def in_dataset_scope(url: str, dataset: DatasetConfig, allowed_hosts: set[str]) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if allowed_hosts and parsed.hostname.lower() not in allowed_hosts:
        return False
    text = f"{parsed.netloc}{parsed.path}".lower()
    return any(marker in text for marker in dataset.scope_markers)


def should_download(url: str, dataset: DatasetConfig, year: int) -> tuple[bool, str | None]:
    product = matching_product(url, dataset)
    if product is None:
        return False, None
    if not matches_year(url, year):
        return False, None
    if not has_allowed_extension(url, dataset):
        return False, None
    return True, product


def local_path_for_url(data_root: Path, dataset_id: str, product: str, url: str) -> Path:
    parsed = urllib.parse.urlparse(url)
    host = sanitize_component(parsed.hostname or "unknown-host")
    decoded_path = urllib.parse.unquote(parsed.path).lstrip("/")
    if not decoded_path or decoded_path.endswith("/"):
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        decoded_path = f"download-{digest}"
    parts = [sanitize_component(part) for part in decoded_path.split("/") if part not in {"", ".", ".."}]
    if parsed.query:
        stem = parts[-1] if parts else "download"
        digest = hashlib.sha256(parsed.query.encode("utf-8")).hexdigest()[:12]
        parts[-1:] = [f"{stem}-{digest}"]
    return data_root / dataset_id / product / host / Path(*parts)


def sanitize_component(value: str) -> str:
    keep = []
    for char in value:
        if char.isalnum() or char in {"-", "_", ".", "=", "+"}:
            keep.append(char)
        else:
            keep.append("_")
    cleaned = "".join(keep).strip("._")
    return cleaned or "unknown"


def parse_links(base_url: str, body: bytes, content_type: str) -> list[str]:
    normalized_type = content_type.lower()
    if content_type and not any(kind in normalized_type for kind in TEXT_CONTENT_TYPES):
        return []
    text = body.decode("utf-8", errors="replace")
    parser = HrefParser()
    parser.feed(text)
    links = []
    for raw in parser.links:
        joined = join_link(base_url, raw)
        if joined:
            links.append(joined)
    for key in KEY_PATTERN.findall(text):
        joined = join_object_key(base_url, key)
        if joined:
            links.append(joined)
    for raw_url in URL_PATTERN.findall(text):
        joined = join_link(base_url, raw_url)
        if joined:
            links.append(joined)
    return list(dict.fromkeys(links))


def join_object_key(base_url: str, key: str) -> str | None:
    stripped_key = key.lstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    base_path = parsed.path.lstrip("/")
    first_base_part = base_path.split("/", 1)[0] if base_path else ""
    first_key_part = stripped_key.split("/", 1)[0] if stripped_key else ""
    if first_base_part and first_base_part == first_key_part:
        root = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/", "", "", ""))
        return join_link(root, stripped_key)
    return join_link(base_url, stripped_key)


def body_sample(body: bytes, max_chars: int = 500) -> str:
    text = body.decode("utf-8", errors="replace")
    return " ".join(text.split())[:max_chars]


def relevant_links(links: Iterable[str]) -> list[str]:
    result = []
    for link in links:
        lowered = link.lower()
        if any(keyword in lowered for keyword in RELEVANT_LINK_KEYWORDS):
            result.append(link)
    return result


def discover_dataset(
    *,
    dataset: DatasetConfig,
    year: int,
    allowed_hosts: set[str],
    data_root: Path,
    client: URLClient,
    max_pages: int,
    max_depth: int | None,
    log_index_links: bool,
    index_link_sample: int,
    manifest: JsonlWriter,
) -> list[Candidate]:
    depth_limit = dataset.max_depth if max_depth is None else max_depth
    queue: deque[tuple[str, int]] = deque((normalize_url(seed), 0) for seed in dataset.seeds)
    visited: set[str] = set()
    candidates: dict[str, Candidate] = {}

    LOG.info("Discovering %s for %s", dataset.id, year)
    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        if not in_dataset_scope(url, dataset, allowed_hosts):
            continue

        download, product = should_download(url, dataset, year)
        if download and product:
            local_path = local_path_for_url(data_root, dataset.id, product, url)
            candidates[url] = Candidate(dataset.id, product, url, local_path)
            manifest.append("discovered_file", dataset=dataset.id, product=product, url=url)
            continue

        if depth >= depth_limit or not is_probably_index_page(url):
            continue

        try:
            with client.request(url) as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read()
        except Exception as exc:  # noqa: BLE001 - log-and-continue discovery is intentional.
            LOG.warning("Failed to fetch index page for discovery: %s (%s)", url, exc)
            manifest.append("discovery_error", dataset=dataset.id, url=url, error=str(exc))
            continue

        links = parse_links(url, body, content_type)
        if log_index_links:
            sample = links[: max(0, index_link_sample)]
            body_preview = body_sample(body)
            relevant = relevant_links(links)[: max(0, index_link_sample)]
            LOG.info(
                "Index page links dataset=%s url=%s content_type=%s bytes=%d links=%d sample=%s relevant=%s body_sample=%r",
                dataset.id,
                url,
                content_type or "unknown",
                len(body),
                len(links),
                sample,
                relevant,
                body_preview,
            )
            manifest.append(
                "index_page",
                dataset=dataset.id,
                url=url,
                content_type=content_type or "unknown",
                bytes=len(body),
                link_count=len(links),
                link_sample=sample,
                relevant_links=relevant,
                body_sample=body_preview,
            )
        else:
            LOG.debug("Discovered %d links from %s", len(links), url)
        for link in links:
            if link not in visited and in_dataset_scope(link, dataset, allowed_hosts) and not is_out_of_year_scope(link, year):
                queue.append((link, depth + 1))

    if queue:
        LOG.warning("Discovery stopped at max_pages=%d for %s", max_pages, dataset.id)
    LOG.info("Found %d candidate files for %s", len(candidates), dataset.id)
    return sorted(candidates.values(), key=lambda item: item.url)


def remote_size(client: URLClient, url: str) -> int | None:
    try:
        with client.request(url, method="HEAD") as response:
            length = response.headers.get("Content-Length")
            if length and length.isdigit():
                return int(length)
    except Exception:
        return None
    return None


def download_candidate(
    candidate: Candidate,
    *,
    client: URLClient,
    force: bool,
    recheck_existing: bool,
    chunk_size: int,
    delay: float,
    manifest: JsonlWriter,
) -> DownloadResult:
    dest = candidate.local_path
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        if not recheck_existing:
            manifest.append(
                "skipped_exists",
                dataset=candidate.dataset_id,
                product=candidate.product,
                url=candidate.url,
                path=str(dest),
                size=dest.stat().st_size,
            )
            return DownloadResult(candidate, "skipped_exists", dest.stat().st_size)
        expected = remote_size(client, candidate.url)
        if expected is None or expected == dest.stat().st_size:
            return DownloadResult(candidate, "skipped_exists", dest.stat().st_size)
        LOG.warning("Existing file size differs from remote; re-downloading: %s", dest)

    headers: dict[str, str] = {}
    mode = "wb"
    starting_at = 0
    if part.exists() and not force:
        starting_at = part.stat().st_size
        if starting_at > 0:
            headers["Range"] = f"bytes={starting_at}-"
            mode = "ab"

    if force:
        with contextlib.suppress(FileNotFoundError):
            part.unlink()

    try:
        with client.request(candidate.url, headers=headers) as response:
            status = getattr(response, "status", response.getcode())
            if starting_at and status != 206:
                LOG.info("Server did not resume %s; restarting download", candidate.url)
                mode = "wb"
                starting_at = 0
            if "text/html" in response.headers.get("Content-Type", "").lower():
                return DownloadResult(candidate, "failed", detail="URL returned HTML, not a data file")
            bytes_written = copy_response(response, part, mode, chunk_size)
    except Exception as exc:  # noqa: BLE001 - failure is recorded and other files continue.
        manifest.append(
            "download_error",
            dataset=candidate.dataset_id,
            product=candidate.product,
            url=candidate.url,
            path=str(dest),
            error=str(exc),
        )
        return DownloadResult(candidate, "failed", detail=str(exc))

    os.replace(part, dest)
    if delay > 0:
        time.sleep(delay)
    final_size = dest.stat().st_size
    manifest.append(
        "downloaded",
        dataset=candidate.dataset_id,
        product=candidate.product,
        url=candidate.url,
        path=str(dest),
        size=final_size,
        resumed_from=starting_at,
        bytes_written=bytes_written,
    )
    return DownloadResult(candidate, "downloaded", final_size)


def copy_response(response, part: Path, mode: str, chunk_size: int) -> int:  # type: ignore[no-untyped-def]
    bytes_written = 0
    with part.open(mode) as handle:
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            handle.write(chunk)
            bytes_written += len(chunk)
    return bytes_written


def write_candidate_list(path: Path, candidates: Iterable[Candidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for candidate in candidates:
            handle.write(
                json.dumps(
                    {
                        "dataset": candidate.dataset_id,
                        "product": candidate.product,
                        "url": candidate.url,
                        "local_path": str(candidate.local_path),
                    },
                    sort_keys=True,
                )
                + "\n"
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download 2026 UCAR GDEX observation data.")
    parser.add_argument("--config", type=Path, default=Path("config/datasets.json"))
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--data-root", type=Path, default=Path("../data"))
    parser.add_argument("--state-dir", type=Path, default=Path("../data/_state"))
    parser.add_argument("--log-dir", type=Path, default=Path("../data/_logs"))
    parser.add_argument("--dataset", action="append", help="Dataset id to run. Repeat for multiple ids.")
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=20000)
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None, help="Limit candidate count, useful for smoke tests.")
    parser.add_argument("--dry-run", action="store_true", help="Discover and list files without downloading.")
    parser.add_argument("--force", action="store_true", help="Re-download even when the final file exists.")
    parser.add_argument("--recheck-existing", action="store_true", help="Use HEAD size checks before skipping final files.")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-sleep", type=float, default=5.0)
    parser.add_argument("--download-delay", type=float, default=0.25)
    parser.add_argument("--chunk-size", type=int, default=1024 * 1024)
    parser.add_argument("--lock-stale-hours", type=float, default=36.0)
    parser.add_argument("--log-index-links", action="store_true", help="Log index-page link counts and samples during discovery.")
    parser.add_argument("--index-link-sample", type=int, default=20, help="Number of discovered links to include when --log-index-links is set.")
    parser.add_argument("--header", action="append", default=[], help="Extra HTTP header, e.g. 'Authorization: Bearer ...'.")
    parser.add_argument("--no-netrc", action="store_true", help="Disable ~/.netrc Basic Auth lookup.")
    parser.add_argument("--insecure-tls", action="store_true", help="Disable HTTPS certificate verification. Use only for site proxy/CA issues.")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_dir, args.verbose)

    default_year, allowed_hosts, datasets = load_config(args.config)
    year = args.year or default_year
    if args.dataset:
        selected = set(args.dataset)
        datasets = [dataset for dataset in datasets if dataset.id in selected]
        missing = selected - {dataset.id for dataset in datasets}
        if missing:
            raise SystemExit(f"Unknown dataset id(s): {', '.join(sorted(missing))}")
    if not datasets:
        raise SystemExit("No datasets selected.")

    client = URLClient(
        timeout=args.timeout,
        retries=args.retries,
        retry_sleep=args.retry_sleep,
        headers=args.header,
        use_netrc=not args.no_netrc,
        user_agent=args.user_agent,
        insecure_tls=args.insecure_tls,
    )
    manifest = JsonlWriter(args.state_dir / "manifest.jsonl")
    lock_path = args.state_dir / "gdex_downloader.lock"

    with SingleRunLock(lock_path, args.lock_stale_hours):
        all_candidates: list[Candidate] = []
        for dataset in datasets:
            all_candidates.extend(
                discover_dataset(
                    dataset=dataset,
                    year=year,
                    allowed_hosts=allowed_hosts,
                    data_root=args.data_root,
                    client=client,
                    max_pages=args.max_pages,
                    max_depth=args.max_depth,
                    log_index_links=args.log_index_links,
                    index_link_sample=args.index_link_sample,
                    manifest=manifest,
                )
            )

        if args.limit is not None:
            all_candidates = all_candidates[: args.limit]

        candidate_list_path = args.state_dir / f"candidates-{year}.jsonl"
        write_candidate_list(candidate_list_path, all_candidates)
        LOG.info("Wrote candidate list: %s", candidate_list_path)

        if args.dry_run:
            for candidate in all_candidates:
                print(f"{candidate.dataset_id}\t{candidate.product}\t{candidate.url}\t{candidate.local_path}")
            LOG.info("Dry run complete: %d candidate files", len(all_candidates))
            return 0

        if not all_candidates:
            LOG.warning("No candidates discovered for %s", year)
            return 2

        failures = 0
        downloaded = 0
        skipped = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as pool:
            futures = [
                pool.submit(
                    download_candidate,
                    candidate,
                    client=client,
                    force=args.force,
                    recheck_existing=args.recheck_existing,
                    chunk_size=args.chunk_size,
                    delay=args.download_delay,
                    manifest=manifest,
                )
                for candidate in all_candidates
            ]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result.status == "downloaded":
                    downloaded += 1
                    LOG.info("Downloaded %s", result.candidate.local_path)
                elif result.status == "skipped_exists":
                    skipped += 1
                    LOG.info("Skipped existing %s", result.candidate.local_path)
                else:
                    failures += 1
                    LOG.error("Failed %s: %s", result.candidate.url, result.detail)

        LOG.info("Run complete: downloaded=%d skipped=%d failed=%d", downloaded, skipped, failures)
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
