#!/bin/sh
# Install the gateway on this machine (run as root). Does NOT start anything and does NOT touch CUPS:
#   1. sh deploy/install.sh          2. edit /etc/spoolsteward/env, add TLS cert
#   3. sh deploy/setup-cups.sh       4. systemctl enable --now spoolsteward
# Overridable for tests/packaging: PREFIX ETC UNITDIR SKIP_USER=1 SKIP_SYSTEMD=1 PYTHON
set -eu

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${PREFIX:-/opt/spoolsteward}"
ETC="${ETC:-/etc/spoolsteward}"
UNITDIR="${UNITDIR:-/etc/systemd/system}"
PYTHON="${PYTHON:-python3}"
SVC_USER=spoolsteward

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 12))' || { echo "need Python >= 3.12" >&2; exit 1; }
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null || { echo "python venv module missing (apt install python3-venv)" >&2; exit 1; }

if [ -z "${SKIP_USER:-}" ] && ! id "$SVC_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$SVC_USER"
    echo "created system user $SVC_USER"
fi
GROUP="$SVC_USER"; [ -n "${SKIP_USER:-}" ] && GROUP=root

mkdir -p "$PREFIX"
"$PYTHON" -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet "$SRC"
echo "installed into $PREFIX/venv"

mkdir -p "$ETC/tls"
chown root:"$GROUP" "$ETC" "$ETC/tls"; chmod 0750 "$ETC" "$ETC/tls"
if [ ! -e "$ETC/env" ]; then
    secret="$("$PREFIX/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    sed "s|__GENERATED__|$secret|" "$SRC/deploy/env.example" > "$ETC/env"
    chown root:"$GROUP" "$ETC/env"; chmod 0640 "$ETC/env"
    echo "wrote $ETC/env (edit RADIUS and TLS settings)"
else
    echo "kept existing $ETC/env"
fi

if [ -z "${SKIP_SYSTEMD:-}" ]; then
    install -m 0644 "$SRC/deploy/spoolsteward.service" "$UNITDIR/spoolsteward.service"
    systemctl daemon-reload
fi
cp "$SRC/README.md" "$PREFIX/README.md" 2>/dev/null || true
# the documentation the portal shows to administrators (/docs); replaced at every install so it matches the version
if [ -d "$SRC/docs" ]; then rm -rf "$PREFIX/docs"; cp -R "$SRC/docs" "$PREFIX/docs"; fi
echo "done. next: edit $ETC/env, put the TLS cert in $ETC/tls, run deploy/setup-cups.sh"
