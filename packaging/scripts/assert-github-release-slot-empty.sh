#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=common.sh disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

readonly repository="${1:?GitHub owner/repository is required}"
readonly tag="${2:?release tag is required}"
[[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]
[[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(rc[1-9][0-9]*)?$ ]]
require_cmd gh

scratch="$(mktemp -d)"
readonly scratch
trap 'rm -rf -- "$scratch"' EXIT

# Use successful list endpoints rather than treating a failed single-object
# lookup as absence. Any API or authentication failure must stop publication.
gh api "/repos/${repository}/git/matching-refs/tags/${tag}" \
  --jq ".[] | select(.ref == \"refs/tags/${tag}\") | .ref" \
  > "$scratch/tags"
if [[ -s "$scratch/tags" ]]; then
  echo "error: release tag already exists: $tag" >&2
  exit 1
fi

gh api --paginate "/repos/${repository}/releases?per_page=100" \
  --jq ".[] | select(.tag_name == \"${tag}\") | .id" \
  > "$scratch/releases"
if [[ -s "$scratch/releases" ]]; then
  echo "error: draft or published release already exists: $tag" >&2
  exit 1
fi
