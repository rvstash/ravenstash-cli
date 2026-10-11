"""Compatibility-channel policy for signed Ravenstash APT releases."""

import re


_NUMBER = r"(?:0|[1-9][0-9]*)"
_CHANNEL_PATTERN = re.compile(rf"^v({_NUMBER})$")
_MINOR_PATTERN = re.compile(rf"^({_NUMBER})\.({_NUMBER})$")
_VERSION_PATTERN = re.compile(
    r"^([0-9]+)\.([0-9]+)\.([0-9]+)(?:rc[1-9][0-9]*|[~+.-][A-Za-z0-9.-]+)?$"
)


def channel_for_version(version: str) -> str:
    """Return the compatibility channel that owns a Debian package version."""
    match = _VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError(f"unsupported rvs version: {version}")
    major = int(match.group(1))
    return f"v{major}"


def normalize_channel(value: str) -> str:
    """Normalize a rolling major release channel such as ``0`` or ``v1``."""
    candidate = value if value.startswith("v") else f"v{value}"
    if _CHANNEL_PATTERN.fullmatch(candidate) is None:
        raise ValueError("release channel must look like 0 or v1")
    return candidate


def normalize_minor_target(value: str) -> str:
    """Normalize an exact minor-line selector such as ``0.15``."""
    match = _MINOR_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("update target must look like 0, v0, or 0.15")
    return f"{int(match.group(1))}.{int(match.group(2))}"


def minor_target_for_version(version: str) -> str:
    match = _VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError(f"unsupported rvs version: {version}")
    return f"{int(match.group(1))}.{int(match.group(2))}"


def version_matches_channel(version: str, channel: str) -> bool:
    try:
        return channel_for_version(version) == normalize_channel(channel)
    except ValueError:
        return False


def channel_order(channel: str) -> tuple[int]:
    """Return a monotonic ordering key for explicit forward upgrades."""
    normalized = normalize_channel(channel)
    match = _CHANNEL_PATTERN.fullmatch(normalized)
    assert match is not None
    return (int(match.group(1)),)
