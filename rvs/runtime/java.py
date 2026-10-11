"""Install Java runtimes using Eclipse Temurin (Adoptium project).

Uses the Adoptium REST API to discover and download the latest GA release
for a given major version.

API: https://api.adoptium.net/v3/assets/latest/{major}/hotspot?os=linux&...

Installs to: ~/.rvs/runtimes/java/<semver>/
"""

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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


_ADOPTIUM = "https://api.adoptium.net/v3"
_ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}

# Adoptium arch strings
_ARCH_MAP = {"x64": "x64", "aarch64": "aarch64"}


def _latest_release(major: int, arch: str, os_name: str) -> dict[str, Any]:
    """Return the Adoptium release metadata for the latest GA JDK of *major*."""
    url = f"{_ADOPTIUM}/assets/latest/{major}/hotspot"
    params = {
        "architecture": arch,
        "image_type": "jdk",
        "jvm_impl": "hotspot",
        "os": os_name,
        "vendor": "eclipse",
    }
    output.info(f"Fetching Temurin JDK {major} metadata ...")
    with httpx.Client(timeout=30.0) as hx:
        resp = hx.get(url, params=params)
        resp.raise_for_status()
        data: list[dict[str, Any]] = resp.json()
    if not data:
        output.fatal(f"No Temurin JDK {major} release found.")
    return data[0]


def install(version: str) -> Path:
    """Download and install Temurin JDK *version* to ``~/.rvs/runtimes/java/``.

    *version* is a major version number (``"21"``) or full semver.
    Returns the installation directory.
    """
    platform_target = runtime_platform()
    arch = _ARCH_MAP[platform_target.arch]
    os_name = {
        "linux": "alpine-linux" if platform_target.libc == "musl" else "linux",
        "macos": "mac",
        "windows": "windows",
    }[platform_target.system]

    try:
        major = int(version.split(".")[0].split("+")[0])
    except ValueError:
        output.fatal(f"Cannot parse Java version '{version}'.")

    release = _latest_release(major, arch, os_name)
    binary_meta = release.get("binary", {})
    pkg = binary_meta.get("package", {})
    dl_url: str = pkg.get("link", "")
    checksum: str | None = pkg.get("checksum")
    semver: str = release.get("version", {}).get("semver", version)
    parsed_download = urlsplit(dl_url)
    if (
        parsed_download.scheme != "https"
        or parsed_download.hostname not in _ALLOWED_DOWNLOAD_HOSTS
        or parsed_download.username is not None
        or parsed_download.password is not None
    ):
        output.fatal("Temurin release metadata returned an untrusted download URL.")

    dest = RUNTIMES_DIR / "java" / semver
    if dest.exists():
        output.info(f"Java {semver} already installed at {dest}")
        return dest

    with tempfile.TemporaryDirectory() as tmp:
        if not checksum:
            output.fatal("Temurin release metadata did not provide a SHA-256 digest.")
        fname = dl_url.split("/")[-1].split("?")[0] or f"jdk-{semver}.tar.gz"
        archive = Path(tmp) / fname
        download(dl_url, archive, expected_sha256=checksum)
        required_java = "bin/java.exe" if platform_target.windows else "bin/java"
        extract(
            archive,
            dest,
            nested_root="Contents/Home" if platform_target.system == "macos" else None,
            required_paths=(required_java,),
        )

    bin_dir = dest / "bin"
    for exe in ("java", "javac", "jar", "javadoc"):
        executable = f"{exe}.exe" if platform_target.windows else exe
        if (bin_dir / executable).exists():
            write_shim(exe, bin_dir / executable, runtime_kind="java")

    write_env_file()
    output.success(f"Java {semver} (Temurin) installed at {dest}")
    if platform_target.windows:
        output.info(f"Set JAVA_HOME: $env:JAVA_HOME = '{dest}'")
        output.info("Add to PATH: . $HOME/.rvs/env.ps1")
    else:
        output.info(f"Set JAVA_HOME: export JAVA_HOME={dest}")
        output.info("Add to PATH: source ~/.rvs/env")
    return dest


def list_installed() -> list[tuple[str, Path]]:
    """Return [(version, path), ...] for all rvs-managed Java installations."""
    base = RUNTIMES_DIR / "java"
    if not base.exists():
        return []
    return sorted(
        [(p.name, p) for p in base.iterdir() if p.is_dir()],
        key=lambda t: version_key(t[0]),
    )


def find(version_prefix: str) -> Path | None:
    """Return the newest installed Java matching *version_prefix*."""
    vp = version_prefix.rstrip(".")
    if not vp:
        installed = list_installed()
        return installed[-1][1] if installed else None
    matches = [
        path
        for ver, path in list_installed()
        if ver == vp or ver.startswith(vp + ".") or ver.startswith(vp + "+")
    ]
    return matches[-1] if matches else None


def java_bin(version_prefix: str) -> Path | None:
    """Return the ``java`` binary path for *version_prefix*, or None."""
    base = find(version_prefix)
    return (base / "bin" / ("java.exe" if os.name == "nt" else "java")) if base else None
