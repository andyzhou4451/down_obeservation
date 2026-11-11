# GridSat deployment validation

Verified on 2026-11-11. This records small-sample acceptance, not completion of
the full archive.

## Environment and inventory

- Login host: `th-hpc6-ln0`, account `yangjh18`.
- Python 3.12.7, NumPy 1.26.4, SciPy 1.13.1, netCDF4 1.7.2 from the existing
  science launcher. No packages installed.
- NOAA access succeeded through the site's normal login proxy with TLS
  verification enabled. Local Windows network access had an SSL EOF; production
  Python access on TH-HPC6 succeeded without disabling verification.
- Official year listings contain 31,656 selected files from 2016-01-01 00 UTC
  through 2026-10-31 21 UTC. The first full quarter contains 728 files. The
  requested November-December 2026 slots (488) are not listed, not successful
  downloads.

## Acceptance evidence

All 124 automated tests passed in the deployed Linux environment. Local Windows
testing also passed, with the platform's symlink-privilege test skipped. Python
compilation and Bash syntax checks passed; no external linter/typechecker was
installed. Tests cover quarterly sequencing, leap dates, source-list truncation,
HTTP range validation, absent validators, corrupt data, atomic NPY publication,
owned-file cleanup, locks, and Slurm reconciliation/failure handling.

The real-file comparator reproduced the approved legacy GridSat converter
exactly, including NaN locations, for 2016-01-01 00 UTC and 2026-01-01 00/03 UTC.
The 2026 comparison also passed independently on Windows. Comparison reports are
retained in the server's `data/gridsat-validation` directory.
The reference script SHA-256 is
`620fac891600a2b4370d27a6564be4db0814af5e96d3d1ac6c63f6e75dc9f568`.

| Isolated run | Published/verified NPY | Slurm job | Node | State / exit | Raw cache after validation |
| --- | --- | --- | --- | --- | --- |
| 2016-01-01 | 8 / 8 | 2445275 | node-5090-13 | COMPLETED / 0:0 | 0 files |
| 2026-01-01 | 8 / 8 | 2445283 | node-5090-13 | COMPLETED / 0:0 | 0 files |

Both workers requested 2 CPUs and 8 GiB, with no GPU. `status --verify` rechecked
all sixteen output hashes after cleanup. Restarting the completed 2016 pilot
performed no downloads and submitted no new Slurm job.

## Remaining runtime verification

The first full 728-file quarter and the entire archive were not completed by
these pilots. The production controller must validate each whole quarter before
removing its owned raw files. A long-running download is not a completed archive;
use `status` and the per-quarter receipts to check progress. No recurring schedule
is installed.
