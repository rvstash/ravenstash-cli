#!/usr/bin/env python3
"""Compatibility-channel policy and signed manifest generation."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


NUMBER = r"(?:0|[1-9][0-9]*)"
CHANNEL_PATTERN = re.compile(rf"^v({NUMBER})$")
VERSION_PATTERN = re.compile(
    r"^([0-9]+)\.([0-9]+)\.([0-9]+)(?:rc[1-9][0-9]*|[~+.-][A-Za-z0-9.-]+)?$"
)
# Signed freshness fields. The manifest is re-signed with the APT metadata by the
# daily refresh, so it shares the APT Valid-Until period.
TIMESTAMP_FIELDS = ("generated_at", "expires")
MANIFEST_VALIDITY = dt.timedelta(days=7)
MAX_MANIFEST_VALIDITY = dt.timedelta(days=8)
CLOCK_SKEW = dt.timedelta(minutes=5)


def release_identity() -> dict[str, str]:
    """Read the build and release identity shared with the packaging scripts."""
    source = Path(__file__).resolve().parents[1] / "identity.env"
    lines = source.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


IDENTITY = release_identity()
PACKAGE_NAME = IDENTITY["RAVENSTASH_CLI_PACKAGE"]
KEYRING_NAME = IDENTITY["RAVENSTASH_CLI_KEYRING"]


def utc_now() -> dt.datetime:
    # Repository tooling runs in the Ubuntu 20.04 signing image (Python 3.8).
    return dt.datetime.now(dt.timezone.utc)  # noqa: UP017


def format_timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: UP017


def parse_timestamp(value: object) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"invalid manifest timestamp: {value!r}")
    parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return parsed.replace(tzinfo=dt.timezone.utc)  # noqa: UP017


def policy(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a manifest without its signed freshness fields."""
    return {key: value for key, value in payload.items() if key not in TIMESTAMP_FIELDS}


def validate_freshness(payload: dict[str, Any], now: dt.datetime | None = None) -> None:
    """Require current, bounded freshness fields."""
    current = now or utc_now()
    generated_at = parse_timestamp(payload.get("generated_at"))
    expires = parse_timestamp(payload.get("expires"))
    if generated_at > current + CLOCK_SKEW:
        raise ValueError("manifest generated_at is in the future")
    if not generated_at < expires <= generated_at + MAX_MANIFEST_VALIDITY:
        raise ValueError("manifest validity period exceeds policy")
    if expires <= current:
        raise ValueError("manifest has expired")


def channel_for_version(version: str) -> str:
    match = VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError(f"unsupported rvs version: {version}")
    return f"v{int(match.group(1))}"


def normalize_channel(value: str) -> str:
    candidate = value if value.startswith("v") else f"v{value}"
    if CHANNEL_PATTERN.fullmatch(candidate) is None:
        raise ValueError("invalid compatibility channel")
    return candidate


def compatibility_channel(distribution: str) -> str:
    return normalize_channel(distribution)


def version_matches_channel(version: str, channel: str) -> bool:
    try:
        return channel_for_version(version) == normalize_channel(channel)
    except ValueError:
        return False


def stanzas(text: str) -> list[str]:
    return [block for block in text.strip().split("\n\n") if block.strip()]


def stanza_fields(stanza: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in stanza.splitlines():
        if line.startswith((" ", "\t")):
            continue
        key, separator, value = line.partition(":")
        if separator:
            result[key] = value.strip()
    return result


def filter_packages(text: str, channel: str, architecture: str | None = None) -> str:
    normalized = normalize_channel(channel)
    selected: list[str] = []
    for stanza in stanzas(text):
        fields = stanza_fields(stanza)
        version = fields.get("Version", "")
        if version_matches_channel(version, normalized) and (
            architecture is None or fields.get("Architecture") == architecture
        ):
            selected.append(stanza)
    return "\n\n".join(selected) + ("\n" if selected else "")


def newer(left: str, right: str) -> bool:
    result = subprocess.run(
        ["dpkg", "--compare-versions", left, "gt", right],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def versions(packages_file: Path, channel: str) -> list[str]:
    return [
        fields["Version"]
        for block in stanzas(packages_file.read_text(encoding="utf-8"))
        if (fields := stanza_fields(block)).get("Package") == PACKAGE_NAME
        and version_matches_channel(fields.get("Version", ""), channel)
    ]


def latest(versions: list[str], label: str) -> str:
    if not versions:
        raise ValueError(f"{label} has no {PACKAGE_NAME} packages")
    result = versions[0]
    for candidate in versions[1:]:
        if newer(candidate, result):
            result = candidate
    return result


def latest_version(packages_file: Path, channel: str) -> str:
    return latest(versions(packages_file, channel), f"channel {channel}")


def minor_targets(packages_file: Path, channel: str) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for version in versions(packages_file, channel):
        match = VERSION_PATTERN.fullmatch(version)
        assert match is not None
        selector = f"{int(match.group(1))}.{int(match.group(2))}"
        grouped.setdefault(selector, []).append(version)
    return {
        selector: latest(candidates, f"minor target {selector}")
        for selector, candidates in sorted(grouped.items())
    }


def manifest(
    repository: Path, recommended: str, *, now: dt.datetime | None = None
) -> dict[str, Any]:
    recommended = normalize_channel(recommended)
    channels: dict[str, dict[str, Any]] = {}
    for distribution in sorted((repository / "dists").iterdir()):
        if not distribution.is_dir():
            continue
        channel = normalize_channel(distribution.name)
        packages = distribution / "main/binary-amd64/Packages"
        if not packages.is_file():
            continue
        channels[channel] = {
            "latest": latest_version(packages, channel),
            "minor_targets": minor_targets(packages, channel),
            "migration_notes": f"https://docs.ravenstash.com/cli/releases/{channel[1:]}/",
            "status": "supported",
        }
    if recommended not in channels:
        raise ValueError(f"recommended channel {recommended} is not published")
    generated_at = (now or utc_now()).replace(microsecond=0)
    return {
        "channels": channels,
        "expires": format_timestamp(generated_at + MANIFEST_VALIDITY),
        "generated_at": format_timestamp(generated_at),
        "recommended": recommended,
        "schema": 1,
    }


def existing_recommended(repository: Path, fallback: str) -> str:
    path = repository / "channels.json"
    if not path.is_file():
        return normalize_channel(fallback)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return normalize_channel(payload["recommended"])


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    channel_parser = subparsers.add_parser("channel-for-version")
    channel_parser.add_argument("version")
    compatibility_parser = subparsers.add_parser("compatibility-channel")
    compatibility_parser.add_argument("distribution")
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("version")
    validate_parser.add_argument("channel")
    filter_parser = subparsers.add_parser("filter")
    filter_parser.add_argument("channel")
    filter_parser.add_argument("--architecture", choices=("amd64", "arm64"))
    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("repository", type=Path)
    manifest_parser.add_argument("recommended")
    recommended_parser = subparsers.add_parser("existing-recommended")
    recommended_parser.add_argument("repository", type=Path)
    recommended_parser.add_argument("fallback")
    same_policy_parser = subparsers.add_parser(
        "same-policy",
        help="exit 0 when two manifests differ at most in their freshness fields",
    )
    same_policy_parser.add_argument("left", type=Path)
    same_policy_parser.add_argument("right", type=Path)
    arguments = parser.parse_args()

    try:
        if arguments.command == "channel-for-version":
            print(channel_for_version(arguments.version))
        elif arguments.command == "compatibility-channel":
            print(compatibility_channel(arguments.distribution))
        elif arguments.command == "validate":
            channel = normalize_channel(arguments.channel)
            if not version_matches_channel(arguments.version, channel):
                raise ValueError(
                    f"version {arguments.version} does not belong to channel {channel}"
                )
        elif arguments.command == "filter":
            sys.stdout.write(
                filter_packages(sys.stdin.read(), arguments.channel, arguments.architecture)
            )
        elif arguments.command == "manifest":
            print(
                json.dumps(
                    manifest(arguments.repository, arguments.recommended),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif arguments.command == "same-policy":
            left = json.loads(arguments.left.read_text(encoding="utf-8"))
            right = json.loads(arguments.right.read_text(encoding="utf-8"))
            if policy(left) != policy(right):
                raise SystemExit(1)
        else:
            print(existing_recommended(arguments.repository, arguments.fallback))
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    main()
