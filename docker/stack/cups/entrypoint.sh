#!/bin/sh
# CUPS as a private backend: unix socket only (config mounted from docker/stack/cups/cupsd.conf).
# Queues are created from CUPS_QUEUES="NAME=URI NAME2=URI2" (retried until the printer answers)
# and CUPS_VIRTUAL_QUEUE=1 adds a "virtual" queue that writes PDFs: handy to try quotas without paper.
# Each queue is created ONCE: its name is written to /etc/cups/spoolsteward-queues (the CUPS volume). A queue that was
# deleted later, by hand or because its USB printer stayed away, is not brought back at the next start.
# To bring one back: scripts/queue-restore.sh NAME --reboot (it removes the name from that file and restarts this container).
#
# cupsd runs as a CHILD of this script (cupsd -f) and the script lives as long as it does: if cupsd dies the container ends,
# and `restart: unless-stopped` starts it again. (Run in the background by itself, a dead cupsd left a container that Docker
# thought alive: "unhealthy" forever, nothing printed, nobody restarted it.)
set -u
STATE="${SPOOLSTEWARD_QUEUES_FILE:-/etc/cups/spoolsteward-queues}"   # (the two names can be changed only by the tests)
ERROR_LOG="${CUPS_ERROR_LOG:-/var/log/cups/error_log}"
# CUPS_QUEUES=none (the .env way to say "no queues") is the same as empty
case "${CUPS_QUEUES:-}" in [Nn][Oo][Nn][Ee]) CUPS_QUEUES= ;; esac
cupsd -f &
cupsd_pid=$!

# `docker stop` signals this script (PID 1), not cupsd: pass the signal on and wait, so cupsd writes its job history
# and closes its socket instead of being killed 10 seconds later
stop() {
    kill -TERM "$cupsd_pid" 2>/dev/null
    for i in $(seq 1 40); do kill -0 "$cupsd_pid" 2>/dev/null || break; sleep 0.25; done
    exit 0
}
trap stop TERM INT

for i in $(seq 1 40); do lpstat -r >/dev/null 2>&1 && break; sleep 0.5; done

touch "$STATE"
done_before() { grep -qx -- "$1" "$STATE"; }
remember() { done_before "$1" || echo "$1" >> "$STATE"; }

if [ "${CUPS_VIRTUAL_QUEUE:-0}" = "1" ] && ! lpstat -p virtual >/dev/null 2>&1 && ! done_before virtual; then
    if lpadmin -p virtual -E -v cups-pdf:/ -m lsb/usr/cups-pdf/CUPS-PDF_noopt.ppd \
        || lpadmin -p virtual -E -v cups-pdf:/ -m /usr/share/ppd/cups-pdf/CUPS-PDF_noopt.ppd; then
        remember virtual
    fi
fi

add_queue() {
    name="$1"; uri="$2"
    if lpstat -p "$name" >/dev/null 2>&1; then remember "$name"; return 0; fi
    if done_before "$name"; then
        echo "[entrypoint] queue $name was created before and removed since: not created again (see the top of this file)"
        return 0
    fi
    for i in $(seq 1 60); do   # the printer may take a while (USB enumeration, ipp-usb start)
        if lpadmin -p "$name" -E -v "$uri" -m everywhere 2>/dev/null; then
            remember "$name"; echo "[entrypoint] queue $name -> $uri created"; return 0
        fi
        sleep 5
    done
    echo "[entrypoint] WARNING: could not create queue $name ($uri); add it later: docker compose exec cups lpadmin -p $name -E -v $uri -m everywhere" >&2
}
for q in ${CUPS_QUEUES:-}; do add_queue "${q%%=*}" "${q#*=}" & done

tail -F "$ERROR_LOG" &
wait "$cupsd_pid"
code=$?
echo "[entrypoint] cupsd stopped (status $code): this container ends so that Docker starts it again" >&2
exit "$((code == 0 ? 1 : code))"
