#!/bin/bash
# Build and push drydock-base multi-arch image to GHCR.
#
# Usage:   ./base/build-and-push.sh <version>    (e.g. v1.0.1)
# Example: ./base/build-and-push.sh v1.0.1
#
# Prerequisites:
#   docker login ghcr.io -u stevefan -p $GITHUB_PAT
#   The PAT needs write:packages scope.
#
# Tags pushed: <version>, the major-version pointer (e.g. v1), and latest.
set -euo pipefail

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
    echo "Error: version required (e.g. v1.0.1)" >&2
    echo "Usage: $0 <version>" >&2
    exit 1
fi

# Derive major pointer: v1.0.1 -> v1
MAJOR=$(echo "$VERSION" | sed -E 's/^(v[0-9]+).*/\1/')

echo "Building drydock-base $VERSION (major: $MAJOR) for linux/amd64 + linux/arm64..."

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --push \
  -t "ghcr.io/stevefan/drydock-base:$VERSION" \
  -t "ghcr.io/stevefan/drydock-base:$MAJOR" \
  -t "ghcr.io/stevefan/drydock-base:latest" \
  base/

echo "Pushed: $VERSION, $MAJOR, latest"
