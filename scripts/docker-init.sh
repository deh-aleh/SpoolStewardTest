#!/bin/sh
# One-time setup for the docker stack. Writes only inside this project directory:
#   .env                 ALL the settings (version, host user id, port, printers, login, quotas...), made from
#                        docker/env.example with a generated session secret (mode 600: it holds secrets)
#   config/tls/          self-signed certificate (skipped if you put your own fullchain.pem/privkey.pem there)
#   data/                audit archive and backups
# An existing .env is never touched. Needs only a POSIX shell, sed and openssl (no Python).
# usage: scripts/docker-init.sh [NAME-OR-IP ...]   names/IPs clients will use to reach this server
set -eu
cd "$(dirname "$0")/.."
mkdir -p config/tls data/audit-archive data/backups

if [ ! -e .env ]; then
    [ -f docker/env.example ] || { echo "docker/env.example not found: run this from the SpoolSteward folder" >&2; exit 1; }
    # the version: the file VERSION of the release package, or pyproject.toml in a git clone
    version="$(cat VERSION 2>/dev/null || sed -n 's/^version *= *"\([0-9][0-9.]*\(b[0-9][0-9]*\)\{0,1\}\)".*/\1/p' pyproject.toml 2>/dev/null | head -1)"
    version="$(printf '%s' "$version" | sed 's/^\([0-9][0-9.]*\)b\([0-9][0-9]*\)$/\1-beta.\2/')"   # pyproject says 0.3.1b1, the images are 0.3.1-beta.1
    [ -n "$version" ] || { echo "can not tell which version this is (no VERSION file, no pyproject.toml)" >&2; exit 1; }
    secret="$(openssl rand -base64 48 | tr -d '\n=' | tr '+/' '-_')"
    here="$(pwd)"
    umask 077
    # '|' is not in any of the values (paths of this folder could hold '/' or '&': escape the replacement)
    esc() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }
    sed -e "s|__VERSION__|$(esc "$version")|g" -e "s|__PUID__|$(id -u)|g" -e "s|__PGID__|$(id -g)|g" \
        -e "s|__PROJECT_DIR__|$(esc "$here")|g" -e "s|__GENERATED__|$secret|g" docker/env.example > .env
    chmod 600 .env
    echo "created .env (version $version). Set the time zone and, if you use RADIUS, SPOOLSTEWARD_RADIUS_HOST / SPOOLSTEWARD_RADIUS_SECRET in it."
fi

if [ ! -e config/tls/privkey.pem ]; then
    san="DNS:localhost,IP:127.0.0.1"
    for n in "$@"; do
        case "$n" in
            *[!0-9.]*) san="$san,DNS:$n" ;;
            *)         san="$san,IP:$n" ;;
        esac
    done
    cn="${1:-localhost}"
    openssl req -x509 -newkey rsa:3072 -nodes -days 825 \
        -keyout config/tls/privkey.pem -out config/tls/fullchain.pem \
        -subj "/CN=$cn" -addext "subjectAltName=$san" 2>/dev/null
    chmod 600 config/tls/privkey.pem
    echo "created self-signed certificate for: $san"
    echo "(replace config/tls/fullchain.pem + privkey.pem with your CA-signed pair when you have it)"
fi
echo "next: edit .env (RADIUS is optional), then: docker compose up -d"
echo "then read the one-time code:  docker compose logs gateway | grep 'FIRST RUN'   and open https://<server>:8631/setup"
