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

_is_trivial() {
    # Commands that never need the database (or must work without one).
    case "${1:-}" in
        --version|--help|-h|help|keygen|init) return 0 ;;
        *) return 1 ;;
    esac
}

_run_migrations() {
    echo "[compendium] Running database migrations..."
    if ! compendium db init; then
        _url="${COMPENDIUM_DATABASE_URL:-sqlite:///compendium.db}"
        # strip user:password@ credentials before printing
        _redacted="$(printf '%s' "${_url}" | sed -E 's#//[^@/]+@#//#')"
        echo "[compendium] Error: cannot reach or migrate database (${_redacted}); check COMPENDIUM_DATABASE_URL. Run 'compendium db init' in the container for details." >&2
        exit 1
    fi
}

# Explicit command: `docker run IMAGE compendium --version`, `docker run IMAGE sh`, …
if [ "$#" -gt 0 ]; then
    if [ "$1" = "compendium" ]; then
        if _is_trivial "${2:-}"; then
            exec "$@"
        fi
        _run_migrations
        exec "$@"
    fi
    case "$1" in
        # Bare flags reach the compendium CLI (only introspection flags exist
        # at the root level, so no DB needed).
        -*) exec compendium "$@" ;;
        *)  exec "$@" ;;
    esac
fi

_run_migrations

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
