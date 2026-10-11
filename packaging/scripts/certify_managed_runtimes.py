"""Exercise real managed-runtime downloads through a frozen rvs bundle."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def _run(command: list[str], environment: dict[str, str], *, success: bool = True) -> str:
    result = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=900,
    )
    output = f"{result.stdout}\n{result.stderr}".strip()
    if success and result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {command!r}\n{output}")
    if not success and result.returncode == 0:
        raise SystemExit(f"command unexpectedly succeeded: {command!r}\n{output}")
    return output


def _newest(directory: Path) -> Path:
    candidates = sorted(path for path in directory.iterdir() if path.is_dir())
    if not candidates:
        raise SystemExit(f"no runtime installation found below {directory}")
    return candidates[-1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--expect-node-unavailable", action="store_true")
    arguments = parser.parse_args()

    bundle = arguments.bundle.resolve()
    suffix = ".exe" if os.name == "nt" else ""
    executable = bundle / f"rvs{suffix}"
    if not executable.is_file():
        raise SystemExit(f"rvs launcher is missing: {executable}")

    environment = os.environ.copy()
    environment.pop("RVS_TOKEN", None)
    rvs_home = Path(environment["RVS_HOME"]).resolve()
    rvs_home.mkdir(parents=True, exist_ok=True)

    _run([str(executable), "runtime", "install", "python", "3.14"], environment)
    python_root = _newest(rvs_home / "runtimes" / "python")
    python = python_root / ("python.exe" if os.name == "nt" else "bin/python3")
    python_version = _run([str(python), "--version"], environment)
    if "Python 3.14" not in python_version:
        raise SystemExit(f"unexpected Python version: {python_version}")

    node_install = [str(executable), "runtime", "install", "node", "22"]
    if arguments.expect_node_unavailable:
        node_output = _run(node_install, environment, success=False)
        if "does not publish official musl binaries" not in node_output:
            raise SystemExit(f"unexpected Node.js failure: {node_output}")
    else:
        _run(node_install, environment)
        node_root = _newest(rvs_home / "runtimes" / "node")
        node = node_root / ("node.exe" if os.name == "nt" else "bin/node")
        node_version = _run([str(node), "--version"], environment)
        if not node_version.startswith("v22."):
            raise SystemExit(f"unexpected Node.js version: {node_version}")

    _run([str(executable), "runtime", "install", "java", "21"], environment)
    java_root = _newest(rvs_home / "runtimes" / "java")
    java = java_root / "bin" / ("java.exe" if os.name == "nt" else "java")
    java_version = _run([str(java), "-version"], environment)
    if 'version "21' not in java_version and "openjdk 21" not in java_version.lower():
        raise SystemExit(f"unexpected Java version: {java_version}")

    for runtime, version in (("python", "3.14"), ("java", "21")):
        _run([str(executable), "runtime", "which", runtime, version], environment)
    if not arguments.expect_node_unavailable:
        _run([str(executable), "runtime", "which", "node", "22"], environment)

    print("Managed Python, Node.js, and Java runtime certification passed.")


if __name__ == "__main__":
    main()
