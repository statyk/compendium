#!/bin/sh
set -eu

: "${COMPENDIUM_ADMIN_USERNAME:=admin}"
: "${COMPENDIUM_ADMIN_ROLE:=Administrator}"

# Support *_FILE variants for secrets (mirrors the pattern used by postgres/redis images).
# If POSTGRES_PASSWORD_FILE is set and COMPENDIUM_DATABASE_URL is not already set,
# assemble the database URL from the file contents.
if [ -n "${POSTGRES_PASSWORD_FILE:-}" ] && [ -z "${COMPENDIUM_DATABASE_URL:-}" ]; then
    if [ ! -r "${POSTGRES_PASSWORD_FILE}" ]; then
        echo "[compendium] ERROR: POSTGRES_PASSWORD_FILE='${POSTGRES_PASSWORD_FILE}' is not readable." >&2
        exit 1
    fi
    _pg_pass="$(cat "${POSTGRES_PASSWORD_FILE}")"
    export COMPENDIUM_DATABASE_URL="postgresql+psycopg://compendium:${_pg_pass}@db:5432/compendium"
    unset _pg_pass
fi

echo "[compendium] Running database migrations..."
compendium db init

if compendium user list --limit 1000 2>/dev/null \
        | awk '{print $1}' \
        | grep -qx "${COMPENDIUM_ADMIN_USERNAME}"; then
    echo "[compendium] Admin user '${COMPENDIUM_ADMIN_USERNAME}' already exists; skipping bootstrap."
else
    if [ -z "${COMPENDIUM_ADMIN_PASSWORD:-}" ]; then
        echo "[compendium] ERROR: COMPENDIUM_ADMIN_PASSWORD is required for first-run bootstrap." >&2
        echo "[compendium] Set it in .env (see .env.example) and restart." >&2
        exit 1
    fi
    echo "[compendium] Creating admin user '${COMPENDIUM_ADMIN_USERNAME}' (role: ${COMPENDIUM_ADMIN_ROLE})..."
    compendium user add \
        --username "${COMPENDIUM_ADMIN_USERNAME}" \
        --password "${COMPENDIUM_ADMIN_PASSWORD}" \
        --role "${COMPENDIUM_ADMIN_ROLE}"
fi

echo "[compendium] Starting server on 0.0.0.0:8000..."
exec compendium serve --host 0.0.0.0 --port 8000
