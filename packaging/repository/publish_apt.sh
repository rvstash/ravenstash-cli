#!/usr/bin/env bash
set -euo pipefail

readonly repository="${1:?repository path is required}"
readonly endpoint="${2:?R2 endpoint is required}"
readonly bucket="${3:?R2 bucket is required}"
# shellcheck source=../identity.env disable=SC1091
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/../identity.env"
readonly destination="s3://${bucket}/${RAVENSTASH_CLI_NAMESPACE}/apt"
readonly keyring_name="$RAVENSTASH_CLI_KEYRING"

test -d "$repository/dists"
test -n "$(find "$repository/dists" -mindepth 1 -maxdepth 1 -type d -print -quit)"

# Upload immutable package and by-hash objects first.
aws s3 sync "$repository/pool/" "${destination}/pool/" \
  --endpoint-url "$endpoint" \
  --cache-control "public,max-age=31536000,immutable" \
  --no-progress
aws s3 sync "$repository/dists/" "${destination}/dists/" \
  --endpoint-url "$endpoint" \
  --cache-control "public,max-age=31536000,immutable" \
  --exclude "*" \
  --include "*/by-hash/SHA256/*" \
  --no-progress

# Mutable indexes follow. Each signed distribution becomes active only when its
# InRelease lands, and channel discovery becomes active last.
aws s3 cp "$repository/$keyring_name" "${destination}/${keyring_name}" \
  --endpoint-url "$endpoint" \
  --cache-control "no-cache" \
  --only-show-errors
aws s3 sync "$repository/dists/" "${destination}/dists/" \
  --endpoint-url "$endpoint" \
  --cache-control "no-cache" \
  --exclude "*" \
  --include "*/Packages" \
  --include "*/Packages.gz" \
  --no-progress
aws s3 sync "$repository/dists/" "${destination}/dists/" \
  --endpoint-url "$endpoint" \
  --cache-control "no-cache" \
  --exclude "*" \
  --include "*/Release" \
  --include "*/Release.gpg" \
  --no-progress
aws s3 sync "$repository/dists/" "${destination}/dists/" \
  --endpoint-url "$endpoint" \
  --cache-control "no-cache" \
  --exclude "*" \
  --include "*/InRelease" \
  --no-progress
aws s3 cp "$repository/channels.json" "${destination}/channels.json" \
  --endpoint-url "$endpoint" \
  --cache-control "no-cache" \
  --only-show-errors
aws s3 cp "$repository/channels.json.gpg" "${destination}/channels.json.gpg" \
  --endpoint-url "$endpoint" \
  --cache-control "no-cache" \
  --only-show-errors
