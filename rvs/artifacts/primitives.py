"""Credential-free native addresses, OCI references and setup templates."""

import contextlib
import json
import sys
from typing import Literal

import click
import httpx2 as httpx
import typer

from .. import output
from ..client import ApiError
from .discovery import discover
from .native_config import render


native_app = typer.Typer(
    help="Print setup instructions for native package tools.", no_args_is_help=True
)


def endpoint(
    target: str | None = typer.Option(None, "--target", "-t"),
    kind: str | None = typer.Option(None, "--format", "-f"),
    access: Literal["read", "publish"] | None = typer.Option(None, "--access"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Print native read and publish endpoints without credentials."""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            found = discover(target, profile, account, kind)
            selected = found.select_format(kind)
            selected_access = access or "read"
            address = found.endpoint(selected, selected_access)
        result = {
            "target": found.target.display_selector,
            "format": selected,
            "access": selected_access,
            "endpoint": address,
        }
        publish_address = None
        if access is None and found.target.target_type == "repository":
            publish_address = found.endpoint(selected, "publish")
            result["publish_endpoint"] = publish_address
        if output.is_json():
            click.echo(json.dumps(result))
        elif access is not None:
            click.echo(address)
        else:
            click.echo(f"Read: {address}")
            if publish_address is not None:
                click.echo(f"Publish: {publish_address}")
    except (ApiError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        output.fatal(str(exc))


def reference(
    operand: str | None = typer.Argument(None, help="OCI path, path:tag or path@sha256:digest."),
    target: str | None = typer.Option(None, "--target", "-t"),
    kind: str | None = typer.Option(None, "--format", "-f"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Print one canonical repository-qualified OCI reference."""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            found = discover(target, profile, account, kind)
            selected = found.select_format(kind, ("oci",))
            value = found.reference(selected, operand)
        result = {"target": found.target.display_selector, "format": selected, "reference": value}
        click.echo(json.dumps(result) if output.is_json() else value)
    except (ApiError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        output.fatal(str(exc))


@native_app.command("config")
def config(
    tool: str = typer.Argument(...),
    target: str | None = typer.Option(None, "--target", "-t"),
    kind: str | None = typer.Option(None, "--format", "-f"),
    access: str | None = typer.Option(None, "--access"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Print templates without creating credentials, running tools or writing files."""
    try:
        from .native_config import TOOLS

        with contextlib.redirect_stdout(sys.stderr):
            found = discover(
                target, profile, account, kind or TOOLS.get("mvn" if tool == "maven" else tool)
            )
            result = render(found, tool, kind, access)
        if output.is_json():
            click.echo(json.dumps(result))
        else:
            for snippet in result["snippets"]:
                click.echo(
                    f"# {snippet['kind']}: {snippet.get('filename', snippet.get('shell'))}\n{snippet['text']}\n"
                )
    except (ApiError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        output.fatal(str(exc))
