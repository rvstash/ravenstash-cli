"""Artifact-target parsing, resolution, and credential exchange."""

import os
from dataclasses import dataclass, field
from typing import Literal, cast

from .. import config as cfg_mod
from .. import output
from ..account.commands import acting_account_ref, ensure_active_account
from ..api import (
    artifacts_path,
    collection_all,
    remote_cache_mint_token_path,
    repository_mint_token_path,
)
from ..auth.token_format import STATIC_NATIVE_DURATION_SECONDS, validate_public_token
from ..client import ApiClient, ApiError
from .formats import FORMATS
from .routing import native_path


PackageFormat = Literal["pypi", "npm", "maven"]
PackageOperation = Literal["read", "publish", "delete"]
DEFAULT_OFFICIAL_SOURCES: dict[PackageFormat, str] = {
    "pypi": "pypiorg",
    "npm": "npmjs",
    "maven": "maven-central",
}


@dataclass(frozen=True)
class TargetSpec:
    target_type: cfg_mod.ArtifactTargetType
    selector: str
    namespace_realm: cfg_mod.NamespaceRealm | None = None


@dataclass(frozen=True)
class RegistryContext:
    profile_name: str
    account_ref: str
    kind: cfg_mod.Format
    target: cfg_mod.ArtifactTarget
    read_base_url: str
    push_base_url: str | None
    # Opaque server-issued native path without its outer slashes.
    native_path: str
    token: str = field(repr=False)


def parse_target(value: str) -> TargetSpec:
    """Parse ``[repo:]namespace/repository``, ``in/ar_...``, or a mirror target."""
    candidate = value.strip().strip("/")
    if not candidate:
        output.fatal("Artifact target cannot be empty.")
    if candidate.startswith(cfg_mod.REPOSITORY_TARGET_PREFIX):
        # The canonical repository resource name; a bare repository selector is the default.
        candidate = candidate.removeprefix(cfg_mod.REPOSITORY_TARGET_PREFIX).strip().strip("/")
        if not candidate or ":" in candidate:
            output.fatal("Repository targets must be repo:namespace/repository.")
    if candidate.startswith("mirror:"):
        selector = candidate.removeprefix("mirror:").strip().strip("/")
        target_type: cfg_mod.ArtifactTargetType = "official_cache"
        namespace_realm = None
    elif candidate.startswith("custom-mirror:"):
        selector = candidate.removeprefix("custom-mirror:").strip().strip("/")
        target_type = "custom_cache"
        namespace_realm = None
    elif ":" in candidate or candidate.startswith("@"):
        output.fatal(
            "Unknown repository or mirror. Use a name-based target "
            "(namespace/repository), an ID-based target (in/ar_...), mirror:<source>, "
            "or custom-mirror:<name>."
        )
    else:
        selector = candidate
        target_type = "repository"
        namespace_realm = None
    if not selector or (target_type != "repository" and "/" in selector):
        output.fatal("The repository or mirror name is invalid.")
    if target_type == "repository":
        parts = selector.split("/")
        if len(parts) != 2 or not all(parts):
            output.fatal(
                "Repository targets must be name-based (namespace/repository) "
                "or ID-based (in/ar_...)."
            )
        namespace_part, repository_part = parts
        typed = namespace_part.startswith(("in_", "gn_", "ar_")) or repository_part.startswith(
            ("in_", "gn_", "ar_")
        )
        if repository_part.startswith("ar_"):
            if namespace_part != "in":
                output.fatal("Internal ID-based targets must use `in/ar_...`.")
            namespace_realm = "internal"
        elif typed:
            output.fatal(
                "Permanent IDs cannot be substituted into a name-based target. "
                "Use in/ar_... for an ID-based target."
            )
    return TargetSpec(
        target_type=target_type,
        selector=selector,
        namespace_realm=namespace_realm,
    )


def _package_kind(kind: str | None) -> PackageFormat | None:
    if kind is None:
        return None
    if kind not in DEFAULT_OFFICIAL_SOURCES:
        output.fatal("Remote caches support only pypi, npm, and maven.")
    return cast("PackageFormat", kind)


def token_scope_hint(kind: str | None = None) -> str:
    """Explain a hidden format without revealing whether the repository exists.

    An automation token can be limited to selected formats, and Ravenstash hides
    repositories and formats a token does not cover.
    """
    if "RVS_TOKEN" not in os.environ:
        return ""
    subject = f"the {kind} format" if kind else "these formats"
    return f" Check that RVS_TOKEN includes this repository and {subject}."


def repository_formats(repository: dict) -> tuple[str, ...]:
    """Return the enabled formats of an API ``Repository``."""
    return tuple(item["format"] for item in repository["formats"])


def repository_display_name(repository: dict) -> str:
    return f"{repository['namespace']['name']}/{repository['name']}"


def repository_target_name(repository: dict) -> str:
    """Return the canonical ``repo:namespace/repository`` resource name."""
    return f"{cfg_mod.REPOSITORY_TARGET_PREFIX}{repository_display_name(repository)}"


def remote_public_name(remote: dict) -> str:
    """Return the official source ref or custom name that selects a remote cache."""
    if remote["source_type"] == "official":
        return str(remote.get("official_source_ref") or remote["ref"])
    return str(remote.get("name") or remote["ref"])


def remote_target_name(remote: dict) -> str:
    prefix = "mirror" if remote["source_type"] == "official" else "custom-mirror"
    return f"{prefix}:{remote_public_name(remote)}"


_NAMESPACE_REALMS = ("internal", "global")


def _repository_target(repository: dict) -> cfg_mod.ArtifactTarget:
    namespace = repository["namespace"]
    # The server's value sets are open; never persist a value config loading rejects.
    if namespace["realm"] not in _NAMESPACE_REALMS:
        raise ValueError(f"rvs does not support namespace realm {namespace['realm']!r} yet")
    repository_kinds = repository_formats(repository)
    inferred_kind = (
        repository_kinds[0]
        if len(repository_kinds) == 1 and repository_kinds[0] in FORMATS
        else None
    )
    return cfg_mod.ArtifactTarget(
        target_type="repository",
        account_ref=repository["account"]["ref"],
        stable_selector=f"in/{repository['ref']}",
        display_selector=repository_target_name(repository),
        format=cast("cfg_mod.Format | None", inferred_kind),
        namespace_realm=namespace["realm"],
        namespace_ref=namespace["ref"],
        namespace_name_cache=namespace["name"],
        repository_ref=repository["ref"],
        repository_name_cache=repository["name"],
    )


def _remote_target(remote: dict, target_type: cfg_mod.ArtifactTargetType) -> cfg_mod.ArtifactTarget:
    family = remote["source_type"]
    expected_family = "official" if target_type == "official_cache" else "custom"
    if family != expected_family:
        raise ValueError(f"Remote cache is {family}, not {expected_family}")
    if remote["format"] not in FORMATS:
        raise ValueError(f"rvs does not support the {remote['format']!r} format yet")
    public_name = remote_public_name(remote)
    prefix = "mirror" if target_type == "official_cache" else "custom-mirror"
    remote_cache_ref = remote["ref"]
    return cfg_mod.ArtifactTarget(
        target_type=target_type,
        account_ref=remote["account"]["ref"],
        stable_selector=f"{prefix}:{remote_cache_ref}",
        display_selector=f"{prefix}:{public_name}",
        format=cast("cfg_mod.Format", remote["format"]),
        remote_cache_ref=remote_cache_ref,
        remote_name_cache=public_name,
    )


def matching_remote_caches(
    remotes: list[dict], target_type: cfg_mod.ArtifactTargetType, selector: str
) -> list[dict]:
    family = "official" if target_type == "official_cache" else "custom"
    return [
        remote
        for remote in remotes
        if remote["source_type"] == family
        and selector in {remote["ref"], remote_public_name(remote)}
    ]


def is_stable_repository_selector(selector: str) -> bool:
    """Typed references identify resources; readable names stay account-scoped.

    The API validates reference syntax and authorization. Bare repository refs
    are accepted only by repository-management commands.
    """
    parts = selector.split("/")
    return (len(parts) == 1 and parts[0].startswith("ar_")) or (
        len(parts) == 2 and parts[0] == "in" and parts[1].startswith("ar_")
    )


def resolve_repository_entry(
    client: ApiClient, selector: str, account_ref: str, kind: str | None = None
) -> dict:
    """Resolve a repository selector to its API ``Repository``."""
    stable = is_stable_repository_selector(selector)
    params = {"selector": selector}
    if not stable:
        params["account_ref"] = account_ref
    if kind is not None:
        params["format"] = kind
    try:
        repository = client.get(artifacts_path("repositories/resolve"), params=params).json()
    except ApiError as exc:
        hint = token_scope_hint(kind) if kind is not None else ""
        if exc.status_code != 404 or not hint or not isinstance(exc.detail, str):
            raise
        raise ApiError(exc.status_code, exc.detail.rstrip(".") + "." + hint) from exc
    owner = repository["account"]
    if owner["ref"] != account_ref:
        if not stable:
            output.fatal("The repository belongs to a different account.")
        output.resource_account_hint(selector, account_ref, owner)
    return repository


def resolve_target(
    value: str,
    *,
    profile: str | None = None,
    account_ref: str | None = None,
    kind: str | None = None,
) -> tuple[str, cfg_mod.AccountContext, cfg_mod.ArtifactTarget]:
    spec = parse_target(value)
    profile_name = profile or cfg_mod.current_profile_name()
    if kind is not None and kind not in {"pypi", "npm", "maven", "oci"}:
        output.fatal(f"Unknown format '{kind}'.")
    if spec.target_type != "repository":
        target_format: str | None = _package_kind(kind)
    else:
        target_format = kind
    client = ApiClient.from_profile(profile_name)
    try:
        if spec.target_type == "repository":
            effective_account_ref = account_ref or acting_account_ref(profile_name)
            if effective_account_ref is None:
                output.fatal("No account is selected. Run `rvs account switch`.")
            repository = resolve_repository_entry(
                client, spec.selector, effective_account_ref, target_format
            )
            account = cfg_mod.cache_account(
                profile=profile_name,
                account=repository["account"],
                activate=False,
            )
            target = _repository_target(repository)
        else:
            profile_name, account = ensure_active_account(profile_name, account_ref)
            remotes = collection_all(
                client,
                artifacts_path("remote-caches"),
                {"account_ref": account.account_ref, "format": target_format},
            )
            matches = matching_remote_caches(remotes, spec.target_type, spec.selector)
            if not matches:
                raise ValueError(f"Package target '{value}' was not found in this account")
            if len(matches) > 1:
                kinds = ", ".join(sorted({str(item["format"]) for item in matches}))
                raise ValueError(
                    f"Package target '{value}' is ambiguous across {kinds}. Pass --format."
                )
            target = _remote_target(matches[0], spec.target_type)
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        output.fatal(str(exc))
    return profile_name, account, target


def effective_target(
    *,
    kind: str,
    target: str | None = None,
    repo: str | None = None,
    profile: str | None = None,
    account_ref: str | None = None,
) -> tuple[str, cfg_mod.AccountContext, cfg_mod.ArtifactTarget]:
    package_format = _package_kind(kind)
    assert package_format is not None
    profile_name, account = ensure_active_account(profile, account_ref)
    explicit = target or repo
    if explicit:
        value = explicit if target else cast("str", repo)
        return resolve_target(
            value,
            profile=profile_name,
            account_ref=account.account_ref,
            kind=kind,
        )
    saved = cfg_mod.selected_artifact_target(profile_name, account.account_ref)
    if saved is not None:
        if saved.format is not None and saved.format != kind:
            output.fatal(
                f"Selected target '{saved.display_selector}' is for {saved.format}, not {kind}."
            )
        return resolve_target(
            saved.stable_selector,
            profile=profile_name,
            account_ref=account.account_ref,
            kind=kind,
        )
    output.fatal(
        f"No {kind} repository or mirror is selected. Pass --target or run `rvs art select`."
    )


def expected_repository_target(selected: cfg_mod.ArtifactTarget) -> dict[str, object]:
    """Return the identity guard sent with every repository token mint."""
    return {
        "namespace_ref": selected.namespace_ref,
        "namespace_name": selected.namespace_name_cache,
        "namespace_realm": selected.namespace_realm,
        "repository_ref": selected.repository_ref,
        "repository_name": selected.repository_name_cache,
    }


def repository_native_path(credential: dict, repository_ref: str) -> str:
    """Return the native path of a credential minted for *repository_ref*, or raise.

    The native path is opaque; the credential's target identity is what proves
    it was minted for the selected repository.
    """
    target = credential.get("target")
    if not isinstance(target, dict) or target.get("repository_ref") != repository_ref:
        raise ValueError("Ravenstash returned a credential for a different repository.")
    return native_path(credential.get("native_path"))


def remote_cache_native_path(credential: dict, remote_cache_ref: str, kind: str) -> str:
    """Return the native path of a credential minted for one private mirror, or raise."""
    if credential.get("format") != kind or credential.get("remote_cache_ref") != remote_cache_ref:
        raise ValueError("Ravenstash returned a credential for a different private mirror.")
    return native_path(credential.get("native_path"))


def registry_context(
    *,
    kind: str,
    target: str | None = None,
    repo: str | None = None,
    profile: str | None = None,
    account_ref: str | None = None,
    require_private: bool = False,
    operations: tuple[PackageOperation, ...] = ("read",),
) -> RegistryContext:
    profile_name, account, selected = effective_target(
        kind=kind,
        target=target,
        repo=repo,
        profile=profile,
        account_ref=account_ref,
    )
    if require_private and selected.target_type != "repository":
        output.fatal(f"'{selected.display_selector}' is read-only; select a private repository.")
    client = ApiClient.from_profile(profile_name)
    package_format = cast("PackageFormat", kind)
    try:
        if selected.target_type == "repository":
            if not selected.repository_ref:
                raise ValueError("Selected private repository has no permanent ID")
            credential = client.issue_native(
                repository_mint_token_path(selected.repository_ref),
                {
                    "formats": [kind],
                    "operations": list(operations),
                    "duration_seconds": STATIC_NATIVE_DURATION_SECONDS,
                    "expected_target": expected_repository_target(selected),
                },
            ).json()
            route_path = repository_native_path(credential, selected.repository_ref)
        else:
            if not selected.remote_cache_ref:
                raise ValueError("Selected private mirror has no permanent ID")
            credential = client.issue_native(
                remote_cache_mint_token_path(selected.remote_cache_ref),
                {"duration_seconds": STATIC_NATIVE_DURATION_SECONDS},
            ).json()
            route_path = remote_cache_native_path(credential, selected.remote_cache_ref, kind)
        native_token = validate_public_token(credential["access_token"], native=True)
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        output.fatal(str(exc))
    # Minting may refresh the session and its registry discovery; read the profile
    # afterwards so this invocation uses the current endpoints.
    endpoints = (
        cfg_mod.load().active_profile(profile_name).native_registries.package(package_format)
    )
    if selected.target_type == "repository":
        read_base_url = endpoints.read_base_url
        push_base_url: str | None = endpoints.push_base_url
    else:
        read_base_url = endpoints.mirror_base_url
        push_base_url = None
    return RegistryContext(
        profile_name=profile_name,
        account_ref=account.account_ref,
        kind=cast("cfg_mod.Format", kind),
        target=selected,
        read_base_url=read_base_url,
        push_base_url=push_base_url,
        native_path=route_path,
        token=native_token,
    )
