#!/bin/sh
# Update SpoolSteward to a newer published version, safely.
#   scripts/update.sh              update to the newest release
#   scripts/update.sh 1.2.0        update (or go back) to this exact version. After restoring a backup made with 1.2.0 it
#                                  puts the files of 1.2.0 back too
#   scripts/update.sh --check      only say if a newer version exists
#   scripts/update.sh 1.3.0-beta.1 a beta (a pre-release) is never offered by itself: only by naming it
#   (--from-portal VERSION is used by the "updater" container: the portal already made the backup, there are no questions,
#    and the updater container itself is left alone while it works)
#
# What it does, in order:
#   1. saves a full backup (data/backups/pre-update/) - if this fails, nothing is changed
#   2. downloads the release package of that version (docker-compose.yml, scripts/, docker/env.example) and checks its sha256
#   3. writes the new version in .env (SPOOLSTEWARD_VERSION) and replaces the files of the package (the old ones are kept in
#      .update-prev/). Your .env, config/ and data/ are NEVER touched: the package does not contain them.
#   4. downloads the new images and restarts the containers
#   5. waits until the gateway says it is healthy
#   6. if it is NOT healthy: puts the old files and the old version back and, if the old version does not start either
#      (the new one changed the database), offers to restore the backup of step 1
# Not for git clones (the folder has .git): there "git pull && docker compose up -d --build" is the way.
# Needs: .env with SPOOLSTEWARD_REGISTRY and SPOOLSTEWARD_VERSION set (scripts/docker-init.sh does this), docker compose, curl, tar, sha256sum.
# Environment:
#   SPOOLSTEWARD_RELEASES_API   where the newest version is looked up      (default: GitHub releases of this project)
#   SPOOLSTEWARD_RELEASES_URL   where the packages are downloaded from     (default: GitHub releases of this project)
set -eu
cd "$(dirname "$0")/.."

# The whole script is one function, and the last line runs it and exits: this script is one of the files an update
# replaces, and the shell must not read a half-replaced file.
main() {
API="${SPOOLSTEWARD_RELEASES_API:-https://api.github.com/repos/deh-aleh/SpoolSteward/releases/latest}"
DL="${SPOOLSTEWARD_RELEASES_URL:-https://github.com/deh-aleh/SpoolSteward/releases/download}"
mode="update"; target=""; portal=0
if [ "${1:-}" = "--from-portal" ]; then portal=1; shift; fi
case "${1:-}" in
    --check) mode="check" ;;
    "")      ;;
    -*)      echo "usage: $0 [--check | VERSION]" >&2; return 2 ;;
    *)       target="${1#v}" ;;
esac

# only 1.2.3 or a beta like 1.2.3-beta.1 are accepted: the value ends up in .env, in image names and in a URL
valid() { case "$1" in *[!0-9A-Za-z.-]*) return 1;; esac; printf '%s' "$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-beta\.[0-9]+)?$'; }
envget() { sed -n "s/^$1=//p" .env 2>/dev/null | head -1; }
# a path from a package: relative, no "..", and never something that belongs to the user
safe_path() { case "$1" in ""|/*|*..*|.env|.env/*|config|config/*|data|data/*|.git|.git/*) return 1 ;; esac; return 0; }
sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

if [ "$mode" != "check" ] && [ -d .git ]; then
    echo "This folder is a git clone: update it with  git pull && docker compose up -d --build  (this script replaces files, which would fight with git)." >&2
    return 1
fi
[ -e .env ] || { echo ".env not found: run scripts/docker-init.sh first" >&2; return 1; }
current="$(envget SPOOLSTEWARD_VERSION)"
registry="$(envget SPOOLSTEWARD_REGISTRY)"
valid "${current:-x}" || { echo "SPOOLSTEWARD_VERSION in .env is not a version number like 1.2.3 (found: '${current}')." >&2; return 1; }

if [ -z "$target" ]; then
    target="$(curl -fsS --max-time 15 "$API" | sed -n 's/.*"tag_name": *"v\{0,1\}\([0-9][0-9.]*\)".*/\1/p' | head -1)"
    [ -n "$target" ] || { echo "could not look up the newest version (no network?). Give one: $0 1.2.0" >&2; return 1; }
fi
valid "$target" || { echo "'$target' is not a version number like 1.2.3 (or 1.2.3-beta.1)" >&2; return 1; }

echo "installed: $current    wanted: $target"
if [ "$mode" = "check" ]; then
    [ "$current" = "$target" ] && echo "up to date" || echo "update available: run scripts/update.sh"
    return 0
fi
# after restoring an old backup, .env says one version while the files here (VERSION) are of another: then the files are installed again
files_version="$(cat VERSION 2>/dev/null || true)"
if [ "$current" = "$target" ] && { [ -z "$files_version" ] || [ "$files_version" = "$target" ]; }; then echo "already on $target"; return 0; fi
[ -n "$registry" ] || { echo "SPOOLSTEWARD_REGISTRY is empty in .env: images are built from this folder. Update with: git pull && docker compose up -d --build" >&2; return 1; }

# a jump to another major version can change things by hand: ask first
if [ "$portal" = 0 ] && [ "${current%%.*}" != "${target%%.*}" ]; then
    echo "WARNING: major version change ($current -> $target). Read the release notes first."
    printf "type YES to continue: "; read -r a; [ "$a" = "YES" ] || { echo "cancelled"; return 1; }
fi

backup_file=""
if [ "$portal" = 0 ]; then
    echo "1/5 backup ..."
    scripts/backup.sh backup data/backups/pre-update || { echo "backup failed: nothing was changed" >&2; return 1; }
    backup_file="$(ls -t data/backups/pre-update/spoolsteward-*.tar.gz | head -1)"
else
    # the updater container must not recreate itself: an empty COMPOSE_PROFILES hides the "autoupdate" profile from here on
    export COMPOSE_PROFILES=
fi

echo "2/5 downloading the package of $target ..."
work=".update-new"; prev=".update-prev"
rm -rf "$work"; mkdir -p "$work"
pkg="spoolsteward-$target.tar.gz"
curl -fsSL --max-time 120 -o "$work/$pkg" "$DL/v$target/$pkg" || { rm -rf "$work"; echo "download failed ($DL/v$target/$pkg): nothing was changed" >&2; return 1; }
curl -fsSL --max-time 30 -o "$work/SHA256SUMS" "$DL/v$target/SHA256SUMS" || { rm -rf "$work"; echo "could not download SHA256SUMS: nothing was changed" >&2; return 1; }
want="$(sed -n "s/^\([0-9a-f]\{64\}\)  *\**$pkg\$/\1/p" "$work/SHA256SUMS" | head -1)"
[ -n "$want" ] && [ "$want" = "$(sha256 "$work/$pkg")" ] || { rm -rf "$work"; echo "the package does not match its sha256 sum: nothing was changed" >&2; return 1; }
# nothing outside the package folder, no absolute paths
if tar -tzf "$work/$pkg" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then rm -rf "$work"; echo "the package has unsafe paths: nothing was changed" >&2; return 1; fi
mkdir "$work/x"
tar -xzf "$work/$pkg" -C "$work/x" --strip-components=1 --no-same-owner --no-same-permissions || { rm -rf "$work"; echo "could not unpack the package: nothing was changed" >&2; return 1; }
new="$work/x"
[ "$(cat "$new/VERSION" 2>/dev/null)" = "$target" ] && [ -f "$new/FILES" ] && [ -f "$new/docker-compose.yml" ] \
    || { rm -rf "$work"; echo "the package is not SpoolSteward $target: nothing was changed" >&2; return 1; }
while read -r f; do safe_path "$f" || { rm -rf "$work"; echo "the package lists an unsafe path ($f): nothing was changed" >&2; return 1; }; [ -f "$new/$f" ] || { rm -rf "$work"; echo "the package is incomplete ($f): nothing was changed" >&2; return 1; }; done < "$new/FILES"

echo "3/5 installing the new files ..."
rm -rf "$prev" 2>/dev/null
[ ! -e "$prev" ] || { rm -rf "$work"; echo "cannot remove $prev (owned by root, left by an update made from the portal?). Remove it and run again:  sudo rm -rf $prev    Nothing was changed." >&2; return 1; }
mkdir -p "$prev"
oldlist=""; [ -f FILES ] && oldlist="$(cat FILES)"
{ cat "$new/FILES"; printf '%s\n' "$oldlist"; } | sort -u | while read -r f; do
    if [ -n "$f" ] && safe_path "$f" && [ -f "$f" ]; then mkdir -p "$prev/$(dirname "$f")"; cp -p "$f" "$prev/$f"; fi
done
cp -p .env "$prev/.env.before"
setver() { # replace or add SPOOLSTEWARD_VERSION in .env
    if grep -q '^SPOOLSTEWARD_VERSION=' .env; then sed -i "s/^SPOOLSTEWARD_VERSION=.*/SPOOLSTEWARD_VERSION=$1/" .env
    else echo "SPOOLSTEWARD_VERSION=$1" >> .env; fi
}
put_back() { # the files of the old version
    while read -r f; do if safe_path "$f"; then rm -f "$f"; fi; done < "$new/FILES"
    while read -r f; do
        if [ -n "$f" ] && safe_path "$f" && [ -f "$prev/$f" ]; then mkdir -p "$(dirname "$f")"; cp -p "$prev/$f" "$f.pm-tmp" && mv -f "$f.pm-tmp" "$f"; fi
    done <<EOF
$oldlist
EOF
    setver "$current"
}
own="$(stat -c %u:%g . 2>/dev/null || echo 0:0)"
# every file is written under a temporary name and moved into place: a file that is running (this script) is never overwritten in place
while read -r f; do
    mkdir -p "$(dirname "$f")"
    cp -p "$new/$f" "$f.pm-tmp" && mv -f "$f.pm-tmp" "$f"
    if [ "$(id -u)" = 0 ]; then chown "$own" "$f" "$(dirname "$f")" 2>/dev/null || true; fi   # the updater container runs as root: files and folders stay the user's
done < "$new/FILES"
if [ "$(id -u)" = 0 ]; then chown -R "$own" "$prev" 2>/dev/null || true; fi   # so that a later run of this script by the user can remove it
# files that the old version had and the new one does not
printf '%s\n' "$oldlist" | while read -r f; do
    if [ -n "$f" ] && safe_path "$f" && ! grep -qxF "$f" "$new/FILES"; then rm -f "$f"; fi
done
setver "$target"

healthy() { # wait up to ~2 minutes
    i=0
    while [ $i -lt "${SPOOLSTEWARD_HEALTH_TRIES:-24}" ]; do
        s="$(docker compose ps --format '{{.Health}}' gateway 2>/dev/null | head -1)"
        [ "$s" = "healthy" ] && return 0
        i=$((i+1)); sleep "${SPOOLSTEWARD_HEALTH_WAIT:-5}"
    done
    return 1
}

echo "4/5 downloading the images and restarting ..."
if ! docker compose pull; then
    put_back; rm -rf "$work"
    echo "download of the images failed: the old files and version $current are back" >&2; return 1
fi
docker compose up -d --remove-orphans
echo "5/5 waiting for the gateway ..."
if healthy; then
    rm -rf "$work"
    echo "OK: now on $target.${backup_file:+ Backup taken before: $backup_file}"
    return 0
fi

echo "NOT healthy after the update: going back to $current" >&2
put_back
rm -rf "$work"
docker compose up -d --remove-orphans
if healthy; then
    echo "The old version $current is running again. Look at the cause with:  docker compose logs --tail 50 gateway" >&2
    return 1
fi
# the old version does not start either: the new one probably changed the database
if [ -n "$backup_file" ] && [ "$portal" = 0 ]; then
    echo "The old version does not start either: the new version has probably changed the database." >&2
    printf "Restore the backup taken at the start (%s)? Changes made since then are lost. Type YES to restore: " "$backup_file"
    read -r a
    if [ "$a" = "YES" ]; then
        scripts/backup.sh restore "$backup_file" --yes && return 1
    fi
fi
cat >&2 <<MSG
The old version is running again, but it does not become healthy. The new version may already have changed the database.
Restore the backup taken just before:
    scripts/backup.sh restore ${backup_file:-<file>}   (from the portal: use the newest copy in the menu Backup)
Look at the cause with:  docker compose logs --tail 50 gateway
MSG
return 1
}

main "$@"; exit $?
