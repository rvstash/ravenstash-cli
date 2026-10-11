"""Runtime version selection helpers."""

from pathlib import Path


_MARKERS = {
    "python": ".python-version",
    "node": ".node-version",
    "java": ".java-version",
}


def selected_version(kind: str, cwd: Path | None = None) -> str | None:
    """Return the nearest project-pinned runtime version for *kind*."""
    marker = _MARKERS.get(kind)
    if marker is None:
        return None

    start = (cwd or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        path = directory / marker
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if value:
                return value
    return None
