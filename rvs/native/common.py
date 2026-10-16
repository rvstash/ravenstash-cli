"""Registry routes and argument/credential helpers shared by native-tool adapters."""

import json
import netrc
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from .. import output
from ..artifacts.routing import CanonicalRouter, npm_auth_token_key


if TYPE_CHECKING:
    from ..artifacts.targets import PackageFormat


PackageOperation = Literal["read", "publish", "delete"]

_ROUTER = CanonicalRouter()


@dataclass(frozen=True)
class RegistryRoute:
    kind: PackageFormat
    read_base_url: str
    push_base_url: str | None
    # Opaque server-issued native path without its outer slashes.
    native_path: str
    package_token: str = field(repr=False)
    # Maven looks up credentials by repository ID. A per-run random ID cannot be
    # claimed in advance by a project POM that points elsewhere.
    maven_server_id: str = field(default_factory=lambda: f"rvs-{secrets.token_hex(12)}")

    @property
    def pypi_index_url(self) -> str:
        return _ROUTER.pypi_index_url(
            self.read_base_url,
            self.native_path,
        )

    @property
    def pypi_upload_url(self) -> str:
        if self.push_base_url is None:
            output.fatal("The selected private mirror is read-only.")
        return _ROUTER.pypi_upload_url(
            self.push_base_url,
            self.native_path,
        )

    @property
    def npm_registry_url(self) -> str:
        return _ROUTER.npm_registry_url(
            self.read_base_url,
            self.native_path,
        )

    @property
    def npm_upload_registry_url(self) -> str:
        if self.push_base_url is None:
            output.fatal("The selected private mirror is read-only.")
        return _ROUTER.npm_upload_registry_url(
            self.push_base_url,
            self.native_path,
        )

    @property
    def maven_repo_url(self) -> str:
        return _ROUTER.maven_repo_url(
            self.read_base_url,
            self.native_path,
        )

    @property
    def maven_upload_url(self) -> str:
        if self.push_base_url is None:
            output.fatal("The selected private mirror is read-only.")
        return _ROUTER.maven_upload_url(
            self.push_base_url,
            self.native_path,
        )


def arg_value(argv: list[str], option: str, short: str | None = None) -> str | None:
    for index, arg in enumerate(argv):
        if arg == option and index + 1 < len(argv):
            return argv[index + 1]
        if short and arg == short and index + 1 < len(argv):
            return argv[index + 1]
        prefix = f"{option}="
        if arg.startswith(prefix):
            return arg.removeprefix(prefix)
    return None


def remove_value_options(
    cmd: list[str],
    option_names: set[str],
    native_arg_start: int,
) -> None:
    cleaned = cmd[:native_arg_start]
    skip_next = False
    for arg in cmd[native_arg_start:]:
        if skip_next:
            skip_next = False
            continue
        if arg in option_names:
            skip_next = True
            continue
        if any(arg.startswith(f"{option}=") for option in option_names):
            continue
        cleaned.append(arg)
    cmd[:] = cleaned


def nearest_named_files(names: tuple[str, ...], start: Path | None = None) -> list[Path]:
    found: list[Path] = []
    base = start or Path.cwd()
    for directory in (base, *base.parents):
        for name in names:
            candidate = directory / name
            if candidate.exists() and candidate.is_file():
                found.append(candidate)
    return found


def positionals(argv: list[str], value_options: frozenset[str]) -> list[str]:
    """Return arguments that are not options or the values of *value_options*."""
    found: list[str] = []
    skip_value = False
    for arg in argv:
        if skip_value:
            skip_value = False
        elif arg in value_options:
            skip_value = True
        elif not arg.startswith("-"):
            found.append(arg)
    return found


def inject_npm_auth(env: dict[str, str], urls: list[str], token: str, prefix: str) -> None:
    for url in urls:
        env[f"{prefix}{npm_auth_token_key(url)}"] = token


def write_netrc(
    env: dict[str, str],
    urls: list[str],
    token: str,
    temp_dir: Path,
) -> None:
    hosts = sorted({host for url in urls if (host := urlparse(url).hostname) is not None})
    if not hosts:
        return
    netrc_path = temp_dir / "netrc"
    original = Path(env.get("NETRC", str(Path.home() / ".netrc"))).expanduser()
    preserved = ""
    if original.is_file():
        try:
            existing = netrc.netrc(str(original))
        except OSError, netrc.NetrcParseError:
            output.fatal("Cannot read native netrc credentials; repair the file before retrying.")
        for host in hosts:
            existing.hosts.pop(host, None)
        for host, (login, account, password) in existing.hosts.items():
            entry = "default" if host == "default" else f"machine {json.dumps(host)}"
            entry += f" login {json.dumps(login or '')}"
            if account:
                entry += f" account {json.dumps(account)}"
            entry += f" password {json.dumps(password or '')}\n"
            preserved += entry
    lines = [f"machine {host} login __token__ password {token}" for host in hosts]
    netrc_path.write_text(preserved + "\n".join(lines) + "\n", encoding="utf-8")
    netrc_path.chmod(0o600)
    env["NETRC"] = str(netrc_path)
