#!/usr/bin/env python3
"""Fail closed unless the complete multi-channel APT tree is authentic and exact."""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import gzip
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from channel_policy import (
    PACKAGE_NAME,
    compatibility_channel,
    manifest,
    policy,
    validate_freshness,
    version_matches_channel,
)


def fail(message: str) -> None:
    raise SystemExit(f"error: {message}")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        fail(f"unsafe repository path: {value}")
    return path


def field(text: str, key: str, *, required: bool = True) -> str | None:
    prefix = f"{key}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    if required:
        fail(f"Release is missing {key}")
    return None


def release_entries(text: str) -> dict[PurePosixPath, tuple[str, int]]:
    entries: dict[PurePosixPath, tuple[str, int]] = {}
    active = False
    for line in text.splitlines():
        if line == "SHA256:":
            active = True
            continue
        if active and line and not line.startswith(" "):
            break
        if not active or not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3 or len(parts[0]) != 64:
            fail("malformed Release SHA256 inventory")
        path = relative(parts[2])
        if path in entries:
            fail(f"duplicate Release path: {path}")
        entries[path] = (parts[0].lower(), int(parts[1]))
    return entries


def stanzas(text: str) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    for block in text.strip().split("\n\n"):
        item: dict[str, str] = {}
        for line in block.splitlines():
            if not line.startswith((" ", "\t")):
                key, separator, value = line.partition(":")
                if separator:
                    item[key] = value.strip()
        if item:
            parsed.append(item)
    return parsed


def deb_field(package: Path, name: str) -> str:
    result = subprocess.run(
        ["dpkg-deb", "--field", str(package), name],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        fail(f"cannot inspect Debian package: {package.name}")
    return result.stdout.strip()


def verify_signature(keyring: Path, signature: Path, content: Path | None = None) -> bytes:
    command = ["gpgv", f"--keyring={keyring.resolve()}"]
    with tempfile.TemporaryDirectory(prefix="rvs-verified-") as directory:
        cleartext = Path(directory) / "cleartext"
        if content is None:
            command.extend([f"--output={cleartext}", str(signature)])
        else:
            command.extend([str(signature), str(content)])
        result = subprocess.run(command, check=False, capture_output=True)
        if result.returncode:
            fail(f"signature verification failed: {signature.name}")
        if content is None:
            return cleartext.read_bytes()
    return content.read_bytes() if content is not None else b""


def verify_distribution(repo: Path, keyring: Path, distribution: str) -> set[PurePosixPath]:
    channel = compatibility_channel(distribution)
    dist = repo / "dists" / distribution
    release = verify_signature(keyring, dist / "InRelease").decode("utf-8")

    if field(release, "Acquire-By-Hash").lower() != "yes":
        fail(f"Acquire-By-Hash is not enabled for {distribution}")
    valid_until = email.utils.parsedate_to_datetime(field(release, "Valid-Until"))
    # Repository tooling runs in the Ubuntu 20.04 signing image (Python 3.8).
    now = dt.datetime.now(dt.timezone.utc)  # noqa: UP017
    if valid_until.tzinfo is None or not now < valid_until <= now + dt.timedelta(days=8):
        fail(f"Valid-Until is expired or exceeds policy for {distribution}")
    if field(release, "Suite") != distribution or field(release, "Codename") != distribution:
        fail(f"Release identity mismatch for {distribution}")
    declared_channel = field(release, "Ravenstash-Compatibility-Channel")
    if declared_channel != channel:
        fail(f"compatibility channel mismatch for {distribution}")

    entries = release_entries(release)
    architectures = (field(release, "Architectures") or "").split()
    if not architectures or any(
        architecture not in {"amd64", "arm64"} for architecture in architectures
    ):
        fail(f"unsupported or empty architecture list for {distribution}")
    required = {
        PurePosixPath(f"main/binary-{architecture}/{index}")
        for architecture in architectures
        for index in ("Packages", "Packages.gz")
    }
    if not required.issubset(entries):
        fail(f"Release does not cover both package indexes for {distribution}")
    for path, (expected_digest, expected_size) in entries.items():
        target = dist.joinpath(*path.parts)
        if (
            not target.is_file()
            or target.stat().st_size != expected_size
            or digest(target) != expected_digest
        ):
            fail(f"Release inventory mismatch for {distribution}: {path}")

    listed: set[PurePosixPath] = set()
    required_fields = {"Package", "Version", "Architecture", "Filename", "Size", "SHA256"}
    for architecture in architectures:
        packages_file = dist / f"main/binary-{architecture}/Packages"
        with gzip.open(
            dist / f"main/binary-{architecture}/Packages.gz", "rt", encoding="utf-8"
        ) as stream:
            if stream.read() != packages_file.read_text(encoding="utf-8"):
                fail(f"Packages.gz does not reproduce Packages for {distribution}/{architecture}")
        for package_data in stanzas(packages_file.read_text(encoding="utf-8")):
            if missing := required_fields - package_data.keys():
                fail(f"Packages stanza is missing {sorted(missing)}")
            path = relative(package_data["Filename"])
            package = repo.joinpath(*path.parts)
            expected_digest = package_data["SHA256"].lower()
            if (
                package_data["Package"] != PACKAGE_NAME
                or package_data["Architecture"] != architecture
                or not version_matches_channel(package_data["Version"], channel)
                or path in listed
                or path.suffix != ".deb"
                or not package.is_file()
                or package.stat().st_size != int(package_data["Size"])
                or digest(package) != expected_digest
                or f"_{expected_digest[:16]}.deb" not in package.name
            ):
                fail(f"package inventory mismatch for {distribution}: {path}")
            for control_field in ("Package", "Version", "Architecture"):
                if deb_field(package, control_field) != package_data[control_field]:
                    fail(f"Debian control mismatch for {path}: {control_field}")
            listed.add(path)
    if not listed:
        fail(f"distribution {distribution} contains no packages")
    return listed


def verify_manifest(repo: Path, keyring: Path, distributions: set[str]) -> None:
    manifest_path = repo / "channels.json"
    signature_path = repo / "channels.json.gpg"
    if not manifest_path.is_file() or not signature_path.is_file():
        fail("signed channel manifest is missing")
    verify_signature(keyring, signature_path, manifest_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest(repo, payload["recommended"])
        validate_freshness(payload)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        fail(f"invalid channel manifest: {exc}")
    if policy(payload) != policy(expected):
        fail("channel manifest does not match signed APT distributions")
    if set(payload["channels"]) != distributions:
        fail("channel manifest and distribution inventory differ")


def verify(repository: Path, keyring: Path) -> None:
    repo = repository.resolve()
    if not keyring.is_file():
        fail("trusted keyring is missing")
    distributions = {
        path.name
        for path in (repo / "dists").iterdir()
        if path.is_dir() and (path / "InRelease").is_file()
    }
    if not distributions:
        fail("repository has no signed distributions")
    listed: set[PurePosixPath] = set()
    for distribution in sorted(distributions):
        listed.update(verify_distribution(repo, keyring, distribution))
    present = {
        PurePosixPath(path.relative_to(repo).as_posix()) for path in (repo / "pool").rglob("*.deb")
    }
    if present != listed:
        fail("the pool contains missing or unlisted Debian packages")
    verify_manifest(repo, keyring, distributions)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", type=Path)
    parser.add_argument("trusted_keyring", type=Path)
    arguments = parser.parse_args()
    verify(arguments.repository, arguments.trusted_keyring)
    print("APT repository verification passed.")


if __name__ == "__main__":
    main()
