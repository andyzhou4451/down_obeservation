from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path

from gdex_downloader.cli import (
    Candidate,
    DatasetConfig,
    DateTemplateConfig,
    JsonlWriter,
    candidate_sort_key,
    download_candidate,
    generated_date_candidates,
    is_out_of_year_scope,
    iter_year_dates,
    load_config,
    local_path_for_url,
    matching_product,
    matches_year,
    parse_links,
    parse_args,
    relevant_links,
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
            date_templates=(),
            scope_markers=("d735000", "ds735.0"),
            products={
                "amsu-a": ("amsua", "amsu-a", "1bamua"),
                "seviri": ("seviri", "msg", "sevcsr", "airsev"),
            },
            file_extensions=(".nc", ".tar.gz"),
            max_depth=8,
        )

    def test_matches_requested_year(self) -> None:
        self.assertTrue(matches_year("https://data.rda.ucar.edu/ds735.0/2026/amsua_file.nc", 2026))
        self.assertFalse(matches_year("https://data.rda.ucar.edu/ds735.0/2025/amsua_file.nc", 2026))

    def test_out_of_year_scope_only_uses_standalone_years(self) -> None:
        self.assertFalse(is_out_of_year_scope("https://x/ncar/gdex/d735000/atms/", 2026))
        self.assertFalse(is_out_of_year_scope("https://x/ncar/gdex/d735000/atms/2026/", 2026))
        self.assertFalse(is_out_of_year_scope("https://x/ncar/gdex/d735000/atms/2026/atms.20260623.tar.gz", 2026))
        self.assertTrue(is_out_of_year_scope("https://x/ncar/gdex/d735000/atms/2025/", 2026))

    def test_matches_product_aliases(self) -> None:
        self.assertEqual(matching_product("https://x/ds735.0/2026/amsu-a/file.nc", self.dataset), "amsu-a")
        self.assertEqual(matching_product("https://x/ncar/gdex/d735000/1bamua/2026/1bamua.20260623.tar.gz", self.dataset), "amsu-a")
        self.assertEqual(matching_product("https://x/ds735.0/2026/msg/file.nc", self.dataset), "seviri")
        self.assertEqual(matching_product("https://x/ncar/gdex/d735000/sevcsr/2026/sevcsr.20260623.tar.gz", self.dataset), "seviri")
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

    def test_iter_year_dates_stops_at_today_for_current_year(self) -> None:
        days = list(iter_year_dates(2026, today=date(2026, 1, 3)))

        self.assertEqual(days, [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)])

    def test_generated_date_candidates_use_template_product(self) -> None:
        dataset = DatasetConfig(
            id="d735000",
            label="test",
            dataaccess_url="https://data.gdex.ucar.edu/d735000/",
            seeds=(),
            date_templates=(
                DateTemplateConfig(
                    product="mhs",
                    url="https://data.gdex.ucar.edu/d735000/1bmhs/{year}/1bmhs.{yyyymmdd}.tar.gz",
                ),
            ),
            scope_markers=("d735000",),
            products={"mhs": ("mhs", "1bmhs")},
            file_extensions=(".tar.gz",),
            max_depth=0,
        )

        candidates = generated_date_candidates(
            dataset=dataset,
            year=2026,
            allowed_hosts={"data.gdex.ucar.edu"},
            data_root=Path("/data"),
            today=date(2026, 1, 2),
        )

        self.assertEqual([candidate.product for candidate in candidates], ["mhs", "mhs"])
        self.assertEqual(
            [candidate.url for candidate in candidates],
            [
                "https://data.gdex.ucar.edu/d735000/1bmhs/2026/1bmhs.20260101.tar.gz",
                "https://data.gdex.ucar.edu/d735000/1bmhs/2026/1bmhs.20260102.tar.gz",
            ],
        )

    def test_candidate_sort_key_uses_template_product_order(self) -> None:
        dataset = DatasetConfig(
            id="d735000",
            label="test",
            dataaccess_url="https://data.gdex.ucar.edu/d735000/",
            seeds=(),
            date_templates=(
                DateTemplateConfig(product="mhs", url="https://x/{yyyymmdd}.tar.gz"),
                DateTemplateConfig(product="atms", url="https://x/{yyyymmdd}.tar.gz"),
                DateTemplateConfig(product="amsu-a", url="https://x/{yyyymmdd}.tar.gz"),
            ),
            scope_markers=("d735000",),
            products={},
            file_extensions=(".tar.gz",),
            max_depth=0,
        )
        candidates = [
            Candidate("d735000", "amsu-a", "https://x/amsua.tar.gz", Path("/tmp/amsua.tar.gz")),
            Candidate("d735000", "mhs", "https://x/mhs.tar.gz", Path("/tmp/mhs.tar.gz")),
            Candidate("d735000", "atms", "https://x/atms.tar.gz", Path("/tmp/atms.tar.gz")),
        ]

        ordered = sorted(candidates, key=lambda candidate: candidate_sort_key(dataset, candidate))

        self.assertEqual([candidate.product for candidate in ordered], ["mhs", "atms", "amsu-a"])


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

    def test_relevant_links_keeps_dataset_and_file_links(self) -> None:
        links = [
            "https://gdex.ucar.edu/static/css/main.css",
            "https://osdf-director.osg-htc.org/ncar/gdex/d735000/atms/2026/atms.20260623.tar.gz",
            "https://osdf-director.osg-htc.org/ncar/gdex/d337000/tarfiles/2026/prepbufr.20260623.nr.tar.gz",
        ]

        self.assertEqual(relevant_links(links), links[1:])

    def test_th_hpc4_config_prioritizes_new_data_gdex_templates(self) -> None:
        _, allowed_hosts, datasets = load_config(Path("config/datasets.th-hpc4.json"))

        self.assertNotIn("data.rda.ucar.edu", allowed_hosts)
        self.assertNotIn("osdf-director.osg-htc.org", allowed_hosts)
        self.assertNotIn("sdsc-cache.nationalresearchplatform.org", allowed_hosts)
        self.assertIn("data.gdex.ucar.edu", allowed_hosts)
        products_in_order = [
            template.product
            for dataset in datasets
            for template in dataset.date_templates
        ]

        self.assertEqual(products_in_order, ["cris", "mtiasi", "gdas-prepbufr", "mhs", "atms", "amsu-a", "gpsro"])
        self.assertNotIn("hirs4", products_in_order)
        self.assertNotIn("seviri", products_in_order)
        self.assertTrue(all(not dataset.seeds for dataset in datasets))
        self.assertTrue(all("data.rda.ucar.edu" not in seed for dataset in datasets for seed in dataset.seeds))
        self.assertTrue(all("data.gdex.ucar.edu" in template.url for dataset in datasets for template in dataset.date_templates))

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

    def test_download_candidate_treats_404_as_missing_remote(self) -> None:
        class MissingClient:
            def request(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = Candidate(
                dataset_id="d735000",
                product="hirs4",
                url="https://data.gdex.ucar.edu/d735000/1bhrs4/2026/1bhrs4.20260101.tar.gz",
                local_path=root / "1bhrs4.20260101.tar.gz",
            )
            manifest = JsonlWriter(root / "manifest.jsonl")

            result = download_candidate(
                candidate,
                client=MissingClient(),  # type: ignore[arg-type]
                force=False,
                recheck_existing=False,
                chunk_size=1024,
                delay=0,
                manifest=manifest,
            )

            records = [json.loads(line) for line in (root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]

        self.assertEqual(result.status, "missing_remote")
        self.assertEqual(records[0]["event"], "missing_remote")


if __name__ == "__main__":
    unittest.main()
