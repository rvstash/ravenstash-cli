"""Version ordering helpers for managed runtimes."""

import re


def version_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Return a natural ordering key for dotted and vendor runtime versions."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.findall(r"\d+|[^\d]+", value)
    )
