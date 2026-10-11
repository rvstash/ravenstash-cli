"""Safe environment construction for child processes launched by RVS."""

import os
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Mapping


_CONTROL_CREDENTIAL_VARIABLES = ("RVS_TOKEN",)


def child_environment(updates: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy the process environment without forwarding RVS control credentials."""
    env = dict(os.environ)
    for variable in _CONTROL_CREDENTIAL_VARIABLES:
        env.pop(variable, None)
    if updates:
        env.update(updates)
    return env


__all__ = ["child_environment"]
