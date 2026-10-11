"""Build and package one native, self-contained rvs release bundle."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from release_identity import ASSET_STEM


ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"


def _linux_libc() -> str:
    libc, _version = platform.libc_ver()
    if libc.lower() == "musl":
        return "musl"
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


def _target() -> tuple[str, str]:
    system = platform.system().lower()
    system_name = {"darwin": "macos", "windows": "windows", "linux": "linux"}.get(system)
    if system_name is None:
        raise SystemExit(f"unsupported release operating system: {system}")
    machine = platform.machine().lower()
    architecture = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if architecture is None:
        raise SystemExit(f"unsupported release architecture: {machine}")
    if system_name == "linux":
        system_name = "linux-musl" if _linux_libc() == "musl" else "linux"
    return system_name, architecture


def _version() -> str:
    import tomllib

    with (ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def _copy_alias(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)
    if os.name != "nt":
        destination.chmod(0o755)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    system, architecture = _target()
    version = _version()
    work = ROOT / "build" / "pyinstaller"
    frozen = DIST / "pyinstaller"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(frozen),
            "--workpath",
            str(work),
            str(ROOT / "packaging" / "pyinstaller" / "rvs.spec"),
        ],
        cwd=ROOT,
        check=True,
    )
    bundle = frozen / "rvs"
    suffix = ".exe" if system == "windows" else ""
    executable = bundle / f"rvs{suffix}"
    bundled_cryptography = list(bundle.rglob("cryptography"))
    if system == "windows" and bundled_cryptography:
        raise SystemExit("Windows bundle unexpectedly contains cryptography")
    if system != "windows" and not bundled_cryptography:
        raise SystemExit("POSIX bundle is missing encrypted-vault cryptography support")
    for alias in ("ravenstash", "docker-credential-rvs"):
        _copy_alias(executable, bundle / f"{alias}{suffix}")
    subprocess.run([str(executable), "--version"], check=True)
    subprocess.run([str(executable), "--help"], check=True, stdout=subprocess.DEVNULL)

    release = DIST / "release"
    release.mkdir(parents=True, exist_ok=True)
    archive_root = f"{ASSET_STEM}-v{version}-{system}-{architecture}"
    staging = ROOT / "build" / "release" / archive_root
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(bundle, staging)
    shutil.copy2(ROOT / "README.md", staging / "README.md")
    shutil.copy2(ROOT / "LICENSE", staging / "LICENSE")
    shutil.copy2(ROOT / "NOTICE", staging / "NOTICE")
    if system == "windows":
        archive = release / f"{archive_root}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    output.write(path, Path(archive_root) / path.relative_to(staging))
    else:
        archive = release / f"{archive_root}.tar.gz"
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))

        def normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = epoch
            return info

        with tarfile.open(archive, "w:gz") as output:
            output.add(staging, arcname=archive_root, filter=normalized)
    print(f"{_sha256(archive)}  {archive.name}")


if __name__ == "__main__":
    main()
