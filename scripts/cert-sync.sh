#!/bin/sh
# Copy the TLS certificate from the machine that renews it (Nginx Proxy Manager, certbot, ...) into config/tls and
# restart the gateway when it changed. Meant to be run by cron on the machine where SpoolSteward runs.
#   scripts/cert-sync.sh             do it
#   scripts/cert-sync.sh --dry-run   download and check, change nothing
#
# What it does, in order:
#   1. downloads the two files with rsync over ssh into a private temporary folder
#   2. checks them: the certificate is not expired and the private key belongs to it
#   3. if they are the same as the ones in use: stops here (nothing to do, nothing restarted)
#   4. otherwise puts the old pair aside (config/tls/previous/), installs the new one, restarts the gateway
#   5. writes the outcome to config/tls/sync-status.json: the portal shows a notice when it says "failed"
# It never touches the files in use before the new pair has been checked.
#
# Settings are read from .env (see docker/env.example, section "Certificate synchronisation"):
#   CERT_SYNC_HOST         machine that holds the certificate (name or address)         [required]
#   CERT_SYNC_USER         ssh user on that machine (one that can only read: docs/en/certificates.md)   [required]
#   CERT_SYNC_KEY          full path of the ssh private key of this machine             [required]
#   CERT_SYNC_REMOTE_DIR   full path of the folder on that machine that holds the two files    [required]
#   CERT_SYNC_PORT         ssh port                                                     [22]
#   CERT_SYNC_CERT_FILE    name of the certificate file (full chain)                    [fullchain.pem]
#   CERT_SYNC_KEY_FILE     name of the private key file                                 [privkey.pem]
# Needs: rsync, ssh, openssl, docker compose.
set -eu
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
TLS="$ROOT/config/tls"
STATUS="$TLS/sync-status.json"
dry=0
case "${1:-}" in
    --dry-run) dry=1 ;;
    "")        ;;
    *)         echo "usage: $0 [--dry-run]" >&2; exit 2 ;;
esac

envget() { # value of NAME in .env, without the quotes around it
    sed -n "s/^$1=//p" .env 2>/dev/null | head -1 | sed "s/^[\"']//; s/[\"']\$//"
}

now() { date -u +%Y-%m-%dT%H:%M:%S+00:00; }

write_status() { # ok(true|false) changed(true|false) message
    msg="$(printf '%s' "$3" | tr '\n\r\t' '   ' | sed 's/\\/\\\\/g; s/"/\\"/g' | cut -c1-300)"
    mkdir -p "$TLS"
    printf '{"ts": "%s", "ok": %s, "changed": %s, "message": "%s"}\n' "$(now)" "$1" "$2" "$msg" > "$STATUS.tmp"
    chmod 644 "$STATUS.tmp"
    mv "$STATUS.tmp" "$STATUS"
}

fail() {
    echo "cert-sync: FAILED: $1" >&2
    [ "$dry" = 1 ] || write_status false false "$1"
    exit 1
}

[ -e .env ] || { echo ".env not found: run this from the SpoolSteward folder (scripts/docker-init.sh creates it)" >&2; exit 1; }
host="$(envget CERT_SYNC_HOST)"; user="$(envget CERT_SYNC_USER)"; key="$(envget CERT_SYNC_KEY)"; rdir="$(envget CERT_SYNC_REMOTE_DIR)"
port="$(envget CERT_SYNC_PORT)"; port="${port:-22}"
certname="$(envget CERT_SYNC_CERT_FILE)"; certname="${certname:-fullchain.pem}"
keyname="$(envget CERT_SYNC_KEY_FILE)"; keyname="${keyname:-privkey.pem}"

[ -n "$host" ] && [ -n "$user" ] && [ -n "$key" ] && [ -n "$rdir" ] \
    || fail "set CERT_SYNC_HOST, CERT_SYNC_USER, CERT_SYNC_KEY and CERT_SYNC_REMOTE_DIR in .env"
[ -r "$key" ] || fail "the ssh key $key does not exist or can not be read by this user"
case "$port" in *[!0-9]*|"") fail "CERT_SYNC_PORT must be a number" ;; esac
# the names end up in a command: plain file names only
case "$certname$keyname" in */*|*" "*|*\;*|*\&*|*\|*|*\$*|*\`*) fail "CERT_SYNC_CERT_FILE / CERT_SYNC_KEY_FILE must be plain file names" ;; esac
for tool in rsync ssh openssl; do command -v "$tool" >/dev/null 2>&1 || fail "$tool is not installed"; done

mkdir -p "$TLS" "$ROOT/config/ssh"
# one run at a time (a stale lock older than an hour is ignored)
lock="$ROOT/config/.cert-sync.lock"
if ! mkdir "$lock" 2>/dev/null; then
    if [ -n "$(find "$lock" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then rmdir "$lock" && mkdir "$lock"; else
        echo "cert-sync: another run is in progress" >&2; exit 1; fi
fi
work="$(mktemp -d "$ROOT/config/.cert-sync.XXXXXX")"
trap 'rm -rf "$work"; rmdir "$lock" 2>/dev/null || true' EXIT INT TERM
chmod 700 "$work"

# the host key is remembered in config/ssh/known_hosts on the first run and checked on every later one
rsh="ssh -i $key -p $port -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$ROOT/config/ssh/known_hosts"
rdir="${rdir%/}"
for f in "$certname:fullchain.pem" "$keyname:privkey.pem"; do
    # -L: Nginx Proxy Manager and certbot keep live/<name>/fullchain.pem as a link to archive/...: copy what it points to
    if ! out="$(rsync -L -e "$rsh" --timeout=30 "$user@$host:$rdir/${f%%:*}" "$work/${f##*:}" 2>&1)"; then
        fail "could not download ${f%%:*} from $host: $out"
    fi
done
chmod 600 "$work/privkey.pem"

openssl x509 -in "$work/fullchain.pem" -noout >/dev/null 2>&1 || fail "$certname is not a certificate (PEM)"
openssl x509 -in "$work/fullchain.pem" -noout -checkend 0 >/dev/null 2>&1 || fail "the certificate on $host is already expired: it was not installed"
a="$(openssl x509 -in "$work/fullchain.pem" -noout -pubkey 2>/dev/null | openssl sha256 2>/dev/null)" || a=""
b="$(openssl pkey -in "$work/privkey.pem" -pubout 2>/dev/null | openssl sha256 2>/dev/null)" || b=""
[ -n "$a" ] && [ "$a" = "$b" ] || fail "the private key ($keyname) does not belong to the certificate ($certname): nothing was installed"

if cmp -s "$work/fullchain.pem" "$TLS/fullchain.pem" && cmp -s "$work/privkey.pem" "$TLS/privkey.pem"; then
    echo "cert-sync: the certificate is already up to date"
    [ "$dry" = 1 ] || write_status true false "up to date"
    exit 0
fi
if [ "$dry" = 1 ]; then
    echo "cert-sync: dry run: a new certificate is available ($(openssl x509 -in "$work/fullchain.pem" -noout -enddate)); nothing was changed"
    exit 0
fi

mkdir -p "$TLS/previous"
for f in fullchain.pem privkey.pem; do
    [ -e "$TLS/$f" ] && cp -p "$TLS/$f" "$TLS/previous/$f"
done
chmod 700 "$TLS/previous"
uid="$(envget PUID)"; gid="$(envget PGID)"
if [ "$(id -u)" = 0 ] && [ -n "$uid" ]; then chown "$uid:${gid:-$uid}" "$work/fullchain.pem" "$work/privkey.pem"; fi
chmod 644 "$work/fullchain.pem"
mv "$work/privkey.pem" "$TLS/privkey.pem"
mv "$work/fullchain.pem" "$TLS/fullchain.pem"
echo "cert-sync: new certificate installed ($(openssl x509 -in "$TLS/fullchain.pem" -noout -enddate))"

if ! out="$(docker compose restart gateway 2>&1)"; then
    fail "the new certificate is installed but the gateway did not restart: $out"
fi
write_status true true "new certificate installed, gateway restarted"
echo "cert-sync: gateway restarted"
