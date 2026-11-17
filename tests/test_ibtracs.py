import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.download_ibtracs import SOURCE_NAME, download_source, main, read_header, run_lock, subset_source


HEADER = ["SID", "SEASON", "ISO_TIME", "BASIN", "LAT", "LON", "TRACK_TYPE", "USA_WIND", "TOKYO_WIND"]
UNITS = ["", "year", "", "", "degrees_north", "degrees_east", "", "kts", "kts"]


class IBTrACSTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.csv"

    def source_file(self, rows):
        with self.source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerows([HEADER, UNITS] + rows)

    def row(self, stamp, season="2024", sid="2015365S10080"):
        return [sid, season, stamp, "SI", "-10.0", "80.0", "main", "50", ""]

    def test_utc_boundaries_units_leap_day_and_multiagency_preserved(self):
        rows = [self.row("2015-12-31 21:00:00"), self.row("2016-01-01 00:00:00"),
                self.row("2016-02-29 03:00:00")]
        rows += [self.row("%d-12-31 21:00:00" % year) for year in range(2017, 2024)]
        rows.append(self.row("2024-01-01 00:00:00"))
        self.source_file(rows)
        result = subset_source(self.source, self.root / "out")
        self.assertEqual(result["rows"], 9)
        self.assertEqual(result["unique_sids"], 1)
        self.assertEqual(result["years"]["2016"]["rows"], 2)
        output = self.root / "out/2016-2023/ibtracs_global_2016_2023.csv"
        with output.open(newline="", encoding="utf-8") as handle:
            actual = list(csv.reader(handle))
        self.assertEqual(actual, [HEADER, UNITS] + rows[1:-1])
        self.assertEqual(subset_source(self.source, self.root / "out"), result)

    def test_missing_year_or_bad_row_does_not_publish(self):
        self.source_file([self.row("2016-01-01 00:00:00")])
        with self.assertRaisesRegex(ValueError, "no records"):
            subset_source(self.source, self.root / "out")
        self.assertFalse((self.root / "out/2016-2023").exists())
        self.source_file([self.row("2016-01-01 00:00:00"), ["bad"]])
        with self.assertRaisesRegex(ValueError, "Malformed"):
            subset_source(self.source, self.root / "out", 2016, 2016)
        self.assertFalse((self.root / "out/2016-2016").exists())

    def test_invalid_timestamp_stops_without_publishing(self):
        self.source_file([self.row("2016-02-30 00:00:00")])
        with self.assertRaises(ValueError):
            subset_source(self.source, self.root / "out", 2016, 2016)
        self.assertFalse((self.root / "out/2016-2016").exists())

    def test_corrupt_existing_output_is_not_silently_replaced(self):
        self.source_file([self.row("2016-01-01 00:00:00")])
        subset_source(self.source, self.root / "out", 2016, 2016)
        target = self.root / "out/2016-2016/ibtracs_global_2016.csv"
        target.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum"):
            subset_source(self.source, self.root / "out", 2016, 2016)
        self.assertEqual(target.read_bytes(), b"corrupt")

    def test_source_revision_is_not_silently_mixed(self):
        self.source_file([self.row("2016-01-01 00:00:00")])
        subset_source(self.source, self.root / "out", 2016, 2016)
        self.source_file([self.row("2016-01-01 03:00:00")])
        with self.assertRaisesRegex(ValueError, "different source"):
            subset_source(self.source, self.root / "out", 2016, 2016)

    def test_html_and_missing_units_rejected(self):
        with self.assertRaises(ValueError):
            read_header(csv.reader(io.StringIO("<html>maintenance</html>\n")))
        with self.assertRaisesRegex(ValueError, "units row"):
            read_header(iter([HEADER, self.row("2016-01-01 00:00:00")]))

    def test_wrong_account_fails_before_writing(self):
        with patch("scripts.download_ibtracs.current_user", return_value="yangjh18"):
            with self.assertRaises(SystemExit):
                main(["--require-user", "yangjh12", "--data-root", str(self.root / "out")])
        self.assertFalse((self.root / "out").exists())

    def test_duplicate_run_is_rejected_and_lock_released(self):
        with run_lock(self.root):
            with self.assertRaisesRegex(OSError, "Another IBTrACS run"):
                with run_lock(self.root):
                    pass
        with run_lock(self.root):
            pass

    def test_complete_source_is_cached_with_receipt(self):
        self.source_file([self.row("2016-01-01 00:00:00")])
        data = self.source.read_bytes()
        response = io.BytesIO(data)
        response.status = 200
        response.headers = {"Content-Length": str(len(data)), "Last-Modified": "test"}
        with patch("scripts.download_ibtracs.urllib.request.urlopen", return_value=response) as opener:
            source, record = download_source(self.root / "out", retries=0)
            self.assertEqual(source.read_bytes(), data)
            self.assertEqual(record["bytes"], len(data))
            self.assertEqual(download_source(self.root / "out", retries=0), (source, record))
            self.assertEqual(opener.call_count, 1)

    def test_truncated_or_html_download_is_not_promoted(self):
        for data, expected in [(b"partial", 1000), (b"<html>maintenance</html>", 24)]:
            with self.subTest(data=data):
                response = io.BytesIO(data)
                response.status = 200
                response.headers = {"Content-Length": str(expected)}
                with patch("scripts.download_ibtracs.urllib.request.urlopen", return_value=response):
                    with self.assertRaises(ValueError):
                        download_source(self.root / "out", retries=0)
                self.assertFalse((self.root / "out/source" / SOURCE_NAME).exists())


if __name__ == "__main__":
    unittest.main()
