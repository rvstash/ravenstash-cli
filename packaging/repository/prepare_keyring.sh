#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly script_dir
# shellcheck source=../identity.env disable=SC1091
source "$script_dir/../identity.env"
readonly armored_key="${1:-${script_dir}/keys/${RAVENSTASH_CLI_SIGNING_KEY}}"
readonly binary_keyring="${2:-${script_dir}/keys/${RAVENSTASH_CLI_KEYRING}}"
expected_fingerprint="$(
  tr -d '[:space:]' < "${script_dir}/keys/fingerprint.txt"
)"
readonly expected_fingerprint
temporary_gnupg_home="$(mktemp -d)"
readonly temporary_gnupg_home
trap 'rm -rf -- "$temporary_gnupg_home"' EXIT
chmod 0700 "$temporary_gnupg_home"
export GNUPGHOME="$temporary_gnupg_home"

test -f "$armored_key"
test -n "$expected_fingerprint"
actual_fingerprint="$(
  gpg --batch --with-colons --import-options show-only --import "$armored_key" 2>/dev/null \
    | awk -F: '$1 == "fpr" { print $10; exit }'
)"
test "$actual_fingerprint" = "$expected_fingerprint"
gpg --batch --yes --dearmor --output "$binary_keyring" "$armored_key"
test "$(gpg --batch --with-colons --show-keys "$binary_keyring" | awk -F: '$1 == "fpr" { print $10; exit }')" \
  = "$expected_fingerprint"
