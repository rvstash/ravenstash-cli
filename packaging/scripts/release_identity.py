"""Build and release identity read from ``packaging/identity.env``."""

from __future__ import annotations

from pathlib import Path


IDENTITY_FILE = Path(__file__).resolve().parents[1] / "identity.env"


def load(path: Path = IDENTITY_FILE) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))


IDENTITY = load()
ASSET_STEM = IDENTITY["RAVENSTASH_CLI_ASSET_STEM"]
HOMEBREW_CASK = IDENTITY["RAVENSTASH_CLI_HOMEBREW_CASK"]
WINGET_PACKAGE = IDENTITY["RAVENSTASH_CLI_WINGET_PACKAGE"]
