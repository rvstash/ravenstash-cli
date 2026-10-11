"""Filesystem path helpers for rvs user state."""

import os
from pathlib import Path


def rvs_home() -> Path:
    """Return the rvs user-state directory.

    Defaults to ``~/.rvs`` and can be overridden with ``RVS_HOME``.  This keeps
    packaged installs independent from user configuration and runtime state.
    """
    configured = os.environ.get("RVS_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".rvs"


def rvs_path(*parts: str) -> Path:
    """Return a path below :func:`rvs_home`."""
    return rvs_home().joinpath(*parts)
