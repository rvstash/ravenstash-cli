#!/usr/bin/env python3
"""Create and apply a bounded signed-metadata handoff for an APT refresh."""

from __future__ import annotations

import argparse
import filecmp
import tarfile
from pathlib import Path, PurePosixPath


ROOT_METADATA = {PurePosixPath("channels.json"), PurePosixPath("channels.json.gpg")}
DIST_METADATA = {"InRelease", "Release", "Release.gpg"}


def fail(message: str) -> None:
    raise SystemExit(f"error: {message}")


def repository_files(repository: Path) -> set[PurePosixPath]:
    paths: set[PurePosixPath] = set()
    for path in repository.rglob("*"):
        if path.is_symlink():
            fail(f"repository contains a symbolic link: {path.relative_to(repository)}")
        if path.is_file():
            paths.add(PurePosixPath(path.relative_to(repository).as_posix()))
    return paths


def is_distribution_metadata(path: PurePosixPath) -> bool:
    return len(path.parts) == 3 and path.parts[0] == "dists" and path.name in DIST_METADATA


def is_handoff_path(path: PurePosixPath) -> bool:
    return path in ROOT_METADATA or is_distribution_metadata(path)


def is_generated_signing_file(path: PurePosixPath) -> bool:
    return (
        len(path.parts) == 1
        and path.name.startswith("apt-ftparchive-release-")
        and path.name.endswith(".conf")
    )


def required_handoff_paths(repository: Path) -> set[PurePosixPath]:
    distributions = {
        path.name
        for path in (repository / "dists").iterdir()
        if path.is_dir() and (path / "InRelease").is_file()
    }
    if not distributions:
        fail("repository has no signed distributions")
    return ROOT_METADATA | {
        PurePosixPath("dists") / distribution / name
        for distribution in distributions
        for name in DIST_METADATA
    }


def assert_only_metadata_changed(prior: Path, signed: Path) -> set[PurePosixPath]:
    required = required_handoff_paths(signed)
    signed_files = repository_files(signed)
    if not required.issubset(signed_files):
        fail(f"signed repository is missing handoff files: {sorted(required - signed_files)}")

    prior_immutable = {path for path in repository_files(prior) if not is_handoff_path(path)}
    signed_immutable = {
        path
        for path in signed_files
        if not is_handoff_path(path) and not is_generated_signing_file(path)
    }
    if prior_immutable != signed_immutable:
        fail("APT refresh changed the immutable repository inventory")
    for path in sorted(prior_immutable):
        if not filecmp.cmp(
            prior.joinpath(*path.parts), signed.joinpath(*path.parts), shallow=False
        ):
            fail(f"APT refresh changed immutable content: {path}")
    return required


def create_handoff(prior: Path, signed: Path, bundle: Path) -> None:
    required = assert_only_metadata_changed(prior, signed)
    bundle.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle, "w:gz") as archive:
        for path in sorted(required):
            archive.add(signed.joinpath(*path.parts), arcname=path.as_posix(), recursive=False)


def apply_handoff(repository: Path, bundle: Path) -> None:
    required = required_handoff_paths(repository)
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        names = [PurePosixPath(member.name) for member in members]
        if (
            len(names) != len(set(names))
            or set(names) != required
            or any(
                not member.isfile() or not is_handoff_path(name)
                for member, name in zip(members, names, strict=True)
            )
        ):
            fail("signed metadata handoff has an invalid inventory")
        for member, path in zip(members, names, strict=True):
            if member.size > 1_000_000:
                fail(f"signed metadata file exceeds the size limit: {path}")
            source = archive.extractfile(member)
            if source is None:
                fail(f"cannot read signed metadata file: {path}")
            destination = repository.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read())


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("prior", type=Path)
    create_parser.add_argument("signed", type=Path)
    create_parser.add_argument("bundle", type=Path)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("repository", type=Path)
    apply_parser.add_argument("bundle", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "create":
        create_handoff(arguments.prior, arguments.signed, arguments.bundle)
    else:
        apply_handoff(arguments.repository, arguments.bundle)


if __name__ == "__main__":
    main()
