"""Install Python runtimes using python-build-standalone (same source as uv).

Downloads a self-contained CPython build from:
  https://github.com/astral-sh/python-build-standalone/releases

Installs to: ~/.rvs/runtimes/python/<full_version>/
"""

import os
import re
import tempfile
from pathlib import Path
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


_REPO = "astral-sh/python-build-standalone"
_GH_RELEASES = f"https://api.github.com/repos/{_REPO}/releases"

# Map normalized architecture to python-build-standalone architecture.
_ARCH_MAP = {"x64": "x86_64", "aarch64": "aarch64"}


def _gh_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _find_asset(version_prefix: str, target: str) -> tuple[str, str, str]:
    """Return (full_version, download_url, sha256) for an immutable PBS asset."""
    suffix = f"{target}-install_only.tar.gz"
    pattern = re.compile(r"cpython-(\d+\.\d+\.\d+)\+\d+-" + re.escape(suffix))
    # Search up to 5 pages of releases
    for page in range(1, 6):
        with httpx.Client(timeout=30.0) as hx:
            resp = hx.get(
                _GH_RELEASES,
                headers=_gh_headers(),
                params={"per_page": "10", "page": str(page)},
            )
            resp.raise_for_status()
            releases: list[dict[str, Any]] = resp.json()
        if not releases:
            break
        for release in releases:
            if release.get("immutable") is not True:
                continue
            for asset in release.get("assets", []):
                name: str = asset["name"]
                m = pattern.match(name)
                if not m:
                    continue
                full_ver = m.group(1)
                # Match if full_ver starts with the requested prefix
                vp = version_prefix.rstrip(".")
                if full_ver == vp or full_ver.startswith(vp + "."):
                    digest = asset.get("digest")
                    if not isinstance(digest, str) or not digest.startswith("sha256:"):
                        continue
                    return full_ver, asset["browser_download_url"], digest
    output.fatal(
        f"No Python {version_prefix} build found for {target}.\n"
        f"Check: https://github.com/{_REPO}/releases"
    )


def install(version: str) -> Path:
    """Download and install Python *version* to ``~/.rvs/runtimes/python/``.

    *version* may be ``"3.12"``, ``"3.12.1"``, etc.
    Returns the installation directory.
    """
    platform_target = runtime_platform()
    arch = _ARCH_MAP[platform_target.arch]
    triple = {
        "linux": f"{arch}-unknown-linux-{'musl' if platform_target.libc == 'musl' else 'gnu'}",
        "macos": f"{arch}-apple-darwin",
        "windows": f"{arch}-pc-windows-msvc",
    }[platform_target.system]

    output.info(f"Resolving Python {version} (python-build-standalone) ...")
    full_ver, url, expected_sha256 = _find_asset(version, triple)

    dest = RUNTIMES_DIR / "python" / full_ver
    if dest.exists():
        output.info(f"Python {full_ver} already installed at {dest}")
        return dest

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / url.split("/")[-1].split("?")[0]
        download(url, archive, expected_sha256=expected_sha256)
        required_python = "python.exe" if platform_target.windows else "bin/python3"
        extract(archive, dest, required_paths=(required_python,))

    # Write shims for python3 / python3.x
    bin_dir = dest if platform_target.windows else dest / "bin"
    python3 = bin_dir / ("python.exe" if platform_target.windows else "python3")
    if not python3.exists():
        candidates = sorted(bin_dir.glob("python3.*"))
        if candidates:
            python3 = candidates[0]
    if python3.exists():
        write_shim("python3", python3, runtime_kind="python")
        minor = ".".join(full_ver.split(".")[:2])  # e.g. "3.14"
        write_shim(
            f"python{minor}",
            python3,
            runtime_kind="python",
            version=minor,
        )

    write_env_file()
    output.success(f"Python {full_ver} installed at {dest}")
    output.info(
        "Add to PATH: . $HOME/.rvs/env.ps1"
        if platform_target.windows
        else "Add to PATH: source ~/.rvs/env"
    )
    return dest


def list_installed() -> list[tuple[str, Path]]:
    """Return [(version, path), ...] for all rvs-managed Python installations."""
    base = RUNTIMES_DIR / "python"
    if not base.exists():
        return []
    return sorted(
        [(p.name, p) for p in base.iterdir() if p.is_dir()],
        key=lambda t: version_key(t[0]),
    )


def find(version_prefix: str) -> Path | None:
    """Return the newest installed Python matching *version_prefix*."""
    vp = version_prefix.rstrip(".")
    if not vp:
        installed = list_installed()
        return installed[-1][1] if installed else None
    matches = [path for ver, path in list_installed() if ver == vp or ver.startswith(vp + ".")]
    return matches[-1] if matches else None


def python_bin(version_prefix: str) -> Path | None:
    """Return the ``python3`` binary path for *version_prefix*, or None."""
    base = find(version_prefix)
    if base is None:
        return None
    windows = os.name == "nt"
    names = (
        ("python.exe",)
        if windows
        else (f"python{'.'.join(version_prefix.split('.')[:2])}", "python3", "python")
    )
    for name in names:
        p = base / ("" if windows else "bin") / name
        if p.exists():
            return p
    return None
