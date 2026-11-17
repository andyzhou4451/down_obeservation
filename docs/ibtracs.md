# NOAA IBTrACS, 2016-2023

This independent, one-shot tool downloads the official global `since1980`
CSV for IBTrACS v04r01, freezes that source, and extracts UTC calendar years
2016 through 2023. It does not modify the GDEX or GridSat pipelines.

Run under the requested account on the 5090 login node (Python 3.8+):

```bash
python3 scripts/download_ibtracs.py --require-user yangjh12 \
  --data-root "$HOME/data/ibtracs" --start-year 2016 --end-year 2023
```

Use the existing approved proxy environment. TLS certificate validation remains
enabled. No scheduler job, GPU, cron task, or new Python dependency is required.
The download is serial with bounded retries; interrupted transfers restart from
zero rather than mixing two versions of a frequently updated official file.

Output layout:

```text
~/data/ibtracs/
  source/ibtracs.since1980.list.v04r01.csv
  source/source.json
  2016-2023/ibtracs_global_2016_2023.csv
  2016-2023/ibtracs_global_2016.csv
  ...
  2016-2023/ibtracs_global_2023.csv
  2016-2023/manifest.json
```

Selection is `2016-01-01 <= ISO_TIME < 2024-01-01`, not `SEASON` (Southern
Hemisphere seasons can cross calendar years). The files preserve every column,
the second units row, missing values, all basins, all track types and source
timestamps. No agency merging, intensity filter, wind normalization, or new
time interpolation is performed. Storms crossing a year boundary occur in
multiple yearly files; storms crossing the requested interval can be clipped.
Keep the original source if complete storm lifetimes are needed.

The manifest records source and output SHA-256 hashes, data-row counts, unique
SIDs, time bounds, basins, and track types. Counts of unique SIDs are not counts
of typhoons reaching a particular intensity. Existing validated outputs are
rechecked and skipped; modified outputs or a different source revision cause an
error instead of being silently overwritten. No raw data is deleted.
A kernel-owned lock prevents duplicate runs in the same data root and is
released automatically when the process exits; do not delete active lock files.

An already downloaded official CSV can be supplied with `--source PATH`.
This checks CSV structure and hashes but cannot independently prove transport
completeness or provenance of an operator-supplied file.

Scientific cautions: most agency reports are six-hourly, while IBTrACS includes
three-hourly interpolated positions and additional special times. Preserve the
interpolation flags. Agency wind averaging periods differ. The CSV's second row
contains units, not an observation (pandas: `pd.read_csv(path, skiprows=[1])`).

Official references:

- https://www.ncei.noaa.gov/products/international-best-track-archive
- https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs/v04r01/access/csv/
- https://www.ncei.noaa.gov/sites/default/files/2025-04/IBTrACS_version4r01_Technical_Details.pdf
- Dataset citation: Gahtan et al. (2024), IBTrACS v04r01, NOAA NCEI,
  https://doi.org/10.25921/82ty-9e16 (record the access date and subset).
