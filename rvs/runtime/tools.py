"""Resolve rvs-managed runtimes before falling back to PATH."""

import os
import shutil
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


def _managed_node_dir() -> Path | None:
    from .node import find
    from .selection import selected_version

    base = find(selected_version("node") or "")
    return (base if os.name == "nt" else base / "bin") if base else None


def _managed_python_dir() -> Path | None:
    from .python import find
    from .selection import selected_version

    base = find(selected_version("python") or "")
    return (base if os.name == "nt" else base / "bin") if base else None


def resolve(name: str) -> str | None:
    """Return an absolute path to *name*, preferring rvs-managed runtimes."""
    if name in ("npm", "npx", "node"):
        node_dir = _managed_node_dir()
        if node_dir:
            candidates = (f"{name}.cmd", f"{name}.exe") if os.name == "nt" else (name,)
            for candidate_name in candidates:
                candidate = node_dir / candidate_name
                if candidate.exists():
                    return str(candidate)

    if name in ("pip", "pip3", "python", "python3"):
        py_dir = _managed_python_dir()
        if py_dir:
            if os.name == "nt":
                candidates = (
                    (py_dir / "python.exe",)
                    if name in {"python", "python3"}
                    else (py_dir / "Scripts" / "pip.exe", py_dir / "pip.exe")
                )
            else:
                candidates = tuple(
                    py_dir / candidate_name
                    for candidate_name in (name, "pip3", "pip", "python3", "python")
                )
            for candidate in candidates:
                if candidate.exists():
                    return str(candidate)

    return shutil.which(name)


def require(name: str, *, install_kind: str) -> str:
    """Return a tool path or exit with a clear install hint."""
    path = resolve(name)
    if path:
        return path

    from .. import output

    hint = (
        f"  Install it via rvs: rvs runtime install {install_kind} <version>\n"
        if install_kind in {"python", "node", "java"}
        else "  Install it system-wide and ensure it is on your PATH.\n"
    )
    output.fatal(f"'{name}' is not installed and was not found on PATH.\n{hint}")


def npm() -> str:
    return require("npm", install_kind="node")


def pip_cmd() -> list[str]:
    pip_path = resolve("pip") or resolve("pip3")
    if pip_path:
        return [pip_path]
    require("pip", install_kind="python")
    return ["pip"]
