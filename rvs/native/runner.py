"""Credential-aware native package-manager launchers.

These wrappers never persist Ravenstash credentials into package-manager config
files. They require a selected target, preserve additional native sources, and inject
short-lived credentials for the child process without editing lockfiles. Each
tool's behavior lives in its adapter (see `adapters.py`).
"""

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import typer

from .. import config as cfg_mod
from .. import output
from ..account.commands import acting_account_ref, resolve_account
from ..artifacts.targets import PackageFormat, registry_context
from ..publishing import PublishItem, confirm_change, confirm_context
from ..subprocesses import child_environment
from ..tool_advisories import warn_if_old
from .adapters import NativeTool, adapter_for
from .common import PackageOperation, RegistryRoute


ConfigPolicy = Literal["respect", "override", "isolate"]

POLICIES: tuple[ConfigPolicy, ...] = ("respect", "override", "isolate")


@dataclass(frozen=True)
class NativeOptions:
    yes: bool = False
    profile: str | None = None
    target: str | None = None
    account: str | None = None
    account_ref: str | None = None
    native_config: ConfigPolicy = "respect"


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
    adapter = adapter_for(tool)
    with tempfile.TemporaryDirectory(prefix="rvs-native-") as temp_dir:
        plan = _build_plan(tool, argv, options, Path(temp_dir))
        targeted = bool(plan.native_command)
        if targeted:
            warn_if_old(adapter.advisory, list(plan.native_command))
            adapter.before_run(argv)
        result = subprocess.run(plan.cmd, env=plan.env, check=False)
        code = getattr(result, "returncode", 0)
        if code:
            if targeted:
                adapter.after_failure(argv)
            raise typer.Exit(code)


def _build_plan(
    tool: NativeTool,
    argv: list[str],
    options: NativeOptions,
    temp_dir: Path,
) -> ExecutionPlan:
    adapter = adapter_for(tool)
    env = child_environment()
    native_args = adapter.native_args(argv)
    command_prefix = adapter.command_prefix()
    cmd = [*command_prefix, *native_args]
    native_arg_start = len(command_prefix)
    if not adapter.needs_target(native_args):
        return ExecutionPlan(cmd=cmd, env=env, native_command=())
    account_ref = _selected_account_ref(options)
    saved = cfg_mod.selected_artifact_target(options.profile, account_ref)
    if not options.target and saved is None:
        output.fatal(
            "No Ravenstash target is selected. Run `rvs art select` or pass --rvs-target. "
            "Use the native tool directly for its default registries."
        )
    adapter.preflight(argv, command_prefix)
    # Never infer a target from native defaults or run an unconfigured client.
    route = _resolve_route(
        adapter.kind,
        options,
        adapter.operations(argv),
        artifacts=adapter.publish_items(argv) if adapter.is_publishing(argv) else None,
        confirmation=adapter.confirmation(argv, env),
    )
    adapter.inject(
        cmd,
        native_arg_start,
        env,
        route,
        temp_dir,
        isolate=options.native_config == "isolate",
    )
    from .sources import warn_additional_sources

    warn_additional_sources(adapter, argv, cmd[native_arg_start:], env, route)
    return ExecutionPlan(cmd=cmd, env=env, native_command=tuple(command_prefix))


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


def _operations_for(tool: NativeTool, argv: list[str]) -> tuple[PackageOperation, ...]:
    return adapter_for(tool).operations(argv)


def _is_publishing(tool: NativeTool, argv: list[str]) -> bool:
    return adapter_for(tool).is_publishing(argv)


def _inject_override(
    tool: NativeTool,
    cmd: list[str],
    native_arg_start: int,
    env: dict[str, str],
    route: RegistryRoute,
    temp_dir: Path,
    *,
    isolate: bool,
) -> None:
    adapter_for(tool).inject(cmd, native_arg_start, env, route, temp_dir, isolate=isolate)
