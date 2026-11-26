#!/usr/bin/env bash
set -euo pipefail

DOMAIN="${MIRROR_DOMAIN:-plugins.uniclipboard.app}"
INSTALL_DIR="${MIRROR_INSTALL_DIR:-/opt/unic-plugins-mirror}"
WEB_ROOT="${MIRROR_WEB_ROOT:-/opt/1panel/apps/openresty/openresty/www/sites/${DOMAIN}/index}"
MAX_BYTES="${MIRROR_MAX_BYTES:-2147483648}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root (e.g. sudo bash mirror/install.sh)." >&2
  exit 1
fi

command -v python3 >/dev/null 2>&1 || {
  echo "python3 is required." >&2
  exit 1
}

echo "==> Installing mirror sync script to ${INSTALL_DIR}"
install -d -m 0755 "${INSTALL_DIR}" "${WEB_ROOT}"
install -m 0755 "${SCRIPT_DIR}/sync_mirror.py" "${INSTALL_DIR}/sync_mirror.py"

echo "==> Installing systemd unit and timer"
sed \
  -e "s|/opt/unic-plugins-mirror|${INSTALL_DIR}|g" \
  -e "s|/opt/1panel/apps/openresty/openresty/www/sites/plugins.uniclipboard.app/index|${WEB_ROOT}|g" \
  -e "s|plugins.uniclipboard.app|${DOMAIN}|g" \
  -e "s|2147483648|${MAX_BYTES}|g" \
  "${SCRIPT_DIR}/systemd/unic-plugins-mirror.service" \
  > /etc/systemd/system/unic-plugins-mirror.service

install -m 0644 "${SCRIPT_DIR}/systemd/unic-plugins-mirror.timer" \
  /etc/systemd/system/unic-plugins-mirror.timer

systemctl daemon-reload
systemctl enable --now unic-plugins-mirror.timer

echo "==> Running initial mirror sync"
systemctl start unic-plugins-mirror.service
systemctl status --no-pager unic-plugins-mirror.timer || true

echo ""
echo "Done. Next steps in 1Panel for https://${DOMAIN}:"
echo "  1. Point DNS A record for ${DOMAIN} directly to this server (no Cloudflare proxy)."
echo "  2. Create static site '${DOMAIN}' in 1Panel (root: ${WEB_ROOT}) and enable HTTPS."
echo "  3. Add the location rules from mirror/openresty/plugins.uniclipboard.app.conf to the site config."
echo "  4. Verify:"
echo "       curl -fsSL https://${DOMAIN}/health.json"
echo "       curl -fsSL https://${DOMAIN}/registry.json | head"
