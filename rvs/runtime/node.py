"""Install Node.js runtimes from the official nodejs.org binary distribution.

Downloads from: https://nodejs.org/dist/v{version}/node-v{version}-linux-{arch}.tar.xz

Installs to: ~/.rvs/runtimes/node/<full_version>/
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any

import httpx2 as httpx

from .. import output
from ._install import (
    RUNTIMES_DIR,
    download,
    extract,
    runtime_platform,
    write_env_file,
    write_shim,
)
from .versions import version_key


_INDEX_URL = "https://nodejs.org/dist/index.json"
_RELEASE_KEYS_COMMIT = "b28073028e6d6855cfb53bf7fa0137599c01f967"
_RELEASE_KEYRING_SHA256 = "8e6f89521a0694e445f42decd022f48369c634f1b5bcb5975135b69c88629ae8"
_RELEASE_KEYRING_URL = (
    "https://raw.githubusercontent.com/nodejs/release-keys/"
    f"{_RELEASE_KEYS_COMMIT}/gpg-only-active-keys/pubring.kbx"
)
_MAX_SIGNED_MANIFEST_BYTES = 16 * 1024 * 1024

# nodejs.org arch strings
_ARCH_MAP = {"x64": "x64", "aarch64": "arm64"}


def _msys_path(path: Path) -> str:
    """Translate an absolute Windows path for an MSYS command."""
    windows_path = PureWindowsPath(path)
    drive = windows_path.drive.rstrip(":").lower()
    if not drive:
        return str(path).replace("\\", "/")
    remainder = "/".join(windows_path.parts[1:])
    return f"/{drive}/{remainder}"


def _gpgv_path(path: Path, gpgv: str) -> str:
    if os.name == "nt" and "msys" in gpgv.casefold():
        return _msys_path(path)
    return str(path)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _resolve_full_version(version: str) -> str:
    """Resolve ``latest``, a major (``"20"``), or a major.minor (``"20.11"``)
    version to the newest matching release; a full version must exist."""
    output.info("Fetching Node.js release index ...")
    with httpx.Client(timeout=30.0) as hx:
        resp = hx.get(_INDEX_URL)
        resp.raise_for_status()
        releases: list[dict[str, Any]] = resp.json()

    available = [str(release["version"]).lstrip("v") for release in releases]
    requested = version.strip().lower().lstrip("v").rstrip(".")
    matches = [
        candidate
        for candidate in available
        if requested == "latest" or candidate == requested or candidate.startswith(f"{requested}.")
    ]
    if not matches:
        output.fatal(f"No Node.js release found for '{version}'.")
    return max(matches, key=_version_key)


def _verified_archive_digest(version: str, archive_name: str, temp_dir: Path) -> str:
    gpgv = shutil.which("gpgv")
    if not gpgv:
        output.fatal("Node.js installation requires gpgv to verify the signed release manifest.")
    keyring = temp_dir / "nodejs-release-keyring.kbx"
    checksums = temp_dir / "SHASUMS256.txt"
    signature = temp_dir / "SHASUMS256.txt.sig"
    download(
        _RELEASE_KEYRING_URL,
        keyring,
        expected_sha256=_RELEASE_KEYRING_SHA256,
    )
    for filename, destination in (
        ("SHASUMS256.txt", checksums),
        ("SHASUMS256.txt.sig", signature),
    ):
        download_url = f"https://nodejs.org/dist/v{version}/{filename}"
        with httpx.stream("GET", download_url, follow_redirects=True, timeout=30.0) as response:
            response.raise_for_status()
            if response.url.scheme != "https":
                output.fatal("Node.js checksum material redirected to a non-HTTPS URL.")
            downloaded = 0
            with destination.open("wb") as target:
                for chunk in response.iter_bytes(65536):
                    downloaded += len(chunk)
                    if downloaded > _MAX_SIGNED_MANIFEST_BYTES:
                        output.fatal("Node.js checksum material exceeds the safety limit.")
                    target.write(chunk)
    verification = subprocess.run(
        [
            gpgv,
            f"--keyring={_gpgv_path(keyring, gpgv)}",
            _gpgv_path(signature, gpgv),
            _gpgv_path(checksums, gpgv),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if verification.returncode != 0:
        output.fatal("Node.js release signature verification failed.")
    for line in checksums.read_text(encoding="utf-8").splitlines():
        digest, separator, filename = line.partition("  ")
        if separator and filename == archive_name:
            return digest
    output.fatal(f"Node.js signed manifest does not contain {archive_name}.")


def install(version: str) -> Path:
    """Download and install Node.js *version* to ``~/.rvs/runtimes/node/``.

    *version* may be ``latest``, a major (``"20"``), major.minor (``"20.11"``), or
    full version string (``"20.11.0"``). Returns the installation directory.
    """
    platform_target = runtime_platform()
    if platform_target.system == "linux" and platform_target.libc == "musl":
        output.fatal(
            "Node.js does not publish official musl binaries. Install Node.js "
            "with the system package manager; rvs will use it from PATH."
        )
    arch = _ARCH_MAP[platform_target.arch]

    full_ver = _resolve_full_version(version)
    dest = RUNTIMES_DIR / "node" / full_ver

    if dest.exists():
        output.info(f"Node.js {full_ver} already installed at {dest}")
        return dest

    os_name = {"linux": "linux", "macos": "darwin", "windows": "win"}[platform_target.system]
    extension = (
        "zip" if platform_target.windows else ("tar.gz" if os_name == "darwin" else "tar.xz")
    )
    target = f"{os_name}-{arch}"
    url = f"https://nodejs.org/dist/v{full_ver}/node-v{full_ver}-{target}.{extension}"
    with tempfile.TemporaryDirectory() as tmp:
        temp_dir = Path(tmp)
        archive_name = url.split("/")[-1]
        archive = temp_dir / archive_name
        expected_sha256 = _verified_archive_digest(full_ver, archive_name, temp_dir)
        download(url, archive, expected_sha256=expected_sha256)
        required_node = "node.exe" if platform_target.windows else "bin/node"
        extract(archive, dest, required_paths=(required_node,))

    bin_dir = dest if platform_target.windows else dest / "bin"
    for exe in ("node", "npm", "npx", "corepack"):
        executable = (
            f"{exe}.exe"
            if platform_target.windows and exe in {"node", "corepack"}
            else (f"{exe}.cmd" if platform_target.windows else exe)
        )
        if (bin_dir / executable).exists():
            write_shim(exe, bin_dir / executable, runtime_kind="node")

    write_env_file()
    output.success(f"Node.js {full_ver} installed at {dest}")
    output.info(
        "Add to PATH: . $HOME/.rvs/env.ps1"
        if platform_target.windows
        else "Add to PATH: source ~/.rvs/env"
    )
    return dest


def list_installed() -> list[tuple[str, Path]]:
    """Return [(version, path), ...] for all rvs-managed Node.js installations."""
    base = RUNTIMES_DIR / "node"
    if not base.exists():
        return []
    return sorted(
        [(p.name, p) for p in base.iterdir() if p.is_dir()],
        key=lambda t: version_key(t[0]),
    )


def find(version_prefix: str) -> Path | None:
    """Return the newest installed Node.js matching *version_prefix*."""
    vp = version_prefix.lstrip("v").rstrip(".")
    if not vp:
        installed = list_installed()
        return installed[-1][1] if installed else None
    matches = [path for ver, path in list_installed() if ver == vp or ver.startswith(vp + ".")]
    return matches[-1] if matches else None


def node_bin(version_prefix: str) -> Path | None:
    """Return the ``node`` binary path for *version_prefix*, or None."""
    base = find(version_prefix)
    if base is None:
        return None
    return base / "node.exe" if os.name == "nt" else base / "bin" / "node"
