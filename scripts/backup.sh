#!/bin/sh
# HOST-SIDE full backup / restore (also takes the CUPS queues and .env; use it to move the machine).
# The gateway has its own scheduled backups in the portal (menu Backup): see README. Run from anywhere; works on the running stack.
#   scripts/backup.sh backup [DEST_DIR]      writes DEST_DIR/spoolsteward-vX.Y.Z-YYYYMMDD-HHMMSS.tar.gz   (default DEST_DIR: ./backups)
#                                            X.Y.Z = the version that was running: to go back to a version, restore a backup with that name
#   scripts/backup.sh restore FILE.tar.gz [--yes]   puts it back (stops the gateway first, asks for confirmation; --yes = already asked,
#                                            used by scripts/update.sh)
#
# What is saved:
#   spoolsteward.db  users, groups, quotas, print keys (hashes), audit. Taken with SQLite's own backup call, so it is
#                   consistent even while people are printing (copying the live file would not be).
#   config/        the TLS certificate and key (and the old gateway.env of older backups)
#   dot-env        .env (all the settings: SECRET_KEY, RADIUS secret...)
#   audit-archive/ the compressed audit files (data/audit-archive)
#   cups-config    the CUPS queues (printers.conf), so queues created from the portal survive a rebuild
# NOT saved: the images (they are downloaded again) and the spool of jobs in progress.
# The archive holds secrets (TLS key, SECRET_KEY): keep it as private as the server itself.
set -eu
cd "$(dirname "$0")/.."
cmd="${1:-}"
project="$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"

case "$cmd" in
backup)
    dest="${2:-backups}"
    mkdir -p "$dest"
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    # consistent copy of the database, streamed out of the running container
    docker compose exec -T gateway python - > "$work/spoolsteward.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect("/data/spoolsteward.db")
dst = sqlite3.connect("/tmp/snapshot.db")
src.backup(dst)
dst.close()
sys.stdout.buffer.write(open("/tmp/snapshot.db", "rb").read())
PY
    [ -s "$work/spoolsteward.db" ] || { echo "database snapshot is empty: is the gateway running?" >&2; exit 1; }
    mkdir -p "$work/config" "$work/audit-archive"
    cp -a config/. "$work/config/"
    [ -e .env ] && cp -a .env "$work/dot-env"
    cp -a data/audit-archive/. "$work/audit-archive/" 2>/dev/null || true
    docker run --rm -v "${project}_cups-config:/c:ro" alpine tar -C /c -cf - . > "$work/cups-config.tar"
    ver="$(sed -n 's/^SPOOLSTEWARD_VERSION=//p' .env 2>/dev/null | head -1 | tr -cd '0-9A-Za-z.-')"
    out="$dest/spoolsteward-v${ver:-unknown}-$(date +%Y%m%d-%H%M%S).tar.gz"
    tar -C "$work" -czf "$out" .
    chmod 600 "$out"
    echo "backup written: $out"
    ;;
restore)
    file="${2:?usage: scripts/backup.sh restore FILE.tar.gz [--yes]}"
    if [ "${3:-}" != "--yes" ]; then
        echo "This REPLACES the database, the configuration and the CUPS queues of this server with the ones in $file."
        printf "type YES to continue: "
        read -r answer
        [ "$answer" = "YES" ] || { echo "cancelled"; exit 1; }
    fi
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    tar -C "$work" -xzf "$file"
    docker compose stop gateway mdns
    mkdir -p config data/audit-archive
    cp -a "$work/config/." config/
    [ -e "$work/dot-env" ] && cp -a "$work/dot-env" .env
    cp -a "$work/audit-archive/." data/audit-archive/ 2>/dev/null || true
    owner="$(sed -n 's/^PUID=//p' .env 2>/dev/null | head -1):$(sed -n 's/^PGID=//p' .env 2>/dev/null | head -1)"
    docker run --rm -i -v "${project}_portal-data:/d" -v "$work:/w:ro" alpine \
        sh -c "rm -f /d/spoolsteward.db /d/spoolsteward.db-wal /d/spoolsteward.db-shm && cp /w/spoolsteward.db /d/spoolsteward.db && chown ${owner:-1000:1000} /d/spoolsteward.db && chmod 600 /d/spoolsteward.db" 
    docker run --rm -v "${project}_cups-config:/c" -v "$work:/w:ro" alpine \
        sh -c 'cd /c && tar -xf /w/cups-config.tar'
    docker compose up -d
    echo "restored. Check:  docker compose logs --tail 20 gateway"
    # the backup brings back .env, so also the version it was made with; the compose file and the scripts here may be of another one
    was="$(sed -n 's/^SPOOLSTEWARD_VERSION=//p' .env 2>/dev/null | head -1)"; files="$(cat VERSION 2>/dev/null || true)"
    if [ -n "$was" ] && [ -n "$files" ] && [ "$was" != "$files" ]; then
        echo "NOTE: this backup was made with version $was, but the files in this folder (docker-compose.yml, scripts) are of $files."
        echo "      Put the files of $was back with:  scripts/update.sh $was"
    fi
    ;;
*)
    echo "usage: $0 backup [DEST_DIR] | restore FILE.tar.gz [--yes]" >&2
    exit 2
    ;;
esac
