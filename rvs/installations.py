"""Installation receipts and update-target detection."""

import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .identity import RECEIPT_NAME


_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:rc[1-9][0-9]*)?$")
_CHANNEL = re.compile(r"^v[0-9]+$")
_TARGETS = {
    "linux-amd64",
    "linux-arm64",
    "linux-musl-amd64",
    "linux-musl-arm64",
    "macos-amd64",
    "macos-arm64",
    "windows-amd64",
    "windows-arm64",
}


@dataclass(frozen=True)
class Installation:
    schema: int
    method: str
    scope: str
    version: str
    channel: str
    target: str
    install_root: str
    bin_directory: str

    @property
    def root(self) -> Path:
        return Path(self.install_root)

    @property
    def bin(self) -> Path:
        return Path(self.bin_directory)


def compatibility_channel(version: str) -> str:
    match = _VERSION.fullmatch(version)
    if match is None:
        raise ValueError("invalid installed rvs version")
    major, _minor, _patch = version.removesuffix(_candidate_suffix(version)).split(".")
    return f"v{major}"


def _candidate_suffix(version: str) -> str:
    match = re.search(r"rc[1-9][0-9]*$", version)
    return match.group(0) if match else ""


def platform_target() -> str:
    architecture = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(platform.machine().lower())
    if architecture is None:
        raise ValueError(f"unsupported CPU architecture: {platform.machine()}")
    system = platform.system()
    if system == "Darwin":
        return f"macos-{architecture}"
    if system == "Windows":
        return f"windows-{architecture}"
    if system != "Linux":
        raise ValueError(f"unsupported operating system: {system}")
    libc, _version = platform.libc_ver()
    if libc.lower() == "musl":
        return f"linux-musl-{architecture}"
    try:
        result = subprocess.run(["ldd", "--version"], check=False, capture_output=True, text=True)
        details = f"{result.stdout}\n{result.stderr}".lower()
    except OSError:
        details = ""
    return f"linux-musl-{architecture}" if "musl" in details else f"linux-{architecture}"


def validate_installation(value: Installation) -> Installation:
    if value.schema != 1 or value.method != "portable":
        raise ValueError("unsupported rvs installation receipt")
    if value.scope not in {"user", "system"}:
        raise ValueError("invalid rvs installation scope")
    if _VERSION.fullmatch(value.version) is None:
        raise ValueError("invalid version in rvs installation receipt")
    if (
        _CHANNEL.fullmatch(value.channel) is None
        or compatibility_channel(value.version) != value.channel
    ):
        raise ValueError("version and channel do not match in rvs installation receipt")
    if value.target not in _TARGETS:
        raise ValueError("invalid target in rvs installation receipt")
    for label, path in (("install root", value.root), ("bin directory", value.bin)):
        if not path.is_absolute() or path == Path(path.anchor):
            raise ValueError(f"invalid {label} in rvs installation receipt")
    return value


def write_receipt(path: Path, installation: Installation) -> None:
    validate_installation(installation)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(asdict(installation), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def read_receipt(path: Path) -> Installation:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        return validate_installation(Installation(**payload))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid rvs installation receipt: {path}") from exc


def detect_portable_installation(
    *, executable: Path | None = None, installed_version: str
) -> Installation | None:
    """Return the validated receipt for a portable installation."""

    if executable is None:
        if not getattr(sys, "frozen", False):
            return None
        executable = Path(sys.executable)
    executable = executable.resolve()
    receipt = executable.parent / RECEIPT_NAME
    if not receipt.is_file():
        return None
    installation = read_receipt(receipt)
    if installation.version != installed_version or installation.target != platform_target():
        raise ValueError("the rvs installation receipt does not describe the running executable")
    if not executable.is_relative_to(installation.root):
        raise ValueError("the running rvs executable is outside its recorded install root")
    return installation
