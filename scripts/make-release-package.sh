#!/bin/sh
# Build the release package that scripts/update.sh downloads (used by .github/workflows/release.yml, and by the tests).
#   scripts/make-release-package.sh VERSION OUT_DIR
# Writes OUT_DIR/spoolsteward-VERSION.tar.gz and OUT_DIR/SHA256SUMS. The package holds what runs on the host next to the
# published images: docker-compose.yml, scripts/*.sh, docker/env.example, the config files the compose file mounts, the docs.
# It never holds .env, config/ or data/: an update must not be able to touch them.
set -eu
cd "$(dirname "$0")/.."
version="${1:?usage: $0 VERSION OUT_DIR}"; out="${2:?usage: $0 VERSION OUT_DIR}"
printf '%s' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(-beta\.[0-9]+)?$' || { echo "VERSION must look like 1.2.3 or 1.2.3-beta.1" >&2; exit 2; }
top="spoolsteward-$version"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/$top" "$out"
{
    echo docker-compose.yml; echo LICENSE; echo NOTICE; echo README.md; echo README.it.md
    ls scripts/*.sh
    echo docker/env.example; echo docker/stack/cups/cupsd.conf; echo docker/radius/clients.conf; echo docker/radius/authorize
    find docs -type f ! -path '*/.*' | sort      # pages and pictures; no hidden file (.DS_Store and the like)
} > "$stage/list"
while read -r f; do
    [ -f "$f" ] || { echo "missing file for the package: $f" >&2; exit 1; }
    mkdir -p "$stage/$top/$(dirname "$f")"
    cp -p "$f" "$stage/$top/$f"
done < "$stage/list"
printf '%s\n' "$version" > "$stage/$top/VERSION"
{ cat "$stage/list"; echo VERSION; echo FILES; } > "$stage/$top/FILES"
tar -C "$stage" --owner=0 --group=0 --numeric-owner --sort=name -czf "$out/$top.tar.gz" "$top"
( cd "$out" && sha256sum "$top.tar.gz" > SHA256SUMS )
echo "$out/$top.tar.gz"
