"""Best-effort advisories for unusually old native package clients."""

import re
from dataclasses import dataclass
from functools import cache
from subprocess import PIPE, Popen, TimeoutExpired
from tempfile import TemporaryDirectory

from . import output


@dataclass(frozen=True)
class ToolAdvisory:
    minimum: tuple[int, ...]
    display_minimum: str
    version_arguments: tuple[str, ...]
    reason: str


# These are warning thresholds, not execution requirements. Keep them low enough
# to preserve broad compatibility and raise them only with an explicit policy update.
ADVISORIES: dict[str, ToolAdvisory] = {
    "pip": ToolAdvisory(
        (23, 0, 0),
        "23.0",
        ("--version",),
        "older pip releases are several years behind current packaging and security fixes",
    ),
    "uv": ToolAdvisory(
        (0, 4, 30),
        "0.4.30",
        ("--version",),
        "older uv releases lack one or more index, publishing, or isolation controls used by rvs",
    ),
    "twine": ToolAdvisory(
        (4, 0, 2),
        "4.0.2",
        ("--version",),
        "older Twine releases predate important upload and dependency compatibility fixes",
    ),
    "build": ToolAdvisory(
        (1, 0, 0),
        "1.0",
        ("--version",),
        "older build releases are several years behind current packaging fixes",
    ),
    "npm": ToolAdvisory(
        (10, 0, 0),
        "10.0",
        ("--version",),
        "older npm lines are normally paired with end-of-life Node.js runtimes",
    ),
    "pnpm": ToolAdvisory(
        (10, 0, 0),
        "10.0",
        ("--version",),
        "pnpm 9 and earlier ignore the registry credential rvs supplies in the environment "
        "when the registry path contains an underscore",
    ),
    "mvn": ToolAdvisory(
        (3, 9, 0),
        "3.9.0",
        ("--version",),
        "Maven 3.8 and earlier are end-of-life upstream",
    ),
    "docker": ToolAdvisory(
        (25, 0, 2),
        "25.0.2",
        ("--version",),
        "older Docker release lines may contain unfixed client or BuildKit security issues",
    ),
    "helm": ToolAdvisory(
        (3, 8, 0),
        "3.8.0",
        ("version", "--short"),
        "Helm OCI support was experimental before 3.8.0",
    ),
    "oras": ToolAdvisory(
        (1, 1, 0),
        "1.1.0",
        ("version",),
        "older ORAS releases predate the OCI 1.1 compatibility behavior exercised by rvs",
    ),
}

_VERSION = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?")


def _parsed_version(value: str) -> tuple[int, ...] | None:
    match = _VERSION.search(value)
    if match is None:
        return None
    return tuple(int(part) if part is not None else 0 for part in match.groups())


def _read_version(command: tuple[str, ...]) -> str | None:
    # Probe outside the project: pnpm switches to the version a project's
    # package.json pins, even for `--version`, and may download it first.
    with TemporaryDirectory(prefix="rvs-version-") as neutral:
        try:
            process = Popen(command, stdout=PIPE, stderr=PIPE, text=True, cwd=neutral)
        except OSError:
            return None
        return _communicate(process)


def _communicate(process: Popen[str]) -> str | None:
    try:
        stdout, stderr = process.communicate(timeout=5)
    except TimeoutExpired:
        process.kill()
        process.communicate()
        return None
    if process.returncode != 0:
        return None
    return f"{stdout}\n{stderr}"


_FULL_VERSION = re.compile(r"(?<![\d.])(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)")


@cache
def _version_output(tool: str, command: tuple[str, ...]) -> str | None:
    advisory = ADVISORIES.get(tool)
    if advisory is None:
        return None
    return _read_version((*command, *advisory.version_arguments))


def detected_version(tool: str, command: tuple[str, ...]) -> str | None:
    """Return the full version that *command* reports, such as `10.34.7`, if parseable."""
    version_output = _version_output(tool, command)
    if version_output is None:
        return None
    match = _FULL_VERSION.search(version_output)
    return match.group(1) if match else None


@cache
def _advisory_message(tool: str, command: tuple[str, ...]) -> str | None:
    advisory = ADVISORIES.get(tool)
    if advisory is None:
        return None
    version_output = _version_output(tool, command)
    if version_output is None:
        return None
    detected = _parsed_version(version_output)
    if detected is None or detected >= advisory.minimum:
        return None
    rendered = ".".join(str(part) for part in detected)
    return (
        f"Detected {tool} {rendered}. Ravenstash recommends {tool} "
        f"{advisory.display_minimum} or newer because {advisory.reason}. "
        "The command will continue for compatibility."
    )


def warn_if_old(tool: str, command: list[str]) -> None:
    """Warn about a parseable old client without blocking native execution."""
    if message := _advisory_message(tool, tuple(command)):
        output.warn(message)
