"""Release identity of the Ravenstash CLI.

This module is the single runtime source of the names under which the CLI is
published, installed, and updated. Build and release tooling reads the same
values from ``packaging/identity.env``; the test suite keeps the two files, the
standalone installers, and every other embedded copy in agreement.

The commands (``rvs``, ``ravenstash``, ``docker-credential-rvs``), the ``rvs``
Python package, ``RVS_*`` user environment variables, and ``~/.rvs`` are command
contracts, not release identity, and do not belong here.
"""

PRODUCT_NAME = "Ravenstash CLI"

# GitHub repository that owns the source and the signed release assets.
REPOSITORY = "rvstash/ravenstash-cli"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}"
RELEASE_DOWNLOAD_ROOT = f"{REPOSITORY_URL}/releases/download"
NIX_FLAKE = f"github:{REPOSITORY}"

# Python project, Debian package, and Nix package and profile element.
PACKAGE_NAME = "ravenstash-cli"
# Stem of every release asset and of the root directory in a portable archive.
ASSET_STEM = "ravenstash-cli"

# Public release storage: the signed APT repository and channel manifest.
RELEASES_URL = "https://releases.ravenstash.com"
DISTRIBUTION_NAMESPACE = "ravenstash"
APT_REPOSITORY_URL = f"{RELEASES_URL}/{DISTRIBUTION_NAMESPACE}/apt"
CHANNELS_URL = f"{RELEASES_URL}/{DISTRIBUTION_NAMESPACE}/channels.json"

# APT client files and the pinned release key bundled in ``rvs.resources``.
KEYRING_NAME = "ravenstash-cli.gpg"
SOURCE_LIST_NAME = "ravenstash-cli.list"
SIGNING_KEY_NAME = "ravenstash-cli.asc"
APT_KEYRING_PATH = f"/etc/apt/keyrings/{KEYRING_NAME}"
APT_SOURCE_PATH = f"/etc/apt/sources.list.d/{SOURCE_LIST_NAME}"

# Receipt written beside the launcher of a portable installation.
RECEIPT_NAME = "ravenstash-cli-install.json"

# Package-manager identifiers; each is completed by a release channel.
HOMEBREW_CASK = "ravenstash-cli"
WINGET_PACKAGE = "Ravenstash.RavenstashCLI"


def archive_name(version: str, target: str) -> str:
    """Return the portable archive asset for one release target."""
    suffix = ".zip" if target.startswith("windows-") else ".tar.gz"
    return f"{archive_root(version, target)}{suffix}"


def archive_root(version: str, target: str) -> str:
    """Return the only top-level directory of a portable archive."""
    return f"{ASSET_STEM}-v{version}-{target}"


def checksums_name(version: str) -> str:
    """Return the signed checksum inventory asset of a release."""
    return f"{ASSET_STEM}-v{version}-checksums.txt"


def debian_package_name(version: str, architecture: str) -> str:
    """Return the Debian package asset for one architecture."""
    return f"{PACKAGE_NAME}_{version}_{architecture}.deb"


def homebrew_cask(channel: str) -> str:
    """Return the Homebrew cask token for a release channel such as ``v0``."""
    return f"{HOMEBREW_CASK}@{channel.removeprefix('v')}"


def winget_package(channel: str) -> str:
    """Return the WinGet package identifier for a release channel such as ``v0``."""
    return f"{WINGET_PACKAGE}.{channel}"
