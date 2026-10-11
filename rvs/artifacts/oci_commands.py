"""OCI graph management through the typed developer API."""

import contextlib
import json
import re
import sys
from typing import Literal

import click
import httpx2 as httpx
import typer

from .. import output
from ..api import artifacts_path, collection_items, segment
from ..client import ApiClient, ApiError
from ..oci.reference import qualify_reference
from .discovery import discover
from .trash import moved_to_trash


app = typer.Typer(help="Manage OCI paths, manifests, tags and referrers.", no_args_is_help=True)
manifest_app = typer.Typer(no_args_is_help=True)
tag_app = typer.Typer(no_args_is_help=True)
referrer_app = typer.Typer(no_args_is_help=True)
app.add_typer(manifest_app, name="manifest")
app.add_typer(tag_app, name="tag")
app.add_typer(referrer_app, name="referrer")


def _reference(value: str, kind: str) -> tuple[str, str]:
    qualify_reference("unused", value)
    separator = "@" if kind == "digest" else ":"
    path, found, reference = value.partition(separator)
    if not found or (kind == "tag" and "@" in value):
        raise ValueError(f"Use PATH{'@sha256:DIGEST' if kind == 'digest' else ':TAG'}.")
    return path, reference


def _path(value: str) -> str:
    qualify_reference("unused", value)
    if "@" in value or ":" in value:
        raise ValueError("Use an OCI path without a tag, digest, or registry URL.")
    return value


def _request(
    method: str,
    suffix: str,
    *,
    target: str | None,
    account: str | None,
    profile: str | None,
    params: dict | None = None,
    body: dict | None = None,
    yes: bool = False,
    subject: str = "",
    trash: bool = False,
) -> None:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            found = discover(target, profile, account, "oci")
            found.select_format("oci")
            if found.target.target_type != "repository":
                raise ValueError("OCI commands require a repository target.")
            reference = found.target.repository_ref
            if not reference:
                raise ValueError("The selected repository has no permanent ID.")
            client = ApiClient.from_profile(found.profile)
            url = artifacts_path(f"repositories/{segment(reference)}/formats/oci/{suffix}")
            if method == "DELETE" and not yes:
                prompt = (
                    f"Move manifest {subject} in {found.target.display_selector} to trash? "
                    "Its referrers move with it and its tags stop resolving. You can "
                    "restore it in the web app until it is permanently deleted."
                    if trash
                    else f"Delete {subject} from {found.target.display_selector}?"
                )
                typer.confirm(prompt, abort=True)
            response = None
            if method == "GET":
                response = client.get(
                    url,
                    params={
                        key: value for key, value in (params or {}).items() if value is not None
                    },
                )
            elif method == "PUT":
                response = client.put(url, json=body, params=params)
            else:
                # Deletions answer 204 without a body; an absent object is a 404.
                client.delete(url, params=params)
        if response is None:
            output.success(
                moved_to_trash(subject, found.target.display_selector)
                if trash
                else f"Deleted {subject} from {found.target.display_selector}."
            )
            return
        payload = response.json()
        if method == "PUT":
            # 201 when the tag was created, 200 when it already pointed at the
            # digest; a tag on another manifest is refused with 409.
            created = response.status_code == 201
            if output.is_json():
                click.echo(json.dumps({**payload, "created": created}))
            elif created:
                output.success(f"Created tag '{payload['tag']}' at {payload['digest']}.")
            else:
                output.success(f"Tag '{payload['tag']}' already points at {payload['digest']}.")
        elif output.is_json():
            click.echo(json.dumps(payload))
        elif method == "GET" and (suffix in _COLLECTIONS or suffix.endswith("/referrers")):
            rows = collection_items(payload)
            columns = [
                key
                for key in (
                    "path",
                    "content_type",
                    "digest",
                    "tag",
                    "manifest_kind",
                    "recent_tags",
                    "tag_count",
                )
                if any(key in row for row in rows)
            ]
            output.table(columns, [[_cell(row.get(key)) for key in columns] for row in rows])
            if payload.get("next_cursor"):
                click.echo(f"Next page: --cursor {payload['next_cursor']}", err=True)
        else:
            output.kv(
                {
                    key: json.dumps(value) if isinstance(value, (list, dict)) else str(value)
                    for key, value in payload.items()
                }
            )
    except (ApiError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        output.fatal(str(exc))


_COLLECTIONS = frozenset({"paths", "manifests", "tags"})
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")


def _cell(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "—"
    if value is None or value == "":
        return "—"
    return str(value)


def _digest(value: str | None) -> str | None:
    if value is not None and _DIGEST.fullmatch(value) is None:
        raise ValueError("Use a manifest digest such as sha256:<64 hex characters>.")
    return value


def _validated(call, *args):
    try:
        return call(*args)
    except ValueError as exc:
        output.fatal(str(exc))


@app.command("list")
def list_paths(
    content_type: Literal["container_image", "helm_chart"] | None = typer.Option(
        None, "--content-type"
    ),
    search: str | None = typer.Option(None, "--search"),
    limit: int = typer.Option(50, "--limit", min=1, max=100),
    cursor: str | None = typer.Option(None, "--cursor"),
    target: str | None = typer.Option(None, "--target", "-t"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    _request(
        "GET",
        "paths",
        target=target,
        account=account,
        profile=profile,
        params={"content_type": content_type, "search": search, "limit": limit, "cursor": cursor},
    )


@app.command("show")
def show_path(
    path: str = typer.Argument(...),
    target: str | None = typer.Option(None, "--target", "-t"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    _request(
        "GET",
        "path",
        target=target,
        account=account,
        profile=profile,
        params={"path": _validated(_path, path)},
    )


@manifest_app.command("list")
def manifest_app_list(
    path: str = typer.Argument(...),
    limit: int = typer.Option(50, "--limit", min=1, max=100),
    cursor: str | None = typer.Option(None, "--cursor"),
    target: str | None = typer.Option(None, "--target", "-t"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    path = _validated(_path, path)
    _request(
        "GET",
        "manifests",
        target=target,
        account=account,
        profile=profile,
        params={"path": path, "limit": limit, "cursor": cursor},
    )


@manifest_app.command("show")
def manifest_app_show(
    reference: str = typer.Argument(...),
    target: str | None = typer.Option(None, "--target", "-t"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    path, digest = _validated(_reference, reference, "digest")
    _request(
        "GET",
        f"manifests/{segment(digest)}",
        target=target,
        account=account,
        profile=profile,
        params={"path": path},
    )


@manifest_app.command("delete")
def manifest_app_delete(
    reference: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    target: str | None = typer.Option(None, "--target", "-t"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Move a manifest to the repository trash.

    Its referrers move with it, and its tags stop resolving until it is restored
    in the web app. A manifest that an index in the path references cannot be
    deleted alone; delete the index instead.
    """
    path, digest = _validated(_reference, reference, "digest")
    _request(
        "DELETE",
        f"manifests/{segment(digest)}",
        target=target,
        account=account,
        profile=profile,
        params={"path": path},
        yes=yes,
        subject=reference,
        trash=True,
    )


@tag_app.command("list")
def tag_app_list(
    path: str = typer.Argument(...),
    digest: str | None = typer.Option(
        None, "--digest", help="List only the tags that point at this manifest digest."
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=100),
    cursor: str | None = typer.Option(None, "--cursor"),
    target: str | None = typer.Option(None, "--target", "-t"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """List a path's tags in tag order, optionally only those of one manifest."""
    path = _validated(_path, path)
    digest = _validated(_digest, digest)
    _request(
        "GET",
        "tags",
        target=target,
        account=account,
        profile=profile,
        params={"path": path, "digest": digest, "limit": limit, "cursor": cursor},
    )


@tag_app.command("delete")
def tag_app_delete(
    reference: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
    target: str | None = typer.Option(None, "--target", "-t"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Delete a tag at once; tags are not moved to the trash and the manifest stays."""
    path, tag = _validated(_reference, reference, "tag")
    _request(
        "DELETE",
        f"tags/{segment(tag)}",
        target=target,
        account=account,
        profile=profile,
        params={"path": path},
        yes=yes,
        subject=reference,
    )


@referrer_app.command("list")
def referrer_app_list(
    reference: str = typer.Argument(...),
    limit: int = typer.Option(50, "--limit", min=1, max=100),
    cursor: str | None = typer.Option(None, "--cursor"),
    target: str | None = typer.Option(None, "--target", "-t"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    path, digest = _validated(_reference, reference, "digest")
    _request(
        "GET",
        f"manifests/{segment(digest)}/referrers",
        target=target,
        account=account,
        profile=profile,
        params={"path": path, "limit": limit, "cursor": cursor},
    )


@tag_app.command("create")
def create_tag(
    reference: str = typer.Argument(..., help="PATH@sha256:DIGEST"),
    tag: str = typer.Argument(...),
    target: str | None = typer.Option(None, "--target", "-t"),
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Create TAG at a manifest; a tag on another manifest is never moved."""
    path, digest = _validated(_reference, reference, "digest")
    _validated(qualify_reference, "unused", f"{path}:{tag}")
    _request(
        "PUT",
        f"tags/{segment(tag)}",
        target=target,
        account=account,
        profile=profile,
        params={"path": path},
        body={"digest": digest},
    )
