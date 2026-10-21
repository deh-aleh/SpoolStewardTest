#!/bin/sh
# Waits for /data/update/request.json (written by the gateway when an administrator presses "Update" in the portal),
# runs the project's scripts/update.sh for that version and reports in /data/update/status.json.
# It never reads a version, path or command from anywhere else: the only input is a plain x.y.z.
set -u
D=/data/update
PROJECT="${PROJECT_DIR:?PROJECT_DIR is not set: run scripts/docker-init.sh}"
mkdir -p "$D"; chmod 1777 "$D"   # the gateway runs as another user and must be able to write the request here

status() { # state version backup
    printf '{"state":"%s","version":"%s","backup":"%s","ts":"%s"}\n' "$1" "$2" "$3" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$D/.status.tmp"
    chmod 644 "$D/.status.tmp"; mv "$D/.status.tmp" "$D/status.json"
}
valid() { case "$1" in *[!0-9A-Za-z.-]*) return 1;; esac; printf '%s' "$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-beta\.[0-9]+)?$'; }
field() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" "$D/request.json" | head -1; }

echo "updater ready (project: $PROJECT)"
while :; do
    touch "$D/heartbeat"; chmod 644 "$D/heartbeat"
    if [ -f "$D/request.json" ]; then
        version="$(field version)"; backup="$(basename "$(field backup)")"
        rm -f "$D/request.json"
        if ! valid "$version"; then status failed "" ""; sleep 10; continue; fi
        current="$(sed -n 's/^SPOOLSTEWARD_VERSION=//p' "$PROJECT/.env" | head -1)"
        # only inside the same major version; bigger jumps are done by hand
        if [ "${current%%.*}" != "${version%%.*}" ]; then status failed "$version" "$backup"; sleep 10; continue; fi
        status running "$version" "$backup"
        if (cd "$PROJECT" && sh scripts/update.sh --from-portal "$version"); then
            status ok "$version" "$backup"
        elif [ "$(sed -n 's/^SPOOLSTEWARD_VERSION=//p' "$PROJECT/.env" | head -1)" = "$current" ]; then
            status rolled_back "$version" "$backup"
        else
            status failed "$version" "$backup"
        fi
    fi
    sleep 10
done
