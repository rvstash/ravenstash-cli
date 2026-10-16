"""pip, uv, Twine, and pypa/build adapters."""

import os
from pathlib import Path
from typing import TYPE_CHECKING

from .. import output
from ..publishing import pypi_publish_items
from ..runtime import tools
from .adapter import NativeAdapter
from .common import arg_value, nearest_named_files, remove_value_options, write_netrc


if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..publishing import PublishItem
    from .common import PackageOperation, RegistryRoute


# `rvs python` runs only pypa/build; other Python code never receives credentials.
_BUILD_MODULE = ["-m", "build"]


def requirement_includes(argv: list[str]) -> list[str]:
    """Return requirement and constraint files named by pip-style arguments."""
    found: list[str] = []
    for index, arg in enumerate(argv):
        if arg in {"-r", "--requirement", "-c", "--constraint", "--build-constraint"}:
            if index + 1 < len(argv):
                found.append(argv[index + 1])
        elif arg.startswith(("--requirement=", "--constraint=", "--build-constraint=")):
            found.append(arg.split("=", 1)[1])
        elif arg.startswith(("-r", "-c")) and len(arg) > 2 and not arg.startswith("--"):
            found.append(arg[2:])
    return found


def _pip_config_files(env: Mapping[str, str]) -> list[Path]:
    candidates: list[Path] = []
    if env.get("PIP_CONFIG_FILE"):
        candidates.append(Path(env["PIP_CONFIG_FILE"]).expanduser())
    if env.get("VIRTUAL_ENV"):
        candidates.append(Path(env["VIRTUAL_ENV"]) / "pip.conf")
    candidates.extend(
        [
            Path.cwd() / "pip.conf",
            Path.home() / ".config/pip/pip.conf",
            Path.home() / ".pip/pip.conf",
            Path("/etc/pip.conf"),
        ]
    )
    return candidates


def _uv_config_files(env: Mapping[str, str]) -> list[Path]:
    candidates = nearest_named_files(("uv.toml", "pyproject.toml"))
    if env.get("UV_CONFIG_FILE"):
        candidates.append(Path(env["UV_CONFIG_FILE"]).expanduser())
    candidates.append(Path.home() / ".config/uv/uv.toml")
    return candidates


class Pip(NativeAdapter):
    name = "pip"
    kind = "pypi"
    env_prefixes = ("PIP_",)

    def command_prefix(self) -> list[str]:
        return tools.pip_cmd()

    def inject(
        self,
        cmd: list[str],
        native_arg_start: int,
        env: dict[str, str],
        route: RegistryRoute,
        temp_dir: Path,
        *,
        isolate: bool,
    ) -> None:
        url = route.pypi_index_url
        write_netrc(env, [url], route.package_token, temp_dir)
        env["PIP_INDEX_URL"] = url
        if isolate:
            env["PIP_CONFIG_FILE"] = os.devnull
        if len(cmd) <= native_arg_start:
            return
        if cmd[native_arg_start] not in {"install", "download", "wheel", "index", "lock"}:
            return
        remove_value_options(cmd, {"--index-url", "-i"}, native_arg_start)
        cmd[native_arg_start + 1 : native_arg_start + 1] = ["--index-url", url]

    def config_files(self, argv: list[str], env: Mapping[str, str]) -> list[Path]:
        del argv
        return _pip_config_files(env)

    def extra_source_files(self, argv: list[str], base: Path) -> list[Path]:
        return [base / name for name in requirement_includes(argv) if "://" not in name]

    def source_notes(self) -> tuple[str, ...]:
        return ("pip combines candidates across indexes; extra indexes have no lower priority.",)


class Uv(NativeAdapter):
    name = "uv"
    kind = "pypi"
    env_prefixes = ("UV_", "NETRC")
    project_files = ("uv.lock", "uv.toml", "pyproject.toml")

    def command_prefix(self) -> list[str]:
        return [tools.require("uv", install_kind="system")]

    def is_publishing(self, argv: list[str]) -> bool:
        return any(arg == "publish" for arg in argv if not arg.startswith("-"))

    def publish_items(self, argv: list[str]) -> list[PublishItem]:
        return pypi_publish_items(argv, command="publish", default_dist=True)

    def inject(
        self,
        cmd: list[str],
        native_arg_start: int,
        env: dict[str, str],
        route: RegistryRoute,
        temp_dir: Path,
        *,
        isolate: bool,
    ) -> None:
        token = route.package_token
        publishing = "publish" in cmd[native_arg_start:]
        urls = [route.pypi_index_url]
        if publishing:
            urls.append(route.pypi_upload_url)
        write_netrc(env, urls, token, temp_dir)
        remove_value_options(cmd, {"--default-index", "--index-url"}, native_arg_start)
        env["UV_DEFAULT_INDEX"] = route.pypi_index_url
        env["UV_INDEX_URL"] = route.pypi_index_url
        if publishing:
            remove_value_options(cmd, {"--publish-url", "--index"}, native_arg_start)
            cmd.extend(["--publish-url", route.pypi_upload_url])
            env["UV_PUBLISH_URL"] = route.pypi_upload_url
            env["UV_PUBLISH_USERNAME"] = "__token__"
            env["UV_PUBLISH_PASSWORD"] = token
        if isolate:
            env["UV_NO_CONFIG"] = "1"
            env["UV_NO_ENV_FILE"] = "1"

    def config_files(self, argv: list[str], env: Mapping[str, str]) -> list[Path]:
        del argv
        return _uv_config_files(env)

    def project_dir(self, argv: list[str]) -> Path:
        base = Path(arg_value(argv, "--directory", "-C") or os.getcwd()).resolve()
        project = arg_value(argv, "--project")
        return (base / project).resolve() if project else base

    def extra_source_files(self, argv: list[str], base: Path) -> list[Path]:
        files = [base / name for name in requirement_includes(argv) if "://" not in name]
        config = arg_value(argv, "--config-file")
        if config:
            files.append(base / config)
        return files


class Twine(NativeAdapter):
    name = "twine"
    kind = "pypi"
    env_prefixes = ("TWINE_",)

    def command_prefix(self) -> list[str]:
        return [tools.require("twine", install_kind="system")]

    def operations(self, argv: list[str]) -> tuple[PackageOperation, ...]:
        del argv
        return ("publish",)

    def is_publishing(self, argv: list[str]) -> bool:
        return "upload" in argv

    def publish_items(self, argv: list[str]) -> list[PublishItem]:
        return pypi_publish_items(argv, command="upload", default_dist=False)

    def inject(
        self,
        cmd: list[str],
        native_arg_start: int,
        env: dict[str, str],
        route: RegistryRoute,
        temp_dir: Path,
        *,
        isolate: bool,
    ) -> None:
        del temp_dir, isolate
        remove_value_options(
            cmd,
            {"--repository-url", "--username", "-u", "--password", "-p"},
            native_arg_start,
        )
        env["TWINE_REPOSITORY_URL"] = route.pypi_upload_url
        env["TWINE_USERNAME"] = "__token__"
        env["TWINE_PASSWORD"] = route.package_token
        env.setdefault("TWINE_NON_INTERACTIVE", "1")

    def config_files(self, argv: list[str], env: Mapping[str, str]) -> list[Path]:
        del env
        candidates: list[Path] = []
        config_file = arg_value(argv, "--config-file")
        if config_file:
            candidates.append(Path(config_file).expanduser())
        candidates.append(Path.home() / ".pypirc")
        return candidates


class Build(NativeAdapter):
    """`python -m build`: pypa/build installs build requirements with pip or uv."""

    name = "python"
    kind = "pypi"
    env_prefixes = ("PIP_", "UV_", "NETRC")

    @property
    def advisory(self) -> str:
        return "build"

    def command_prefix(self) -> list[str]:
        return [tools.python(), *_BUILD_MODULE]

    def native_args(self, argv: list[str]) -> list[str]:
        """Return pypa/build's own arguments, refusing any other Python invocation."""
        if argv[: len(_BUILD_MODULE)] != _BUILD_MODULE:
            output.fatal(
                "rvs python only runs `python -m build`. Run other Python commands directly; "
                "rvs does not pass Ravenstash access to them."
            )
        return argv[len(_BUILD_MODULE) :]

    def needs_target(self, native_args: list[str]) -> bool:
        # A build without isolation installs nothing. Only exact spellings: a
        # missed combined flag merely injects unused access.
        unisolated = "--no-isolation" in native_args or "-n" in native_args
        return super().needs_target(native_args) and not unisolated

    def inject(
        self,
        cmd: list[str],
        native_arg_start: int,
        env: dict[str, str],
        route: RegistryRoute,
        temp_dir: Path,
        *,
        isolate: bool,
    ) -> None:
        # pypa/build passes its environment to the pip or uv process that installs
        # build requirements, so no argument changes are needed.
        del cmd, native_arg_start
        url = route.pypi_index_url
        write_netrc(env, [url], route.package_token, temp_dir)
        env["PIP_INDEX_URL"] = url
        env["UV_DEFAULT_INDEX"] = url
        env["UV_INDEX_URL"] = url
        if isolate:
            env["PIP_CONFIG_FILE"] = os.devnull
            env["UV_NO_CONFIG"] = "1"

    def config_files(self, argv: list[str], env: Mapping[str, str]) -> list[Path]:
        candidates = _pip_config_files(env)
        if arg_value(argv, "--installer") == "uv":
            candidates.extend(_uv_config_files(env))
        return candidates
