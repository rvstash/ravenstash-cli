#!/usr/bin/env bash
set -euo pipefail
umask 077

readonly prior_repository="${1:?prior repository path is required}"
readonly output_repository="${2:?output repository path is required}"
readonly trusted_keyring="${3:?trusted public keyring is required}"
shift 3
debs=("$@")
test "${#debs[@]}" -gt 0
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly script_dir
# shellcheck source=../identity.env disable=SC1091
source "$script_dir/../identity.env"
readonly package="$RAVENSTASH_CLI_PACKAGE"
readonly keyring_name="$RAVENSTASH_CLI_KEYRING"

: "${RAVENSTASH_CLI_APT_GPG_KEY_ID:?RAVENSTASH_CLI_APT_GPG_KEY_ID is required}"
: "${RAVENSTASH_CLI_APT_GPG_FINGERPRINT:?RAVENSTASH_CLI_APT_GPG_FINGERPRINT is required}"
: "${RAVENSTASH_CLI_APT_CHANNEL:?RAVENSTASH_CLI_APT_CHANNEL is required}"
readonly refresh_all="${RAVENSTASH_CLI_APT_REFRESH_ALL:-0}"
[[ "$refresh_all" == "0" || "$refresh_all" == "1" ]]

for command in apt-ftparchive date dpkg-deb find gpg gzip python3 sha256sum; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "error: required command is missing: $command" >&2
    exit 127
  }
done

previous_architectures=()
if compgen -G "$prior_repository/dists/*/InRelease" >/dev/null; then
  python3 "$script_dir/verify_apt.py" "$prior_repository" "$trusted_keyring"
  mapfile -t previous_architectures < <(
    find "$prior_repository/pool" -type f -name '*.deb' -print0 \
      | xargs -0 -r -n1 dpkg-deb --field 2>/dev/null \
      | awk '$1 == "Architecture:" { print $2 }' \
      | sort -u
  )
  mkdir -p "$output_repository"
  cp -a "$prior_repository/." "$output_repository/"
else
  test -f "$prior_repository/BOOTSTRAP"
  mkdir -p "$output_repository"
fi

pool="$output_repository/pool/main/${package:0:1}/$package"
mkdir -p "$pool"
declare -A submitted_architectures=()
submitted_version=""
for deb in "${debs[@]}"; do
  package_name="$(dpkg-deb --field "$deb" Package)"
  version="$(dpkg-deb --field "$deb" Version)"
  architecture="$(dpkg-deb --field "$deb" Architecture)"
  test "$package_name" = "$package"
  [[ "$architecture" == "amd64" || "$architecture" == "arm64" ]]
  if [[ -n "$submitted_version" ]]; then
    test "$version" = "$submitted_version"
  else
    submitted_version="$version"
  fi
  test -z "${submitted_architectures[$architecture]:-}"
  submitted_architectures["$architecture"]=1
  python3 "$script_dir/channel_policy.py" validate "$version" "$RAVENSTASH_CLI_APT_CHANNEL"

  deb_digest="$(sha256sum "$deb" | awk '{print $1}')"
  target="$pool/${package}_${version}_${architecture}_${deb_digest:0:16}.deb"
  mapfile -t same_version < <(
    find "$pool" -maxdepth 1 -type f -name "${package}_${version}_${architecture}_*.deb" -print
  )
  for existing in "${same_version[@]}"; do
    if [[ "$existing" != "$target" ]]; then
      echo "error: version $version already exists with a different digest" >&2
      exit 1
    fi
  done
  if [[ -e "$target" ]]; then
    test "$(sha256sum "$target" | awk '{print $1}')" = "$deb_digest"
  else
    install -m 0644 "$deb" "$target"
  fi
done

mapfile -t architectures < <(
  find "$output_repository/pool" -type f -name '*.deb' -print0 \
    | xargs -0 -r -n1 dpkg-deb --field 2>/dev/null \
    | awk '$1 == "Architecture:" { print $2 }' \
    | sort -u
)
test "${#architectures[@]}" -gt 0
architecture_list="${architectures[*]}"

declare -A distribution_set=()
if [[ -d "$output_repository/dists" ]]; then
  while IFS= read -r distribution; do
    distribution_set["$distribution"]=1
  done < <(
    find "$output_repository/dists" -mindepth 1 -maxdepth 1 -type d -printf '%f\n'
  )
fi
distribution_set["$RAVENSTASH_CLI_APT_CHANNEL"]=1
mapfile -t distributions < <(printf '%s\n' "${!distribution_set[@]}" | sort)

declare -A distributions_to_update=()
distributions_to_update["$RAVENSTASH_CLI_APT_CHANNEL"]=1
if [[ "$refresh_all" == "1" || "${previous_architectures[*]}" != "${architectures[*]}" ]]; then
  for distribution in "${distributions[@]}"; do
    distributions_to_update["$distribution"]=1
  done
fi
mapfile -t updated_distributions < <(
  printf '%s\n' "${!distributions_to_update[@]}" | sort
)

actual_fingerprint="$(
  gpg --batch --with-colons --fingerprint "$RAVENSTASH_CLI_APT_GPG_KEY_ID" \
    | awk -F: '$1 == "fpr" { print $10; exit }'
)"
test "$actual_fingerprint" = "$RAVENSTASH_CLI_APT_GPG_FINGERPRINT"
gpg_arguments=(--batch --yes --local-user "$RAVENSTASH_CLI_APT_GPG_KEY_ID" --digest-algo SHA512)
if [[ -n "${RAVENSTASH_CLI_APT_GPG_PASSPHRASE_FILE:-}" ]]; then
  test -f "$RAVENSTASH_CLI_APT_GPG_PASSPHRASE_FILE"
  gpg_arguments+=(--pinentry-mode loopback --passphrase-file "$RAVENSTASH_CLI_APT_GPG_PASSPHRASE_FILE")
fi

valid_until="$(date --utc --date='+7 days' --rfc-email)"
all_packages="$output_repository/all-packages.generated"
(cd "$output_repository" && apt-ftparchive \
  -o APT::FTPArchive::AlwaysStat=true packages pool) > "$all_packages"
for distribution in "${updated_distributions[@]}"; do
  compatibility_channel="$(
    python3 "$script_dir/channel_policy.py" compatibility-channel "$distribution"
  )"
  for indexed_architecture in "${architectures[@]}"; do
    binary="$output_repository/dists/$distribution/main/binary-${indexed_architecture}"
    mkdir -p "$binary"
    # The digest-bearing pool filenames prevent apt-ftparchive's architecture
    # inference. Enumerate the pool once, then reuse that authenticated inventory
    # for every channel/architecture filter instead of rescanning it repeatedly.
    python3 "$script_dir/channel_policy.py" filter \
        "$compatibility_channel" --architecture "$indexed_architecture" \
        < "$all_packages" \
      > "$binary/Packages"
    # A newly supported architecture legitimately has an empty index in older
    # compatibility channels. The repository verifier still requires every
    # distribution to list at least one package across its declared indexes.
    gzip -9n < "$binary/Packages" > "$binary/Packages.gz"
    for index in Packages Packages.gz; do
      index_digest="$(sha256sum "$binary/$index" | awk '{print $1}')"
      by_hash="$binary/by-hash/SHA256/$index_digest"
      mkdir -p "$(dirname "$by_hash")"
      test -e "$by_hash" || install -m 0644 "$binary/$index" "$by_hash"
    done
  done

  config="$output_repository/apt-ftparchive-release-$distribution.conf"
  printf '%s\n' \
    'APT::FTPArchive::Release {' \
    '  Origin "Ravenstash";' \
    '  Label "Ravenstash";' \
    "  Suite \"$distribution\";" \
    "  Codename \"$distribution\";" \
    "  Architectures \"$architecture_list\";" \
    '  Components "main";' \
    '  Description "Ravenstash CLI packages";' \
    '  Acquire-By-Hash "yes";' \
    "  Valid-Until \"$valid_until\";" \
    '};' > "$config"
  rm -f \
    "$output_repository/dists/$distribution/InRelease" \
    "$output_repository/dists/$distribution/Release" \
    "$output_repository/dists/$distribution/Release.gpg"
  release_unsigned="$output_repository/Release-$distribution.unsigned"
  apt-ftparchive -c "$config" release "$output_repository/dists/$distribution" \
    > "$release_unsigned"
  sed "/^Date:/a Valid-Until: $valid_until" "$release_unsigned" \
    | sed "/^Date:/a Ravenstash-Compatibility-Channel: $compatibility_channel" \
    > "$output_repository/dists/$distribution/Release"
  rm -f "$release_unsigned"
  gpg "${gpg_arguments[@]}" \
    --output "$output_repository/dists/$distribution/InRelease" \
    --clearsign "$output_repository/dists/$distribution/Release"
  gpg "${gpg_arguments[@]}" \
    --output "$output_repository/dists/$distribution/Release.gpg" \
    --detach-sign "$output_repository/dists/$distribution/Release"
done
rm -f "$all_packages"
recommended="$(
  python3 "$script_dir/channel_policy.py" existing-recommended \
    "$output_repository" "$RAVENSTASH_CLI_APT_CHANNEL"
)"
python3 "$script_dir/channel_policy.py" manifest \
  "$output_repository" "$recommended" > "$output_repository/channels.json"
gpg "${gpg_arguments[@]}" \
  --output "$output_repository/channels.json.gpg" \
  --detach-sign "$output_repository/channels.json"
gpg --batch --yes --output "$output_repository/$keyring_name" \
  --export "$RAVENSTASH_CLI_APT_GPG_KEY_ID"
cmp "$output_repository/$keyring_name" "$trusted_keyring"
python3 "$script_dir/verify_apt.py" "$output_repository" "$trusted_keyring"
