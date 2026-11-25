"""Read-only summary of the signed-in user and the current CLI selections."""

from dataclasses import dataclass, replace
from typing import Literal

import httpx2 as httpx
import typer

from . import config as cfg_mod
from . import output
from .account.commands import (
    accounts,
    credential_account,
    identity_display,
    identity_person,
    match_account,
    payload_display_name,
    payload_named_display,
    token_account_conflict,
    verified_identity,
)
from .artifacts.formats import PACKAGE_FORMATS


# Everyday sources stay implicit; any other source is shown next to its value.
_DEFAULT_SOURCES = frozenset({"persisted default", "persisted profile", "personal account default"})


@dataclass(frozen=True)
class Selection:
    """The effective selections, resolved without writing configuration."""

    profile_name: str
    profile_source: str
    api_url: str
    identity: dict
    account: dict | None
    account_ref: str | None
    account_source: str
    # One selection per format; ``target`` is the single target selected for
    # every selected format, kept for the pre-format-selection output shape.
    targets: dict[cfg_mod.Format, cfg_mod.ArtifactTarget]
    target: cfg_mod.ArtifactTarget | None


@dataclass(frozen=True)
class FormatSelection:
    """Where one format's commands go when --target is omitted."""

    format: cfg_mod.Format
    source: Literal["selected", "default_mirror", "none"]
    target: str | None = None
    type: str | None = None


def _single_target(
    targets: dict[cfg_mod.Format, cfg_mod.ArtifactTarget],
) -> cfg_mod.ArtifactTarget | None:
    distinct = {item.stable_selector: item for item in targets.values()}
    if len(distinct) != 1:
        return None
    target = next(iter(distinct.values()))
    return replace(target, format=next(iter(targets)) if len(targets) == 1 else None)


def inspect_selection(profile: str | None = None, account: str | None = None) -> Selection:
    """Verify the sign-in and resolve the acting account and its selected target.

    Every lookup is read-only: nothing is cached, activated, or selected.
    """
    cfg = cfg_mod.load()
    profile_name = profile or cfg_mod.current_profile_name(cfg)
    identity = verified_identity(profile_name)
    items = accounts(profile_name)
    token = credential_account(identity)
    if account is not None:
        selected: dict | None = match_account(items, account)
        account_ref = str(selected["ref"])
        account_source = "command option (--account)"
    elif token is not None:
        # Commands act only for the token's account; a conflict stops them.
        account_ref = str(token["ref"])
        account_source = "access token (RVS_TOKEN)"
        if conflict := token_account_conflict(token):
            output.warn(conflict)
        selected = next((item for item in items if item.get("ref") == account_ref), token)
    else:
        account_ref = cfg_mod.current_account_ref(profile_name, cfg)
        account_source = cfg_mod.account_selection_source(profile_name, cfg)
        if account_ref is None:
            # Commands fall back to the one personal account the same way.
            personal = [item for item in items if item.get("type") == "personal"]
            if len(personal) == 1:
                account_ref = str(personal[0]["ref"])
                account_source = "personal account default"
        selected = next((item for item in items if item.get("ref") == account_ref), None)
        if account_ref is not None and selected is None:
            output.warn(f"The selected account {account_ref} is not available to this sign-in.")
    cached = cfg_mod.cached_account(profile_name, account_ref) if account_ref else None
    targets = dict(cached.selected_targets) if cached is not None else {}
    return Selection(
        profile_name=profile_name,
        profile_source=(
            "command option (--profile)" if profile else cfg_mod.profile_selection_source()
        ),
        api_url=cfg.active_profile(profile_name).api_url,
        identity=identity,
        account=selected,
        account_ref=account_ref,
        account_source=account_source,
        targets=targets,
        target=_single_target(targets),
    )


def _default_mirrors(selection: Selection) -> dict[str, str | None] | None:
    """Return each package format's default mirror name, or ``None`` if unknown."""
    from .api import artifacts_path, collection_all
    from .artifacts.targets import default_official_cache, remote_target_name
    from .client import ApiClient, ApiError

    try:
        remotes = collection_all(
            ApiClient.from_profile(selection.profile_name),
            artifacts_path("remote-caches"),
            {"account_ref": selection.account_ref},
        )
    except ApiError, httpx.HTTPError, KeyError, TypeError, ValueError:
        return None
    result: dict[str, str | None] = {}
    for kind in PACKAGE_FORMATS:
        remote = default_official_cache(remotes, kind)
        result[kind] = remote_target_name(remote) if remote is not None else None
    return result


def format_selections(selection: Selection) -> list[FormatSelection]:
    """Describe every format: its selection, the automatic default mirror, or nothing.

    Only a personal account's interactive session uses a default mirror, and only
    for package formats; the mirror names are looked up without changing anything.
    """
    from .artifacts.targets import interactive_session

    personal = (
        selection.account is not None
        and selection.account.get("type") == "personal"
        and interactive_session()
    )
    unselected = [kind for kind in PACKAGE_FORMATS if kind not in selection.targets]
    mirrors = _default_mirrors(selection) if personal and unselected else None
    rows: list[FormatSelection] = []
    for kind in cfg_mod.FORMAT_VALUES:
        target = selection.targets.get(kind)
        if target is not None:
            rows.append(
                FormatSelection(kind, "selected", target.display_selector, target.target_type)
            )
        elif personal and kind in PACKAGE_FORMATS and (mirrors is None or mirrors[kind]):
            # An unknown mirror name still means the command will use the default.
            name = mirrors[kind] if mirrors is not None else None
            rows.append(FormatSelection(kind, "default_mirror", name, "official_cache"))
        else:
            rows.append(FormatSelection(kind, "none"))
    return rows


def formats_json(rows: list[FormatSelection]) -> dict[str, dict]:
    return {
        row.format: {"target": row.target, "type": row.type, "source": row.source} for row in rows
    }


def format_rows(rows: list[FormatSelection]) -> dict[str, str]:
    """Return the human status line of each format."""
    values: dict[str, str] = {}
    for row in rows:
        if row.source == "selected":
            value = row.target or ""
        elif row.source == "default_mirror":
            value = (
                f"{row.target} (default mirror, automatic)"
                if row.target
                else "default mirror, automatic (not checked)"
            )
        else:
            value = "not selected"
        values[f"{row.format} target"] = value
    return values


def with_source(value: str, source: str) -> str:
    return value if source in _DEFAULT_SOURCES else f"{value} · {source}"


def selection_account_display(selection: Selection) -> str | None:
    if selection.account is not None:
        return payload_named_display(selection.account)
    return selection.account_ref


def account_json(selection: Selection) -> dict | None:
    if selection.account_ref is None:
        return None
    return {
        "ref": selection.account_ref,
        "handle": (
            payload_display_name(selection.account) if selection.account is not None else None
        ),
        "display_name": (
            selection.account.get("display_name") if selection.account is not None else None
        ),
        "selected_by": selection.account_source,
    }


def target_json(target: cfg_mod.ArtifactTarget | None) -> dict | None:
    if target is None:
        return None
    return {
        "target": target.display_selector,
        "type": target.target_type,
        "format": target.format,
    }


def _identity_json(identity: dict) -> dict:
    credential = identity.get("credential")
    handle, name = identity_person(identity)
    return {
        "display": identity_display(identity),
        "principal_type": identity.get("principal_type") or "user",
        "user": handle,
        "display_name": name,
        "credential": credential.get("scenario") if isinstance(credential, dict) else None,
    }


def status(
    profile: str | None = typer.Option(
        None,
        "--profile",
        "-p",
        help="Local profile to inspect (default: selected profile).",
    ),
) -> None:
    """Show who is signed in and what the CLI currently acts on."""
    selection = inspect_selection(profile)
    target = selection.target
    rows = format_selections(selection)
    account = selection_account_display(selection)
    output.kv(
        {
            "Profile": with_source(selection.profile_name, selection.profile_source),
            "Signed in as": identity_display(selection.identity),
            "Account": (
                with_source(account, selection.account_source) if account else "not selected"
            ),
            **format_rows(rows),
            "Ravenstash API": selection.api_url,
        },
        title="Ravenstash status",
        json_values={
            "profile": {
                "name": selection.profile_name,
                "selected_by": selection.profile_source,
            },
            "signed_in_as": _identity_json(selection.identity),
            "account": account_json(selection),
            "targets": {
                "artifacts": target_json(target),
                "artifacts_by_format": formats_json(rows),
            },
            "api_url": selection.api_url,
        },
    )
