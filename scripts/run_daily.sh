#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DATA_DIR="${DATA_DIR:-$(cd "${APP_DIR}/.." && pwd)/data}"

GDEX_CONFIG="${GDEX_CONFIG:-${APP_DIR}/config/datasets.json}"
GDEX_YEAR="${GDEX_YEAR:-2026}"
GDEX_DATA_ROOT="${GDEX_DATA_ROOT:-${DATA_DIR}}"
GDEX_STATE_DIR="${GDEX_STATE_DIR:-${DATA_DIR}/_state}"
GDEX_LOG_DIR="${GDEX_LOG_DIR:-${DATA_DIR}/_logs}"
GDEX_MAX_WORKERS="${GDEX_MAX_WORKERS:-2}"
GDEX_MAX_DEPTH="${GDEX_MAX_DEPTH:-8}"
GDEX_MAX_PAGES="${GDEX_MAX_PAGES:-20000}"
GDEX_INSECURE_TLS="${GDEX_INSECURE_TLS:-0}"
GDEX_LOG_INDEX_LINKS="${GDEX_LOG_INDEX_LINKS:-0}"
GDEX_INDEX_LINK_SAMPLE="${GDEX_INDEX_LINK_SAMPLE:-20}"

EXTRA_ARGS=()
if [ "${GDEX_INSECURE_TLS}" = "1" ]; then
  EXTRA_ARGS+=(--insecure-tls)
fi
if [ "${GDEX_LOG_INDEX_LINKS}" = "1" ]; then
  EXTRA_ARGS+=(--log-index-links --index-link-sample "${GDEX_INDEX_LINK_SAMPLE}")
fi

cd "${APP_DIR}"

exec "${PYTHON_BIN}" -m gdex_downloader \
  --config "${GDEX_CONFIG}" \
  --year "${GDEX_YEAR}" \
  --data-root "${GDEX_DATA_ROOT}" \
  --state-dir "${GDEX_STATE_DIR}" \
  --log-dir "${GDEX_LOG_DIR}" \
  --max-workers "${GDEX_MAX_WORKERS}" \
  --max-depth "${GDEX_MAX_DEPTH}" \
  --max-pages "${GDEX_MAX_PAGES}" \
  "${EXTRA_ARGS[@]}" \
  "$@"
