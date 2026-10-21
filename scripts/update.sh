#!/bin/sh
# Update SpoolSteward to a newer published version, safely.
#   scripts/update.sh              update to the newest release (betas too, unless SPOOLSTEWARD_UPDATE_BETA=false in .env)
#   scripts/update.sh 1.2.0        update (or go back) to this exact version. After restoring a backup made with 1.2.0 it
#                                  puts the files of 1.2.0 back too
#   scripts/update.sh --check      only say if a newer version exists
#   scripts/update.sh 1.3.0-beta.1 a beta (a GitHub pre-release) can always be named, whatever SPOOLSTEWARD_UPDATE_BETA says
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
# Before step 1 it asks for a YES when the update has breaking changes: another major version, another minor version
# while on 0.x, or a release on the way (after the installed one, up to the new one) whose notes have a heading
# "Breaking changes". It also asks for a beta it found by itself (not for one you named).
# Not for git clones (the folder has .git): there "git pull && docker compose up -d --build" is the way.
# Needs: .env with SPOOLSTEWARD_REGISTRY and SPOOLSTEWARD_VERSION set (scripts/docker-init.sh does this), docker compose, curl, tar,
# sha256sum, awk.
# From .env it also reads SPOOLSTEWARD_UPDATE_REPOSITORY (owner/name on GitHub, default deh-aleh/SpoolSteward) and
# SPOOLSTEWARD_UPDATE_BETA (true or false, default true), the same settings the portal uses.
# Environment (for tests and mirrors):
#   SPOOLSTEWARD_RELEASES_API   the list of releases, as GitHub's API gives it  (default: https://api.github.com/repos/<repository>/releases)
#   SPOOLSTEWARD_RELEASES_URL   where the packages are downloaded from          (default: https://github.com/<repository>/releases/download)
set -eu
cd "$(dirname "$0")/.."

# The whole script is one function, and the last line runs it and exits: this script is one of the files an update
# replaces, and the shell must not read a half-replaced file.
main() {
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
envget() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -n 1; }   # the last line wins, as in docker compose
# a path from a package: relative, no "..", and never something that belongs to the user
safe_path() { case "$1" in ""|/*|*..*|.env|.env/*|config|config/*|data|data/*|.git|.git/*) return 1 ;; esac; return 0; }
# a GitHub repository owner/name, with the characters GitHub allows (the same rule as spoolsteward/config.py)
valid_repo() { case "$1" in *[!0-9A-Za-z._/-]*|*/.|*/..) return 1;; esac; printf '%s' "$1" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}$'; }
sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }

# The list of releases is JSON; awk reads it, so nothing else is needed on the server. PARSE prints one line per release:
# TAG (or "-" when it is not v1.2.3 / v1.2.3-beta.1) DRAFT PRERELEASE BREAKING (1 when the notes have a heading "Breaking
# changes", any level, any case), and a line ERR when the answer is not a complete list. The records are cut at every '"',
# so a string is read in pieces and nothing is ever copied twice (release notes can be long).
PARSE='
BEGIN { RS = "\""; depth = 0 }
FNR == 1 { instr = 0; seen = 0 }
function heading(s,   n, L, i, t, u) {
    gsub(/\\\\/, "\001", s); gsub(/\\r/, "", s); gsub(/\\t/, " ", s)
    n = split(s, L, /\\n/)
    for (i = 1; i <= n; i++) {
        t = tolower(L[i]); sub(/^ ? ? ?/, "", t)
        if (t ~ /^#+ /) {
            match(t, /^#+/); if (RLENGTH > 6) continue
            t = substr(t, RLENGTH + 1); sub(/^ +/, "", t); sub(/ +$/, "", t); sub(/ +#+$/, "", t); sub(/ +$/, "", t)
            sub(/:$/, "", t); sub(/ +$/, "", t)
            if (t == "breaking changes") return 1
        } else if (i < n) {
            sub(/ +$/, "", t); sub(/:$/, "", t); sub(/ +$/, "", t)
            u = L[i + 1]; sub(/^ ? ? ?/, "", u)
            if (t == "breaking changes" && (u ~ /^=+ *$/ || u ~ /^-+ *$/)) return 1
        }
    }
    return 0
}
function piece(s, cont, more) {
    if (depth != 2 || !obj) return
    if (want_key) { key = (cont || more) ? "-" : s; if (!more) want_key = 0; return }
    if (key == "tag_name") tag = (!cont && !more && s ~ /^v?[0-9]+\.[0-9]+\.[0-9]+(-beta\.[0-9]+)?$/ && length(s) <= 40) ? s : "-"
    else if (key == "body" && heading(cont ? "x" s : s)) brk = 1
}
function outside(r,   i, c, n) {
    n = length(r)
    for (i = 1; i <= n; i++) {
        c = substr(r, i, 1)
        if (c == " " || c == "\n" || c == "\r" || c == "\t" || c == ":") continue
        if (!seen) { seen = 1; if (c != "[") bad = 1 }
        if (c == "{" || c == "[") {
            depth++
            if (depth == 2) { obj = (c == "{"); want_key = 1; tag = "-"; draft = "-"; pre = "-"; brk = 0; key = "" }
        } else if (c == "}" || c == "]") {
            if (depth == 2 && obj) { print tag "\t" draft "\t" pre "\t" brk; obj = 0 }
            depth--
        } else if (c == ",") {
            if (depth == 2) want_key = 1
        } else if (match(substr(r, i), /^[a-z0-9.eE+-]+/)) {
            if (depth == 2 && obj && !want_key) { if (key == "draft") draft = substr(r, i, RLENGTH); else if (key == "prerelease") pre = substr(r, i, RLENGTH) }
            i += RLENGTH - 1
        } else bad = 1
    }
}
{
    if (!instr) { outside($0); instr = 1; cont = 0; next }
    more = match($0, /\\+$/) && RLENGTH % 2 == 1
    piece($0, cont, more)
    if (more) { cont = 1; next }
    instr = 0
}
END { if (bad || !seen || depth != 0 || !instr) print "ERR" }
'
# PICK reads those lines with cur= (installed), beta= (true/false), want= (a named version, or empty for the newest) and
# prints VERSION BETA BREAKING, BREAKING meaning that a release after cur up to that version says so in its notes;
# nothing when no release is newer. Order: 1.2.3-beta.2 < 1.2.3 < 1.2.4-beta.1.
PICK='
function key(v,   a, n) {
    sub(/^v/, "", v); n = split(v, a, /[.-]/)
    if (length(a[1]) > 9 || length(a[2]) > 9 || length(a[3]) > 9 || length(a[5]) > 9) return ""
    return sprintf("k%09d.%09d.%09d.%d.%09d", a[1], a[2], a[3], n == 3, n == 3 ? 0 : a[5])
}
$1 != "-" && $2 == "false" { n++; K[n] = key($1); V[n] = $1; sub(/^v/, "", V[n]); P[n] = ($3 != "false" || $1 ~ /-beta\./); B[n] = $4 }
END {
    now = key(cur); if (now == "") exit
    if (want != "") { top = key(want); t = want; tb = (want ~ /-beta\./) }
    else {
        for (i = 1; i <= n; i++) if (K[i] != "" && K[i] > now && (beta == "true" || !P[i]) && (top == "" || K[i] > top)) { top = K[i]; t = V[i]; tb = P[i] }
        if (top == "") exit
    }
    for (i = 1; i <= n; i++) if (K[i] != "" && K[i] > now && K[i] <= top && B[i] == 1) brk = 1
    for (i = 1; i <= n; i++) if (K[i] == top && P[i]) tb = 1
    print t "\t" (tb ? 1 : 0) "\t" (brk ? 1 : 0)
}
'
# every release of the project, one line each (see PARSE). Fails when the list cannot be read
releases() {
    case "$API" in *\?*) sep="&" ;; *) sep="?" ;; esac
    p=1
    while [ "$p" -le 10 ]; do
        out="$(curl -fsS --max-time 15 --max-filesize 20000000 -H "Accept: application/vnd.github+json" "$API${sep}per_page=100&page=$p" | LC_ALL=C awk "$PARSE")"
        if printf '%s\n' "$out" | grep -qx ERR; then return 1; fi
        [ -z "$out" ] || printf '%s\n' "$out"
        [ "$(printf '%s\n' "$out" | grep -c .)" -ge 100 ] || return 0
        p=$((p + 1))
    done
}

if [ "$mode" != "check" ] && [ -d .git ]; then
    echo "This folder is a git clone: update it with  git pull && docker compose up -d --build  (this script replaces files, which would fight with git)." >&2
    return 1
fi
[ -e .env ] || { echo ".env not found: run scripts/docker-init.sh first" >&2; return 1; }
current="$(envget SPOOLSTEWARD_VERSION)"
registry="$(envget SPOOLSTEWARD_REGISTRY)"
valid "${current:-x}" || { echo "SPOOLSTEWARD_VERSION in .env is not a version number like 1.2.3 (found: '${current}')." >&2; return 1; }
repo="$(envget SPOOLSTEWARD_UPDATE_REPOSITORY)"; repo="${repo:-deh-aleh/SpoolSteward}"
valid_repo "$repo" || { echo "SPOOLSTEWARD_UPDATE_REPOSITORY in .env is not a GitHub repository written owner/name (found: '$repo')." >&2; return 1; }
beta="$(envget SPOOLSTEWARD_UPDATE_BETA)"; beta="${beta:-true}"
case "$beta" in true|false) ;; *) echo "SPOOLSTEWARD_UPDATE_BETA in .env must be true or false (found: '$beta')." >&2; return 1 ;; esac
API="${SPOOLSTEWARD_RELEASES_API:-https://api.github.com/repos/$repo/releases}"
DL="${SPOOLSTEWARD_RELEASES_URL:-https://github.com/$repo/releases/download}"

# What the releases say (not asked when the portal sent us: it has checked already). `list`: one line per release,
# TAG DRAFT PRERELEASE BREAKING; `pick`: VERSION BETA BREAKING of the release to install (see the two awk programs above)
named=1; [ -n "$target" ] || named=0
listed=0; is_beta=0; notes=0; pick=""
if [ "$portal" = 0 ] && list="$(releases)"; then listed=1; fi
if [ "$named" = 0 ]; then
    [ "$listed" = 1 ] || { echo "could not look up the newest version (no network?). Give one: $0 1.2.0" >&2; return 1; }
    pick="$(printf '%s\n' "$list" | LC_ALL=C awk -F '\t' -v cur="$current" -v beta="$beta" -v want= "$PICK")"
    if [ -z "$pick" ]; then
        echo "installed: $current"
        [ "$beta" = true ] && echo "up to date" || echo "up to date (betas are not offered: SPOOLSTEWARD_UPDATE_BETA=false)"
        return 0
    fi
    target="$(printf '%s' "$pick" | cut -f1)"
elif [ "$listed" = 1 ] && valid "$target"; then
    pick="$(printf '%s\n' "$list" | LC_ALL=C awk -F '\t' -v cur="$current" -v beta="$beta" -v want="$target" "$PICK")"
fi
valid "$target" || { echo "'$target' is not a version number like 1.2.3 (or 1.2.3-beta.1)" >&2; return 1; }
if [ -n "${pick:-}" ]; then is_beta="$(printf '%s' "$pick" | cut -f2)"; notes="$(printf '%s' "$pick" | cut -f3)"; fi
case "$target" in *-beta.*) is_beta=1 ;; esac
notes_page="https://github.com/$repo/releases/tag/v$target"

# breaking: another major version, another minor version on 0.x, or the notes of a release on the way say so
cmaj="${current%%.*}"; tmaj="${target%%.*}"; cmin="$(echo "$current" | cut -d. -f2)"; tmin="$(echo "$target" | cut -d. -f2)"
breaking=0
if [ "$cmaj" != "$tmaj" ] || { [ "$tmaj" = 0 ] && [ "$cmin" != "$tmin" ]; } || [ "$notes" = 1 ]; then breaking=1; fi

echo "installed: $current    wanted: $target"
if [ "$mode" = "check" ]; then
    if [ "$current" = "$target" ]; then echo "up to date"; return 0; fi
    [ "$is_beta" = 1 ] && echo "$target is a test release (pre-release): it can contain bugs."
    [ "$breaking" = 1 ] && echo "$target has breaking changes: read the release notes first: $notes_page"
    echo "update available: run scripts/update.sh"
    return 0
fi
# after restoring an old backup, .env says one version while the files here (VERSION) are of another: then the files are installed again
files_version="$(cat VERSION 2>/dev/null || true)"
if [ "$current" = "$target" ] && { [ -z "$files_version" ] || [ "$files_version" = "$target" ]; }; then echo "already on $target"; return 0; fi
[ -n "$registry" ] || { echo "SPOOLSTEWARD_REGISTRY is empty in .env: images are built from this folder. Update with: git pull && docker compose up -d --build" >&2; return 1; }

# breaking changes, or a beta that was not named: ask first (the portal asked already)
if [ "$portal" = 0 ] && [ "$named" = 1 ] && [ "$listed" = 0 ]; then
    echo "NOTE: the list of releases could not be read, so the release notes were not searched for breaking changes. Read them: $notes_page"
fi
if [ "$portal" = 0 ] && { [ "$breaking" = 1 ] || { [ "$is_beta" = 1 ] && [ "$named" = 0 ]; }; }; then
    [ "$is_beta" = 1 ] && echo "NOTE: $target is a test release (pre-release): it can contain bugs."
    if [ "$breaking" = 1 ]; then
        echo "WARNING: the update $current -> $target has breaking changes. The installation could fail and leave SpoolSteward not working."
        echo "Before you go on, keep a copy of the file .env and of the folder config (this script saves a backup of the data in data/backups/pre-update first)."
        echo "Read the release notes first: $notes_page"
    fi
    printf "type YES to continue: "; read -r a || a=""; [ "$a" = "YES" ] || { echo "cancelled"; return 1; }
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
