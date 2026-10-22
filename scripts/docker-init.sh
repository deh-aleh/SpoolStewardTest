#!/bin/sh
# One-time setup for the docker stack. Writes only inside this project directory:
#   .env                 ALL the settings (version, host user id, port, printers, login, quotas...), made from
#                        docker/env.example with a generated session secret (mode 600: it holds secrets)
#   config/tls/          self-signed certificate (skipped if you put your own fullchain.pem/privkey.pem there)
#   data/                audit archive and backups
# An existing .env is never touched (a missing or weak SPOOLSTEWARD_SECRET_KEY is only reported).
# Needs only a POSIX shell, sed and openssl (no Python).
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
    # 64 characters (the minimum the gateway accepts), letters, digits, '-' and '_' only: nothing that .env or docker
    # compose would read in a special way
    secret="$(openssl rand -base64 48 | tr -d '\n=' | tr '+/' '-_')"
    here="$(pwd)"
    umask 077
    # '|' is not in any of the values (paths of this folder could hold '/' or '&': escape the replacement)
    esc() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }
    sed -e "s|__VERSION__|$(esc "$version")|g" -e "s|__PUID__|$(id -u)|g" -e "s|__PGID__|$(id -g)|g" \
        -e "s|__PROJECT_DIR__|$(esc "$here")|g" -e "s|__GENERATED__|$secret|g" docker/env.example > .env
    chmod 600 .env
    echo "created .env (version $version). Set the time zone and, if you use RADIUS, SPOOLSTEWARD_RADIUS_HOST / SPOOLSTEWARD_RADIUS_SECRET in it."
else
    # report, never fix: the file is the user's. Without a key file names are stored in clear and sessions end at every
    # restart; the placeholder or a short key stops the gateway. The value itself is never printed.
    help="See \"No secret key is set\" in docs/en/troubleshooting.md."
    if ! grep -q '^SPOOLSTEWARD_SECRET_KEY=' .env; then
        echo "WARNING: .env has no SPOOLSTEWARD_SECRET_KEY line (the file was left untouched): the file names in the audit are stored without encryption and everybody signs in again after every restart. Add the line SPOOLSTEWARD_SECRET_KEY= with the output of 'openssl rand -hex 32' after the '=', then run docker compose up -d. $help" >&2
    else
        key="$(sed -n 's/^SPOOLSTEWARD_SECRET_KEY=//p' .env | tail -n 1 | tr -d "\"' ")"
        case "$key" in
            [Nn][Oo][Nn][Ee]) echo "WARNING: SPOOLSTEWARD_SECRET_KEY in .env is 'none': file names are not encrypted and everybody signs in again after every restart. Replace it with a long random value (openssl rand -hex 32). $help" >&2 ;;
            __*__) echo "WARNING: SPOOLSTEWARD_SECRET_KEY in .env is still the placeholder of docker/env.example: the gateway refuses to start. Replace it with the output of 'openssl rand -hex 32'. $help" >&2 ;;
            *) [ "${#key}" -ge 64 ] || echo "WARNING: SPOOLSTEWARD_SECRET_KEY in .env is shorter than 64 characters: the gateway refuses to start. Replace it with the output of 'openssl rand -hex 32'. $help" >&2 ;;
        esac
    fi
    echo "kept the existing .env"
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
    if [ "${#cn}" -gt 64 ]; then   # OpenSSL refuses a common name longer than 64 characters
        cn=localhost
        echo "the name is longer than 64 characters: the certificate's main name is localhost; the long name is in its alternative names"
    fi
    if ! err="$(openssl req -x509 -newkey rsa:3072 -nodes -days 825 \
        -keyout config/tls/privkey.pem -out config/tls/fullchain.pem \
        -subj "/CN=$cn" -addext "subjectAltName=$san" 2>&1)"; then
        echo "can not create the TLS certificate: $err" >&2
        echo "Fix the cause (is openssl installed? are the names valid?) or put your own fullchain.pem and privkey.pem in config/tls/ and run this again." >&2
        exit 1
    fi
    chmod 600 config/tls/privkey.pem
    echo "created self-signed certificate for: $san"
    echo "(replace config/tls/fullchain.pem + privkey.pem with your CA-signed pair when you have it)"
fi
echo "next: edit .env (RADIUS is optional), then: docker compose up -d"
echo "then read the one-time code:  docker compose logs gateway | grep 'FIRST RUN'   and open https://<server>:8631/setup"
