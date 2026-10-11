"""Typer adapters for OCI-native passthrough commands."""

from typing import TYPE_CHECKING

import typer

from .. import output


if TYPE_CHECKING:
    from . import runner


PASSTHROUGH_CONTEXT = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
    "help_option_names": ["-h", "--help"],
}


def _options(
    profile: str | None,
    target: str | None,
    account: str | None,
    account_ref: str | None,
    yes: bool = False,
) -> runner.OciOptions:
    from . import runner

    return runner.OciOptions(
        yes=yes,
        profile=profile,
        target=target,
        account=account,
        account_ref=account_ref,
    )


def _run(
    tool: runner.OciTool,
    ctx: typer.Context,
    profile: str | None,
    target: str | None,
    account: str | None,
    account_ref: str | None,
    yes: bool = False,
) -> None:
    from . import runner

    if any(arg.split("=", 1)[0].startswith("--rvs-") for arg in ctx.args):
        output.fatal("Unknown rvs option. Use --rvs-target for the target.")
    runner.run(tool, list(ctx.args), _options(profile, target, account, account_ref, yes))


def docker(
    ctx: typer.Context,
    rvs_yes: bool = typer.Option(False, "--rvs-yes", help="Skip publishing confirmation."),
    rvs_profile: str | None = typer.Option(None, "--rvs-profile"),
    rvs_target: str | None = typer.Option(None, "--rvs-target"),
    rvs_account: str | None = typer.Option(None, "--rvs-account"),
    rvs_account_ref: str | None = typer.Option(None, "--rvs-account-ref", hidden=True),
) -> None:
    _run(
        "docker",
        ctx,
        rvs_profile,
        rvs_target,
        rvs_account,
        rvs_account_ref,
        rvs_yes,
    )


def helm(
    ctx: typer.Context,
    rvs_yes: bool = typer.Option(False, "--rvs-yes", help="Skip publishing confirmation."),
    rvs_profile: str | None = typer.Option(None, "--rvs-profile"),
    rvs_target: str | None = typer.Option(None, "--rvs-target"),
    rvs_account: str | None = typer.Option(None, "--rvs-account"),
    rvs_account_ref: str | None = typer.Option(None, "--rvs-account-ref", hidden=True),
) -> None:
    _run("helm", ctx, rvs_profile, rvs_target, rvs_account, rvs_account_ref, rvs_yes)


def oras(
    ctx: typer.Context,
    rvs_yes: bool = typer.Option(False, "--rvs-yes", help="Skip publishing confirmation."),
    rvs_profile: str | None = typer.Option(None, "--rvs-profile"),
    rvs_target: str | None = typer.Option(None, "--rvs-target"),
    rvs_account: str | None = typer.Option(None, "--rvs-account"),
    rvs_account_ref: str | None = typer.Option(None, "--rvs-account-ref", hidden=True),
) -> None:
    _run("oras", ctx, rvs_profile, rvs_target, rvs_account, rvs_account_ref, rvs_yes)
