#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_cmd apt-ftparchive
require_cmd dpkg-deb
require_cmd gzip
require_cmd python3
require_cmd sha256sum

VERSION="${RAVENSTASH_CLI_VERSION:-$(rvs_version)}"
ARCH="${RAVENSTASH_CLI_ARCH:-$(rvs_arch)}"
PACKAGE="$RAVENSTASH_CLI_PACKAGE"
APT_REPO_DIR="${APT_REPO_DIR:-dist/apt}"
POLICY_SCRIPT="packaging/repository/channel_policy.py"
DEFAULT_CHANNEL="$(python3 "$POLICY_SCRIPT" channel-for-version "$VERSION")"
CODENAME="${RAVENSTASH_CLI_APT_CHANNEL:-$DEFAULT_CHANNEL}"
COMPONENT="${RAVENSTASH_CLI_APT_COMPONENT:-main}"
DEB="dist/packages/${PACKAGE}_${VERSION}_${ARCH}.deb"
python3 "$POLICY_SCRIPT" validate "$VERSION" "$CODENAME"

if [[ ! -f "$DEB" ]]; then
  echo "error: package not found: $DEB" >&2
  echo "run packaging/scripts/build-deb.sh first" >&2
  exit 1
fi

mkdir -p "$APT_REPO_DIR"
APT_REPO_DIR="$(cd "$APT_REPO_DIR" && pwd)"

POOL_DIR="${APT_REPO_DIR}/pool/${COMPONENT}/${PACKAGE:0:1}/${PACKAGE}"
BINARY_DIR="${APT_REPO_DIR}/dists/${CODENAME}/${COMPONENT}/binary-${ARCH}"
mkdir -p "$POOL_DIR" "$BINARY_DIR"
DEB_SHA256="$(sha256sum "$DEB" | awk '{print $1}')"
DEB_TARGET="${POOL_DIR}/${PACKAGE}_${VERSION}_${ARCH}_${DEB_SHA256:0:16}.deb"
mapfile -t SAME_VERSION < <(
  find "$POOL_DIR" -maxdepth 1 -type f -name "${PACKAGE}_${VERSION}_${ARCH}_*.deb" -print
)
for existing in "${SAME_VERSION[@]}"; do
  if [[ "$existing" != "$DEB_TARGET" ]]; then
    echo "error: version ${VERSION} already exists with a different digest" >&2
    exit 1
  fi
done
if [[ -e "$DEB_TARGET" ]]; then
  test "$(sha256sum "$DEB_TARGET" | awk '{print $1}')" = "$DEB_SHA256"
else
  cp "$DEB" "$DEB_TARGET"
fi

(cd "$APT_REPO_DIR" && apt-ftparchive packages pool) \
  | python3 "$POLICY_SCRIPT" filter "$CODENAME" \
  > "${BINARY_DIR}/Packages"
gzip -9n < "${BINARY_DIR}/Packages" > "${BINARY_DIR}/Packages.gz"

for index in Packages Packages.gz; do
  digest="$(sha256sum "${BINARY_DIR}/${index}" | awk '{print $1}')"
  by_hash="${BINARY_DIR}/by-hash/SHA256/${digest}"
  mkdir -p "$(dirname "$by_hash")"
  if [[ ! -e "$by_hash" ]]; then
    cp "${BINARY_DIR}/${index}" "$by_hash"
  fi
done

VALID_UNTIL="$(
  date --utc --date="+${RAVENSTASH_CLI_APT_VALID_DAYS:-7} days" --rfc-email
)"

cat > "${APT_REPO_DIR}/apt-ftparchive-release.conf" <<EOF
APT::FTPArchive::Release {
  Origin "Ravenstash";
  Label "Ravenstash";
  Suite "${CODENAME}";
  Codename "${CODENAME}";
  Architectures "${ARCH}";
  Components "${COMPONENT}";
  Description "Ravenstash CLI packages";
  Acquire-By-Hash "yes";
  Valid-Until "${VALID_UNTIL}";
};
EOF

rm -f \
  "${APT_REPO_DIR}/dists/${CODENAME}/InRelease" \
  "${APT_REPO_DIR}/dists/${CODENAME}/Release" \
  "${APT_REPO_DIR}/dists/${CODENAME}/Release.gpg"
RELEASE_UNSIGNED="${APT_REPO_DIR}/Release.unsigned"
apt-ftparchive \
  -c "${APT_REPO_DIR}/apt-ftparchive-release.conf" \
  release "${APT_REPO_DIR}/dists/${CODENAME}" \
  > "$RELEASE_UNSIGNED"
sed "/^Date:/a Valid-Until: ${VALID_UNTIL}" "$RELEASE_UNSIGNED" \
  | sed "/^Date:/a Ravenstash-Compatibility-Channel: ${CODENAME}" \
  > "${APT_REPO_DIR}/dists/${CODENAME}/Release"
rm -f "$RELEASE_UNSIGNED"

if [[ -n "${RAVENSTASH_CLI_APT_GPG_KEY_ID:-}" ]]; then
  require_cmd gpg
  if [[ -z "${RAVENSTASH_CLI_APT_GPG_FINGERPRINT:-}" ]]; then
    echo "error: RAVENSTASH_CLI_APT_GPG_FINGERPRINT is required for signing" >&2
    exit 1
  fi
  actual_fingerprint="$(
    gpg --batch --with-colons --fingerprint "$RAVENSTASH_CLI_APT_GPG_KEY_ID" \
      | awk -F: '$1 == "fpr" { print $10; exit }'
  )"
  if [[ "$actual_fingerprint" != "$RAVENSTASH_CLI_APT_GPG_FINGERPRINT" ]]; then
    echo "error: signing-key fingerprint does not match the approved fingerprint" >&2
    exit 1
  fi
  GPG_ARGS=(--batch --yes --local-user "$RAVENSTASH_CLI_APT_GPG_KEY_ID")
  if [[ -n "${RAVENSTASH_CLI_APT_GPG_PASSPHRASE_FILE:-}" ]]; then
    if [[ ! -f "$RAVENSTASH_CLI_APT_GPG_PASSPHRASE_FILE" ]]; then
      echo "error: GPG passphrase file not found: $RAVENSTASH_CLI_APT_GPG_PASSPHRASE_FILE" >&2
      exit 1
    fi
    GPG_ARGS+=(--pinentry-mode loopback --passphrase-file "$RAVENSTASH_CLI_APT_GPG_PASSPHRASE_FILE")
  fi

  gpg "${GPG_ARGS[@]}" \
    --digest-algo SHA512 \
    --output "${APT_REPO_DIR}/dists/${CODENAME}/InRelease" \
    --clearsign "${APT_REPO_DIR}/dists/${CODENAME}/Release"
  gpg "${GPG_ARGS[@]}" \
    --digest-algo SHA512 \
    --output "${APT_REPO_DIR}/dists/${CODENAME}/Release.gpg" \
    --detach-sign "${APT_REPO_DIR}/dists/${CODENAME}/Release"
  gpg --batch --yes \
    --output "${APT_REPO_DIR}/${RAVENSTASH_CLI_KEYRING}" \
    --export "$RAVENSTASH_CLI_APT_GPG_KEY_ID"
else
  echo "warning: RAVENSTASH_CLI_APT_GPG_KEY_ID not set; APT repo metadata is unsigned" >&2
fi
