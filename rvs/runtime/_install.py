"""Shared helpers for downloading and extracting runtime archives."""

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx2 as httpx

from .. import output
from ..paths import rvs_home


# ── Directory constants ────────────────────────────────────────────────────────

RVS_DIR = rvs_home()
RUNTIMES_DIR = RVS_DIR / "runtimes"  # ~/.rvs/runtimes/{kind}/{version}/
SHIMS_DIR = RVS_DIR / "shims"  # ~/.rvs/shims/{binary} → shim scripts
ENV_FILE = RVS_DIR / "env"  # ~/.rvs/env  (source in shell rc)


# ── Platform helpers ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuntimePlatform:
    system: str
    arch: str
    libc: str | None = None

    @property
    def windows(self) -> bool:
        return self.system == "windows"


def runtime_platform() -> RuntimePlatform:
    """Return the normalized native runtime target."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    system = {"darwin": "macos"}.get(system, system)
    if system not in {"linux", "macos", "windows"}:
        output.fatal(f"rvs runtime management is unsupported on {system}.")
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    else:
        output.fatal(f"Unsupported CPU architecture: {machine}.")
    libc = _linux_libc() if system == "linux" else None
    return RuntimePlatform(system, arch, "musl" if libc == "musl" else libc)


def _linux_libc() -> str:
    libc = platform.libc_ver()[0].lower()
    if libc:
        return libc
    try:
        detected = subprocess.run(
            ["ldd", "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return "glibc"
    details = f"{detected.stdout}\n{detected.stderr}".lower()
    return "musl" if "musl" in details else "glibc"


def is_debian() -> bool:
    """Return True on Debian/Ubuntu systems."""
    return Path("/etc/debian_version").exists()


# ── Download ───────────────────────────────────────────────────────────────────


_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 200_000
_MAX_EXTRACTED_BYTES = 8 * 1024 * 1024 * 1024


def download(url: str, dest: Path, *, expected_sha256: str) -> None:
    """Download *url* to *dest* with a simple progress indicator.

    The expected SHA-256 digest is mandatory. Runtime archives are executable
    supply-chain inputs and must never be accepted based on HTTPS alone.
    """
    normalized_digest = expected_sha256.removeprefix("sha256:").lower()
    if len(normalized_digest) != 64 or any(
        character not in "0123456789abcdef" for character in normalized_digest
    ):
        output.fatal("Runtime archive is missing a valid SHA-256 digest.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fname = url.split("/")[-1].split("?")[0]
    output.info(f"Downloading {fname} ...")
    digest = hashlib.sha256()
    with httpx.stream("GET", url, follow_redirects=True, timeout=180.0) as resp:
        resp.raise_for_status()
        if resp.url.scheme != "https":
            output.fatal("Runtime archive redirected to a non-HTTPS URL.")
        total = int(resp.headers.get("content-length", 0))
        if total > _MAX_DOWNLOAD_BYTES:
            output.fatal(f"Runtime archive exceeds {_MAX_DOWNLOAD_BYTES} bytes.")
        downloaded = 0
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(65536):
                fh.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                if downloaded > _MAX_DOWNLOAD_BYTES:
                    dest.unlink(missing_ok=True)
                    output.fatal(f"Runtime archive exceeds {_MAX_DOWNLOAD_BYTES} bytes.")
                if total and not output.is_json():
                    pct = downloaded * 100 // total
                    mb_done = downloaded // (1024 * 1024)
                    mb_total = total // (1024 * 1024)
                    print(f"\r  {pct:3d}%  {mb_done} / {mb_total} MB  ", end="", flush=True)
    if total and not output.is_json():
        print()  # newline after progress bar
    actual = digest.hexdigest()
    if actual != normalized_digest:
        dest.unlink(missing_ok=True)
        output.fatal(
            f"Checksum mismatch for {fname}:\n  expected {normalized_digest}\n  got      {actual}"
        )


# ── Extract ────────────────────────────────────────────────────────────────────


def extract(
    archive: Path,
    dest: Path,
    *,
    strip_root: bool = True,
    nested_root: str | None = None,
    required_paths: tuple[str, ...] = (),
) -> None:
    """Extract a .tar.gz / .tar.xz archive to *dest*.

    When *strip_root* is True (default) the single top-level directory inside
    the archive is stripped so that ``dest/bin/`` contains binaries directly.
    """
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.info(f"Extracting to {dest} ...")
    with tempfile.TemporaryDirectory(prefix=".rvs-extract-", dir=dest.parent) as tmp:
        tmp_path = Path(tmp)
        extracted_path = tmp_path / "archive"
        extracted_path.mkdir()
        try:
            if zipfile.is_zipfile(archive):
                with zipfile.ZipFile(archive) as zf:
                    members = zf.infolist()
                    if len(members) > _MAX_ARCHIVE_MEMBERS:
                        output.fatal("Runtime archive contains too many entries.")
                    if sum(member.file_size for member in members) > _MAX_EXTRACTED_BYTES:
                        output.fatal("Runtime archive expands beyond the safety limit.")
                    root = extracted_path.resolve()
                    for member in members:
                        if not (root / member.filename).resolve().is_relative_to(root):
                            output.fatal("Runtime archive contains an unsafe path.")
                    zf.extractall(extracted_path)
            else:
                with tarfile.open(archive) as tf:
                    members = tf.getmembers()
                    if len(members) > _MAX_ARCHIVE_MEMBERS:
                        output.fatal("Runtime archive contains too many entries.")
                    extracted_bytes = sum(member.size for member in members if member.isfile())
                    if extracted_bytes > _MAX_EXTRACTED_BYTES:
                        output.fatal("Runtime archive expands beyond the safety limit.")
                    tf.extractall(extracted_path, members=members, filter="data")
        except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
            output.fatal(f"Runtime archive extraction was rejected: {exc}")
        roots = list(extracted_path.iterdir())
        src = roots[0] if strip_root and len(roots) == 1 and roots[0].is_dir() else extracted_path
        if nested_root is not None:
            nested = (src / nested_root).resolve()
            if not nested.is_relative_to(src.resolve()) or not nested.is_dir():
                output.fatal(f"Runtime archive is missing required directory: {nested_root}")
            src = nested
        prepared = tmp_path / "prepared"
        prepared.mkdir()
        for child in src.iterdir():
            shutil.move(str(child), str(prepared))
        prepared_root = prepared.resolve()
        for relative_path in required_paths:
            candidate = prepared / relative_path
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                output.fatal(f"Runtime archive is missing required file: {relative_path}")
            if not resolved.is_relative_to(prepared_root) or not resolved.is_file():
                output.fatal(f"Runtime archive has an unsafe required file: {relative_path}")
        if dest.exists():
            output.fatal(f"Runtime destination appeared during installation: {dest}")
        os.replace(prepared, dest)


# ── Shim ──────────────────────────────────────────────────────────────────────


def write_shim(
    name: str,
    target: Path,
    *,
    runtime_kind: str | None = None,
    version: str | None = None,
) -> None:
    """Write a shim, resolving project pins dynamically when a kind is supplied."""
    SHIMS_DIR.mkdir(parents=True, exist_ok=True)
    windows = os.name == "nt"
    shim = SHIMS_DIR / (f"{name}.cmd" if windows else name)
    if runtime_kind:
        version_arg = f' "{version}"' if version else ""
        if windows:
            script = (
                "@echo off\r\n"
                f'for /f "delims=" %%i in (\'rvs runtime which "{runtime_kind}"{version_arg} '
                f'--executable "{name}"\') do set "RVS_RUNTIME_TARGET=%%i"\r\n'
                '"%RVS_RUNTIME_TARGET%" %*\r\n'
            )
        else:
            script = (
                "#!/bin/sh\n"
                f'target="$(rvs runtime which "{runtime_kind}"{version_arg} '
                f'--executable "{name}")" || exit $?\n'
                'exec "$target" "$@"\n'
            )
    else:
        script = f'@"{target}" %*\r\n' if windows else f'#!/bin/sh\nexec "{target}" "$@"\n'
    shim.write_text(script, encoding="utf-8", newline="")
    shim.chmod(0o755)


# ── Shell env file ─────────────────────────────────────────────────────────────


def write_env_file() -> None:
    """Write (or overwrite) ~/.rvs/env — a shell snippet adding rvs shims to PATH."""
    RVS_DIR.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(
        "# rvs managed runtimes — source this in ~/.bashrc or ~/.zshrc\n"
        'export RVS_HOME="$HOME/.rvs"\n'
        'export PATH="$RVS_HOME/shims:$PATH"\n',
        encoding="utf-8",
    )
    if os.name == "nt":
        (RVS_DIR / "env.ps1").write_text(
            '$env:RVS_HOME = Join-Path $HOME ".rvs"\n'
            '$env:PATH = "$env:RVS_HOME\\shims;$env:PATH"\n',
            encoding="utf-8",
        )
