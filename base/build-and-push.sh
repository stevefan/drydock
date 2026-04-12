#!/bin/bash
# Build and push drydock-base multi-arch image to GHCR.
# Prerequisites: docker login ghcr.io -u stevefan -p $GITHUB_PAT
# The PAT needs write:packages scope.
set -euo pipefail

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --push \
  -t ghcr.io/stevefan/drydock-base:v1.0.0 \
  -t ghcr.io/stevefan/drydock-base:v1 \
  -t ghcr.io/stevefan/drydock-base:latest \
  base/
