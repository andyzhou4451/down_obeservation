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

## TH-HPC4 / GPU-system scheduler alternative

On TH-HPC4, submit jobs from the login node with `yhbatch`. The template uses the `debug` CPU partition because this downloader is network/IO-bound and resumes incomplete `.part` files:

```bash
mkdir -p ../data/_logs
yhbatch deploy/th-hpc4-gdex-download.sub.example
```

The TH-HPC4 example assumes `down_obeservation/` and `data/` are sibling directories.

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
