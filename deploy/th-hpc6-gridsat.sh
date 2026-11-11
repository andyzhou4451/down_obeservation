#!/bin/bash
# One-shot login-node controller; the operator may launch this under nohup.
# Locking, download snapshots and quarter caches belong to the Python controller.

set +u
declare -A configured_proxy=()
for name in http_proxy https_proxy all_proxy no_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY; do
  if [[ -v ${name} ]]; then
    configured_proxy["${name}"]="${!name}"
  fi
done
if [[ -f /etc/profile ]]; then
  . /etc/profile >/dev/null || printf 'warning=failed_to_source_etc_profile\n' >&2
fi
for name in "${!configured_proxy[@]}"; do
  printf -v "${name}" '%s' "${configured_proxy[${name}]}"
  export "${name}"
done
unset configured_proxy
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP_DIR="$(cd "${APP_DIR}" && pwd)"
DATA_ROOT="${DATA_ROOT:-$(dirname "${APP_DIR}")/data/gridsat-b1}"
PYTHON_LAUNCHER="${PYTHON_LAUNCHER:-/fs6/home/yangjh18/gdas_healda_20260916/runtime/science-python.sh}"
export APP_DIR DATA_ROOT PYTHON_LAUNCHER

case "${1:-}" in
  run|status|process) action="$1"; shift ;;
  ''|-*) action=run ;;
  *) printf 'usage: %s [run|status|process] [arguments...]\n' "$0" >&2; exit 2 ;;
esac

args=("${action}" --data-root "${DATA_ROOT}")
case "${action}" in
  run) args+=(--executor "${EXECUTOR:-slurm}" --workers "${WORKERS:-2}" --python-launcher "${PYTHON_LAUNCHER}") ;;
  process) args+=(--workers "${WORKERS:-2}") ;;
esac
preview=0
for arg in "$@"; do
  case "${arg}" in
    --dry-run|--help|-h) preview=1 ;;
  esac
done
cd "${APP_DIR}"
if [[ ${action} == status || ${preview} == 1 ]]; then
  exec "${PYTHON_LAUNCHER}" -m gdex_downloader.gridsat "${args[@]}" "$@"
fi

mkdir -p "${DATA_ROOT}/_logs"
log_file="${DATA_ROOT}/_logs/gridsat-${action}-$(date +%Y%m%dT%H%M%S)-$$.log"
exec > >(tee -a "${log_file}") 2>&1
printf 'started_at=%s\ncontroller_pid=%s\nhost=%s\napp_dir=%s\ndata_root=%s\nlog_file=%s\n' \
  "$(date -Is)" "$$" "$(hostname)" "${APP_DIR}" "${DATA_ROOT}" "${log_file}"
exec "${PYTHON_LAUNCHER}" -m gdex_downloader.gridsat "${args[@]}" "$@"
