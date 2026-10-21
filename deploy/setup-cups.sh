#!/bin/sh
# Make CUPS a private backend of the gateway (run as root, after install.sh).
#  - /etc/cups/cupsd.conf  -> docker/stack/cups/cupsd.conf (original saved once as cupsd.conf.pre-gateway)
#  - cups.socket           -> unix socket only, mode 0660, group spoolsteward; no TCP listener
# Restarts CUPS. Printers (queues) are kept. Revert: sh deploy/setup-cups.sh --revert
set -eu

SRC="$(cd "$(dirname "$0")/.." && pwd)"
CONF=/etc/cups/cupsd.conf
BACKUP=/etc/cups/cupsd.conf.pre-gateway
DROPIN=/etc/systemd/system/cups.socket.d

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
getent group spoolsteward >/dev/null || { echo "group spoolsteward missing: run deploy/install.sh first" >&2; exit 1; }

if [ "${1:-}" = "--revert" ]; then
    [ -e "$BACKUP" ] || { echo "no backup at $BACKUP" >&2; exit 1; }
    cp "$BACKUP" "$CONF"
    rm -rf "$DROPIN"
    systemctl daemon-reload
    systemctl restart cups.socket cups.service
    echo "reverted to the original CUPS configuration"
    exit 0
fi

CUPSD="$(command -v cupsd || ls /usr/sbin/cupsd 2>/dev/null || true)"
[ -n "$CUPSD" ] || { echo "cupsd not found" >&2; exit 1; }
# validate a COPY: cupsd chowns/chmods the file it reads and creates ppd/ next to it
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp "$SRC/docker/stack/cups/cupsd.conf" "$TMP/cupsd.conf"
"$CUPSD" -t -c "$TMP/cupsd.conf" >/dev/null 2>&1 || { echo "cupsd.conf failed validation" >&2; exit 1; }

[ -e "$BACKUP" ] || cp -p "$CONF" "$BACKUP"
install -m 0644 "$SRC/docker/stack/cups/cupsd.conf" "$CONF"
mkdir -p "$DROPIN"
install -m 0644 "$SRC/deploy/cups.socket.d/gateway.conf" "$DROPIN/gateway.conf"
systemctl daemon-reload
systemctl restart cups.socket cups.service

sleep 1
if ss -ltn 2>/dev/null | grep -qE 'LISTEN.*[:.]631\b'; then
    echo "WARNING: something still listens on TCP 631 (cups-browsed? another cups.socket drop-in?)" >&2
fi
ls -l /run/cups/cups.sock
echo "CUPS is now a private backend. Start the gateway: systemctl enable --now spoolsteward"
