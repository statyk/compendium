#!/bin/sh
# Compendium — install scheduled-maintenance crontab for the Docker deployment.
#
# Appends docker/crontab.sample to the current user's crontab, replacing the
# COMPENDIUM_DIR placeholder with the absolute path to the docker/ directory.
# Idempotent: re-running detects a previous install and refuses to duplicate.
#
# Usage: docker/install-cron.sh

set -eu

DOCKER_DIR="$(cd "$(dirname "$0")" && pwd)"
SAMPLE="${DOCKER_DIR}/crontab.sample"
TAG_BEGIN="# >>> compendium docker maintenance >>>"
TAG_END="# <<< compendium docker maintenance <<<"

if [ ! -f "${SAMPLE}" ]; then
    echo "Error: ${SAMPLE} not found." >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "Error: 'docker' is not on PATH for this user." >&2
    echo "       cron jobs run under your shell, so docker must be invokable here." >&2
    exit 1
fi

EXISTING="$(crontab -l 2>/dev/null || true)"

if printf '%s' "${EXISTING}" | grep -qF "${TAG_BEGIN}"; then
    echo "Compendium maintenance block already present in this crontab." >&2
    echo "To reinstall, remove the block between:" >&2
    echo "  ${TAG_BEGIN}" >&2
    echo "  ${TAG_END}" >&2
    echo "with 'crontab -e', then re-run this script." >&2
    exit 1
fi

RENDERED="$(sed "s|COMPENDIUM_DIR|${DOCKER_DIR}|g" "${SAMPLE}")"

{
    printf '%s\n' "${EXISTING}"
    printf '%s\n' "${TAG_BEGIN}"
    printf '%s\n' "${RENDERED}"
    printf '%s\n' "${TAG_END}"
} | crontab -

echo "Installed Compendium maintenance crontab. Run 'crontab -l' to view."
echo "Logs will be written to /var/log/compendium-maintenance.log — make sure"
echo "your user can write there (or edit the redirects)."
