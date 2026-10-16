"""Top-level native package-manager passthrough commands."""

from typing import TYPE_CHECKING

import typer


if TYPE_CHECKING:
    from . import runner


_ACCOUNT_HELP = "Typed account handle (user:USERNAME or org:HANDLE) or a bare handle."


PASSTHROUGH_CONTEXT = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
    # Maven -D properties are parsed as combined short options by Click. Keeping
    # -h as an eager wrapper flag makes a value such as `-Dartifact=org.apache...`
    # print RVS help and exit successfully instead of launching Maven.
    "help_option_names": ["--help"],
}


def _options(
    profile: str | None,
    target: str | None,
    account: str | None,
    account_ref: str | None,
    native_config: str,
    yes: bool = False,
) -> runner.NativeOptions:
    from . import runner

    return runner.NativeOptions(
        yes=yes,
        profile=profile,
        target=target,
        account=account,
        account_ref=account_ref,
        native_config=runner.normalize_policy(native_config),
    )


def _run(tool: runner.NativeTool, ctx: typer.Context, options: runner.NativeOptions) -> None:
    from . import runner

    runner.run(tool, list(ctx.args), options)


def _native_config_option() -> str:
    return "respect"


def pip(
    ctx: typer.Context,
    rvs_profile: str | None = typer.Option(None, "--rvs-profile", help="Local CLI profile."),
    rvs_target: str | None = typer.Option(
        None, "--rvs-target", help="Repository or mirror for this command."
    ),
    rvs_account: str | None = typer.Option(None, "--rvs-account", help=_ACCOUNT_HELP),
    rvs_account_ref: str | None = typer.Option(
        None,
        "--rvs-account-ref",
        help="Typed account reference for advanced use.",
        hidden=True,
    ),
    rvs_native_config: str = typer.Option(
        _native_config_option(),
        "--rvs-native-config",
        help="Native config policy: respect, override, or isolate.",
    ),
) -> None:
    _run(
        "pip",
        ctx,
        _options(rvs_profile, rvs_target, rvs_account, rvs_account_ref, rvs_native_config),
    )


def uv(
    ctx: typer.Context,
    rvs_yes: bool = typer.Option(False, "--rvs-yes", help="Skip publishing confirmation."),
    rvs_profile: str | None = typer.Option(None, "--rvs-profile", help="Local CLI profile."),
    rvs_target: str | None = typer.Option(
        None, "--rvs-target", help="Repository or mirror for this command."
    ),
    rvs_account: str | None = typer.Option(None, "--rvs-account", help=_ACCOUNT_HELP),
    rvs_account_ref: str | None = typer.Option(
        None,
        "--rvs-account-ref",
        help="Typed account reference for advanced use.",
        hidden=True,
    ),
    rvs_native_config: str = typer.Option(
        _native_config_option(),
        "--rvs-native-config",
        help="Native config policy: respect, override, or isolate.",
    ),
) -> None:
    _run(
        "uv",
        ctx,
        _options(rvs_profile, rvs_target, rvs_account, rvs_account_ref, rvs_native_config, rvs_yes),
    )


def twine(
    ctx: typer.Context,
    rvs_yes: bool = typer.Option(False, "--rvs-yes", help="Skip publishing confirmation."),
    rvs_profile: str | None = typer.Option(None, "--rvs-profile", help="Local CLI profile."),
    rvs_target: str | None = typer.Option(
        None, "--rvs-target", help="Repository or mirror for this command."
    ),
    rvs_account: str | None = typer.Option(None, "--rvs-account", help=_ACCOUNT_HELP),
    rvs_account_ref: str | None = typer.Option(
        None,
        "--rvs-account-ref",
        help="Typed account reference for advanced use.",
        hidden=True,
    ),
    rvs_native_config: str = typer.Option(
        _native_config_option(),
        "--rvs-native-config",
        help="Native config policy: respect, override, or isolate.",
    ),
) -> None:
    _run(
        "twine",
        ctx,
        _options(rvs_profile, rvs_target, rvs_account, rvs_account_ref, rvs_native_config, rvs_yes),
    )


def python(
    ctx: typer.Context,
    rvs_profile: str | None = typer.Option(None, "--rvs-profile", help="Local CLI profile."),
    rvs_target: str | None = typer.Option(
        None, "--rvs-target", help="Repository or mirror for this command."
    ),
    rvs_account: str | None = typer.Option(None, "--rvs-account", help=_ACCOUNT_HELP),
    rvs_account_ref: str | None = typer.Option(
        None,
        "--rvs-account-ref",
        help="Typed account reference for advanced use.",
        hidden=True,
    ),
    rvs_native_config: str = typer.Option(
        _native_config_option(),
        "--rvs-native-config",
        help="Native config policy: respect, override, or isolate.",
    ),
) -> None:
    _run(
        "python",
        ctx,
        _options(rvs_profile, rvs_target, rvs_account, rvs_account_ref, rvs_native_config),
    )


def npm(
    ctx: typer.Context,
    rvs_yes: bool = typer.Option(False, "--rvs-yes", help="Skip publishing confirmation."),
    rvs_profile: str | None = typer.Option(None, "--rvs-profile", help="Local CLI profile."),
    rvs_target: str | None = typer.Option(
        None, "--rvs-target", help="Repository or mirror for this command."
    ),
    rvs_account: str | None = typer.Option(None, "--rvs-account", help=_ACCOUNT_HELP),
    rvs_account_ref: str | None = typer.Option(
        None,
        "--rvs-account-ref",
        help="Typed account reference for advanced use.",
        hidden=True,
    ),
    rvs_native_config: str = typer.Option(
        _native_config_option(),
        "--rvs-native-config",
        help="Native config policy: respect, override, or isolate.",
    ),
) -> None:
    _run(
        "npm",
        ctx,
        _options(rvs_profile, rvs_target, rvs_account, rvs_account_ref, rvs_native_config, rvs_yes),
    )


def pnpm(
    ctx: typer.Context,
    rvs_yes: bool = typer.Option(False, "--rvs-yes", help="Skip publishing confirmation."),
    rvs_profile: str | None = typer.Option(None, "--rvs-profile", help="Local CLI profile."),
    rvs_target: str | None = typer.Option(
        None, "--rvs-target", help="Repository or mirror for this command."
    ),
    rvs_account: str | None = typer.Option(None, "--rvs-account", help=_ACCOUNT_HELP),
    rvs_account_ref: str | None = typer.Option(
        None,
        "--rvs-account-ref",
        help="Typed account reference for advanced use.",
        hidden=True,
    ),
    rvs_native_config: str = typer.Option(
        _native_config_option(),
        "--rvs-native-config",
        help="Native config policy: respect, override, or isolate.",
    ),
) -> None:
    _run(
        "pnpm",
        ctx,
        _options(rvs_profile, rvs_target, rvs_account, rvs_account_ref, rvs_native_config, rvs_yes),
    )


def mvn(
    ctx: typer.Context,
    rvs_yes: bool = typer.Option(False, "--rvs-yes", help="Skip publishing confirmation."),
    rvs_profile: str | None = typer.Option(None, "--rvs-profile", help="Local CLI profile."),
    rvs_target: str | None = typer.Option(
        None, "--rvs-target", help="Repository or mirror for this command."
    ),
    rvs_account: str | None = typer.Option(None, "--rvs-account", help=_ACCOUNT_HELP),
    rvs_account_ref: str | None = typer.Option(
        None,
        "--rvs-account-ref",
        help="Typed account reference for advanced use.",
        hidden=True,
    ),
    rvs_native_config: str = typer.Option(
        _native_config_option(),
        "--rvs-native-config",
        help="Native config policy: respect, override, or isolate.",
    ),
) -> None:
    _run(
        "mvn",
        ctx,
        _options(rvs_profile, rvs_target, rvs_account, rvs_account_ref, rvs_native_config, rvs_yes),
    )
