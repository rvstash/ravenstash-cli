#!/usr/bin/env python3
"""Restore the complete authenticated APT tree from its public endpoint."""

from __future__ import annotations

import argparse
import json
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from channel_policy import (
    KEYRING_NAME,
    PACKAGE_NAME,
    normalize_channel,
    stanza_fields,
    stanzas,
)
from verify_apt import digest, fail, relative, release_entries, verify, verify_signature


Fetcher = Callable[[str, Path], None]


def public_fetcher(base_url: str) -> Fetcher:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("public APT base URL must be an HTTPS origin or path")
    normalized_base = base_url.rstrip("/") + "/"

    def fetch(path: str, destination: Path) -> None:
        safe_path = relative(path)
        encoded_path = urllib.parse.quote(safe_path.as_posix(), safe="/")
        request = urllib.request.Request(
            urllib.parse.urljoin(normalized_base, encoded_path),
            headers={
                "Cache-Control": "no-cache",
                "User-Agent": "Ravenstash-APT-Metadata-Refresh/1.0",
            },
        )
        last_error: OSError | urllib.error.URLError | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    final_url = urllib.parse.urlsplit(response.geturl())
                    if final_url.scheme != "https":
                        fail(f"public APT download left HTTPS: {safe_path}")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("wb") as output:
                        shutil.copyfileobj(response, output)
                return
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        fail(f"cannot download public APT path {safe_path}: {last_error}")

    return fetch


def authenticated_distributions(
    manifest_path: Path, signature_path: Path, keyring: Path
) -> set[str]:
    verify_signature(keyring, signature_path, manifest_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload["schema"] != 1 or not isinstance(payload["channels"], dict):
            raise ValueError("unsupported channel manifest schema")
        channels = set(payload["channels"])
        if not channels or any(normalize_channel(channel) != channel for channel in channels):
            raise ValueError("channel manifest contains an invalid channel")
        if normalize_channel(payload["recommended"]) not in channels:
            raise ValueError("recommended channel is not published")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        fail(f"invalid authenticated channel manifest: {exc}")
    return channels


def package_paths(packages: str) -> set[PurePosixPath]:
    paths: set[PurePosixPath] = set()
    for stanza in stanzas(packages):
        fields = stanza_fields(stanza)
        if fields.get("Package") != PACKAGE_NAME or "Filename" not in fields:
            fail("public APT Packages contains an invalid package stanza")
        path = relative(fields["Filename"])
        if path.suffix != ".deb":
            fail(f"public APT package is not a Debian archive: {path}")
        paths.add(path)
    return paths


def restore_public_repository(
    destination: Path,
    trusted_keyring: Path,
    fetch: Fetcher,
    *,
    include_packages: bool = True,
) -> None:
    if destination.exists() and any(destination.iterdir()):
        fail(f"restore destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)

    for path in ("channels.json", "channels.json.gpg"):
        fetch(path, destination / path)
    distributions = authenticated_distributions(
        destination / "channels.json",
        destination / "channels.json.gpg",
        trusted_keyring,
    )

    packages_to_fetch: set[PurePosixPath] = set()
    for distribution in sorted(distributions):
        prefix = PurePosixPath("dists") / distribution
        for name in ("InRelease", "Release", "Release.gpg"):
            path = prefix / name
            fetch(path.as_posix(), destination.joinpath(*path.parts))

        inrelease = destination.joinpath(*prefix.parts, "InRelease")
        release = destination.joinpath(*prefix.parts, "Release")
        release_signature = destination.joinpath(*prefix.parts, "Release.gpg")
        authenticated_release = verify_signature(trusted_keyring, inrelease)
        detached_release = verify_signature(trusted_keyring, release_signature, release)
        if authenticated_release != detached_release:
            fail(f"InRelease and Release disagree for {distribution}")

        entries = release_entries(authenticated_release.decode("utf-8"))
        for index, (expected_digest, expected_size) in entries.items():
            index_path = prefix / index
            index_target = destination.joinpath(*index_path.parts)
            fetch(index_path.as_posix(), index_target)
            if (
                index_target.stat().st_size != expected_size
                or digest(index_target) != expected_digest
            ):
                fail(f"public APT index does not match signed Release: {index_path}")
            by_hash = index_path.parent / "by-hash" / "SHA256" / expected_digest
            by_hash_target = destination.joinpath(*by_hash.parts)
            fetch(by_hash.as_posix(), by_hash_target)
            if by_hash_target.read_bytes() != index_target.read_bytes():
                fail(f"public APT by-hash index mismatch: {index_path}")

        package_indexes = {
            index
            for index in entries
            if len(index.parts) == 3
            and index.parts[0] == "main"
            and index.parts[1].startswith("binary-")
            and index.parts[2] == "Packages"
        }
        if not package_indexes:
            fail(f"public APT Release has no package indexes: {distribution}")
        distribution_packages: set[PurePosixPath] = set()
        for index in package_indexes:
            packages = destination.joinpath(*prefix.parts, *index.parts)
            distribution_packages.update(package_paths(packages.read_text(encoding="utf-8")))
        if not distribution_packages:
            fail(f"public APT distribution contains no {PACKAGE_NAME} packages: {distribution}")
        packages_to_fetch.update(distribution_packages)

    if include_packages:
        for package in sorted(packages_to_fetch):
            fetch(package.as_posix(), destination.joinpath(*package.parts))
    shutil.copyfile(trusted_keyring, destination / KEYRING_NAME)
    if include_packages:
        verify(destination, trusted_keyring)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("destination", type=Path)
    parser.add_argument("trusted_keyring", type=Path)
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="authenticate signed indexes without downloading referenced packages",
    )
    arguments = parser.parse_args()
    try:
        fetch = public_fetcher(arguments.base_url)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    restore_public_repository(
        arguments.destination,
        arguments.trusted_keyring,
        fetch,
        include_packages=not arguments.metadata_only,
    )
    print("Authenticated public APT repository restored.")


if __name__ == "__main__":
    main()
