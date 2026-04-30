#!/bin/sh
# Compendium — install scheduled-maintenance crontab for a bare-metal install.
#
# Appends docs/crontab.sample to the current user's crontab, with two
# placeholders substituted:
#   COMPENDIUM_DIR  → absolute path to the Compendium project (default: $(pwd))
#   LOG_REDIRECT    → either ">> /path/to/log 2>&1" (file mode) or
#                     "2>&1 | logger -t compendium-maintenance" (journal)
#
# Idempotent: re-running detects a previous install and refuses to duplicate.
#
# Usage:
#   scripts/install-cron.sh [--project-dir PATH] [--log-file PATH | --log-file journal]
#
# Defaults:
#   --project-dir $(pwd)
#   --log-file $HOME/.local/state/compendium/maintenance.log
#
# Override examples:
#   scripts/install-cron.sh --log-file journal
#   scripts/install-cron.sh --log-file /var/log/compendium/maintenance.log
#   scripts/install-cron.sh --project-dir /opt/compendium
#
# For paths outside writable territory (e.g. /var/log/...), the installer
# expects the directory to already exist and be writable. It prints one-time
# setup commands if not, and exits without modifying crontab.

set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SAMPLE="${REPO_DIR}/docs/crontab.sample"
TAG_BEGIN="# >>> compendium maintenance >>>"
TAG_END="# <<< compendium maintenance <<<"

PROJECT_DIR="$(pwd)"
LOG_DEST="${HOME}/.local/state/compendium/maintenance.log"

while [ $# -gt 0 ]; do
    case "$1" in
        --project-dir)
            shift
            if [ $# -eq 0 ]; then
                echo "Error: --project-dir needs a path." >&2
                exit 1
            fi
            PROJECT_DIR="$1"
            ;;
        --project-dir=*)
            PROJECT_DIR="${1#--project-dir=}"
            ;;
        --log-file)
            shift
            if [ $# -eq 0 ]; then
                echo "Error: --log-file needs a value (path or 'journal')." >&2
                exit 1
            fi
            LOG_DEST="$1"
            ;;
        --log-file=*)
            LOG_DEST="${1#--log-file=}"
            ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Error: unknown argument: $1" >&2
            exit 1
            ;;
    esac
    shift
done

PROJECT_DIR="$(cd "${PROJECT_DIR}" 2>/dev/null && pwd)" || {
    echo "Error: --project-dir does not exist or isn't a directory." >&2
    exit 1
}

if [ ! -f "${SAMPLE}" ]; then
    echo "Error: ${SAMPLE} not found." >&2
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "Warning: 'uv' is not on PATH for this shell." >&2
    echo "         Cron jobs run under your login shell; if uv isn't on its" >&2
    echo "         PATH the maintenance commands will fail. Install uv or" >&2
    echo "         edit the rendered crontab to use an absolute path." >&2
fi

# Resolve the LOG_REDIRECT replacement string.
if [ "${LOG_DEST}" = "journal" ]; then
    if ! command -v logger >/dev/null 2>&1; then
        echo "Error: 'logger' not found on PATH; cannot use --log-file journal." >&2
        exit 1
    fi
    REDIRECT='2>\&1 | logger -t compendium-maintenance'
    LOG_DESCRIPTION="systemd journal (view with: journalctl -t compendium-maintenance -f)"
else
    LOG_DIR="$(dirname -- "${LOG_DEST}")"
    if [ -d "${LOG_DIR}" ]; then
        if [ ! -w "${LOG_DIR}" ]; then
            echo "Error: log directory '${LOG_DIR}' is not writable by '$(id -un)'." >&2
            echo "Run once as root to grant ownership:" >&2
            echo "  sudo chown $(id -un) -- ${LOG_DIR}" >&2
            echo "Then re-run install-cron.sh --log-file '${LOG_DEST}'." >&2
            exit 1
        fi
    elif ! mkdir -p -- "${LOG_DIR}" 2>/dev/null; then
        echo "Error: cannot create log directory '${LOG_DIR}' as '$(id -un)'." >&2
        echo "Run once as root to create it:" >&2
        echo "  sudo install -d -o $(id -un) -- ${LOG_DIR}" >&2
        echo "Then re-run install-cron.sh --log-file '${LOG_DEST}'." >&2
        exit 1
    fi
    REDIRECT=">> ${LOG_DEST} 2>\&1"
    LOG_DESCRIPTION="${LOG_DEST}"
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

# `!` delimiter avoids slash-escaping for paths AND avoids colliding with
# the `|` in the journal-mode redirect's pipe-to-logger.
RENDERED="$(sed \
    -e "s!COMPENDIUM_DIR!${PROJECT_DIR}!g" \
    -e "s!LOG_REDIRECT!${REDIRECT}!g" \
    "${SAMPLE}")"

{
    printf '%s\n' "${EXISTING}"
    printf '%s\n' "${TAG_BEGIN}"
    printf '%s\n' "${RENDERED}"
    printf '%s\n' "${TAG_END}"
} | crontab -

echo "Installed Compendium maintenance crontab. Run 'crontab -l' to view."
echo "Project: ${PROJECT_DIR}"
echo "Logs: ${LOG_DESCRIPTION}"
