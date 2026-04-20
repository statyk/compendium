#!/bin/sh
set -eu

: "${COMPENDIUM_ADMIN_USERNAME:=admin}"
: "${COMPENDIUM_ADMIN_PASSWORD:=compendium}"
: "${COMPENDIUM_ADMIN_ROLE:=Librarian}"

echo "[compendium] Running database migrations..."
compendium db init

if compendium user list --limit 1000 2>/dev/null \
        | awk '{print $1}' \
        | grep -qx "${COMPENDIUM_ADMIN_USERNAME}"; then
    echo "[compendium] Admin user '${COMPENDIUM_ADMIN_USERNAME}' already exists; skipping bootstrap."
else
    echo "[compendium] Creating admin user '${COMPENDIUM_ADMIN_USERNAME}' (role: ${COMPENDIUM_ADMIN_ROLE})..."
    compendium user add \
        --username "${COMPENDIUM_ADMIN_USERNAME}" \
        --password "${COMPENDIUM_ADMIN_PASSWORD}" \
        --role "${COMPENDIUM_ADMIN_ROLE}"
fi

echo "[compendium] Starting server on 0.0.0.0:8000..."
exec compendium serve --host 0.0.0.0 --port 8000
