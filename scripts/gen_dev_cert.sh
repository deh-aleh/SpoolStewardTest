#!/bin/sh
# DEV ONLY self-signed certificate. Production: supply a real cert via SPOOLSTEWARD_TLS_CERTFILE/KEYFILE.
set -e
dir="${1:-certs}"
mkdir -p "$dir"
openssl req -x509 -newkey rsa:3072 -nodes -days 365 \
  -keyout "$dir/dev.key" -out "$dir/dev.crt" -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
chmod 600 "$dir/dev.key"
echo "created $dir/dev.crt and $dir/dev.key"
