#!/bin/sh
# Prints ONE small text page through the gateway, as a user, with a personal print key. Uses one sheet of paper.
#   scripts/print-test.sh SERVER[:PORT] QUEUE [USERNAME]
#   e.g.  scripts/print-test.sh print.example.lan:8631 office-printer mario
# SERVER and QUEUE are required. USERNAME defaults to your login name. Instead of SERVER you can set the
# environment variable GATEWAY (e.g. GATEWAY=print.example.lan:8631); then the arguments are QUEUE [USERNAME].
# The key (portal -> My profile -> Create a key) is asked silently or taken from $PRINT_KEY.
# Exit codes: 0 sent and accepted, 1 failed (unreachable, refused, wrong key), 2 wrong usage (nothing is sent).
set -eu
if [ -n "${GATEWAY:-}" ]; then
    [ $# -ge 1 ] || { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
else
    [ $# -ge 2 ] || { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
    GATEWAY="$1"; shift
fi
PRINTER="$1"
USER_NAME="${2:-$(id -un)}"

if [ -z "${PRINT_KEY:-}" ]; then
    printf 'Print key for %s: ' "$USER_NAME" >&2
    stty -echo; trap 'stty echo' EXIT; read -r PRINT_KEY; stty echo; trap - EXIT; echo >&2
fi
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
export PRINT_KEY USER_NAME
enc_user="$(python3 -c 'import os,urllib.parse as u; print(u.quote(os.environ["USER_NAME"], safe=""))')"
enc_pass="$(python3 -c 'import os,urllib.parse as u; print(u.quote(os.environ["PRINT_KEY"], safe=""))')"

printf 'SpoolSteward test page\nUser: %s\nQueue: %s\nTime: %s\n' "$USER_NAME" "$PRINTER" "$(date)" > "$tmp/test.txt"
cat > "$tmp/job.test" <<'TEST'
{
  OPERATION Print-Job
  GROUP operation-attributes-tag
  ATTR charset attributes-charset utf-8
  ATTR language attributes-natural-language en
  ATTR uri printer-uri $uri
  ATTR name job-name "print-test.txt"
  ATTR mimetype document-format text/plain
  FILE $filename
  STATUS successful-ok
}
TEST
out="$tmp/out"
if ! ipptool -T 15 -tv -f "$tmp/test.txt" "ipps://$enc_user:$enc_pass@$GATEWAY/printers/$PRINTER" "$tmp/job.test" > "$out" 2>"$tmp/err"; then
    grep -E "${SHOW:-PASS|FAIL|status-code|status-message|job-id|job-state }" "$out" || true
    echo "Print test failed: $(head -n 1 "$tmp/err" 2>/dev/null)" >&2
    echo "Check the server address ($GATEWAY), the queue name ($PRINTER), the user name and the key." >&2
    exit 1
fi
grep -E "${SHOW:-PASS|FAIL|status-code|status-message|job-id|job-state }" "$out" || true
