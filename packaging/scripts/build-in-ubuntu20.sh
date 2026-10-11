#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_cmd docker
require_cmd git

BUILDER_IMAGE="${RAVENSTASH_CLI_BUILDER_IMAGE:-${RAVENSTASH_CLI_PACKAGE}-builder-ubuntu20}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}"
docker build \
  --file packaging/Dockerfile.ubuntu20 \
  --tag "$BUILDER_IMAGE" \
  .
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --volume "$PWD:/workspace" \
  --env HOME=/tmp/rvs-build-home \
  --env SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-}" \
  "$BUILDER_IMAGE" \
  "set -euo pipefail; \
   shellcheck packaging/install.sh; \
   bash -n packaging/install.sh; \
   export UV_PROJECT_ENVIRONMENT=/tmp/rvs-build-venv; \
   uv sync --frozen --python 3.14 --group release; \
   export PATH=/tmp/rvs-build-venv/bin:\$PATH; \
   packaging/scripts/build-release-artifacts.sh"
