"""Explicit, secret-only manual native credential output."""

import contextlib
import json
import re
import sys
from typing import Literal

import click
import httpx2 as httpx
import typer

from .. import output
from ..api import remote_cache_mint_token_path, repository_mint_token_path
from ..auth.token_format import validate_public_token
from ..client import ApiClient, ApiError
from .discovery import discover
from .formats import flatten_formats
from .targets import (
    expected_repository_target,
    remote_cache_native_path,
    repository_native_path,
    token_scope_hint,
)


app = typer.Typer(help="Create short-lived tokens for package tools.", no_args_is_help=True)

ACCESS_OPERATIONS: dict[str, list[str]] = {
    "read": ["read"],
    "publish": ["read", "publish"],
    "admin": ["read", "publish", "delete"],
}


def duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)(m|h)", value)
    if match is None:
        raise ValueError("Use a duration such as 15m, 30m, 4h, or 12h.")
    seconds = int(match[1]) * (60 if match[2] == "m" else 3600)
    if not 900 <= seconds <= 43200:
        raise ValueError("Temporary credentials support 15 minutes to 12 hours.")
    return seconds


@app.command("mint")
def mint(
    target: str | None = typer.Option(None, "--target", "-t"),
    formats: list[str] | None = typer.Option(
        None, "--format", "-f", help="Comma-separated formats; may also be repeated."
    ),
    all_formats: bool = typer.Option(False, "--all-formats"),
    access: Literal["read", "publish", "admin"] = typer.Option(
        "read",
        "--access",
        help="Admin also permits deletion when your sign-in or automation token allows it.",
    ),
    duration: str = typer.Option(
        "4h", "--duration", help="15m to 12h; source expiry may shorten it."
    ),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    account: str | None = typer.Option(None, "--account"),
) -> None:
    """Print a short-lived token for direct package-tool access.

    Prefer the rvs wrapper for your native tool when possible. The generated token
    is secret until it expires. This command prints it to stdout; other messages go
    to stderr.
    """
    try:
        seconds = duration_seconds(duration)
        # Existing account/target helpers may explain context changes. Their
        # diagnostics cannot contaminate a shell-captured bearer value.
        with contextlib.redirect_stdout(sys.stderr):
            requested = flatten_formats(formats or [])
            if all_formats and requested:
                raise ValueError("--all-formats and --format are mutually exclusive.")
            found = discover(
                target, profile, account, requested[0] if len(requested) == 1 else None
            )
            selected = found.target
            if not requested and not all_formats:
                requested = [found.select_format(None)]
            if any(item not in found.formats for item in requested):
                raise ValueError(
                    "Every requested format must be enabled on the exact target."
                    + (token_scope_hint() if selected.target_type == "repository" else "")
                )
            client = ApiClient.from_profile(found.profile)
            operations = ACCESS_OPERATIONS[access]
            if selected.target_type == "repository":
                if not selected.repository_ref:
                    raise ValueError("The selected repository has no permanent ID.")
                selection: dict[str, object] = (
                    {"all_formats": True} if all_formats else {"formats": requested}
                )
                response = client.issue_native(
                    repository_mint_token_path(selected.repository_ref),
                    {
                        **selection,
                        "operations": operations,
                        "duration_seconds": seconds,
                        "expected_target": expected_repository_target(selected),
                    },
                ).json()
                repository_native_path(response, selected.repository_ref)
                minted_formats = response["formats"]
            else:
                if access != "read":
                    raise ValueError("Private mirrors support read credentials only.")
                if not selected.remote_cache_ref:
                    raise ValueError("The selected private mirror has no permanent ID.")
                mirror_format = found.select_format(requested[0] if requested else None)
                response = client.issue_native(
                    remote_cache_mint_token_path(selected.remote_cache_ref),
                    {"duration_seconds": seconds},
                ).json()
                remote_cache_native_path(response, selected.remote_cache_ref, mirror_format)
                minted_formats = [mirror_format]
        secret = response.get("access_token")
        if not isinstance(secret, str):
            raise ValueError("Ravenstash returned an invalid temporary token.")
        validate_public_token(secret, native=True)
        if output.is_json():
            result = {
                "target": selected.display_selector,
                "formats": minted_formats,
                "access": access,
                "access_token": secret,
                "token_type": response["token_type"],
                "expires_in": response["expires_in"],
            }
            click.echo(json.dumps(result, separators=(",", ":")))
        else:
            click.echo(secret)
    except (ApiError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        output.fatal(str(exc))
