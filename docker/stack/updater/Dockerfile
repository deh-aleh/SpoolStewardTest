# Optional helper that applies updates asked for in the portal. Started only with:  docker compose --profile autoupdate up -d
# It needs the Docker socket, which is the same as root on the host: read docs/en/installation.md ("Updates") before using it.
# an exact version: what the updater runs changes only when this line does (it brings `docker compose` too)
FROM docker:29.9.2-cli
# update.sh downloads the release package (curl) and unpacks it (GNU tar: busybox tar lacks some options)
RUN apk add --no-cache curl tar
COPY updater.sh /updater.sh
RUN chmod +x /updater.sh
ENTRYPOINT ["/updater.sh"]
