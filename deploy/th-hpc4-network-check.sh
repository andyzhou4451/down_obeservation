#!/usr/bin/env bash
# Diagnose TH-HPC4 external-network access for UCAR/GDEX downloads.
# This script does not print proxy values, only whether proxy variables exist.

set -u

HOSTS="gdex.ucar.edu data.rda.ucar.edu data.gdex.ucar.edu"
URLS="https://gdex.ucar.edu/datasets/d735000/dataaccess/ https://data.rda.ucar.edu/ds337.0/ https://data.gdex.ucar.edu/d337000/"

echo "started_at=$(date -Is)"
echo "host=$(hostname)"
echo "workdir=$(pwd)"

if env | grep -Eiq '^(https?_proxy|all_proxy)='; then
  echo "proxy_env_detected=1"
else
  echo "proxy_env_detected=0"
fi

echo "== DNS direct resolution =="
for host in ${HOSTS}; do
  if command -v getent >/dev/null 2>&1; then
    getent hosts "${host}" >/dev/null 2>&1 && echo "dns_ok=${host}" || echo "dns_failed=${host}"
  elif command -v nslookup >/dev/null 2>&1; then
    nslookup "${host}" >/dev/null 2>&1 && echo "dns_ok=${host}" || echo "dns_failed=${host}"
  else
    echo "dns_check_unavailable=${host}"
  fi
done

echo "== HTTPS with current environment =="
for url in ${URLS}; do
  if command -v curl >/dev/null 2>&1; then
    code="$(curl -k -L -sS -o /dev/null -w '%{http_code}' --connect-timeout 15 --max-time 30 "${url}" 2>&1)"
    status=$?
    echo "curl_env status=${status} code=${code} url=${url}"
  else
    python3 - <<PY
import urllib.request
url = "${url}"
try:
    with urllib.request.urlopen(url, timeout=30) as response:
        print(f"python_env ok code={response.status} url={url}")
except Exception as exc:
    print(f"python_env failed error={exc!r} url={url}")
PY
  fi
done

echo "== HTTPS with proxy variables cleared =="
for url in ${URLS}; do
  if command -v curl >/dev/null 2>&1; then
    code="$(env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY curl -k -L -sS -o /dev/null -w '%{http_code}' --connect-timeout 15 --max-time 30 "${url}" 2>&1)"
    status=$?
    echo "curl_direct status=${status} code=${code} url=${url}"
  else
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY python3 - <<PY
import urllib.request
url = "${url}"
try:
    with urllib.request.urlopen(url, timeout=30) as response:
        print(f"python_direct ok code={response.status} url={url}")
except Exception as exc:
    print(f"python_direct failed error={exc!r} url={url}")
PY
  fi
done

echo "finished_at=$(date -Is)"
