#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_cmd dpkg-deb

VERSION="${RAVENSTASH_CLI_VERSION:-$(rvs_version)}"
DEBIAN_VERSION="$(rvs_debian_version "$VERSION")"
ARCH="${RAVENSTASH_CLI_ARCH:-$(rvs_arch)}"
if [[ "$ARCH" == "unsupported" ]]; then
  echo "error: unsupported architecture $(uname -m)" >&2
  exit 1
fi

if [[ ! -x dist/pyinstaller/rvs/rvs ]]; then
  packaging/scripts/build-pyinstaller.sh
fi

PACKAGE="$RAVENSTASH_CLI_PACKAGE"
STAGING="build/deb/${PACKAGE}_${VERSION}_${ARCH}"
rm -rf "$STAGING"
mkdir -p \
  "$STAGING/DEBIAN" \
  "$STAGING/usr/lib/$PACKAGE" \
  "$STAGING/usr/bin" \
  "$STAGING/usr/share/doc/$PACKAGE"
cp -a dist/pyinstaller/rvs/. "$STAGING/usr/lib/$PACKAGE/"
ln -s "../lib/$PACKAGE/rvs" "$STAGING/usr/bin/rvs"
ln -s "../lib/$PACKAGE/rvs" "$STAGING/usr/bin/ravenstash"
ln -s "../lib/$PACKAGE/docker-credential-rvs" "$STAGING/usr/bin/docker-credential-rvs"
cp README.md "$STAGING/usr/share/doc/$PACKAGE/README.md"
cp LICENSE "$STAGING/usr/share/doc/$PACKAGE/LICENSE"
cp NOTICE "$STAGING/usr/share/doc/$PACKAGE/NOTICE"
install -m 0755 packaging/scripts/postinstall.sh "$STAGING/DEBIAN/postinst"
cat > "$STAGING/DEBIAN/control" <<EOF
Package: ${PACKAGE}
Version: ${DEBIAN_VERSION}
Architecture: ${ARCH}
Maintainer: Ravenstash <support@ravenstash.com>
Depends: ca-certificates, gpgv
Section: devel
Priority: optional
Homepage: https://ravenstash.com
Description: Ravenstash developer CLI
 A self-contained Python 3.14 command-line client for Ravenstash.
EOF

find "$STAGING" -exec touch -h -d "@${SOURCE_DATE_EPOCH:-0}" {} +
mkdir -p dist/packages
dpkg-deb --root-owner-group --build "$STAGING" "dist/packages/${PACKAGE}_${VERSION}_${ARCH}.deb"
