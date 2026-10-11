"""Read-only summary of the signed-in user and the current CLI selections."""

from dataclasses import dataclass

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
    target: cfg_mod.ArtifactTarget | None


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
        target=cached.selected_target if cached is not None else None,
    )


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
    account = selection_account_display(selection)
    output.kv(
        {
            "Profile": with_source(selection.profile_name, selection.profile_source),
            "Signed in as": identity_display(selection.identity),
            "Account": (
                with_source(account, selection.account_source) if account else "not selected"
            ),
            "Artifacts target": (
                " · ".join(part for part in (target.display_selector, target.format) if part)
                if target is not None
                else "not selected"
            ),
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
            "targets": {"artifacts": target_json(target)},
            "api_url": selection.api_url,
        },
    )
