#!/bin/sh

set -eu

TRIVY_IMAGE="aquasec/trivy:0.74.0@sha256:62b1e65e8869bc4b4c6aa4fa2b21595256c7c2f6018a9d9ad61caf87187c1969"
TRIVY_CACHE_VOLUME="${TRIVY_CACHE_VOLUME:-recipes-trivy-cache}"
FRONTEND_IMAGE="recipes-frontend-build:node24.20.0"
SCRIPT_DIRECTORY=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPOSITORY_ROOT=$(CDPATH= cd -- "$SCRIPT_DIRECTORY/.." && pwd)
BUILD_IMAGES=1

if [ "${1:-}" = "--skip-build" ]; then
    BUILD_IMAGES=0
    shift
fi

if [ "$#" -ne 0 ]; then
    echo "Usage: $0 [--skip-build]" >&2
    exit 2
fi

cd "$REPOSITORY_ROOT"
docker compose config --quiet

if [ "$BUILD_IMAGES" -eq 1 ]; then
    docker build --pull --target frontend --tag "$FRONTEND_IMAGE" .
    docker compose build --pull app proxy
    docker compose pull --ignore-buildable db redis
fi

scan_image() {
    image_reference=$1
    ignore_file=${2:-}

    image_id=$(docker image inspect --format '{{.Id}}' "$image_reference")
    echo "Scanning $image_reference ($image_id)"

    if [ -n "$ignore_file" ]; then
        docker run --rm \
            -v /var/run/docker.sock:/var/run/docker.sock \
            -v "$TRIVY_CACHE_VOLUME:/root/.cache/trivy" \
            -v "$REPOSITORY_ROOT:/workspace:ro" \
            "$TRIVY_IMAGE" image \
            --scanners vuln \
            --severity HIGH,CRITICAL \
            --ignore-unfixed \
            --ignorefile "/workspace/$ignore_file" \
            --show-suppressed \
            --table-mode detailed \
            --exit-code 1 \
            "$image_id"
    else
        docker run --rm \
            -v /var/run/docker.sock:/var/run/docker.sock \
            -v "$TRIVY_CACHE_VOLUME:/root/.cache/trivy" \
            "$TRIVY_IMAGE" image \
            --scanners vuln \
            --severity HIGH,CRITICAL \
            --ignore-unfixed \
            --table-mode detailed \
            --exit-code 1 \
            "$image_id"
    fi
}

scan_image "$FRONTEND_IMAGE"

for image_reference in $(docker compose config --images | sort -u); do
    case "$image_reference" in
        postgres:*)
            scan_image "$image_reference" security/trivy/postgres.yaml
            ;;
        redis:*)
            scan_image "$image_reference" security/trivy/redis.yaml
            ;;
        recipes-app:* | recipes-caddy:*)
            scan_image "$image_reference"
            ;;
        *)
            echo "No scan policy is defined for $image_reference" >&2
            exit 1
            ;;
    esac
done
