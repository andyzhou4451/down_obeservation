# down_obeservation

Daily, resumable downloader for 2026 UCAR GDEX observation data:

- `d735000`: AMSU-A, ATMS, HIRS4, MHS, GPSRO/GNSSRO, SEVIRI
- `d337000`: unrestricted GDAS PREPBUFR daily tar files

The downloader discovers files from the OSDF/XrdHTTP GDEX listings, filters for the requested products and year, skips existing completed files, and resumes interrupted `.part` downloads when the server supports HTTP ranges.

## Requirements

- Python 3.10+
- Network access from the server to `osdf-director.osg-htc.org` and the OSDF cache host it redirects to for each file
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

On the observed TH-HPC4 login node, direct DNS resolution fails but HTTPS through the site proxy can reach external data services. The login-node wrapper therefore keeps proxy variables by default and uses `config/datasets.th-hpc4.json`, which points at the official OSDF director download listings:

- `https://osdf-director.osg-htc.org/ncar/gdex/d735000/`
- `https://osdf-director.osg-htc.org/ncar/gdex/d337000/tarfiles/2026/`

The director may redirect individual files to a cache host on port `8443`. Do not hard-code a cache host in the TH-HPC4 config; use the director URL, matching the official UCAR/GDEX `wget -N --no-check-certificate` example.

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

The TH-HPC4 wrapper logs index-page link samples by default. If discovery reports zero candidates, inspect the latest log for `Index page links` lines:

```bash
grep 'Index page links' "$(ls -t ../data/_logs/login-download-*.log | head -1)"
```

The log includes a `relevant=[...]` field. With the OSDF configuration, it should show product directories or `.tar.gz` files such as `1bamua.20260623.tar.gz`, `atms.20260623.tar.gz`, `gpsro.20260623.tar.gz`, or `prepbufr.20260623.nr.tar.gz`. If it does not, run `deploy/th-hpc4-network-check.sh` and inspect whether the proxy can reach the OSDF hosts.

To test the official `wget` path manually on the login node:

```bash
mkdir -p ../data/_manual_wget_test
cd ../data/_manual_wget_test
wget --no-check-certificate -N https://osdf-director.osg-htc.org/ncar/gdex/d735000/1bmhs/2026/1bmhs.20260101.tar.gz
```

As of 2026-06-24, the OSDF `1bhrs4/` and SEVIRI-related `sevcsr/`/`airsev/` listings do not expose 2026 subdirectories. They remain configured as product-root seeds so future 2026 files will be picked up automatically when they appear.

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
