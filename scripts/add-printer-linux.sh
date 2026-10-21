#!/bin/sh
# Adds a gateway printer to THIS Linux computer's CUPS, remembering the print key so nobody is asked again.
#   scripts/add-printer-linux.sh <server[:port]> <queue> <username> [local-name]
#   e.g.  scripts/add-printer-linux.sh print.lan:8631 HP7305 mario
# The key (portal -> My profile -> Create a key) is asked silently, or taken from $PRINT_KEY.
# It is stored in /etc/cups/printers.conf, which only root and the cups group can read. It is a print-only key:
# if this computer is lost, revoke it in the portal and the domain password was never on the machine.
set -eu
[ $# -ge 3 ] || { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
SERVER="$1"; QUEUE="$2"; USER_NAME="$3"; LOCAL="${4:-$QUEUE}"

if [ -z "${PRINT_KEY:-}" ]; then
    printf 'Print key for %s: ' "$USER_NAME" >&2
    stty -echo; trap 'stty echo' EXIT; read -r PRINT_KEY; stty echo; trap - EXIT; echo >&2
fi
export PRINT_KEY USER_NAME
enc_user="$(python3 -c 'import os,urllib.parse as u; print(u.quote(os.environ["USER_NAME"], safe=""))')"
enc_key="$(python3 -c 'import os,urllib.parse as u; print(u.quote(os.environ["PRINT_KEY"], safe=""))')"

lpadmin -p "$LOCAL" -E -v "ipps://$enc_user:$enc_key@$SERVER/printers/$QUEUE" -m everywhere -o auth-info-required=none
echo "Printer '$LOCAL' added. Print as usual; you will not be asked for a password."
echo "If it fails with a certificate error, install your organisation's CA:  sudo trust anchor <ca.crt>"
