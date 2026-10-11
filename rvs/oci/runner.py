"""Exact-target Docker, Helm, and ORAS launchers over one OCI endpoint."""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import typer

from .. import config as cfg_mod
from .. import output
from ..account.commands import acting_account_ref, resolve_account
from ..api import repository_mint_token_path
from ..artifacts.routing import native_path as opaque_native_path
from ..artifacts.targets import expected_repository_target, resolve_target
from ..artifacts.trash import NATIVE_MANIFEST_DELETE_NOTICE
from ..auth.token_format import STATIC_NATIVE_DURATION_SECONDS, validate_public_token
from ..client import ApiClient, ApiError
from ..publishing import confirm_publish, oci_artifacts
from ..runtime import tools
from ..subprocesses import child_environment
from ..tool_advisories import warn_if_old
from . import docker, helm, oras
from .registry import normalized_registry_host


OciTool = Literal["docker", "helm", "oras"]
OciFormat = Literal["oci"]
PackageOperation = Literal["read", "publish", "delete"]
_OCI_COMPONENT = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")
_UNIQUE_ID = re.compile(r"^[23456789abcdefghijkmnpqrstuvwxyz]{8}$")


@dataclass(frozen=True)
class OciOptions:
    yes: bool = False
    profile: str | None = None
    target: str | None = None
    account: str | None = None
    account_ref: str | None = None


@dataclass(frozen=True)
class OciRoute:
    kind: OciFormat
    registry_url: str
    registry_host: str
    native_root: str
    accepted_roots: frozenset[str]
    package_token: str = field(repr=False)
    account: cfg_mod.AccountContext | None = None
    repository: str = ""


def _native_route_parts(native_path: str) -> tuple[str, str] | None:
    parts = native_path.strip("/").split("/")
    if len(parts) != 2:
        return None
    namespace, repository = parts
    stable = repository.startswith("ar_")
    if stable:
        if not (namespace == "in" and _UNIQUE_ID.fullmatch(repository.removeprefix("ar_"))):
            return None
    elif (
        not _OCI_COMPONENT.fullmatch(namespace)
        or not _OCI_COMPONENT.fullmatch(repository)
        or namespace.startswith(("in_", "gn_", "ar_"))
        or repository.startswith(("in_", "gn_", "ar_"))
    ):
        return None
    return namespace, repository


def _stable_oci_root(repository_ref: object) -> str:
    if not isinstance(repository_ref, str):
        output.fatal("Invalid OCI capability response: repository permanent ID is missing.")
    if not repository_ref.startswith("ar_"):
        output.fatal("Invalid OCI capability response: repository permanent ID is invalid.")
    if not _UNIQUE_ID.fullmatch(repository_ref.removeprefix("ar_")):
        output.fatal("Invalid OCI capability response: repository permanent ID is invalid.")
    return f"in/{repository_ref}"


def _friendly_oci_root(namespace_name: object, repository_name: object) -> str:
    if not isinstance(namespace_name, str) or not isinstance(repository_name, str):
        output.fatal("Invalid OCI capability response: current names are missing.")
    # Display spelling preserves case; OCI paths must be lowercase.
    root = f"{namespace_name.lower()}/{repository_name.lower()}"
    if _native_route_parts(root) is None:
        output.fatal("Invalid OCI capability response: current names are invalid.")
    return root


def _registry_url(profile: cfg_mod.ProfileConfig) -> tuple[str, str]:
    raw = profile.native_registries.oci_registry_base_url
    try:
        value = cfg_mod.validate_service_url(raw, label="OCI registry URL")
        parsed = urlsplit(value)
    except ValueError as exc:
        output.fatal(str(exc))
    if parsed.path not in {"", "/"}:
        output.fatal("OCI registry URL must not contain a path.")
    return value, normalized_registry_host(value)


def resolve_route(
    tool: OciTool,
    options: OciOptions,
    operations: tuple[PackageOperation, ...] = ("read",),
) -> OciRoute:
    kind: OciFormat = "oci"
    config = cfg_mod.load()
    profile_name = options.profile or cfg_mod.current_profile_name(config)
    account_ref = options.account_ref
    if account_ref is None and options.account is not None:
        account_ref = str(resolve_account(options.account, profile_name)["ref"])
    account_ref = account_ref or acting_account_ref(profile_name)
    selected = cfg_mod.selected_artifact_target(profile_name, account_ref)
    repo_ref = options.target
    if repo_ref is None and selected is not None:
        if selected.target_type != "repository":
            output.fatal("OCI commands require a private repository target.")
        repo_ref = selected.stable_selector
    if not repo_ref:
        output.fatal(f"No {kind} repository selected. Pass --rvs-target or run `rvs art select`.")
    _, account, target = resolve_target(
        repo_ref,
        profile=profile_name,
        account_ref=account_ref,
        kind=kind,
    )
    if target.target_type != "repository" or target.repository_ref is None:
        output.fatal("OCI commands require a private repository target.")
    try:
        client = ApiClient.from_profile(profile_name)
        credential_body: dict[str, object] = {
            "formats": [kind],
            "operations": list(operations),
            "duration_seconds": STATIC_NATIVE_DURATION_SECONDS,
            "expected_target": expected_repository_target(target),
        }
        credential = client.issue_native(
            repository_mint_token_path(target.repository_ref),
            credential_body,
        ).json()
        token = validate_public_token(credential["access_token"], native=True)
        # The native path is opaque; the minted target proves the repository.
        response_root = opaque_native_path(credential["native_path"])
        minted_target = credential["target"]
        if not isinstance(minted_target, dict):
            raise TypeError("target is not an object")
    except ApiError as exc:
        output.fatal(str(exc))
    except (KeyError, TypeError, ValueError) as exc:
        output.fatal(f"Invalid OCI capability response: {exc}")
    if minted_target.get("repository_ref") != target.repository_ref:
        output.fatal("Invalid OCI capability response: the credential is for another repository.")
    # Minting may refresh the session and its registry discovery; read the profile
    # afterwards so this invocation uses the current OCI host.
    registry_url, registry_host = _registry_url(cfg_mod.load().active_profile(profile_name))
    friendly_root = _friendly_oci_root(
        minted_target.get("namespace_name"),
        minted_target.get("repository_name"),
    )
    stable_root = _stable_oci_root(target.repository_ref)
    return OciRoute(
        kind=kind,
        registry_url=registry_url,
        registry_host=registry_host,
        native_root=f"{registry_host}/{response_root}",
        accepted_roots=frozenset((response_root, friendly_root, stable_root)),
        package_token=token,
        account=account,
        repository=target.display_selector,
    )


def _ravenstash_routes(
    argv: list[str], registry_host: str, accepted_roots: frozenset[str] = frozenset()
) -> set[str]:
    """Return the Ravenstash repository roots referenced on the OCI host.

    A reference below one of *accepted_roots* reports that root, whatever its
    depth; any other reference reports its first two path components when they
    form a repository root.
    """
    marker = f"{registry_host}/"
    routes: set[str] = set()
    for argument in argv:
        start = 0
        while (index := argument.find(marker, start)) >= 0:
            suffix = argument[index + len(marker) :]
            candidate = re.split(r"[\s,;\]\[(){}]", suffix, maxsplit=1)[0]
            accepted = next(
                (
                    root
                    for root in accepted_roots
                    if candidate == root or candidate.startswith(f"{root}/")
                ),
                None,
            )
            parts = candidate.split("/")
            if accepted is not None:
                routes.add(accepted)
            elif len(parts) >= 2 and _native_route_parts("/".join(parts[:2])) is not None:
                routes.add("/".join(parts[:2]))
            start = index + len(marker)
    return routes


def _assert_exact_targets(argv: list[str], route: OciRoute) -> None:
    observed = _ravenstash_routes(argv, route.registry_host, route.accepted_roots)
    if any(value not in route.accepted_roots for value in observed):
        output.fatal(
            "This invocation references another Ravenstash logical repository. "
            "One native invocation may use only the exact --rvs-target value."
        )


def _normalize_friendly_targets(argv: list[str], route: OciRoute) -> list[str]:
    """Normalize only selected namespace/repository spelling, not OCI paths or tags."""
    pattern = re.compile(
        r"(?<![A-Za-z0-9._-])"
        + re.escape(f"{route.registry_host}/")
        + r"([A-Za-z][A-Za-z0-9-]*)/([A-Za-z][A-Za-z0-9-]*)(?=/|:|@|$)"
    )

    def normalize(match: re.Match[str]) -> str:
        root = f"{match[1].lower()}/{match[2].lower()}"
        if root not in route.accepted_roots:
            output.fatal(
                "This invocation references another Ravenstash logical repository. "
                "One native invocation may use only the exact --rvs-target value."
            )
        return f"{route.registry_host}/{root}"

    return [pattern.sub(normalize, argument) for argument in argv]


def _write_broker(temp_dir: Path, route: OciRoute) -> Path:
    path = temp_dir / "oci-credential.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "server": route.registry_host,
                "username": "__token__",
                "secret": route.package_token,
            },
            handle,
            separators=(",", ":"),
        )
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _registry_config_source(tool: OciTool) -> Path:
    if tool == "helm":
        configured = os.environ.get("HELM_REGISTRY_CONFIG")
        if configured:
            return Path(configured).expanduser()
        config_home = os.environ.get("XDG_CONFIG_HOME")
        root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
        return root / "helm" / "registry" / "config.json"
    docker_config = os.environ.get("DOCKER_CONFIG")
    root = Path(docker_config).expanduser() if docker_config else Path.home() / ".docker"
    return root / "config.json"


def _native_registry_config(tool: OciTool, source: Path | None = None) -> dict[str, object]:
    source = source or _registry_config_source(tool)
    try:
        if not source.is_file():
            return {}
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        output.fatal(f"Cannot read native {tool} registry config {source}: {exc}")
    if not isinstance(value, dict):
        output.fatal(f"Native {tool} registry config {source} must be an object.")
    return value


def _write_registry_config(
    temp_dir: Path, route: OciRoute, tool: OciTool, source: Path | None = None
) -> Path:
    config = _native_registry_config(tool, source)
    auths = config.get("auths")
    if isinstance(auths, dict):
        config["auths"] = {
            key: value
            for key, value in auths.items()
            if not isinstance(key, str) or normalized_registry_host(key) != route.registry_host
        }
    helpers = config.get("credHelpers")
    if not isinstance(helpers, dict):
        helpers = {}
    else:
        helpers = {
            key: value
            for key, value in helpers.items()
            if not isinstance(key, str) or normalized_registry_host(key) != route.registry_host
        }
    helpers[route.registry_host] = "rvs"
    config["credHelpers"] = helpers
    path = temp_dir / "config.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(config, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _executable(tool: OciTool) -> str:
    path = tools.require(tool, install_kind="system")
    try:
        if Path(path).resolve() == Path(os.environ.get("RVS_EXECUTABLE", "")).resolve():
            output.fatal(f"Refusing recursive {tool} executable resolution.")
    except OSError:
        pass
    return path


def _operations_for(
    tool: OciTool,
    argv: list[str],
) -> tuple[PackageOperation, ...]:
    if tool == "docker":
        return ("read", "publish") if docker.parse(argv).publishing else ("read",)
    if tool == "helm" and helm.parse(argv).publishing:
        return ("read", "publish")
    if tool == "oras" and oras.parse(argv).deleting:
        return ("read", "delete")
    if tool == "oras" and oras.parse(argv).publishing:
        return ("read", "publish")
    return ("read",)


def run(tool: OciTool, argv: list[str], options: OciOptions) -> None:
    if tool == "oras" and oras.local_only(argv):
        _run_process([_executable(tool), *argv], child_environment({}))
        return
    invocation = docker.parse(argv) if tool == "docker" else None
    helm_invocation = helm.parse(argv) if tool == "helm" else None
    route = resolve_route(tool, options, _operations_for(tool, argv))
    tagging = None
    if invocation is not None:
        tagging = docker.expand(invocation, route.native_root)
        argv = invocation.argv
    if helm_invocation is not None:
        argv = helm.expand(helm_invocation, route.native_root)
    # A tag source is local and may legitimately be named for another repository.
    if invocation is not None and invocation.command[-1:] == ("tag",) and invocation.operands:
        index = invocation.operands[-1]
        argv[index] = _normalize_friendly_targets([argv[index]], route)[0]
        _assert_exact_targets([argv[index]], route)
    else:
        argv = _normalize_friendly_targets(argv, route)
        _assert_exact_targets(argv, route)
    publishing = (
        (invocation is not None and invocation.publishing)
        or (helm_invocation is not None and helm_invocation.publishing)
        or (tool == "oras" and oras.parse(argv).publishing)
    )
    if publishing:
        if route.account is None:
            output.fatal("Cannot identify the publishing account.")
        publish_argv = argv
        if helm_invocation is not None and helm_invocation.operands:
            operands = helm_invocation.operands
            destination = argv[operands[1]] if len(operands) == 2 else argv[-1]
            publish_argv = ["push", argv[operands[0]], destination]
        confirm_publish(
            route.repository,
            route.account,
            oci_artifacts(tool, publish_argv, route.registry_host),
            yes=options.yes,
        )
    with tempfile.TemporaryDirectory(prefix="rvs-oci-") as temporary:
        temp_dir = Path(temporary)
        broker = _write_broker(temp_dir, route)
        source = _registry_config_source(tool)
        if invocation is not None and invocation.config_dir is not None:
            source = invocation.config_dir / "config.json"
        if helm_invocation is not None and helm_invocation.config is not None:
            source = helm_invocation.config
        registry_config = _write_registry_config(temp_dir, route, tool, source)
        if tool == "oras":
            try:
                argv, sources = oras.registry_configs(argv)
            except ValueError as exc:
                output.fatal(str(exc))
            injected: list[str] = []
            for flag, selected in sources.items():
                overlay_dir = temp_dir / flag.removeprefix("--")
                overlay_dir.mkdir(mode=0o700)
                overlay = _write_registry_config(
                    overlay_dir, route, tool, Path(selected).expanduser() if selected else source
                )
                injected.extend((flag, str(overlay)))
            end_options = argv.index("--") if "--" in argv else len(argv)
            argv[end_options:end_options] = injected
        if tool == "docker":
            # Context TLS material and locally installed CLI plugins are directory-backed.
            for name in ("contexts", "cli-plugins"):
                original = source.parent / name
                if original.is_dir():
                    destination = temp_dir / name
                    try:
                        destination.symlink_to(original.absolute(), target_is_directory=True)
                    except OSError:
                        # Windows commonly denies unprivileged directory symlinks.
                        # These trees contain public context metadata/plugins; secrets
                        # remain in the generated credential overlay.
                        shutil.copytree(original, destination)
        env = child_environment(
            {
                "DOCKER_CONFIG": str(temp_dir),
                "HELM_REGISTRY_CONFIG": str(registry_config),
                "RVS_OCI_CREDENTIAL_FILE": str(broker),
            }
        )
        if tool == "docker":
            env.setdefault("BUILDX_CONFIG", str((source.parent / "buildx").absolute()))
        executable = _executable(tool)
        warn_if_old(tool, [executable])
        if tagging is not None and invocation is not None:
            source_image, destination = tagging
            docker.check_tag(executable, invocation, env, source_image, destination)
            # Keep Docker's named-image tagging semantics, including multi-platform
            # images in the containerd store; a platform image ID may narrow the graph.
            _run_process(
                [executable, *invocation.globals, "image", "tag", source_image, destination], env
            )
            typer.echo(f"Local tag: {destination} (retained if push fails).", err=True)
            if invocation.command[-1] == "tag":
                return
        if tool == "oras" and oras.parse(argv).command == ("manifest", "delete"):
            # ORAS confirms and reports the delete itself, and exits 0 when its
            # prompt is declined, so the trash is explained before it runs.
            typer.echo(NATIVE_MANIFEST_DELETE_NOTICE, err=True)
        _run_process([executable, *argv], env)


def _run_process(argv: list[str], env: dict[str, str]) -> None:
    process = subprocess.Popen(argv, env=env)
    forwarded_signals = tuple(
        candidate
        for candidate in (
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGHUP", None),
        )
        if candidate is not None
    )
    previous_handlers = {signum: signal.getsignal(signum) for signum in forwarded_signals}

    def _forward(signum: int, _frame) -> None:
        if process.poll() is None:
            process.send_signal(signum)

    try:
        for signum in forwarded_signals:
            signal.signal(signum, _forward)
        returncode = process.wait()
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
    if returncode:
        raise typer.Exit(128 - returncode if returncode < 0 else returncode)


__all__ = ["OciFormat", "OciOptions", "OciTool", "resolve_route", "run"]
