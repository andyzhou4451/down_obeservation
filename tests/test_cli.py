from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gdex_downloader.cli import (
    Candidate,
    DatasetConfig,
    load_config,
    local_path_for_url,
    matching_product,
    matches_year,
    parse_links,
    parse_args,
    should_download,
    write_candidate_list,
)


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dataset = DatasetConfig(
            id="d735000",
            label="test",
            dataaccess_url="https://gdex.ucar.edu/datasets/d735000/dataaccess/",
            seeds=("https://data.rda.ucar.edu/ds735.0/",),
            scope_markers=("d735000", "ds735.0"),
            products={
                "amsu-a": ("amsua", "amsu-a"),
                "seviri": ("seviri", "msg"),
            },
            file_extensions=(".nc", ".tar.gz"),
            max_depth=8,
        )

    def test_matches_requested_year(self) -> None:
        self.assertTrue(matches_year("https://data.rda.ucar.edu/ds735.0/2026/amsua_file.nc", 2026))
        self.assertFalse(matches_year("https://data.rda.ucar.edu/ds735.0/2025/amsua_file.nc", 2026))

    def test_matches_product_aliases(self) -> None:
        self.assertEqual(matching_product("https://x/ds735.0/2026/amsu-a/file.nc", self.dataset), "amsu-a")
        self.assertEqual(matching_product("https://x/ds735.0/2026/msg/file.nc", self.dataset), "seviri")
        self.assertIsNone(matching_product("https://x/ds735.0/2026/unknown/file.nc", self.dataset))

    def test_should_download_requires_year_product_and_extension(self) -> None:
        good = "https://data.rda.ucar.edu/ds735.0/2026/amsua/file.nc"
        wrong_year = "https://data.rda.ucar.edu/ds735.0/2025/amsua/file.nc"
        wrong_product = "https://data.rda.ucar.edu/ds735.0/2026/other/file.nc"
        wrong_extension = "https://data.rda.ucar.edu/ds735.0/2026/amsua/file.html"

        self.assertEqual(should_download(good, self.dataset, 2026), (True, "amsu-a"))
        self.assertEqual(should_download(wrong_year, self.dataset, 2026), (False, None))
        self.assertEqual(should_download(wrong_product, self.dataset, 2026), (False, None))
        self.assertEqual(should_download(wrong_extension, self.dataset, 2026), (False, None))


class PathTests(unittest.TestCase):
    def test_cli_defaults_use_sibling_data_directory(self) -> None:
        args = parse_args([])

        self.assertEqual(args.data_root, Path("../data"))
        self.assertEqual(args.state_dir, Path("../data/_state"))
        self.assertEqual(args.log_dir, Path("../data/_logs"))
        self.assertFalse(args.insecure_tls)
        self.assertFalse(args.log_index_links)
        self.assertEqual(args.index_link_sample, 20)

    def test_cli_accepts_insecure_tls_flag(self) -> None:
        args = parse_args(["--insecure-tls"])

        self.assertTrue(args.insecure_tls)

    def test_cli_accepts_index_link_logging_flags(self) -> None:
        args = parse_args(["--log-index-links", "--index-link-sample", "5"])

        self.assertTrue(args.log_index_links)
        self.assertEqual(args.index_link_sample, 5)

    def test_parse_links_reads_s3_keys(self) -> None:
        body = b"<ListBucketResult><Key>d337000/2026/gdas.prepbufr.tar.gz</Key></ListBucketResult>"
        links = parse_links("https://data.gdex.ucar.edu/d337000/", body, "application/xml")

        self.assertIn("https://data.gdex.ucar.edu/d337000/2026/gdas.prepbufr.tar.gz", links)

    def test_parse_links_reads_plain_urls_without_content_type(self) -> None:
        body = b"https://data.gdex.ucar.edu/d735000/2026/atms/file.nc\n"
        links = parse_links("https://data.gdex.ucar.edu/d735000/", body, "")

        self.assertEqual(links, ["https://data.gdex.ucar.edu/d735000/2026/atms/file.nc"])

    def test_th_hpc4_config_avoids_data_rda_seed(self) -> None:
        _, allowed_hosts, datasets = load_config(Path("config/datasets.th-hpc4.json"))

        self.assertNotIn("data.rda.ucar.edu", allowed_hosts)
        for dataset in datasets:
            self.assertTrue(dataset.seeds)
            self.assertTrue(all("data.rda.ucar.edu" not in seed for seed in dataset.seeds))

    def test_local_path_preserves_dataset_product_host_and_remote_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = local_path_for_url(
                root,
                "d337000",
                "gdas-prepbufr",
                "https://data.rda.ucar.edu/ds337.0/2026/gdas.20260101.prepbufr.tar.gz",
            )

        self.assertEqual(path.parts[-5:], ("gdas-prepbufr", "data.rda.ucar.edu", "ds337.0", "2026", "gdas.20260101.prepbufr.tar.gz"))

    def test_candidate_list_is_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "candidates.jsonl"
            candidate = Candidate(
                dataset_id="d735000",
                product="atms",
                url="https://data.rda.ucar.edu/ds735.0/2026/atms/file.nc",
                local_path=Path("/data/gdex/d735000/atms/file.nc"),
            )
            write_candidate_list(output, [candidate])
            [line] = output.read_text(encoding="utf-8").splitlines()
            record = json.loads(line)

        self.assertEqual(record["dataset"], "d735000")
        self.assertEqual(record["product"], "atms")
        self.assertEqual(record["url"], candidate.url)


if __name__ == "__main__":
    unittest.main()
