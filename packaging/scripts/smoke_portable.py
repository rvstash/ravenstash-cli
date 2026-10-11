"""Smoke-test a native portable bundle without network credentials."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from release_identity import ASSET_STEM


ROOT = Path(__file__).resolve().parents[2]
COMMANDS = ("rvs", "ravenstash", "docker-credential-rvs")


def _reported_version(executable: Path, environment: dict[str, str]) -> str:
    result = subprocess.run(
        [str(executable), "--version"],
        check=True,
        env=environment,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _verify_update_archive(
    archive: Path,
    scratch: Path,
    version: str,
    target: str,
    suffix: str,
    environment: dict[str, str],
) -> None:
    """Unpack the release archive exactly as `rvs update --apply` does."""
    sys.path.insert(0, str(ROOT))
    from rvs.portable_update import UpdateError, extract_release

    if not archive.is_file():
        raise SystemExit(f"missing release archive: {archive}")
    # A just-run Windows executable can stay locked briefly; leftovers are build output.
    with tempfile.TemporaryDirectory(
        prefix="smoke-update-", dir=scratch, ignore_cleanup_errors=True
    ) as work:
        try:
            extracted = extract_release(archive, Path(work) / "extracted", version, target)
        except UpdateError as exc:
            raise SystemExit(f"rvs update would reject {archive.name}: {exc}") from exc
        for name in COMMANDS:
            path = extracted / f"{name}{suffix}"
            if path.is_symlink() or not path.is_file():
                raise SystemExit(f"the updater did not unpack a regular executable: {path.name}")
        reported = _reported_version(extracted / f"rvs{suffix}", environment)
        if reported != f"Ravenstash CLI {version}":
            raise SystemExit(f"unpacked archive version mismatch: {reported}")
    print(f"rvs update accepts {archive.name}")


def main() -> None:
    bundle = Path(sys.argv[1]).resolve()
    identity = re.fullmatch(
        rf"{re.escape(ASSET_STEM)}-v(?P<version>.+)-"
        r"(?P<target>(?:linux(?:-musl)?|macos|windows)-(?:amd64|arm64))",
        bundle.name,
    )
    if identity is None:
        raise SystemExit(f"unexpected bundle directory name: {bundle.name}")
    suffix = ".exe" if os.name == "nt" else ""
    for name in COMMANDS:
        path = bundle / f"{name}{suffix}"
        if not path.is_file():
            raise SystemExit(f"missing executable: {path}")
    environment = os.environ.copy()
    environment.pop("RVS_TOKEN", None)
    temporary_home = bundle.parent / "smoke-home"
    temporary_home.mkdir(mode=0o700, exist_ok=True)
    environment["RVS_HOME"] = str(temporary_home / ".rvs")
    version = _reported_version(bundle / f"rvs{suffix}", environment)
    if version != f"Ravenstash CLI {identity.group('version')}":
        raise SystemExit(f"bundle version mismatch: {version}")
    print(version)
    subprocess.run(
        [str(bundle / f"rvs{suffix}"), "--help"],
        check=True,
        env=environment,
        stdout=subprocess.DEVNULL,
    )
    status = subprocess.run(
        [str(bundle / f"ravenstash{suffix}"), "auth", "status"],
        check=False,
        env=environment,
        stdout=subprocess.DEVNULL,
    )
    if status.returncode != 1:
        raise SystemExit(f"unexpected unauthenticated status exit code: {status.returncode}")
    archive_suffix = ".zip" if identity.group("target").startswith("windows-") else ".tar.gz"
    archive = (
        Path(sys.argv[2]).resolve()
        if len(sys.argv) > 2
        else ROOT / "dist" / "release" / f"{bundle.name}{archive_suffix}"
    )
    _verify_update_archive(
        archive,
        bundle.parent,
        identity.group("version"),
        identity.group("target"),
        suffix,
        environment,
    )


if __name__ == "__main__":
    main()
