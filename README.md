# down_obeservation

Daily, resumable downloader for 2026 UCAR GDEX observation data:

- `d735000`: AMSU-A, ATMS, HIRS4, MHS, GPSRO/GNSSRO, SEVIRI
- `d337000`: unrestricted GDAS PREPBUFR daily tar files

The downloader discovers files from the GDEX data-access pages and known UCAR data-directory seeds, filters for the requested products and year, skips existing completed files, and resumes interrupted `.part` downloads when the server supports HTTP ranges.

## Requirements

- Python 3.10+
- Network access from the server to `gdex.ucar.edu`, `data.rda.ucar.edu`, and/or `data.gdex.ucar.edu`
- Optional: `~/.netrc` or extra HTTP headers if your GDEX account/session requires authentication

No third-party Python packages are required.

If `python` is not on the server PATH, set `PYTHON_BIN` when using the daily wrapper:

```bash
PYTHON_BIN=/usr/bin/python3 scripts/run_daily.sh --dry-run --limit 5
```

## One-time dry run

```bash
python -m gdex_downloader \
  --config config/datasets.json \
  --year 2026 \
  --data-root /data/gdex \
  --dry-run
```

## Download / resume

```bash
python -m gdex_downloader \
  --config config/datasets.json \
  --year 2026 \
  --data-root /data/gdex \
  --state-dir /data/gdex_state \
  --log-dir /data/gdex_logs \
  --max-workers 2
```

Completed files are trusted and skipped by default. Interrupted downloads are kept as `.part` files and resumed on the next run when possible.

## Authentication options

If UCAR requires login for your account, keep credentials outside git.

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

Edit a local environment file:

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
```
