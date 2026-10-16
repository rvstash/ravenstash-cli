"""Credential-aware native package-manager launchers.

These wrappers never persist Ravenstash credentials into package-manager config
files. They require a selected target, preserve additional native sources, and inject
short-lived credentials for the child process without editing lockfiles.
"""

import json
import netrc
import os
import re
import secrets
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse, urlsplit

import typer

from .. import config as cfg_mod
from .. import output
from ..account.commands import acting_account_ref, resolve_account
from ..artifacts.routing import (
    CanonicalRouter,
    npm_auth_token_key,
)
from ..artifacts.targets import PackageFormat, registry_context
from ..artifacts.trash import NATIVE_UNPUBLISH_NOTICE, WHOLE_PACKAGE_UNPUBLISH_HINT
from ..publishing import PublishItem, confirm_change, confirm_context, native_artifacts
from ..runtime import tools
from ..subprocesses import child_environment
from ..tool_advisories import warn_if_old


if TYPE_CHECKING:
    from collections.abc import Mapping


NativeTool = Literal["pip", "uv", "twine", "python", "npm", "mvn"]
PackageOperation = Literal["read", "publish", "delete"]
ConfigPolicy = Literal["respect", "override", "isolate"]

POLICIES: tuple[ConfigPolicy, ...] = ("respect", "override", "isolate")

_ROUTER = CanonicalRouter()
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
# `rvs python` runs only pypa/build; other Python code never receives credentials.
_BUILD_MODULE = ["-m", "build"]
_NO_TARGET_ARGS = (["--version"], ["-V"], ["--help"], ["-h"], ["help"])


@dataclass(frozen=True)
class NativeOptions:
    yes: bool = False
    profile: str | None = None
    target: str | None = None
    account: str | None = None
    account_ref: str | None = None
    native_config: ConfigPolicy = "respect"


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


@dataclass
class ExecutionPlan:
    cmd: list[str]
    env: dict[str, str]
    native_command: tuple[str, ...]


def normalize_policy(value: str) -> ConfigPolicy:
    policy = value.strip().lower()
    if policy not in POLICIES:
        joined = ", ".join(POLICIES)
        output.fatal(f"Unknown --rvs-native-config '{value}'. Use one of: {joined}.")
    return policy  # type: ignore[return-value]


def run(tool: NativeTool, argv: list[str], options: NativeOptions) -> None:
    """Run *tool* with ephemeral Ravenstash credential injection."""
    with tempfile.TemporaryDirectory(prefix="rvs-native-") as temp_dir:
        plan = _build_plan(tool, argv, options, Path(temp_dir))
        if plan.native_command:
            warn_if_old("build" if tool == "python" else tool, list(plan.native_command))
        unpublishing = bool(plan.native_command) and tool == "npm" and _npm_is_unpublish(argv)
        if unpublishing:
            typer.echo(NATIVE_UNPUBLISH_NOTICE, err=True)
        result = subprocess.run(plan.cmd, env=plan.env, check=False)
        code = getattr(result, "returncode", 0)
        if code:
            if unpublishing:
                typer.echo(WHOLE_PACKAGE_UNPUBLISH_HINT, err=True)
            raise typer.Exit(code)


def _build_plan(
    tool: NativeTool,
    argv: list[str],
    options: NativeOptions,
    temp_dir: Path,
) -> ExecutionPlan:
    env = child_environment()
    native_args = _build_module_args(argv) if tool == "python" else argv
    command_prefix = _command_prefix_for(tool)
    if tool == "python":
        command_prefix = [*command_prefix, *_BUILD_MODULE]
    cmd = [*command_prefix, *native_args]
    native_arg_start = len(command_prefix)
    # Help/version do not resolve packages and need no account or target, and
    # a build without isolation installs nothing.
    if native_args in _NO_TARGET_ARGS or (tool == "python" and _build_is_unisolated(native_args)):
        return ExecutionPlan(cmd=cmd, env=env, native_command=())
    kind = _kind_for_tool(tool)
    account_ref = _selected_account_ref(options)
    saved = cfg_mod.selected_artifact_target(options.profile, account_ref)
    if not options.target and saved is None:
        output.fatal(
            "No Ravenstash target is selected. Run `rvs art select` or pass --rvs-target. "
            "Use the native tool directly for its default registries."
        )
    operations = _operations_for(tool, argv)
    # Never infer a target from native defaults or run an unconfigured client.
    route = _resolve_route(
        kind,
        options,
        operations,
        artifacts=native_artifacts(tool, argv) if _is_publishing(tool, argv) else None,
        confirmation=_npm_dist_tag_confirmation(argv, env) if tool == "npm" else None,
    )
    _inject_override(
        tool,
        cmd,
        native_arg_start,
        env,
        route,
        route.package_token,
        temp_dir,
        isolate=options.native_config == "isolate",
    )
    from .sources import warn_additional_sources

    warn_additional_sources(tool, argv, cmd[native_arg_start:], env, route)
    return ExecutionPlan(cmd=cmd, env=env, native_command=tuple(command_prefix))


def _command_prefix_for(tool: NativeTool) -> list[str]:
    match tool:
        case "pip":
            return tools.pip_cmd()
        case "npm":
            return [tools.npm()]
        case "uv":
            return [tools.require("uv", install_kind="system")]
        case "twine":
            return [tools.require("twine", install_kind="system")]
        case "python":
            return [tools.python()]
        case "mvn":
            return [tools.require("mvn", install_kind="system")]


def _build_module_args(argv: list[str]) -> list[str]:
    """Return pypa/build's own arguments, refusing any other Python invocation."""
    if argv[: len(_BUILD_MODULE)] != _BUILD_MODULE:
        output.fatal(
            "rvs python only runs `python -m build`. Run other Python commands directly; "
            "rvs does not pass Ravenstash access to them."
        )
    return argv[len(_BUILD_MODULE) :]


def _build_is_unisolated(argv: list[str]) -> bool:
    # Only exact spellings: a missed combined flag merely injects unused access.
    return "--no-isolation" in argv or "-n" in argv


def _kind_for_tool(tool: NativeTool) -> PackageFormat:
    match tool:
        case "pip" | "uv" | "twine" | "python":
            return "pypi"
        case "npm":
            return "npm"
        case "mvn":
            return "maven"


def _resolve_route(
    kind: PackageFormat,
    options: NativeOptions,
    operations: tuple[PackageOperation, ...],
    artifacts: list[PublishItem] | None = None,
    confirmation: str | None = None,
) -> RegistryRoute:
    context = registry_context(
        kind=kind,
        target=options.target,
        repo=None,
        profile=options.profile,
        account_ref=_selected_account_ref(options),
        operations=operations,
    )
    if artifacts is not None:
        confirm_context(context, artifacts, yes=options.yes)
    if confirmation is not None:
        confirm_change(context, confirmation, yes=options.yes)
    return RegistryRoute(
        kind=kind,
        read_base_url=context.read_base_url,
        push_base_url=context.push_base_url,
        native_path=context.native_path,
        package_token=context.token,
    )


def _selected_account_ref(options: NativeOptions) -> str | None:
    if options.account_ref:
        return options.account_ref
    if options.account:
        return str(resolve_account(options.account, options.profile)["ref"])
    return acting_account_ref(options.profile or cfg_mod.current_profile_name())


def _is_publishing(tool: NativeTool, argv: list[str]) -> bool:
    return (
        (tool == "twine" and "upload" in argv)
        or (tool == "uv" and "publish" in argv)
        or (tool == "npm" and _npm_is_publish(argv))
        or (tool == "mvn" and _maven_is_upload(argv))
    )


def _operations_for(
    tool: NativeTool,
    argv: list[str],
) -> tuple[PackageOperation, ...]:
    if tool == "twine":
        return ("publish",)
    if tool == "uv" and any(arg == "publish" for arg in argv if not arg.startswith("-")):
        return ("publish",)
    if tool == "npm" and _npm_is_publish(argv):
        return ("publish",)
    if tool == "npm" and _npm_is_unpublish(argv):
        # Publish never implies deletion. npm reads the write packument and
        # sends the edited document to the upload host before its delete, and
        # those steps keep requiring publish.
        return ("read", "publish", "delete")
    if tool == "npm" and _npm_is_mutation(argv):
        return ("read", "publish")
    if tool == "mvn" and _maven_is_upload(argv):
        return ("read", "publish")
    return ("read",)


def _extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in _URL_RE.finditer(text):
        urls.append(match.group(0).rstrip(".,);]"))
    return urls


def _same_origin(left: str, right: str) -> bool:
    first = urlsplit(left)
    second = urlsplit(right)
    try:
        return (
            first.scheme.lower(),
            first.hostname.rstrip(".").lower() if first.hostname else None,
            first.port or (443 if first.scheme.lower() == "https" else 80),
        ) == (
            second.scheme.lower(),
            second.hostname.rstrip(".").lower() if second.hostname else None,
            second.port or (443 if second.scheme.lower() == "https" else 80),
        )
    except ValueError:
        return False


def _relevant_env_values(tool: NativeTool, env: dict[str, str]) -> list[str]:
    prefixes = {
        "pip": ("PIP_",),
        "uv": ("UV_", "NETRC"),
        "twine": ("TWINE_",),
        "python": ("PIP_", "UV_", "NETRC"),
        "npm": ("NPM_CONFIG_", "npm_config_"),
        "mvn": ("MAVEN_",),
    }[tool]
    return [
        value for key, value in env.items() if key.startswith(prefixes) and isinstance(value, str)
    ]


def _candidate_config_files(
    tool: NativeTool,
    argv: list[str],
    env: Mapping[str, str],
) -> list[Path]:
    candidates: list[Path] = []
    match tool:
        case "pip":
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
        case "uv":
            candidates.extend(_nearest_named_files(("uv.toml", "pyproject.toml")))
            if env.get("UV_CONFIG_FILE"):
                candidates.append(Path(env["UV_CONFIG_FILE"]).expanduser())
            candidates.append(Path.home() / ".config/uv/uv.toml")
        case "python":
            # pypa/build installs build requirements with pip, or with uv when asked.
            candidates.extend(_candidate_config_files("pip", argv, env))
            if _arg_value(argv, "--installer") == "uv":
                candidates.extend(_candidate_config_files("uv", argv, env))
        case "twine":
            config_file = _arg_value(argv, "--config-file")
            if config_file:
                candidates.append(Path(config_file).expanduser())
            candidates.append(Path.home() / ".pypirc")
        case "npm":
            user_config = env.get("NPM_CONFIG_USERCONFIG") or env.get("npm_config_userconfig")
            global_config = env.get("NPM_CONFIG_GLOBALCONFIG") or env.get("npm_config_globalconfig")
            candidates.extend(_nearest_named_files((".npmrc",)))
            if user_config:
                candidates.append(Path(user_config).expanduser())
            candidates.append(Path.home() / ".npmrc")
            if global_config:
                candidates.append(Path(global_config).expanduser())
        case "mvn":
            settings_file = _maven_settings_arg(argv)
            if settings_file:
                candidates.append(Path(settings_file).expanduser())
            candidates.append(Path.home() / ".m2/settings.xml")
            candidates.extend(_nearest_named_files(("pom.xml",)))
    return [path for path in candidates if path.exists() and path.is_file()]


def _nearest_named_files(names: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for directory in (Path.cwd(), *Path.cwd().parents):
        for name in names:
            candidate = directory / name
            if candidate.exists() and candidate.is_file():
                found.append(candidate)
    return found


def _arg_value(argv: list[str], option: str, short: str | None = None) -> str | None:
    for index, arg in enumerate(argv):
        if arg == option and index + 1 < len(argv):
            return argv[index + 1]
        if short and arg == short and index + 1 < len(argv):
            return argv[index + 1]
        prefix = f"{option}="
        if arg.startswith(prefix):
            return arg.removeprefix(prefix)
    return None


def _inject_override(
    tool: NativeTool,
    cmd: list[str],
    native_arg_start: int,
    env: dict[str, str],
    route: RegistryRoute,
    token: str,
    temp_dir: Path,
    *,
    isolate: bool,
) -> None:
    match tool:
        case "pip":
            _inject_pip_override(
                cmd,
                native_arg_start,
                env,
                route,
                token,
                temp_dir,
                isolate=isolate,
            )
        case "uv":
            _inject_uv_override(
                cmd,
                native_arg_start,
                env,
                route,
                token,
                temp_dir,
                isolate=isolate,
            )
        case "twine":
            _inject_twine_override(cmd, native_arg_start, env, route, token, isolate=isolate)
        case "python":
            _inject_build_override(env, route, token, temp_dir, isolate=isolate)
        case "npm":
            _inject_npm_override(
                cmd,
                native_arg_start,
                env,
                route,
                token,
                isolate=isolate,
                temp_dir=temp_dir,
            )
        case "mvn":
            maven_args = cmd[native_arg_start:]
            urls = [route.maven_repo_url]
            if _maven_is_upload(maven_args):
                urls.append(route.maven_upload_url)
            settings_path = _write_maven_settings(
                urls=urls,
                token=token,
                temp_dir=temp_dir,
                argv=cmd[native_arg_start:],
                isolate=isolate,
                route=route,
            )
            _replace_maven_settings_arg(cmd, settings_path, native_arg_start)
            server_id = route.maven_server_id
            if _maven_has_goal(maven_args, "deploy"):
                cmd.append(
                    f"-DaltDeploymentRepository={server_id}::default::{route.maven_upload_url}"
                )
            if _maven_has_goal(maven_args, "deploy-file"):
                cmd.extend(
                    [
                        f"-DrepositoryId={server_id}",
                        f"-Durl={route.maven_upload_url}",
                    ]
                )


def _inject_pip_override(
    cmd: list[str],
    native_arg_start: int,
    env: dict[str, str],
    route: RegistryRoute,
    token: str,
    temp_dir: Path,
    *,
    isolate: bool,
) -> None:
    url = route.pypi_index_url
    _write_netrc(env, [url], token, temp_dir)
    env["PIP_INDEX_URL"] = url
    if isolate:
        env["PIP_CONFIG_FILE"] = os.devnull
    _insert_pip_index_arg(cmd, native_arg_start, url)


def _insert_pip_index_arg(cmd: list[str], native_arg_start: int, url: str) -> None:
    if len(cmd) <= native_arg_start:
        return
    subcommand = cmd[native_arg_start]
    if subcommand not in {"install", "download", "wheel", "index", "lock"}:
        return
    _remove_value_options(cmd, {"--index-url", "-i"}, native_arg_start)
    cmd[native_arg_start + 1 : native_arg_start + 1] = ["--index-url", url]


def _inject_uv_override(
    cmd: list[str],
    native_arg_start: int,
    env: dict[str, str],
    route: RegistryRoute,
    token: str,
    temp_dir: Path,
    *,
    isolate: bool,
) -> None:
    publishing = _is_publishing("uv", cmd[native_arg_start:])
    urls = [route.pypi_index_url]
    if publishing:
        urls.append(route.pypi_upload_url)
    _write_netrc(env, urls, token, temp_dir)
    _remove_value_options(cmd, {"--default-index", "--index-url"}, native_arg_start)
    env["UV_DEFAULT_INDEX"] = route.pypi_index_url
    env["UV_INDEX_URL"] = route.pypi_index_url
    if publishing:
        _remove_value_options(cmd, {"--publish-url", "--index"}, native_arg_start)
        cmd.extend(["--publish-url", route.pypi_upload_url])
        env["UV_PUBLISH_URL"] = route.pypi_upload_url
        env["UV_PUBLISH_USERNAME"] = "__token__"
        env["UV_PUBLISH_PASSWORD"] = token
    if isolate:
        env["UV_NO_CONFIG"] = "1"
        env["UV_NO_ENV_FILE"] = "1"


def _inject_build_override(
    env: dict[str, str],
    route: RegistryRoute,
    token: str,
    temp_dir: Path,
    *,
    isolate: bool,
) -> None:
    # pypa/build passes its environment to the pip or uv process that installs
    # build requirements, so no argument changes are needed.
    url = route.pypi_index_url
    _write_netrc(env, [url], token, temp_dir)
    env["PIP_INDEX_URL"] = url
    env["UV_DEFAULT_INDEX"] = url
    env["UV_INDEX_URL"] = url
    if isolate:
        env["PIP_CONFIG_FILE"] = os.devnull
        env["UV_NO_CONFIG"] = "1"


def _inject_twine_override(
    cmd: list[str],
    native_arg_start: int,
    env: dict[str, str],
    route: RegistryRoute,
    token: str,
    *,
    isolate: bool,
) -> None:
    del isolate
    _remove_value_options(
        cmd,
        {"--repository-url", "--username", "-u", "--password", "-p"},
        native_arg_start,
    )
    env["TWINE_REPOSITORY_URL"] = route.pypi_upload_url
    env["TWINE_USERNAME"] = "__token__"
    env["TWINE_PASSWORD"] = token
    env.setdefault("TWINE_NON_INTERACTIVE", "1")


def _inject_npm_override(
    cmd: list[str],
    native_arg_start: int,
    env: dict[str, str],
    route: RegistryRoute,
    token: str,
    *,
    isolate: bool,
    temp_dir: Path,
) -> None:
    registry_url = (
        route.npm_upload_registry_url
        if _npm_is_publish(cmd[native_arg_start:]) or _npm_is_mutation(cmd[native_arg_start:])
        else route.npm_registry_url
    )
    _replace_npm_registry_arg(cmd, native_arg_start, registry_url)
    env["NPM_CONFIG_REGISTRY"] = registry_url
    _inject_npm_auth(env, [registry_url], token)
    if isolate:
        user_npmrc = temp_dir / "user.npmrc"
        global_npmrc = temp_dir / "global.npmrc"
        user_npmrc.write_text("", encoding="utf-8")
        global_npmrc.write_text("", encoding="utf-8")
        env["NPM_CONFIG_USERCONFIG"] = str(user_npmrc)
        env["NPM_CONFIG_GLOBALCONFIG"] = str(global_npmrc)


# npm options that take a separate value and may precede the command, so the
# value is not mistaken for it (for example `npm --loglevel verbose publish`).
_NPM_VALUE_OPTIONS = frozenset(
    {
        "--registry",
        "--userconfig",
        "--globalconfig",
        "--prefix",
        "-C",
        "--cache",
        "--loglevel",
        "--location",
        "-L",
        "--workspace",
        "-w",
        "--otp",
        "--tag",
        "--access",
        "--scope",
        "--auth-type",
    }
)
# npm's own aliases for the commands classified here.
_NPM_COMMAND_ALIASES = {"dist-tags": "dist-tag"}


# npm's own `dist-tag` subcommand spellings. Any other subcommand lists tags
# (`npm dist-tag` and `npm dist-tag PACKAGE` list) or is a usage error.
_NPM_DIST_TAG_ADD = frozenset({"add", "a", "set", "s"})
_NPM_DIST_TAG_REMOVE = frozenset({"rm", "r", "del", "d", "remove"})
_NPM_DEFAULT_TAG = "latest"


def _npm_positionals(argv: list[str]) -> list[str]:
    positionals: list[str] = []
    skip_value = False
    for arg in argv:
        if skip_value:
            skip_value = False
        elif arg in _NPM_VALUE_OPTIONS:
            skip_value = True
        elif not arg.startswith("-"):
            positionals.append(arg)
    return positionals


def _npm_command(argv: list[str]) -> str | None:
    """Return the npm command: the first positional argument, alias-resolved.

    Later positionals are package specs or command arguments, so a package named
    `unpublish` in `npm install unpublish` never changes the classification.
    """
    positionals = _npm_positionals(argv)
    return _NPM_COMMAND_ALIASES.get(positionals[0], positionals[0]) if positionals else None


def _npm_dist_tag_action(argv: list[str]) -> Literal["add", "rm", "ls"] | None:
    """Classify `npm dist-tag` by its subcommand; reads are everything else."""
    if _npm_command(argv) != "dist-tag":
        return None
    positionals = _npm_positionals(argv)
    subcommand = positionals[1] if len(positionals) > 1 else None
    if subcommand in _NPM_DIST_TAG_ADD:
        return "add"
    if subcommand in _NPM_DIST_TAG_REMOVE:
        return "rm"
    return "ls"


def _npm_dist_tag_confirmation(argv: list[str], env: Mapping[str, str]) -> str | None:
    """Return the question to confirm before an `npm dist-tag` change, if any.

    Moving `latest` changes what `npm install PACKAGE` resolves to, and removing
    a tag breaks installs by it unless an upstream supplies the same tag, so both
    are confirmed; other additions are not. The registry refuses a change to a
    tag or version an upstream defines with its own message.
    """
    action = _npm_dist_tag_action(argv)
    if action not in {"add", "rm"}:
        return None
    positionals = _npm_positionals(argv)
    spec = positionals[2] if len(positionals) > 2 else None
    explicit = positionals[3] if len(positionals) > 3 else None
    if spec is None or (action == "rm" and explicit is None):
        # npm refuses the command with its usage message before any request.
        return None
    if action == "rm":
        return (
            f"Remove npm dist-tag '{explicit}' from {spec}? Installs by this tag stop "
            "working unless an upstream supplies the same tag."
        )
    # npm falls back to its `tag` setting, which defaults to `latest`.
    tag = (
        explicit
        or _arg_value(argv, "--tag")
        or env.get("NPM_CONFIG_TAG")
        or env.get("npm_config_tag")
        or _NPM_DEFAULT_TAG
    )
    if tag != _NPM_DEFAULT_TAG:
        return None
    # A spec without a version is not a usage error: npm (npm-package-arg) reads
    # `demo`, `@scope/demo`, and `demo@` as the range `*` and sends that as the
    # tag's version, so the change is confirmed like any other.
    target = spec if _npm_spec_has_version(spec) else f"{spec} (no version given; npm sends '*')"
    return (
        f"Point npm dist-tag 'latest' at {target}? "
        "`npm install` without a version or tag then installs it."
    )


def _npm_spec_has_version(spec: str) -> bool:
    """Whether `name@version` names a version; scoped names start with `@`."""
    name_and_version = spec[1:] if spec.startswith("@") else spec
    _, separator, version = name_and_version.partition("@")
    return bool(separator and version)


def _npm_is_publish(argv: list[str]) -> bool:
    return _npm_command(argv) == "publish"


def _npm_is_unpublish(argv: list[str]) -> bool:
    return _npm_command(argv) == "unpublish"


def _npm_is_mutation(argv: list[str]) -> bool:
    # `dist-tag ls` reads from the download registry with a read-only credential;
    # `dist-tag add`/`rm` change tags on the publish registry.
    command = _npm_command(argv)
    if command == "dist-tag":
        return _npm_dist_tag_action(argv) != "ls"
    return command in {"unpublish", "deprecate", "undeprecate"}


def _replace_npm_registry_arg(cmd: list[str], native_arg_start: int, registry_url: str) -> None:
    _remove_value_options(cmd, {"--registry"}, native_arg_start)
    if len(cmd) > native_arg_start and not cmd[native_arg_start].startswith("-"):
        insert_at = native_arg_start + 1
        cmd[insert_at:insert_at] = ["--registry", registry_url]
    else:
        cmd.append("--registry")
        cmd.append(registry_url)


def _remove_value_options(
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


def _inject_npm_auth(env: dict[str, str], urls: list[str], token: str) -> None:
    for url in urls:
        auth_key = npm_auth_token_key(url)
        env[f"NPM_CONFIG_{auth_key}"] = token


def _write_netrc(
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


def _write_maven_settings(
    *,
    urls: list[str],
    token: str,
    temp_dir: Path,
    argv: list[str],
    isolate: bool,
    route: RegistryRoute | None,
) -> Path:
    base_settings = None if isolate else _maven_settings_arg(argv)
    base_path = (
        Path(base_settings).expanduser() if base_settings else Path.home() / ".m2/settings.xml"
    )
    root = _load_maven_settings(base_path) if not isolate else ET.Element("settings")
    server_ids = set() if route is not None else _maven_server_ids_for_urls(urls, argv)
    if route is not None:
        server_ids.add(route.maven_server_id)
        _ensure_maven_profile(root, route, isolate=isolate)
    if not server_ids:
        server_ids.add(f"rvs-{secrets.token_hex(12)}")
    servers = _ensure_xml_child(root, "servers")
    if route is not None:
        for element in root.iter():
            if _local_name(element.tag) in {"repository", "pluginRepository", "snapshotRepository"}:
                if (
                    _child_text(element, "id") == route.maven_server_id
                    and _child_text(element, "url") not in urls
                ):
                    output.fatal(
                        "Native Maven settings reuse this run's Ravenstash repository ID "
                        "for another URL."
                    )
    for server_id in sorted(server_ids):
        _upsert_maven_server(servers, server_id, token)

    settings_path = temp_dir / "settings.xml"
    ET.ElementTree(root).write(settings_path, encoding="utf-8", xml_declaration=True)
    settings_path.chmod(0o600)
    return settings_path


def _load_maven_settings(path: Path) -> ET.Element:
    if path.exists() and path.is_file():
        try:
            return ET.parse(path).getroot()
        except ET.ParseError:
            pass
    return ET.Element("settings")


def _maven_server_ids_for_urls(urls: list[str], argv: list[str]) -> set[str]:
    ids: set[str] = set()
    for path in _candidate_config_files("mvn", argv, os.environ):
        if path.suffix.lower() != ".xml":
            continue
        try:
            root = ET.parse(path).getroot()
        except OSError, ET.ParseError:
            continue
        for element in root.iter():
            if _local_name(element.tag) not in {
                "repository",
                "pluginRepository",
                "snapshotRepository",
            }:
                continue
            repo_url = _child_text(element, "url")
            repo_id = _child_text(element, "id")
            if repo_id and repo_url and repo_url in urls:
                ids.add(repo_id)
    return ids


def _ensure_maven_profile(root: ET.Element, route: RegistryRoute, *, isolate: bool = False) -> None:
    server_id = route.maven_server_id
    mirrors = _ensure_xml_child(root, "mirrors")
    for existing in mirrors:
        if _child_text(existing, "id") == server_id:
            if _child_text(existing, "url") != route.maven_repo_url:
                output.fatal(
                    "Native Maven settings reuse this run's Ravenstash mirror ID for another URL."
                )
            continue
        pattern = _child_text(existing, "mirrorOf") or ""
        if pattern:
            output.warn(
                "Existing Maven mirrors are preserved for additional repositories; "
                "Central and the selected Ravenstash repository use the selected target."
            )
            # Exclude our repository and Central even from an existing exact match.
            remaining = [
                part.strip()
                for part in pattern.split(",")
                if part.strip() not in {"central", server_id}
            ]
            _ensure_xml_child(existing, "mirrorOf").text = ",".join(
                [*remaining, "!central", f"!{server_id}"]
            )
    mirror = _find_child_with_text(mirrors, "mirror", "id", server_id)
    if mirror is None:
        mirror = ET.SubElement(mirrors, "mirror")
        ET.SubElement(mirror, "id").text = server_id
    _ensure_xml_child(mirror, "url").text = route.maven_repo_url
    _ensure_xml_child(mirror, "mirrorOf").text = "*" if isolate else "central"

    profiles = _ensure_xml_child(root, "profiles")
    profile = _find_child_with_text(profiles, "profile", "id", "rvs")
    if profile is None:
        profile = ET.SubElement(profiles, "profile")
        ET.SubElement(profile, "id").text = "rvs"
    repositories = _ensure_xml_child(profile, "repositories")
    plugin_repositories = _ensure_xml_child(profile, "pluginRepositories")
    _upsert_maven_repository(repositories, "repository", route.maven_repo_url, server_id)
    _upsert_maven_repository(
        plugin_repositories, "pluginRepository", route.maven_repo_url, server_id
    )
    active_profiles = _ensure_xml_child(root, "activeProfiles")
    if _find_child_text(active_profiles, "activeProfile", "rvs") is None:
        ET.SubElement(active_profiles, "activeProfile").text = "rvs"


def _upsert_maven_repository(parent: ET.Element, tag: str, url: str, server_id: str) -> None:
    repo = _find_child_with_text(parent, tag, "id", server_id)
    if repo is None:
        repo = ET.SubElement(parent, tag)
        ET.SubElement(repo, "id").text = server_id
    url_element = _ensure_xml_child(repo, "url")
    url_element.text = url


def _upsert_maven_server(servers: ET.Element, server_id: str, token: str) -> None:
    server = _find_child_with_text(servers, "server", "id", server_id)
    if server is None:
        server = ET.SubElement(servers, "server")
        ET.SubElement(server, "id").text = server_id
    username = _ensure_xml_child(server, "username")
    password = _ensure_xml_child(server, "password")
    username.text = "__token__"
    password.text = token


def _replace_maven_settings_arg(
    cmd: list[str],
    settings_path: Path,
    native_arg_start: int,
) -> None:
    cleaned: list[str] = cmd[:native_arg_start]
    skip_next = False
    for arg in cmd[native_arg_start:]:
        if skip_next:
            skip_next = False
            continue
        if arg in {"-s", "--settings"}:
            skip_next = True
            continue
        if arg.startswith("--settings="):
            continue
        cleaned.append(arg)
    cleaned[native_arg_start:native_arg_start] = ["--settings", str(settings_path)]
    cmd[:] = cleaned


def _maven_settings_arg(argv: list[str]) -> str | None:
    return _arg_value(argv, "--settings", "-s")


def _maven_has_goal(argv: list[str], goal: str) -> bool:
    return any(arg == goal or arg.endswith(f":{goal}") for arg in argv if not arg.startswith("-"))


def _maven_is_upload(argv: list[str]) -> bool:
    return _maven_has_goal(argv, "deploy") or _maven_has_goal(argv, "deploy-file")


def _ensure_xml_child(parent: ET.Element, tag: str) -> ET.Element:
    for child in parent:
        if _local_name(child.tag) == tag:
            return child
    return ET.SubElement(parent, tag)


def _find_child_with_text(
    parent: ET.Element,
    child_tag: str,
    grandchild_tag: str,
    value: str,
) -> ET.Element | None:
    for child in parent:
        if _local_name(child.tag) != child_tag:
            continue
        if _child_text(child, grandchild_tag) == value:
            return child
    return None


def _find_child_text(parent: ET.Element, tag: str, value: str) -> str | None:
    for child in parent:
        if _local_name(child.tag) == tag and (child.text or "").strip() == value:
            return child.text
    return None


def _child_text(parent: ET.Element, tag: str) -> str | None:
    for child in parent:
        if _local_name(child.tag) == tag:
            text = (child.text or "").strip()
            return text or None
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
