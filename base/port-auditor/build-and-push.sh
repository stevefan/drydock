#!/bin/bash
# Build and push drydock-port-auditor multi-arch image to GHCR.
#
# Usage:   ./base/port-auditor/build-and-push.sh <version>
# Example: ./base/port-auditor/build-and-push.sh v0.1.0
#
# Prerequisites:
#   docker login ghcr.io -u stevefan -p $GITHUB_PAT  (write:packages scope)
#   Run from repo root — the build context IS the repo root because
#   the Dockerfile pip-installs the local drydock package.
#
# Tags pushed: <version>, the major-version pointer, and latest.
#
# This image is referenced by core/auditor/role_validator.py
# (_APPROVED_IMAGE_PREFIXES). Renaming the image in GHCR requires
# updating that allowlist.
set -euo pipefail

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    echo "Error: version required (e.g. v0.1.0)" >&2
    echo "Usage: $0 <version>" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

MAJOR=$(echo "$VERSION" | sed -E 's/^(v[0-9]+).*/\1/')

echo "Building drydock-port-auditor $VERSION (major: $MAJOR) for linux/amd64 + linux/arm64..."

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --push \
  -f base/port-auditor/Dockerfile \
  -t "ghcr.io/stevefan/drydock-port-auditor:$VERSION" \
  -t "ghcr.io/stevefan/drydock-port-auditor:$MAJOR" \
  -t "ghcr.io/stevefan/drydock-port-auditor:latest" \
  .

echo "Pushed: $VERSION, $MAJOR, latest"
