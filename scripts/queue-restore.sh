#!/bin/sh
# Bring back a print queue that was deleted (by you, or by the gateway because its USB printer stayed away).
#
# Why this exists: each queue listed in CUPS_QUEUES (.env), and the "virtual" test queue, is created ONCE. The container
# remembers the names it already created in /etc/cups/spoolsteward-queues, so a queue you deleted does not come back by itself
# at the next start. This script takes a name out of that list, so the NEXT start of the cups container creates it again.
#
#   scripts/queue-restore.sh NAME             forget NAME (it is created again at the next start of the cups container)
#   scripts/queue-restore.sh NAME --reboot    the same, and restart the cups container now
#   scripts/queue-restore.sh --list           show the names the container remembers
#
# The queue must still be listed in CUPS_QUEUES in .env (or be "virtual" with CUPS_VIRTUAL_QUEUE=1), otherwise there is
# nothing to create it from. USB printers that are plugged in again are found by themselves and need none of this.
set -eu
cd "$(dirname "$0")/.."
FILE=/etc/cups/spoolsteward-queues

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
cups() { docker compose exec -T "$@"; }

name=""; reboot=0; list=0
for arg in "$@"; do
    case "$arg" in
        --reboot) reboot=1 ;;
        --list)   list=1 ;;
        -h|--help|-*) usage ;;
        *) [ -z "$name" ] && name="$arg" || usage ;;
    esac
done

# the cups container must be running
if ! cups cups true >/dev/null 2>&1; then
    echo "The cups container is not running. Start it with:  docker compose up -d" >&2
    exit 1
fi

if [ "$list" = 1 ]; then
    echo "Queues the cups container remembers (created once, not created again if missing):"
    cups cups sh -c "cat $FILE 2>/dev/null" | sed 's/^/  /'
    exit 0
fi

[ -n "$name" ] || usage
# the name goes into a file and a command: only the characters CUPS allows in a queue name, nothing else
case "$name" in
    *[!A-Za-z0-9_.-]*|"") echo "Not a valid queue name: '$name' (letters, digits, _ . - only)" >&2; exit 2 ;;
esac

if ! cups cups sh -c "grep -qxF -- '$name' $FILE 2>/dev/null"; then
    echo "'$name' is not in the list of remembered queues: nothing to forget."
    if cups cups lpstat -p "$name" >/dev/null 2>&1; then
        echo "The queue exists now, so it does not need to be restored."
    else
        echo "It was never created by the cups container. Check that it is listed in CUPS_QUEUES in .env, then:  docker compose restart cups"
    fi
    exit 1
fi

cups cups sh -c "grep -vxF -- '$name' $FILE > $FILE.new; mv $FILE.new $FILE"
echo "'$name' forgotten: the cups container will create it again at its next start."

if [ "$reboot" = 1 ]; then
    echo "Restarting the cups container ..."
    docker compose restart cups
    echo "Done. The queue appears in a few seconds (the container retries until the printer answers, up to 5 minutes)."
else
    echo "Restart it now with:  docker compose restart cups     (or run this script again with --reboot)"
fi
