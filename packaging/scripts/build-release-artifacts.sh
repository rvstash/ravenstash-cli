#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_cmd sha256sum
require_cmd cyclonedx-py
require_cmd uv

VERSION="${RAVENSTASH_CLI_VERSION:-$(rvs_version)}"
ARCH="${RAVENSTASH_CLI_ARCH:-$(rvs_arch)}"
STEM="$RAVENSTASH_CLI_ASSET_STEM"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || echo 0)}"

packaging/scripts/build-pyinstaller.sh
RAVENSTASH_CLI_VERSION="$VERSION" RAVENSTASH_CLI_ARCH="$ARCH" packaging/scripts/build-deb.sh
RAVENSTASH_CLI_VERSION="$VERSION" RAVENSTASH_CLI_ARCH="$ARCH" packaging/scripts/build-tarball.sh
# Exercise the bundle and unpack the archive exactly as `rvs update --apply` does.
uv run --no-sync python packaging/scripts/smoke_portable.py \
  "build/release/${STEM}-v${VERSION}-linux-${ARCH}"

mkdir -p dist/release
cp "dist/packages/${RAVENSTASH_CLI_PACKAGE}_${VERSION}_${ARCH}.deb" dist/release/
install -m 0755 packaging/install.sh "dist/release/${STEM}-install.sh"
uv export \
  --frozen \
  --no-dev \
  --no-group release \
  --no-emit-project \
  --format requirements-txt \
  --output-file build/release/runtime-requirements.txt
cyclonedx-py requirements \
  build/release/runtime-requirements.txt \
  --pyproject pyproject.toml \
  --spec-version 1.6 \
  --output-reproducible \
  --output-format JSON \
  --output-file "dist/release/${STEM}-v${VERSION}-sbom.cdx.json"

(
  cd dist/release
  rm -f "${STEM}-v${VERSION}-checksums.txt"
  find . -maxdepth 1 -type f ! -name '*-checksums.txt' -printf '%f\n' \
    | LC_ALL=C sort \
    | xargs sha256sum > "${STEM}-v${VERSION}-checksums.txt"
)
