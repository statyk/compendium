#!/bin/sh
set -eu

CERT_DIR="/etc/nginx/certs"
CERT_FILE="${CERT_DIR}/fullchain.pem"
KEY_FILE="${CERT_DIR}/privkey.pem"
CERT_CN="${COMPENDIUM_CERT_CN:-compendium.local}"

mkdir -p "${CERT_DIR}"

if [ -s "${CERT_FILE}" ] && [ -s "${KEY_FILE}" ]; then
    echo "[nginx] Using existing certificate at ${CERT_FILE}"
else
    echo "[nginx] No certificate found; generating self-signed cert for CN=${CERT_CN}..."
    openssl req -x509 -nodes -days 365 -newkey rsa:4096 \
        -keyout "${KEY_FILE}" \
        -out "${CERT_FILE}" \
        -subj "/CN=${CERT_CN}" \
        -addext "subjectAltName=DNS:${CERT_CN},DNS:localhost,IP:127.0.0.1" \
        >/dev/null 2>&1
    chmod 600 "${KEY_FILE}"
    echo "[nginx] Self-signed cert written to ${CERT_FILE} (valid 365 days)."
fi

exec nginx -g 'daemon off;'
