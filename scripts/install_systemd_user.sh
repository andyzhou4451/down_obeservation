#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-${HOME}/.config/systemd/user}"

mkdir -p "${SYSTEMD_USER_DIR}"
sed "s#__APP_DIR__#${APP_DIR//\\/\\\\}#g" \
  "${APP_DIR}/deploy/gdex-observation-download.service" \
  > "${SYSTEMD_USER_DIR}/gdex-observation-download.service"
cp "${APP_DIR}/deploy/gdex-observation-download.timer" \
  "${SYSTEMD_USER_DIR}/gdex-observation-download.timer"

chmod +x "${APP_DIR}/scripts/run_daily.sh"
systemctl --user daemon-reload
systemctl --user enable --now gdex-observation-download.timer

echo "Installed user timer. Check it with:"
echo "  systemctl --user status gdex-observation-download.timer"
