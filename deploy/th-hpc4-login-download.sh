#!/usr/bin/env bash
# Run on the TH-HPC4 login node for data-transfer downloads.
# This is intentionally not submitted with yhbatch/yhrun because observed debug
# compute nodes cannot reach the external network.

set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_DIR="${DATA_DIR:-$(cd "${APP_DIR}/.." && pwd)/data}"
LOG_DIR="${DATA_DIR}/_logs"
STATE_DIR="${DATA_DIR}/_state"
RUN_ID="$(date +%Y%m%dT%H%M%S)"
LOCK_FILE="${STATE_DIR}/th-hpc4-login-download.lock"

mkdir -p "${LOG_DIR}" "${STATE_DIR}"

cd "${APP_DIR}"

run_download() {
  exec >> "${LOG_DIR}/login-download-${RUN_ID}.log" 2>&1
  echo "started_at=$(date -Is)"
  echo "host=$(hostname)"
  echo "workdir=$(pwd)"
  echo "data_dir=${DATA_DIR}"

  if [ -f /etc/profile ]; then
    # Cron often starts with a minimal environment; this restores module setup on HPC systems.
    . /etc/profile
  fi

  module add python/3.10 2>/dev/null || true
  module add python 2>/dev/null || true

  export PYTHON_BIN="${PYTHON_BIN:-python3}"
  export DATA_DIR
  export GDEX_YEAR="${GDEX_YEAR:-2026}"
  export GDEX_MAX_WORKERS="${GDEX_MAX_WORKERS:-2}"

  bash scripts/run_daily.sh

  echo "finished_at=$(date -Is)"
}

if command -v flock >/dev/null 2>&1; then
  exec 9>"${LOCK_FILE}"
  if ! flock -n 9; then
    echo "$(date -Is) another login-node downloader is already running" >> "${LOG_DIR}/login-download-skip.log"
    exit 0
  fi
  run_download
else
  run_download
fi
