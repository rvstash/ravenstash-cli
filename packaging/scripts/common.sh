#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# Every package, asset, and storage name comes from the release identity.
# shellcheck source=../identity.env disable=SC1091
source "$ROOT/packaging/identity.env"

require_cmd() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "error: required command '$name' was not found on PATH" >&2
    exit 127
  fi
}

rvs_version() {
  python3 - <<'PY'
from pathlib import Path
import tomllib

with Path("pyproject.toml").open("rb") as f:
    print(tomllib.load(f)["project"]["version"])
PY
}

rvs_debian_version() {
  local version="${1:-$(rvs_version)}"
  if [[ "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+)\.dev([1-9][0-9]*)\+g([0-9a-f]{8})$ ]]; then
    printf '%s~dev%s+g%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
    return
  fi
  if [[ "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+)rc([0-9]+)$ ]]; then
    printf '%s~rc%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    return
  fi
  if [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf '%s\n' "$version"
    return
  fi
  echo "error: unsupported rvs release version '$version'" >&2
  return 2
}

rvs_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo "amd64" ;;
    aarch64|arm64) echo "arm64" ;;
    *) echo "unsupported" ;;
  esac
}
