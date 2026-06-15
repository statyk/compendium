#!/bin/sh
# Compendium one-command Docker installer.
#
#   curl -fsSL https://raw.githubusercontent.com/statyk/compendium/master/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- ./my-library --image ghcr.io/statyk/compendium:1.3.0
#
# Scaffolds a deployment bundle via the published image, then brings it up.
set -eu

IMAGE="${COMPENDIUM_IMAGE:-ghcr.io/statyk/compendium:latest}"
TARGET="compendium"
FORCE=""
ASSUME_YES=""

while [ $# -gt 0 ]; do
    case "$1" in
        --image) [ $# -ge 2 ] || { echo "--image needs a value" >&2; exit 2; }; IMAGE="$2"; shift 2 ;;
        --force) FORCE="--force"; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -*) echo "Unknown option: $1" >&2; exit 2 ;;
        *) TARGET="$1"; shift ;;
    esac
done

# 1. Prereqs
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed. See https://docs.docker.com/get-docker/" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "ERROR: 'docker compose' (v2) is required. Update Docker Desktop / the compose plugin." >&2
    exit 1
fi

# 2. Prompt (only when interactive and not --yes)
ADMIN_USER="admin"
ADMIN_PASS=""
CERT_CN="compendium.local"
if [ -t 0 ] && [ -z "$ASSUME_YES" ]; then
    printf "Admin username [admin]: "; read -r _u || true; [ -n "${_u:-}" ] && ADMIN_USER="$_u"
    printf "Admin password [auto-generate]: "; read -r _p || true; ADMIN_PASS="${_p:-}"
    printf "Hostname / TLS CN [compendium.local]: "; read -r _h || true; [ -n "${_h:-}" ] && CERT_CN="$_h"
fi

# 3. Pull image
echo "Pulling $IMAGE ..."
docker pull "$IMAGE"

# 4. Scaffold inside the image, writing to the host dir as the current user.
# --entrypoint overrides the image's default entrypoint (which migrates + serves);
# without it, `compendium init …` would be passed as args to that entrypoint and
# never run.
mkdir -p "$TARGET"
ABS_TARGET=$(cd "$TARGET" && pwd)
set -- init . --admin-username "$ADMIN_USER" --cert-cn "$CERT_CN"
[ -n "$FORCE" ] && set -- "$@" "$FORCE"
[ -n "$ADMIN_PASS" ] && set -- "$@" --admin-password "$ADMIN_PASS"
docker run --rm --user "$(id -u):$(id -g)" -v "$ABS_TARGET":/out -w /out \
    --entrypoint compendium "$IMAGE" "$@"

# 5. Up
echo "Starting the stack ..."
( cd "$ABS_TARGET" && docker compose up -d )

echo ""
echo "Compendium is starting. Browse to https://${CERT_CN}/ (self-signed cert warning is expected)."
echo "Deployment files are in: $ABS_TARGET"
echo "Optional: cd $ABS_TARGET && ./install-cron.sh   # scheduled maintenance"
