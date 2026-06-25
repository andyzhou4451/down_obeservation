# down_obeservation

Daily, resumable downloader for 2026 UCAR GDEX observation data:

- `d735000`: CRIS, METOP-2 IASI, AMSU-A, ATMS, MHS, GPSRO/GNSSRO
- `d337000`: unrestricted GDAS PREPBUFR daily tar files

The downloader discovers files from configured GDEX listings or generates known daily file URLs, filters for the requested products and year, skips existing completed files, and resumes interrupted `.part` downloads when the server supports HTTP ranges.

## Requirements

- Python 3.10+
- Network access from the server to the configured GDEX endpoint. The default config uses `osdf-director.osg-htc.org`; the TH-HPC4 config uses `sdsc-cache.nationalresearchplatform.org:8443` for priority downloads and `data.gdex.ucar.edu` for final backfill checks.
- Optional: `~/.netrc` or extra HTTP headers if your GDEX account/session requires authentication

No third-party Python packages are required.

## Directory layout

Put the repository and download directory side by side:

```text
parent/
  down_obeservation/
  data/
    d735000/
    d337000/
    _state/
    _logs/
```

`scripts/run_daily.sh` uses this layout by default. Data files go to `../data`, runtime state goes to `../data/_state`, and logs go to `../data/_logs`.

If `python` is not on the server PATH, set `PYTHON_BIN` when using the daily wrapper:

```bash
PYTHON_BIN=/usr/bin/python3 scripts/run_daily.sh --dry-run --limit 5
```

## One-time dry run

```bash
bash scripts/run_daily.sh --dry-run --limit 20
```

## Download / resume

```bash
bash scripts/run_daily.sh
```

Completed files are trusted and skipped by default. Interrupted downloads are kept as `.part` files and resumed on the next run when possible.

To override the sibling `data` directory:

```bash
DATA_DIR=/scratch/$USER/data bash scripts/run_daily.sh
```

## Authentication options

The configured OSDF endpoints are public for the requested paths. If UCAR/GDEX later requires login for your account, keep credentials outside git.

For Basic Auth-compatible endpoints, use `~/.netrc`:

```text
machine data.rda.ucar.edu
  login YOUR_USERNAME
  password YOUR_PASSWORD

machine gdex.ucar.edu
  login YOUR_USERNAME
  password YOUR_PASSWORD
```

For token/cookie workflows, pass explicit headers from a protected environment file:

```bash
python -m gdex_downloader ... \
  --header "Authorization: Bearer $GDEX_TOKEN"
```

## Daily automation with systemd user timer

The systemd timer also uses the sibling `data` directory by default. Create an environment file only if you need to override paths:

```bash
mkdir -p ~/.config
cp deploy/gdex-observation-download.env.example ~/.config/gdex-observation-download.env
vi ~/.config/gdex-observation-download.env
```

Install the timer:

```bash
scripts/install_systemd_user.sh
systemctl --user status gdex-observation-download.timer
```

Run immediately:

```bash
systemctl --user start gdex-observation-download.service
journalctl --user -u gdex-observation-download.service -f
```

If the server does not keep user services alive after logout:

```bash
loginctl enable-linger "$USER"
```

## Cron alternative

Use `deploy/cron.example` if systemd timers are unavailable.

## TH-HPC4 login-node data-transfer mode

On the observed TH-HPC4 system, `debug` compute nodes return `Network is unreachable` for external GDEX URLs. Use the login node for this data-transfer task.

The TH-HPC4 login-node examples assume `down_obeservation/` and `data/` are sibling directories.

Start once after 30 minutes from the login node:

```bash
mkdir -p ../data/_logs
nohup bash -lc 'sleep 1800; cd "$HOME/down_obeservation"; bash deploy/th-hpc4-login-download.sh' >> ../data/_logs/login-delayed-submit.log 2>&1 &
```

Run the check every day at midnight:

```bash
crontab deploy/th-hpc4-login-crontab.example
```

The login-node wrapper uses a lock so the midnight check will skip itself if an earlier download is still running.

On the observed TH-HPC4 login node, direct DNS resolution fails but HTTPS through the site proxy can reach selected external data services. The login-node wrapper therefore keeps proxy variables by default and uses `config/datasets.th-hpc4.json`, which generates daily file URLs in this order:

- New priority products under `https://sdsc-cache.nationalresearchplatform.org:8443/ncar/gdex/d735000/`: `cris` (`crisf4.YYYYMMDD.tar.gz`) and `mtiasi`
- New PREPBUFR daily files under `https://sdsc-cache.nationalresearchplatform.org:8443/ncar/gdex/d337000/tarfiles/2026/`
- Final backfill checks for already mostly downloaded `mhs`, `atms`, `amsu-a`, and `gpsro` under `https://data.gdex.ucar.edu/d735000/`

The earlier OSDF director path is official, but the observed TH-HPC4 proxy returns `Tunnel connection failed: 403 Forbidden` for `osdf-director.osg-htc.org`. The TH-HPC4 config avoids that proxy block by using explicit cache or `data.gdex` file URLs directly.

The TH-HPC4 wrapper defaults to one download worker to match UCAR/GDEX guidance against simultaneous file downloads.

If an administrator later provides direct DNS/external access, you can bypass the proxy:

```bash
GDEX_BYPASS_PROXY=1 bash deploy/th-hpc4-login-download.sh
```

The TH-HPC4 wrapper defaults `GDEX_INSECURE_TLS=1` because the observed site proxy can present an expired certificate. To force normal certificate verification after the site proxy is fixed:

```bash
GDEX_INSECURE_TLS=0 bash deploy/th-hpc4-login-download.sh
```

Run the network diagnostic and send the output to the HPC administrator when proxy or DNS behavior changes:

```bash
bash deploy/th-hpc4-network-check.sh | tee ../data/_logs/network-check.log
```

If both proxy and direct modes fail, the required site-side fix is one of: an approved proxy that permits OSDF/GDEX HTTPS, DNS/external access on the login/data-transfer node, or a dedicated data-transfer node. The downloader cannot bypass a site firewall or proxy policy by itself.

The TH-HPC4 config does not depend on remote directory listings; it generates daily candidate URLs from file-name templates. If a product/date has not been published, HTTP 404 is recorded as `missing_remote` and the run continues.

If discovery reports zero candidates, verify that the TH-HPC4 config is current:

```bash
grep -E 'Generated|Found|missing_remote|failed' "$(ls -t ../data/_logs/login-download-*.log | head -1)"
```

The log should include lines such as `Generated ... date-template candidate files` and `Found ... candidate files`.

To test the TH-HPC4 priority cache paths manually on the login node:

```bash
mkdir -p ../data/_manual_wget_test
cd ../data/_manual_wget_test
wget --no-check-certificate -N https://sdsc-cache.nationalresearchplatform.org:8443/ncar/gdex/d735000/cris/2026/crisf4.20260101.tar.gz
wget --no-check-certificate -N https://sdsc-cache.nationalresearchplatform.org:8443/ncar/gdex/d735000/mtiasi/2026/mtiasi.20260101.tar.gz
wget --no-check-certificate -N https://sdsc-cache.nationalresearchplatform.org:8443/ncar/gdex/d337000/tarfiles/2026/prepbufr.20260101.nr.tar.gz
```

As of 2026-06-25, sample checks found 2026 files for CRIS (`crisf4`), METOP-2 IASI (`mtiasi`), and GDAS PREPBUFR under the `sdsc-cache` URLs above. HIRS4 and SEVIRI are intentionally not included in the TH-HPC4 config.

The legacy `yhbatch` debug template remains in `deploy/th-hpc4-gdex-download.sub.example`, but it should only be used if the selected compute partition has external network access.

## Repository push

After checking the files:

```bash
git remote add origin https://github.com/andyzhou4451/down_obeservation.git
git branch -M main
git push -u origin main
```

## Verification

```bash
python -m compileall gdex_downloader
python -m unittest discover -s tests
python -m gdex_downloader --config config/datasets.json --year 2026 --dry-run --limit 5
bash scripts/run_daily.sh --dry-run --max-pages 0
```
