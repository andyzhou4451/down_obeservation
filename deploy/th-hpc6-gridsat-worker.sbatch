#!/bin/bash
# SlurmScheduler passes APP_DIR, DATA_ROOT, PYTHON_LAUNCHER, QUARTER, WORKERS
# as five positional arguments, avoiding shell quoting and --export separators.
# A direct invocation may instead supply these as environment variables.
#SBATCH --job-name=gridsat-quarter
#SBATCH --partition=5090
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=2-00:00:00
#SBATCH --no-requeue

set -euo pipefail
if (( $# != 0 && $# != 5 )); then
  printf 'expected APP_DIR DATA_ROOT PYTHON_LAUNCHER QUARTER WORKERS\n' >&2
  exit 2
fi
APP_DIR="${1:-${APP_DIR:?APP_DIR must point to the repository root}}"
DATA_ROOT="${2:-${DATA_ROOT:?DATA_ROOT must be set}}"
PYTHON_LAUNCHER="${3:-${PYTHON_LAUNCHER:-/fs6/home/yangjh18/gdas_healda_20260916/runtime/science-python.sh}}"
QUARTER="${4:-${QUARTER:?QUARTER must be set}}"
WORKERS="${5:-${WORKERS:-2}}"
if [[ ! ${QUARTER} =~ ^[0-9]{4}Q[1-4]$ || ! ${WORKERS} =~ ^[12]$ ]]; then
  printf 'quarter must look like 2016Q1; workers must be 1 or 2\n' >&2
  exit 2
fi
export APP_DIR DATA_ROOT PYTHON_LAUNCHER
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1
cd "${APP_DIR}"
printf 'started_at=%s\nworker_pid=%s\njob_id=%s\nquarter=%s\n' \
  "$(date -Is)" "$$" "${SLURM_JOB_ID:-local}" "${QUARTER}"
exec "${PYTHON_LAUNCHER}" -m gdex_downloader.gridsat process \
  --data-root "${DATA_ROOT}" --quarter "${QUARTER}" --workers "${WORKERS}"
