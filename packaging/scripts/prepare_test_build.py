"""Derive and apply an ephemeral version for a GitHub Actions test build."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


BASE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
TEST_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.dev[1-9][0-9]*\+g[0-9a-f]{8}$")


def derive_version(base_version: str, source_sha: str, run_id: str) -> str:
    if not BASE_VERSION.fullmatch(base_version):
        raise ValueError("base version must be X.Y.Z")
    if not SOURCE_SHA.fullmatch(source_sha):
        raise ValueError("source SHA must be 40 lowercase hexadecimal characters")
    if not run_id.isdecimal() or int(run_id) < 1:
        raise ValueError("run ID must be a positive integer")
    return f"{base_version}.dev{run_id}+g{source_sha[:8]}"


def _replace_project_version(path: Path, version: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_project = False
    replaced = 0
    for index, line in enumerate(lines):
        if line.startswith("["):
            in_project = line.strip() == "[project]"
        elif in_project and line.startswith("version = "):
            lines[index] = f'version = "{version}"\n'
            replaced += 1
    if replaced != 1:
        raise ValueError(f"expected one [project] version in {path}, found {replaced}")
    path.write_text("".join(lines), encoding="utf-8")


def _replace_lock_version(path: Path, project: str, version: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    package_start: int | None = None
    name: str | None = None
    replaced = 0

    def replace_block(end: int) -> None:
        nonlocal replaced
        if package_start is None or name != project:
            return
        for index in range(package_start, end):
            if lines[index].startswith("version = "):
                lines[index] = f'version = "{version}"\n'
                replaced += 1
                return
        raise ValueError(f"{project} package has no version in {path}")

    for index, line in enumerate(lines):
        if line.strip() == "[[package]]":
            replace_block(index)
            package_start = index
            name = None
        elif package_start is not None and line.startswith("name = "):
            name = line.partition('"')[2].partition('"')[0]
    replace_block(len(lines))

    if replaced != 1:
        raise ValueError(f"expected one {project} package in {path}, found {replaced}")
    path.write_text("".join(lines), encoding="utf-8")


def _replace_literal(path: Path, pattern: str, replacement: str) -> None:
    source = path.read_text(encoding="utf-8")
    updated, replacements = re.subn(pattern, replacement, source, count=1, flags=re.MULTILINE)
    if replacements != 1:
        raise ValueError(f"expected one release version in {path}, found {replacements}")
    path.write_text(updated, encoding="utf-8")


def apply_version(root: Path, version: str) -> None:
    if not TEST_VERSION.fullmatch(version):
        raise ValueError("test version must be X.Y.Z.devRUN_ID+gSHA8")
    _replace_project_version(root / "pyproject.toml", version)
    with (root / "pyproject.toml").open("rb") as source:
        project = str(tomllib.load(source)["project"]["name"])
    _replace_lock_version(root / "uv.lock", project, version)
    _replace_literal(
        root / "packaging/install.sh",
        r'^readonly release_version="[^"]+"$',
        f'readonly release_version="{version}"',
    )
    _replace_literal(
        root / "packaging/install.ps1",
        r'^\$ReleaseVersion = "[^"]+"$',
        f'$ReleaseVersion = "{version}"',
    )
    with (root / "pyproject.toml").open("rb") as source:
        if tomllib.load(source)["project"]["version"] != version:
            raise ValueError("failed to apply test version")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    derive = subparsers.add_parser("derive")
    derive.add_argument("base_version")
    derive.add_argument("source_sha")
    derive.add_argument("run_id")
    apply = subparsers.add_parser("apply")
    apply.add_argument("version")
    apply.add_argument("--root", type=Path, default=Path.cwd())
    arguments = parser.parse_args()

    try:
        if arguments.command == "derive":
            print(derive_version(arguments.base_version, arguments.source_sha, arguments.run_id))
        else:
            apply_version(arguments.root, arguments.version)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
