#!/usr/bin/env python3
"""Recover omitted APT indexes only when signed metadata proves exact bytes."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path, PurePosixPath

from channel_policy import compatibility_channel, filter_packages
from verify_apt import field, release_entries, verify_signature


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def matching_content(
    content: bytes,
    expected: tuple[str, int],
    *,
    description: str,
) -> bytes:
    expected_digest, expected_size = expected
    if len(content) != expected_size or digest(content) != expected_digest:
        raise SystemExit(f"error: cannot exactly recover signed APT index: {description}")
    return content


def recover(repository: Path, trusted_keyring: Path) -> None:
    repo = repository.resolve()
    all_packages = subprocess.run(
        ["apt-ftparchive", "-o", "APT::FTPArchive::AlwaysStat=true", "packages", "pool"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    for distribution in sorted((repo / "dists").iterdir()):
        if not distribution.is_dir() or not (distribution / "InRelease").is_file():
            continue
        release = verify_signature(trusted_keyring, distribution / "InRelease").decode("utf-8")
        entries = release_entries(release)
        channel = compatibility_channel(distribution.name)
        architectures = (field(release, "Architectures") or "").split()
        for architecture in architectures:
            packages_path = PurePosixPath(f"main/binary-{architecture}/Packages")
            compressed_path = PurePosixPath(f"main/binary-{architecture}/Packages.gz")
            if packages_path not in entries or compressed_path not in entries:
                raise SystemExit(
                    f"error: signed APT indexes are incomplete for {distribution.name}/{architecture}"
                )
            packages = matching_content(
                filter_packages(all_packages, channel, architecture).encode(),
                entries[packages_path],
                description=f"{distribution.name}/{packages_path}",
            )
            compressed = matching_content(
                subprocess.run(
                    ["gzip", "-9n"],
                    input=packages,
                    check=True,
                    capture_output=True,
                ).stdout,
                entries[compressed_path],
                description=f"{distribution.name}/{compressed_path}",
            )
            generated = {digest(packages): packages, digest(compressed): compressed}
            for path, expected in entries.items():
                content: bytes | None = None
                if path == packages_path:
                    content = packages
                elif path == compressed_path:
                    content = compressed
                elif (
                    len(path.parts) == 5
                    and path.parts[:3] == (*packages_path.parts[:2], "by-hash")
                    and path.parts[3] == "SHA256"
                ):
                    content = generated.get(path.name)
                if content is None:
                    continue
                target = distribution.joinpath(*path.parts)
                if target.is_file():
                    continue
                matching_content(
                    content,
                    expected,
                    description=f"{distribution.name}/{path}",
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", type=Path)
    parser.add_argument("trusted_keyring", type=Path)
    arguments = parser.parse_args()
    recover(arguments.repository, arguments.trusted_keyring)


if __name__ == "__main__":
    main()
