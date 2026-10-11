#!/usr/bin/env bash
set -euo pipefail

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

# This script runs standalone, so it embeds the release identity that
# packaging/identity.env defines; the test suite keeps the two in agreement.
readonly repository_url="https://releases.ravenstash.com/ravenstash/apt"
readonly github_repository="rvstash/ravenstash-cli"
readonly github_release_url="https://github.com/${github_repository}/releases/download"
readonly package_name="ravenstash-cli"
readonly asset_stem="ravenstash-cli"
readonly release_version="0.15.0.dev0"
readonly compatibility_channel="v0"
readonly keyring_name="ravenstash-cli.gpg"
readonly signing_key_url="${repository_url}/${keyring_name}"
readonly signing_key_fingerprint="3B7C20FC370D1A7C813DF3A2E9679F951AD8BAA0"
readonly keyring_path="/etc/apt/keyrings/${keyring_name}"
readonly source_path="/etc/apt/sources.list.d/ravenstash-cli.list"
readonly receipt_name="ravenstash-cli-install.json"
machine_architecture="$(uname -m)"
readonly machine_architecture
case "$machine_architecture" in
  x86_64 | amd64) readonly package_architecture="amd64" ;;
  aarch64 | arm64) readonly package_architecture="arm64" ;;
  *) readonly package_architecture="unsupported" ;;
esac
readonly expected_source="deb [arch=${package_architecture} signed-by=${keyring_path}] ${repository_url} ${compatibility_channel} main"
readonly repair="${RVS_INSTALL_REPAIR:-0}"
readonly portable_installer_api="1"
readonly install_method="${RVS_INSTALL_METHOD:-auto}"
readonly selected_release_version="${RVS_INSTALL_VERSION:-${release_version}}"
# Exposed for CI setup tooling, which authenticates the installer before reading
# this compatibility contract.
: "$portable_installer_api"

say() {
  printf 'rvs installer: %s\n' "$*"
}

print_success_banner() {
  if [[ "${RVS_INSTALL_NO_BANNER:-0}" == "1" ]]; then
    return
  fi

  cat <<'RAVENSTASH_BANNER'

  █████████████████████████▄▄▄
  █████████████████████████████▄▄
  ████████████████████████████████▄
  █████████████████████████████████▄
  ██████████████████████████████████▄
  ███████▀▀▀▀▀      ▀▀███████████████▄
  ████████▀      ▄▄▄▄   ▀▀▀███████████
  ██████▀         ▀▀         ▀████████
  ████▀                  ▄▄▄▄▄▄███████
  ███▀               ▄███████████████▀
  ██▀               ████████████████▀
  ▀▀               ▄███████████████▀
                   ▀█████████████▀
                    ██████████▀▀
                     █████████
                      ▀████████▄
                       ▀████████▄
                        ▀████████▄
                          █████████
                           ▀▀███████
                                ▀▀▀██▄

              RAVENSTASH
       CLI installed successfully
RAVENSTASH_BANNER
}

fail() {
  printf 'rvs installer: error: %s\n' "$*" >&2
  exit 1
}

case "$install_method" in
  auto | portable) ;;
  *) fail "RVS_INSTALL_METHOD must be auto or portable" ;;
esac
if [[ -n "${RVS_INSTALL_VERSION:-}" ]]; then
  [[ "$install_method" == "portable" ]] \
    || fail "RVS_INSTALL_VERSION requires RVS_INSTALL_METHOD=portable"
  [[ "$selected_release_version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)(rc[1-9][0-9]*)?$ ]] \
    || fail "RVS_INSTALL_VERSION must be a stable or release-candidate version"
  # 0.15.0 is the first published release; no earlier version has assets.
  (( BASH_REMATCH[1] > 0 || BASH_REMATCH[2] >= 15 )) \
    || fail "RVS_INSTALL_VERSION must be 0.15.0 or later; 0.15.0 is the first published release"
fi

has_os_family() {
  local candidate="$1"
  [[ " ${ID:-} ${ID_LIKE:-} " == *" ${candidate} "* ]]
}

as_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    fail "this installation step needs root; run as root or install sudo"
  fi
}

verify_signing_key() {
  local key="$1"
  local actual_fingerprint
  actual_fingerprint="$(
    gpg --batch --with-colons --import-options show-only --import "$key" 2>/dev/null \
      | awk -F: '$1 == "fpr" { print $10; exit }'
  )"
  if [[ "$actual_fingerprint" != "$signing_key_fingerprint" ]]; then
    fail "the downloaded signing key has an unexpected fingerprint"
  fi
}

download_release_asset() {
  local release_url="$1"
  local asset_name="$2"
  local destination="$3"
  if [[ -z "${RVS_GITHUB_TOKEN:-}" ]]; then
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL \
      "${release_url}/${asset_name}" -o "$destination"
    return
  fi
  local release_api asset_api auth_header
  auth_header="${destination}.github-auth"
  (umask 077; printf 'Authorization: Bearer %s\n' "$RVS_GITHUB_TOKEN" > "$auth_header")
  release_api="https://api.github.com/repos/${github_repository}/releases/tags/v${selected_release_version}"
  asset_api="$(
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL \
      -H "@${auth_header}" \
      -H 'Accept: application/vnd.github+json' \
      "$release_api" \
      | awk -v expected="\"name\": \"${asset_name}\"" '
          /"url": "https:\/\/api.github.com\/repos\/.*\/releases\/assets\// {
            url=$2; gsub(/[",]/, "", url)
          }
          index($0, expected) { print url; exit }
        '
  )"
  [[ "$asset_api" == "https://api.github.com/repos/${github_repository}/releases/assets/"* ]] \
    || fail "release v${selected_release_version} does not contain ${asset_name}"
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL \
    -H "@${auth_header}" \
    -H 'Accept: application/octet-stream' \
    "$asset_api" -o "$destination"
  rm -f -- "$auth_header"
}

install_apt_package() {
  local temporary_directory="$1"
  local temporary_key="${temporary_directory}/${keyring_name}"

  for command in apt-get awk curl dpkg install mktemp tee; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: ${command}"
  done
  if [[ "$(dpkg --print-architecture)" != "$package_architecture" ]] \
    || [[ "$package_architecture" == "unsupported" ]]; then
    fail "the Ravenstash APT repository does not support architecture ${machine_architecture}"
  fi
  if ! command -v gpg >/dev/null 2>&1; then
    say "installing signing-key verification tools"
    as_root apt-get update -qq
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates gnupg
  fi

  say "downloading and verifying the Ravenstash APT signing key"
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 \
    -fsSL "$signing_key_url" -o "$temporary_key"
  verify_signing_key "$temporary_key"

  if [[ -e "$keyring_path" ]]; then
    local existing_fingerprint
    existing_fingerprint="$(
      gpg --batch --with-colons --import-options show-only --import "$keyring_path" 2>/dev/null \
        | awk -F: '$1 == "fpr" { print $10; exit }'
    )"
    if [[ "$existing_fingerprint" != "$signing_key_fingerprint" && "$repair" != "1" ]]; then
      fail "an unexpected Ravenstash keyring already exists; inspect it, then rerun with RVS_INSTALL_REPAIR=1 to replace it"
    fi
  fi
  if [[ -e "$source_path" ]]; then
    local existing_source
    existing_source="$(<"$source_path")"
    if [[ "$existing_source" != "$expected_source" && "$repair" != "1" ]]; then
      fail "a conflicting Ravenstash APT source exists; inspect it, then rerun with RVS_INSTALL_REPAIR=1 to replace it"
    fi
  fi

  as_root install -d -m 0755 /etc/apt/keyrings
  as_root install -m 0644 "$temporary_key" "$keyring_path"
  printf '%s\n' "$expected_source" | as_root tee "$source_path" >/dev/null

  say "installing rvs with APT"
  as_root apt-get update -qq
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$package_name"

  local installed_version
  installed_version="$(dpkg-query -W -f='${Version}' "$package_name")"
  say "installed rvs ${installed_version} on compatibility channel ${compatibility_channel}"
}

require_portable_tools() {
  if command -v gpg >/dev/null 2>&1 \
    && { command -v sha256sum >/dev/null 2>&1 || command -v shasum >/dev/null 2>&1; } \
    && command -v tar >/dev/null 2>&1; then
    return
  fi
  say "installing portable archive verification tools"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    fail "GnuPG is required to authenticate rvs releases; install it with: brew install gnupg"
  elif command -v apk >/dev/null 2>&1; then
    as_root apk add --no-cache ca-certificates coreutils gnupg tar
  elif command -v dnf >/dev/null 2>&1; then
    local rpm_packages=(ca-certificates gzip tar)
    command -v sha256sum >/dev/null 2>&1 || rpm_packages+=(coreutils)
    if ! command -v gpg >/dev/null 2>&1; then
      rpm_packages+=(gnupg2)
    fi
    as_root dnf install -y "${rpm_packages[@]}"
  elif command -v yum >/dev/null 2>&1; then
    local yum_packages=(ca-certificates gzip tar)
    command -v sha256sum >/dev/null 2>&1 || yum_packages+=(coreutils)
    if ! command -v gpg >/dev/null 2>&1; then
      yum_packages+=(gnupg2)
    fi
    as_root yum install -y "${yum_packages[@]}"
  elif command -v zypper >/dev/null 2>&1; then
    local suse_packages=(ca-certificates gzip tar)
    command -v sha256sum >/dev/null 2>&1 || suse_packages+=(coreutils)
    if ! command -v gpg >/dev/null 2>&1; then
      suse_packages+=(gpg2)
    fi
    as_root zypper --non-interactive install "${suse_packages[@]}"
  elif command -v pacman >/dev/null 2>&1; then
    as_root pacman --sync --needed --noconfirm ca-certificates coreutils gnupg gzip tar
  else
    fail "gpg, sha256sum, and tar are required; install GnuPG, coreutils, and tar, then rerun"
  fi
  command -v gpg >/dev/null 2>&1 || fail "GnuPG installation completed but gpg is unavailable"
}

verify_archive_checksum() {
  local expected_line="$1"
  local directory="$2"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s\n' "$expected_line" | (cd "$directory" && sha256sum --check --strict - >/dev/null)
  else
    local expected filename actual
    expected="${expected_line%% *}"
    filename="${expected_line##* }"
    actual="$(shasum -a 256 "${directory}/${filename}" | awk '{print $1}')"
    [[ "$actual" == "$expected" ]]
  fi
}

portable_architecture() {
  case "$machine_architecture" in
    x86_64 | amd64) printf 'amd64\n' ;;
    aarch64 | arm64) printf 'arm64\n' ;;
    *) fail "unsupported CPU architecture: ${machine_architecture}" ;;
  esac
}

require_glibc_228() {
  local libc_version ldd_output
  if ! libc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null)"; then
    ldd_output="$(ldd --version 2>&1 || true)"
    if grep -qi musl <<<"$ldd_output"; then
      fail "the glibc rvs build cannot run on musl; use the Linux musl archive"
    fi
    fail "could not identify a supported glibc runtime"
  fi
  libc_version="${libc_version#glibc }"
  local major="${libc_version%%.*}"
  local remainder="${libc_version#*.}"
  local minor="${remainder%%.*}"
  if [[ ! "$major" =~ ^[0-9]+$ || ! "$minor" =~ ^[0-9]+$ ]] \
    || (( major < 2 || (major == 2 && minor < 28) )); then
    fail "the portable rvs build requires glibc 2.28 or newer (found ${libc_version})"
  fi
}

install_portable_archive() {
  local temporary_directory="$1"
  local architecture ldd_output
  architecture="$(portable_architecture)"
  local system_name
  case "$(uname -s)" in
    Linux)
      if getconf GNU_LIBC_VERSION >/dev/null 2>&1; then
        require_glibc_228
        system_name="linux"
      else
        ldd_output="$(ldd --version 2>&1 || true)"
        grep -qi musl <<<"$ldd_output" \
          || fail "could not identify a supported Linux libc"
        system_name="linux-musl"
      fi
      ;;
    Darwin) system_name="macos" ;;
    *) fail "this installer supports Linux and macOS; use install.ps1 on Windows" ;;
  esac
  require_portable_tools
  for command in awk curl gpg install mktemp tar; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: ${command}"
  done

  local archive_prefix="${asset_stem}-v${selected_release_version}-${system_name}-${architecture}"
  local archive_name="${archive_prefix}.tar.gz"
  local checksums_name="${asset_stem}-v${selected_release_version}-checksums.txt"
  local signature_name="${checksums_name}.asc"
  local release_url="${github_release_url}/v${selected_release_version}"
  local temporary_key="${temporary_directory}/${keyring_name}"

  say "downloading the signed portable rvs ${selected_release_version} archive"
  download_release_asset "$release_url" "$archive_name" "${temporary_directory}/${archive_name}"
  download_release_asset "$release_url" "$checksums_name" "${temporary_directory}/${checksums_name}"
  download_release_asset "$release_url" "$signature_name" "${temporary_directory}/${signature_name}"
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL \
    "$signing_key_url" -o "$temporary_key"
  verify_signing_key "$temporary_key"
  local temporary_gnupg="${temporary_directory}/gnupg"
  install -d -m 0700 "$temporary_gnupg"
  gpg --homedir "$temporary_gnupg" --batch --import "$temporary_key" >/dev/null 2>&1 \
    || fail "the Ravenstash signing key could not be imported"
  gpg --homedir "$temporary_gnupg" --batch --verify \
    "${temporary_directory}/${signature_name}" \
    "${temporary_directory}/${checksums_name}" >/dev/null 2>&1 \
    || fail "the portable release checksum signature is invalid"

  local expected_checksum
  expected_checksum="$(
    awk -v filename="$archive_name" '$2 == filename { print; found = 1 } END { if (!found) exit 1 }' \
      "${temporary_directory}/${checksums_name}"
  )" || fail "the signed checksum inventory does not contain ${archive_name}"
  verify_archive_checksum "$expected_checksum" "$temporary_directory" \
    || fail "the portable archive checksum is invalid"

  local install_root bin_directory install_directory
  case "${RVS_INSTALL_SCOPE:-user}" in
    user)
      [[ -n "${HOME:-}" ]] || fail "HOME must be set for a user installation"
      install_root="${RVS_INSTALL_ROOT:-${HOME}/.local/share/ravenstash-cli}"
      bin_directory="${RVS_INSTALL_BIN_DIR:-${HOME}/.local/bin}"
      ;;
    system)
      install_root="${RVS_INSTALL_ROOT:-/opt/ravenstash-cli}"
      bin_directory="${RVS_INSTALL_BIN_DIR:-/usr/local/bin}"
      ;;
    *) fail "RVS_INSTALL_SCOPE must be user or system" ;;
  esac
  install_directory="${install_root}/${selected_release_version}"
  [[ "$install_root" == /* && "$install_root" != "/" ]] \
    || fail "RVS_INSTALL_ROOT must be an absolute directory other than /"
  [[ "$bin_directory" == /* && "$bin_directory" != "/" ]] \
    || fail "RVS_INSTALL_BIN_DIR must be an absolute directory other than /"
  if [[ -e "$install_directory" && "$repair" != "1" ]]; then
    fail "${install_directory} already exists; rerun with RVS_INSTALL_REPAIR=1 to replace it"
  fi

  local extracted="${temporary_directory}/extracted"
  mkdir -p "$extracted"
  while IFS= read -r member; do
    case "$member" in
      "$archive_prefix" | "$archive_prefix"/*) ;;
      *) fail "the portable archive contains a path outside its release directory" ;;
    esac
    case "/${member}/" in
      */../* | */./*) fail "the portable archive contains an unsafe path" ;;
    esac
  done < <(tar -tzf "${temporary_directory}/${archive_name}")
  tar -xzf "${temporary_directory}/${archive_name}" -C "$extracted"
  local archive_root="${extracted}/${archive_prefix}"
  local launcher
  for launcher in rvs ravenstash docker-credential-rvs; do
    [[ -f "${archive_root}/${launcher}" && ! -L "${archive_root}/${launcher}" && -x "${archive_root}/${launcher}" ]] \
      || fail "the portable archive is missing its ${launcher} launcher"
  done

  "${archive_root}/rvs" _record-install \
    --output "${archive_root}/${receipt_name}" \
    --scope "${RVS_INSTALL_SCOPE:-user}" \
    --version "$selected_release_version" \
    --channel "$compatibility_channel" \
    --target "${system_name}-${architecture}" \
    --install-root "$install_root" \
    --bin-directory "$bin_directory"

  local command_name command_path
  for command_name in rvs ravenstash docker-credential-rvs; do
    command_path="${bin_directory}/${command_name}"
    if [[ -e "$command_path" && ! -L "$command_path" ]]; then
      fail "${command_path} exists and is not an installer-owned symlink"
    fi
  done

  if [[ "${RVS_INSTALL_SCOPE:-user}" == "system" ]]; then
    as_root install -d -m 0755 "$install_root" "$bin_directory"
    if [[ -e "${install_root}/current" && ! -L "${install_root}/current" ]]; then
      fail "${install_root}/current exists and is not an installer-owned symlink"
    fi
    if [[ -e "$install_directory" ]]; then
      as_root rm -rf -- "$install_directory"
    fi
    as_root mv "$archive_root" "$install_directory"
    as_root ln -sfn "$selected_release_version" "${install_root}/current"
    as_root ln -sfn "${install_root}/current/rvs" "${bin_directory}/rvs"
    as_root ln -sfn "${install_root}/current/ravenstash" "${bin_directory}/ravenstash"
    as_root ln -sfn "${install_root}/current/docker-credential-rvs" "${bin_directory}/docker-credential-rvs"
  else
    install -d -m 0755 "$install_root" "$bin_directory"
    if [[ -e "${install_root}/current" && ! -L "${install_root}/current" ]]; then
      fail "${install_root}/current exists and is not an installer-owned symlink"
    fi
    if [[ -e "$install_directory" ]]; then
      rm -rf -- "$install_directory"
    fi
    mv "$archive_root" "$install_directory"
    ln -sfn "$selected_release_version" "${install_root}/current"
    ln -sfn "${install_root}/current/rvs" "${bin_directory}/rvs"
    ln -sfn "${install_root}/current/ravenstash" "${bin_directory}/ravenstash"
    ln -sfn "${install_root}/current/docker-credential-rvs" "${bin_directory}/docker-credential-rvs"
  fi
  "${bin_directory}/rvs" --version >/dev/null
  say "installed rvs ${selected_release_version} in ${install_directory}"
  if [[ ":${PATH}:" != *":${bin_directory}:"* ]]; then
    say "add ${bin_directory} to PATH"
  fi
}

case "$(uname -s)" in
  Linux | Darwin) ;;
  *) fail "this installer supports Linux and macOS; use install.ps1 on Windows" ;;
esac
if [[ "$(uname -s)" == "Linux" && -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  source /etc/os-release
fi
if [[ "${ID:-}" == "nixos" ]]; then
  fail "install rvs on NixOS with: nix profile install github:${github_repository}/v${release_version}"
fi

temporary_directory="$(mktemp -d)"
trap 'rm -rf -- "$temporary_directory"' EXIT

if [[ "$install_method" == "auto" ]] \
  && [[ "$(uname -s)" == "Linux" ]] \
  && command -v apt-get >/dev/null 2>&1 \
  && command -v dpkg >/dev/null 2>&1 \
  && { [[ -e /etc/debian_version ]] || has_os_family debian || has_os_family ubuntu; }; then
  install_apt_package "$temporary_directory"
else
  install_portable_archive "$temporary_directory"
fi

print_success_banner
if [[ "${RVS_INSTALL_NO_HINTS:-0}" != "1" ]]; then
  say "next: rvs auth login"
fi
