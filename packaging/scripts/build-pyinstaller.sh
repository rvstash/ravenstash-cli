#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

PYINSTALLER_BIN="${PYINSTALLER:-pyinstaller}"
if ! command -v "$PYINSTALLER_BIN" >/dev/null 2>&1; then
  if [[ -x .venv/bin/pyinstaller ]]; then
    PYINSTALLER_BIN=".venv/bin/pyinstaller"
  else
    echo "error: required command 'pyinstaller' was not found on PATH or at .venv/bin/pyinstaller" >&2
    exit 127
  fi
fi

mkdir -p build/pyinstaller dist/pyinstaller
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}" PYTHONHASHSEED=1 "$PYINSTALLER_BIN" \
  --clean \
  --noconfirm \
  --distpath dist/pyinstaller \
  --workpath build/pyinstaller \
  packaging/pyinstaller/rvs.spec

ln -sfn rvs dist/pyinstaller/rvs/ravenstash
ln -sfn rvs dist/pyinstaller/rvs/docker-credential-rvs
dist/pyinstaller/rvs/rvs --version
dist/pyinstaller/rvs/rvs --help >/dev/null
dist/pyinstaller/rvs/ravenstash --version
test -x dist/pyinstaller/rvs/docker-credential-rvs

if ! find dist/pyinstaller/rvs -maxdepth 2 -type f -name 'libpython3.14.so*' \
  -print -quit | grep -q .; then
  echo "error: frozen launcher does not identify a Python 3.14 runtime" >&2
  exit 1
fi
