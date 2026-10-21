#!/bin/sh
# Waits for /data/update/request.json (written by the gateway when an administrator presses "Update" in the portal),
# runs the project's scripts/update.sh for that version and reports in /data/update/status.json.
# It never reads a version, path or command from anywhere else: the only input is a version, x.y.z or x.y.z-beta.N (a beta
# is accepted: the portal offers one only when SPOOLSTEWARD_UPDATE_BETA is true, and asks the administrator to confirm it).
# The repository the package comes from is SPOOLSTEWARD_UPDATE_REPOSITORY in the project's .env, read by scripts/update.sh.
set -u
D="${UPDATER_DIR:-/data/update}"   # changed only by the tests
PROJECT="${PROJECT_DIR:?PROJECT_DIR is not set: run scripts/docker-init.sh}"
# The gateway (internet-facing, another user) can write in $D: this script, root, never writes through a name the gateway
# could have made a symlink. New files come from mktemp (created exclusively, owned by root, and the sticky bit stops the
# gateway from swapping them), then rename(2) puts them in place without following anything.
[ ! -L "$D" ] || { echo "$D is a symlink: refusing to run" >&2; exit 1; }
mkdir -p "$D"; chmod 1777 "$D"   # the gateway runs as another user and must be able to write the request here

put() { # name  (content from stdin)
    t="$(mktemp "$D/.tmp.XXXXXX")" || return 1
    cat > "$t"; chmod 644 "$t"; mv -f "$t" "$D/$1"
}
status() { # state version backup
    printf '{"state":"%s","version":"%s","backup":"%s","ts":"%s"}\n' "$1" "$2" "$3" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" | put status.json
}
valid() { case "$1" in *[!0-9A-Za-z.-]*) return 1;; esac; printf '%s' "$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-beta\.[0-9]+)?$'; }
field() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" "$D/request.json" | head -1; }

echo "updater ready (project: $PROJECT)"
while :; do
    : | put heartbeat
    if [ -f "$D/request.json" ] && [ ! -L "$D/request.json" ]; then
        version="$(field version)"; backup="$(basename "$(field backup)")"
        rm -f "$D/request.json"
        if ! valid "$version"; then status failed "" ""; sleep 10; continue; fi
        current="$(sed -n 's/^SPOOLSTEWARD_VERSION=//p' "$PROJECT/.env" | tail -n 1)"
        # only inside the same major version; bigger jumps are done by hand
        if [ "${current%%.*}" != "${version%%.*}" ]; then status failed "$version" "$backup"; sleep 10; continue; fi
        status running "$version" "$backup"
        if (cd "$PROJECT" && sh scripts/update.sh --from-portal "$version"); then
            status ok "$version" "$backup"
        elif [ "$(sed -n 's/^SPOOLSTEWARD_VERSION=//p' "$PROJECT/.env" | tail -n 1)" = "$current" ]; then
            status rolled_back "$version" "$backup"
        else
            status failed "$version" "$backup"
        fi
    fi
    sleep 10
done
