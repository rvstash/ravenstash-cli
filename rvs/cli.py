"""Root command wiring for the Ravenstash developer CLI."""

from pathlib import Path  # noqa: TC003 - Typer resolves command annotations at runtime.

import typer
from typer.core import TyperGroup

from .account.commands import app as account_app
from .artifacts.commands import app as artifacts_app
from .auth.commands import app as auth_app
from .auth.commands import profile_app
from .identity import PACKAGE_NAME, PRODUCT_NAME
from .installations import Installation, write_receipt
from .native import commands as native_commands
from .oci import commands as oci_commands
from .runtime.commands import app as runtime_app
from .status import status
from .update import update


PYTHON_TOOLS_PANEL = "Python tools"
NODE_TOOLS_PANEL = "Node.js tools"
JVM_TOOLS_PANEL = "JVM tools"
CONTAINER_TOOLS_PANEL = "Container tools"
HELM_TOOLS_PANEL = "Helm tools"
ARTIFACT_MANAGEMENT_PANEL = "Artifact management"
ACCOUNT_CONFIGURATION_PANEL = "Account and configuration"
SETUP_MAINTENANCE_PANEL = "Setup and maintenance"

_HELP_PANEL_ORDER = {
    PYTHON_TOOLS_PANEL: 0,
    NODE_TOOLS_PANEL: 1,
    JVM_TOOLS_PANEL: 2,
    CONTAINER_TOOLS_PANEL: 3,
    HELM_TOOLS_PANEL: 4,
    ARTIFACT_MANAGEMENT_PANEL: 5,
    ACCOUNT_CONFIGURATION_PANEL: 6,
    SETUP_MAINTENANCE_PANEL: 7,
}


class CategorizedHelpGroup(TyperGroup):
    """Order root help panels and commands without changing command paths."""

    def list_commands(self, ctx: typer.Context) -> list[str]:
        command_names = super().list_commands(ctx)

        def help_order(command_name: str) -> tuple[int, str]:
            command = self.get_command(ctx, command_name)
            panel = getattr(command, "rich_help_panel", None)
            panel_order = (
                _HELP_PANEL_ORDER.get(panel, len(_HELP_PANEL_ORDER))
                if isinstance(panel, str)
                else len(_HELP_PANEL_ORDER)
            )
            return (panel_order, command_name.casefold())

        return sorted(command_names, key=help_order)


app = typer.Typer(
    name="rvs",
    cls=CategorizedHelpGroup,
    help="Ravenstash developer CLI.",
    no_args_is_help=False,
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

app.add_typer(auth_app, name="auth", rich_help_panel=ACCOUNT_CONFIGURATION_PANEL)
app.add_typer(profile_app, name="profile", rich_help_panel=ACCOUNT_CONFIGURATION_PANEL)
app.add_typer(account_app, name="account", rich_help_panel=ACCOUNT_CONFIGURATION_PANEL)
app.command("status", rich_help_panel=ACCOUNT_CONFIGURATION_PANEL)(status)
app.add_typer(runtime_app, name="runtime", rich_help_panel=SETUP_MAINTENANCE_PANEL)
app.add_typer(artifacts_app, name="art", rich_help_panel=ARTIFACT_MANAGEMENT_PANEL)
app.command("update", rich_help_panel=SETUP_MAINTENANCE_PANEL)(update)


@app.command("_record-install", hidden=True)
def _record_install(
    output_path: Path = typer.Option(..., "--output"),
    scope: str = typer.Option(..., "--scope"),
    version: str = typer.Option(..., "--version"),
    channel: str = typer.Option(..., "--channel"),
    target: str = typer.Option(..., "--target"),
    install_root: Path = typer.Option(..., "--install-root"),
    bin_directory: Path = typer.Option(..., "--bin-directory"),
) -> None:
    """Write the validated, non-secret receipt used by the portable updater."""

    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as package_version

    from .installations import platform_target

    try:
        running_version = package_version(PACKAGE_NAME)
    except PackageNotFoundError:
        running_version = "dev"
    if version != running_version:
        raise typer.BadParameter("receipt version does not match the running rvs executable")
    if target != platform_target():
        raise typer.BadParameter("receipt target does not match the running rvs executable")
    write_receipt(
        output_path,
        Installation(
            schema=1,
            method="portable",
            scope=scope,
            version=version,
            channel=channel,
            target=target,
            install_root=str(install_root.resolve()),
            bin_directory=str(bin_directory.resolve()),
        ),
    )


app.command(
    "pip",
    context_settings=native_commands.PASSTHROUGH_CONTEXT,
    help="Run pip with temporary access to a Ravenstash PyPI repository.",
    rich_help_panel=PYTHON_TOOLS_PANEL,
)(native_commands.pip)
app.command(
    "uv",
    context_settings=native_commands.PASSTHROUGH_CONTEXT,
    help="Run uv with temporary access to a Ravenstash PyPI repository.",
    rich_help_panel=PYTHON_TOOLS_PANEL,
)(native_commands.uv)
app.command(
    "twine",
    context_settings=native_commands.PASSTHROUGH_CONTEXT,
    help="Run Twine with temporary access to publish to Ravenstash.",
    rich_help_panel=PYTHON_TOOLS_PANEL,
)(native_commands.twine)
app.command(
    "python",
    context_settings=native_commands.PASSTHROUGH_CONTEXT,
    help="Run `python -m build` with temporary access to a Ravenstash PyPI repository.",
    rich_help_panel=PYTHON_TOOLS_PANEL,
)(native_commands.python)
app.command(
    "npm",
    context_settings=native_commands.PASSTHROUGH_CONTEXT,
    help="Run npm with temporary access to a Ravenstash npm repository.",
    rich_help_panel=NODE_TOOLS_PANEL,
)(native_commands.npm)
app.command(
    "mvn",
    context_settings=native_commands.PASSTHROUGH_CONTEXT,
    help="Run Maven with temporary access to a Ravenstash Maven repository.",
    rich_help_panel=JVM_TOOLS_PANEL,
)(native_commands.mvn)
app.command(
    "docker",
    context_settings=oci_commands.PASSTHROUGH_CONTEXT,
    help="Run Docker with temporary access; push, pull, and tag accept private image paths.",
    rich_help_panel=CONTAINER_TOOLS_PANEL,
)(oci_commands.docker)
app.command(
    "helm",
    context_settings=oci_commands.PASSTHROUGH_CONTEXT,
    help="Run Helm with temporary access and selected-repository chart paths.",
    rich_help_panel=HELM_TOOLS_PANEL,
)(oci_commands.helm)
app.command(
    "oras",
    context_settings=oci_commands.PASSTHROUGH_CONTEXT,
    help="Run ORAS with temporary access to an OCI-enabled repository.",
    rich_help_panel=CONTAINER_TOOLS_PANEL,
)(oci_commands.oras)


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import PackageNotFoundError, version

        try:
            v = version(PACKAGE_NAME)
        except PackageNotFoundError:
            v = "dev"
        typer.echo(f"{PRODUCT_NAME} {v}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Print output from rvs as JSON.",
    ),
) -> None:
    """Ravenstash developer CLI."""
    from . import output

    output.set_json(json_output)
    if ctx.invoked_subcommand is None and not version:
        typer.echo(ctx.get_help())
        raise typer.Exit()
