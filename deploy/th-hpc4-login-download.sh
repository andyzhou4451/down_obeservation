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
    set +u
    # Some site profile scripts reference optional variables under nounset.
    . /etc/profile >/dev/null 2>&1 || echo "warning=failed_to_source_etc_profile"
    set -u
  fi

  module add python/3.10 2>/dev/null || true
  module add python 2>/dev/null || true

  if [ "${GDEX_BYPASS_PROXY:-0}" = "1" ]; then
    if env | grep -Eiq '^(https?_proxy|all_proxy)='; then
      echo "proxy_env_detected=1"
    else
      echo "proxy_env_detected=0"
    fi
    export no_proxy="osdfcache.ligo.caltech.edu,osdf-director.osg-htc.org,gdex.ucar.edu,data.rda.ucar.edu,data.gdex.ucar.edu,.ucar.edu${no_proxy:+,${no_proxy}}"
    export NO_PROXY="osdfcache.ligo.caltech.edu,osdf-director.osg-htc.org,gdex.ucar.edu,data.rda.ucar.edu,data.gdex.ucar.edu,.ucar.edu${NO_PROXY:+,${NO_PROXY}}"
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
    echo "proxy_bypass=1"
  else
    echo "proxy_bypass=0"
  fi

  export PYTHON_BIN="${PYTHON_BIN:-python3}"
  export GDEX_CONFIG="${GDEX_CONFIG:-${APP_DIR}/config/datasets.th-hpc4.json}"
  export DATA_DIR
  export GDEX_YEAR="${GDEX_YEAR:-2026}"
  export GDEX_MAX_WORKERS="${GDEX_MAX_WORKERS:-2}"
  export GDEX_INSECURE_TLS="${GDEX_INSECURE_TLS:-1}"
  export GDEX_LOG_INDEX_LINKS="${GDEX_LOG_INDEX_LINKS:-1}"
  export GDEX_INDEX_LINK_SAMPLE="${GDEX_INDEX_LINK_SAMPLE:-200}"
  echo "config=${GDEX_CONFIG}"
  echo "insecure_tls=${GDEX_INSECURE_TLS}"
  echo "log_index_links=${GDEX_LOG_INDEX_LINKS}"

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
