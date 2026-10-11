"""`rvs art` command group."""

import json
from typing import cast

import click
import typer

from .. import config as cfg_mod
from .. import output
from ..account.commands import (
    acting_account_ref,
    ensure_active_account,
    payload_display_name,
    resolve_account,
)
from ..account.commands import display_name as account_display_name
from ..api import (
    artifacts_path,
    collection_all,
    is_path_segment,
    platform_path,
    read_collection,
    segment,
)
from ..client import ApiClient, ApiError
from ..publishing import confirm_question
from ..status import (
    account_json,
    inspect_selection,
    selection_account_display,
    target_json,
    with_source,
)
from .auth_commands import app as native_auth_app
from .evidence_commands import app as evidence_app
from .formats import FORMATS, flatten_formats
from .oci_commands import app as oci_app
from .primitives import endpoint, native_app, reference
from .targets import (
    DEFAULT_OFFICIAL_SOURCES,
    PackageFormat,
    parse_target,
    remote_target_name,
    repository_display_name,
    repository_formats,
    repository_target_name,
    resolve_repository_entry,
    resolve_target,
)
from .trash import moved_to_trash


app = typer.Typer(
    name="art",
    help="Manage repositories for packages, container images, and Helm charts.",
    no_args_is_help=True,
)

repo_app = typer.Typer(help="Manage Ravenstash repositories.", no_args_is_help=True)
upstream_app = typer.Typer(
    help="List the package sources used by a repository.", no_args_is_help=True
)
remote_app = typer.Typer(help="Manage private mirrors.", no_args_is_help=True)
package_app = typer.Typer(help="Manage packages hosted in a repository.", no_args_is_help=True)
package_tag_app = typer.Typer(
    help="Manage package tags such as npm distribution tags (latest, next, beta).",
    no_args_is_help=True,
)
app.add_typer(repo_app, name="repo")
repo_app.add_typer(upstream_app, name="upstream")
app.add_typer(remote_app, name="mirror")
app.add_typer(package_app, name="package")
package_app.add_typer(package_tag_app, name="tag")
app.add_typer(evidence_app, name="evidence")
app.add_typer(oci_app, name="oci")
app.add_typer(native_auth_app, name="token")
app.add_typer(native_app, name="native")
app.command("endpoint")(endpoint)
app.command("reference")(reference)

_PACKAGE_KINDS = ("pypi", "npm", "maven")
_REPOSITORY_NAME_HELP = "Name-based target (namespace/repository) or ID-based target (in/ar_...)."
_PACKAGE_TARGET_HELP = (
    "Repository (namespace/repository or in/ar_...); defaults to the target chosen "
    "with `rvs art select`."
)
_PACKAGE_FORMAT_HELP = (
    "Package format: pypi | npm | maven; needed only when the repository has more than one of them."
)
_ACCOUNT_HELP = "Typed account handle (user:USERNAME or org:HANDLE) or a bare handle."


def _format_age_hours(value: float | int | str | None, *, missing: str) -> str:
    if value is None:
        return missing
    return f"{float(value):g} hours"


def _profile_name(profile: str | None) -> str:
    cfg = cfg_mod.load()
    return profile or cfg_mod.current_profile_name(cfg)


def _profile(profile: str | None) -> tuple[str, cfg_mod.ProfileConfig]:
    cfg = cfg_mod.load()
    name = profile or cfg_mod.current_profile_name(cfg)
    return name, cfg.active_profile(name)


def _client(profile: str | None) -> ApiClient:
    return ApiClient.from_profile(profile or _root_package_options().get("profile"))


def _account_ref(profile: str | None, explicit_account_ref: str | None = None) -> str:
    if explicit_account_ref:
        return explicit_account_ref
    options = _root_package_options()
    profile = profile or options.get("profile")
    if options.get("account"):
        return str(resolve_account(cast("str", options["account"]), profile)["ref"])
    profile_name, _ = _profile(profile)
    account_ref = acting_account_ref(profile_name)
    if not account_ref:
        output.fatal("No account is selected. Run `rvs account switch`.")
    return account_ref


def _context_account_ref(profile: str | None, account: str | None) -> str | None:
    if account is None:
        return None
    return str(resolve_account(account, profile)["ref"])


@app.command("select")
def target_select(
    target: str = typer.Argument(
        ...,
        help=(
            "Name-based target (namespace/repository), ID-based target (in/ar_...), "
            "mirror:<source>, or custom-mirror:<name>."
        ),
    ),
    kind: str | None = typer.Option(None, "--format", help="Format if needed."),
    account: str | None = typer.Option(None, "--account", help=_ACCOUNT_HELP),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Choose the repository or mirror used when --target is omitted."""
    account_ref = _context_account_ref(profile, account)
    profile_name, selected_account = ensure_active_account(profile, account_ref)
    _, _, selected = resolve_target(
        target,
        profile=profile_name,
        account_ref=selected_account.account_ref,
        kind=kind,
    )
    try:
        cfg_mod.set_selected_artifact_target(
            selected,
            profile=profile_name,
            account_ref=selected_account.account_ref,
        )
    except ValueError as exc:
        output.fatal(str(exc))
    kind_suffix = f" ({selected.format})" if selected.format else ""
    output.success(
        f"Selected '{selected.display_selector}'{kind_suffix} for "
        f"{account_display_name(selected_account)}."
    )


@app.command("status")
def target_status(
    account: str | None = typer.Option(None, "--account", help=_ACCOUNT_HELP),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Show the account, repository or mirror, and format that commands use."""
    selection = inspect_selection(profile, account)
    selected = selection.target
    account_name = selection_account_display(selection)
    output.kv(
        {
            "Account": (
                with_source(account_name, selection.account_source)
                if account_name
                else "not selected"
            ),
            "Target": selected.display_selector if selected else "not selected",
            "Format": selected.format if selected else None,
        },
        title="Artifacts status",
        json_values={
            "account": account_json(selection),
            **(target_json(selected) or {"target": None, "type": None, "format": None}),
        },
    )


@app.command("clear")
def target_clear(
    account: str | None = typer.Option(None, "--account", help=_ACCOUNT_HELP),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Clear the selected repository or mirror without signing out."""
    account_ref = _context_account_ref(profile, account)
    profile_name, selected_account = ensure_active_account(profile, account_ref)
    try:
        cfg_mod.set_selected_artifact_target(
            None,
            profile=profile_name,
            account_ref=selected_account.account_ref,
        )
    except ValueError as exc:
        output.fatal(str(exc))
    output.success(
        f"Cleared the repository or mirror for {account_display_name(selected_account)}."
    )


def _require_kind(kind: str) -> cfg_mod.Format:
    if kind not in FORMATS:
        output.fatal(f"Unknown format '{kind}'. Use: pypi, npm, maven, oci")
    return cast("cfg_mod.Format", kind)


def _require_package_kind(kind: str) -> cfg_mod.Format:
    if kind not in _PACKAGE_KINDS:
        output.fatal(
            "Package/version commands and private mirrors support only pypi, npm, "
            "and maven. Use rvs docker, rvs helm, or rvs oras for OCI content."
        )
    return cast("cfg_mod.Format", kind)


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


def _package_lifecycle_columns(
    package_format: str,
    version: dict,
) -> tuple[list[str], list[str]]:
    """Return the lifecycle column heading and value of a version for its format."""
    if package_format == "pypi":
        return ["Yanked"], [_yes_no(version.get("yanked"))]
    if package_format == "npm":
        return ["Deprecated"], [_yes_no(version.get("deprecated"))]
    return [], []


def _split_repo_ref(repo_ref: str) -> tuple[str | None, str]:
    value = repo_ref.strip().removeprefix(cfg_mod.REPOSITORY_TARGET_PREFIX).strip().strip("/")
    if not value:
        output.fatal("Repository name cannot be empty.")
    if "/" not in value:
        return None, value
    namespace_selector, repository_name = value.split("/", 1)
    namespace_selector = namespace_selector.strip()
    repository_name = repository_name.strip().strip("/")
    if not namespace_selector or not repository_name or "/" in repository_name:
        output.fatal("Repository must be <repository-name> or <namespace>/<repository-name>.")
    return namespace_selector, repository_name


def _selected_account_handle(
    profile: str | None,
    account_ref: str,
    repositories: list[dict],
) -> str:
    for repository in repositories:
        if repository["account"]["ref"] == account_ref:
            return payload_display_name(repository["account"])

    profile_name, selected = ensure_active_account(profile, account_ref)
    if selected.account_handle:
        return account_display_name(selected)
    return payload_display_name(resolve_account(account_ref, profile_name))


def _root_package_options() -> dict[str, str | None]:
    context = click.get_current_context(silent=True)
    if context is None:
        return {}
    params = context.params
    return {
        "target": params.get("target") or params.get("repo"),
        "account": params.get("account"),
        "kind": params.get("kind"),
        "profile": params.get("profile"),
    }


def _resolve_repository_entry(
    repo: str,
    profile: str | None,
    *,
    kind: str | None = None,
    account_ref: str | None = None,
) -> dict:
    """Resolve a repository selector to its API ``Repository``."""
    candidate = repo.strip().strip("/")
    if not candidate:
        output.fatal("Repository selector cannot be empty.")
    if "/" in candidate or ":" in candidate:
        spec = parse_target(candidate)
        if spec.target_type != "repository":
            output.fatal("Repository commands require a repository target.")
        selector = spec.selector
    else:
        # Repository-management commands also accept a bare repository name or
        # ref. Saved artifact targets require the complete namespace pair.
        selector = candidate
    options = _root_package_options()
    profile = profile or options.get("profile")
    try:
        return resolve_repository_entry(
            _client(profile), selector, _account_ref(profile, account_ref), kind
        )
    except ApiError as exc:
        output.fatal(str(exc))


def _package_lane(
    repo: str | None,
    kind: str | None,
    profile: str | None,
    *,
    only: str | None = None,
    operation: str | None = None,
) -> tuple[str, cfg_mod.Format, str]:
    """Return the repository ref, package format, and display name to act on.

    Without ``--target`` the target chosen with ``rvs art select`` is used. The
    format comes from ``--format``, the only format a command supports
    (``only``), the selection, or the repository's single package format.
    """
    if kind is not None:
        _require_package_kind(kind)
        if only is not None and kind != only:
            output.fatal(f"{operation} is supported only for {only} packages.")
    account_ref: str | None = None
    saved_kind: str | None = None
    if repo is None:
        profile_name, _ = _profile(profile or _root_package_options().get("profile"))
        account_ref = _account_ref(profile_name)
        saved = cfg_mod.selected_artifact_target(profile_name, account_ref)
        if saved is None:
            output.fatal("No repository is selected. Pass --target or run `rvs art select`.")
        if saved.target_type != "repository":
            output.fatal(
                f"'{saved.display_selector}' is a private mirror; package commands need "
                "a private repository. Pass --target."
            )
        repo = saved.stable_selector
        saved_kind = saved.format
    # Only an explicit --format filters the lookup, so a repository without the
    # format a command implies is reported as such rather than as not found.
    repository = _resolve_repository_entry(repo, profile, kind=kind, account_ref=account_ref)
    display = repository_target_name(repository)
    formats = [item for item in repository_formats(repository) if item in _PACKAGE_KINDS]
    wanted = kind or only
    if wanted is not None:
        if wanted not in formats:
            reason = f" {operation} applies only to {only} packages." if only else ""
            output.fatal(f"'{display}' has no {wanted} format.{reason}")
        return repository["ref"], cast("cfg_mod.Format", wanted), display
    if saved_kind in formats:
        return repository["ref"], cast("cfg_mod.Format", saved_kind), display
    if not formats:
        output.fatal(
            f"'{display}' has no pypi, npm, or maven format. Use rvs docker, rvs helm, "
            "or rvs oras for OCI content."
        )
    if len(formats) > 1:
        output.fatal(
            f"'{display}' has several package formats; pass --format {' | '.join(formats)}."
        )
    return repository["ref"], cast("cfg_mod.Format", formats[0]), display


def _format_path(repository_ref: str, package_format: str, suffix: str) -> str:
    return artifacts_path(
        f"repositories/{segment(repository_ref)}/formats/{package_format}/{suffix}"
    )


def _remote_cache_path(remote_cache_ref: str) -> str:
    return artifacts_path(f"remote-caches/{segment(remote_cache_ref)}")


# ── repo ─────────────────────────────────────────────────────────────────────


# Actions only the Admin level adds on top of Maintainer.
_ADMIN_ACTIONS = frozenset({"repository.delete", "repository.settings.write"})
# Actions the Maintainer level adds on top of Publisher.
_MAINTAINER_ACTIONS = frozenset(
    {
        "content.delete",
        "upstream.write",
        "repository.write",
        "repository.security.write",
        "repository.access.write",
        "repository.metrics.read",
    }
)


def _repository_access_label(repository: dict) -> str:
    """Name the highest repository level the caller's actions reach."""
    actions = set(repository["allowed_actions"])
    if actions & _ADMIN_ACTIONS:
        return "Admin"
    if actions & _MAINTAINER_ACTIONS:
        return "Maintainer"
    if "content.publish" in actions:
        return "Publisher"
    if "content.read" in actions:
        return "Reader"
    return "Unknown"


@repo_app.command("list")
def repo_list(
    account: str | None = typer.Option(None, "--account"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    account_ref: str | None = typer.Option(
        None, "--account-ref", help="Typed account reference.", hidden=True
    ),
    kind: str | None = typer.Option(
        None,
        "--format",
        "-f",
        help="Format: pypi | npm | maven | oci.",
    ),
) -> None:
    """List repositories in the selected account's namespaces."""
    if kind:
        _require_kind(kind)
    client = _client(profile)
    selected_account_ref = _account_ref(profile, account_ref)
    params = {
        key: value
        for key, value in {
            "account_ref": selected_account_ref,
            "format": kind,
        }.items()
        if value is not None
    }
    try:
        items = collection_all(client, artifacts_path("repositories"), params)
    except (ApiError, ValueError) as exc:
        output.fatal(str(exc))

    if output.is_json():
        account_handle = (
            _selected_account_handle(profile, selected_account_ref, items) if items else ""
        )
        output.table(
            ["Account", "Namespace", "Repository", "ID-based target", "Formats", "Access"],
            [
                [
                    account_handle,
                    item["namespace"]["name"],
                    item["name"],
                    f"in/{item['ref']}",
                    ", ".join(repository_formats(item)),
                    _repository_access_label(item),
                ]
                for item in items
            ],
            json_keys=[
                "account",
                "namespace",
                "repository",
                "id_based_target",
                "formats",
                "access",
            ],
        )
        return

    account_handle = _selected_account_handle(profile, selected_account_ref, items)
    output.kv({"Account": account_handle})
    if not items:
        output.info("No repositories found.")
        return

    output.table(
        ["Repository", "ID-based target", "Formats", "Access"],
        [
            [
                repository_display_name(item),
                f"in/{item['ref']}",
                ", ".join(repository_formats(item)),
                _repository_access_label(item),
            ]
            for item in items
        ],
    )


@repo_app.command("create")
def repo_create(
    account: str | None = typer.Option(None, "--account"),
    name: str = typer.Argument(..., help="Repository name or namespace/repository."),
    kind: list[str] = typer.Option(
        ...,
        "--format",
        "-f",
        help="Formats to enable, comma-separated or with repeated --format flags.",
    ),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    account_ref: str | None = typer.Option(
        None, "--account-ref", help="Typed owner account reference.", hidden=True
    ),
) -> None:
    """Create a repository."""
    namespace_selector, repository_name = _split_repo_ref(name)
    try:
        kinds = cast("list[cfg_mod.Format]", flatten_formats(kind))
    except ValueError as exc:
        output.fatal(str(exc))
    client = _client(profile)
    try:
        selected_account_ref = _account_ref(profile, account_ref)
        namespaces = collection_all(
            client, platform_path("namespaces"), {"account_ref": selected_account_ref}
        )
        matches = [
            namespace
            for namespace in namespaces
            if namespace["account"]["ref"] == selected_account_ref
            and (
                (namespace["is_default"] and namespace["realm"] == "internal")
                if namespace_selector is None
                else (
                    namespace["ref"] == namespace_selector
                    or namespace["name"].casefold() == namespace_selector.casefold()
                )
            )
        ]
        if len(matches) != 1:
            output.fatal(
                "Select an accessible namespace with namespace/repository. "
                "If this account has no namespace, finish onboarding in the web app."
            )
        payload = {
            "namespace_ref": matches[0]["ref"],
            "name": repository_name,
            "formats": kinds,
        }
        repo = client.post(artifacts_path("repositories"), json=payload).json()
    except (ApiError, ValueError) as exc:
        output.fatal(str(exc))

    output.success(f"Created repository '{repo['name']}' with formats: {', '.join(kinds)}.")


@repo_app.command("show")
def repo_show(
    account: str | None = typer.Option(None, "--account"),
    repo: str = typer.Argument(..., help=_REPOSITORY_NAME_HELP),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Show repository details."""
    try:
        item = _resolve_repository_entry(repo, profile)
    except ApiError as exc:
        output.fatal(str(exc))

    account_handle = payload_display_name(item["account"])
    repository_name = item["name"]
    namespace = item["namespace"]
    id_based_target = f"in/{item['ref']}"
    formats = ", ".join(repository_formats(item))
    totals = item["totals"]
    packages = str(totals["package_count"])
    versions = str(totals["version_count"])
    oci_paths = str(totals.get("oci_path_count", 0))
    manifests = str(totals.get("manifest_count", 0))
    storage_bytes = str(totals["storage_bytes"])
    created = str(item["created_at"])
    if output.is_json():
        output.kv(
            {
                "Name": repository_name,
                "Account": account_handle,
                "Namespace": namespace["name"],
                "Namespace permanent ID": namespace["ref"],
                "Repository permanent ID": item["ref"],
                "Formats": formats,
                "Packages": packages,
                "Versions": versions,
                "OCI paths": oci_paths,
                "Manifests": manifests,
                "Storage bytes": storage_bytes,
                "Created": created,
            },
            title=f"Repository {repo}",
            json_keys=[
                "name",
                "account",
                "namespace",
                "namespace_id",
                "repository_id",
                "formats",
                "packages",
                "versions",
                "oci_paths",
                "manifests",
                "storage_bytes",
                "created",
            ],
        )
        return

    output.kv(
        {
            "Repository": repository_display_name(item),
            "Account": account_handle,
            "ID-based target": id_based_target,
            "Formats": formats,
            "Packages": packages,
            "Versions": versions,
            "OCI paths": oci_paths,
            "Manifests": manifests,
            "Storage bytes": storage_bytes,
            "Created": created,
        },
        title=f"Repository {repository_display_name(item)}",
    )


def _upstream_path(repository_ref: str, package_format: str) -> str:
    return _format_path(repository_ref, package_format, "upstreams")


def _upstream_source_label(source: dict) -> str:
    if source.get("namespace_name"):
        return f"{source['namespace_name']}/{source['display_name']}"
    return str(source["display_name"])


def _print_upstreams(items: list[dict]) -> None:
    output.table(
        ["Position", "Type", "Source", "Source ref", "Minimum age", "Maximum age"],
        [
            [
                str(item["position"]),
                str(item["source"]["kind"]),
                _upstream_source_label(item["source"]),
                # A remote cache the caller cannot see has no public reference.
                str(item["source"]["ref"] or "-"),
                _format_age_hours(item.get("min_age_hours"), missing="No minimum"),
                _format_age_hours(item.get("max_age_hours"), missing="No maximum"),
            ]
            for item in sorted(items, key=lambda value: int(value["position"]))
        ],
        title="Repository upstreams",
        json_keys=["position", "type", "source", "source_ref", "min_age_hours", "max_age_hours"],
    )


@upstream_app.command("list")
def upstream_list(
    account: str | None = typer.Option(None, "--account"),
    repository: str = typer.Argument(...),
    format: str = typer.Argument(..., metavar="FORMAT"),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """List the package sources used by one repository format, by position."""
    package_format = _require_package_kind(format)
    client = _client(profile)
    try:
        destination = _resolve_repository_entry(repository, profile, kind=package_format)
        items = collection_all(client, _upstream_path(destination["ref"], package_format))
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        output.fatal(str(exc))
    _print_upstreams(items)


# ── private mirrors and their remote caches ──────────────────────────────────


@remote_app.command("select")
def remote_select(
    mirror: str = typer.Argument(..., help="Official source slug or custom mirror name."),
    custom: bool = typer.Option(False, "--custom", help="Select a custom mirror."),
    kind: str | None = typer.Option(None, "--format", help="Package format if needed."),
    account: str | None = typer.Option(None, "--account", help=_ACCOUNT_HELP),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Select a Ravenstash-provided or custom private mirror."""
    prefix = "custom-mirror" if custom else "mirror"
    target_select(f"{prefix}:{mirror}", kind=kind, account=account, profile=profile)


@remote_app.command("list")
def remote_list(
    profile: str | None = typer.Option(None, "--profile", "-p"),
    account_ref: str | None = typer.Option(None, "--account-ref", hidden=True),
    account: str | None = typer.Option(None, "--account"),
    kind: str | None = typer.Option(None, "--format", "-f"),
) -> None:
    """List private mirrors for the selected account."""
    if kind:
        _require_package_kind(kind)
    client = _client(profile)
    effective_account_ref = _context_account_ref(profile, account) or _account_ref(
        profile, account_ref
    )
    params = {"account_ref": effective_account_ref}
    if kind:
        params["format"] = kind
    try:
        items = collection_all(client, artifacts_path("remote-caches"), params)
    except (ApiError, ValueError) as exc:
        output.fatal(str(exc))
    if not items:
        output.info("No private mirrors found.")
        return
    output.table(
        [
            "Account",
            "Remote-cache ref",
            "Source",
            "Publication",
            "Mirror",
            "Format",
            "Minimum age",
        ],
        [
            [
                payload_display_name(item["account"]),
                str(item["ref"]),
                str(item["source_type"]),
                "externally_controlled" if item["source_type"] == "official" else "unknown",
                remote_target_name(item),
                str(item["format"]),
                _format_age_hours(item.get("min_age_hours"), missing="No minimum"),
            ]
            for item in items
        ],
        json_keys=[
            "account",
            "remote_cache_ref",
            "source",
            "publication",
            "mirror",
            "format",
            "min_age_hours",
        ],
    )


@remote_app.command("create")
def remote_create(
    source: str | None = typer.Argument(None, help="Official source slug; defaults by format."),
    kind: str | None = typer.Option(None, "--format", help="Package format if needed."),
    min_age_hours: float | None = typer.Option(None, "--min-age-hours", min=0),
    max_age_hours: float | None = typer.Option(None, "--max-age-hours", min=0),
    select: bool = typer.Option(False, "--select", help="Select the mirror after creating it."),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    account: str | None = typer.Option(None, "--account"),
) -> None:
    """Create a Ravenstash-provided private mirror."""
    if source is None:
        if kind not in DEFAULT_OFFICIAL_SOURCES:
            output.fatal("Pass a SOURCE or --format pypi, npm, or maven.")
        source = DEFAULT_OFFICIAL_SOURCES[cast("PackageFormat", kind)]
    account_ref = _context_account_ref(profile, account) or _account_ref(profile)
    if kind:
        _require_package_kind(kind)
    client = _client(profile)
    try:
        sources = collection_all(
            client, artifacts_path("official-sources"), {"account_ref": account_ref}
        )
        matches = [
            item
            for item in sources
            if source == item["ref"] and (kind is None or item["format"] == kind)
        ]
        if not matches:
            output.fatal(f"Official mirror source '{source}' was not found.")
        if len(matches) > 1:
            output.fatal(f"Official mirror source '{source}' is ambiguous. Pass --format.")
        payload: dict[str, object] = {
            "account_ref": account_ref,
            "source_ref": matches[0]["ref"],
        }
        if min_age_hours is not None:
            payload["min_age_hours"] = min_age_hours
        if max_age_hours is not None:
            payload["max_age_hours"] = max_age_hours
        item = client.post(artifacts_path("remote-caches"), json=payload).json()
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        output.fatal(str(exc))
    target_name = remote_target_name(item)
    output.success(f"Added private mirror '{target_name}'.")
    if select:
        target_select(
            target_name,
            kind=str(item["format"]),
            account=account,
            profile=profile,
        )


@remote_app.command("show")
def remote_show(
    remote: str = typer.Argument(
        ..., help="Remote-cache permanent ID (rc_...).", metavar="REMOTE_CACHE_ID"
    ),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    kind: str | None = typer.Option(None, "--format", "-f", help="Require this format."),
) -> None:
    """Show a private mirror."""
    if kind:
        _require_package_kind(kind)
    client = _client(profile)
    try:
        item = client.get(_remote_cache_path(remote)).json()
    except ApiError as exc:
        output.fatal(str(exc))
    if kind and item.get("format") != kind:
        output.fatal(f"Private mirror '{remote}' is for {item.get('format')}, not {kind}.")
    output.kv(
        {
            "Remote-cache ref": item["ref"],
            "Type": item["source_type"],
            "Target": remote_target_name(item),
            "Format": item["format"],
            "Account ref": item["account"]["ref"],
            "Private mirror": "ready",
            "Mirror minimum package age": _format_age_hours(
                item.get("min_age_hours"), missing="No minimum"
            ),
            "Maximum package age": _format_age_hours(
                item.get("max_age_hours"), missing="No maximum"
            ),
        },
        title=f"Private mirror {remote}",
        json_keys=[
            "remote_cache_ref",
            "type",
            "target",
            "format",
            "account_ref",
            "private_mirror",
            "min_age_hours",
            "max_age_hours",
        ],
    )


# ── package ──────────────────────────────────────────────────────────────────


@package_app.command("list")
def package_list(
    account: str | None = typer.Option(None, "--account"),
    repo: str | None = typer.Option(None, "--target", "-t", help=_PACKAGE_TARGET_HELP),
    kind: str | None = typer.Option(None, "--format", "-f", help=_PACKAGE_FORMAT_HELP),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """List packages hosted in a package repository."""
    repository_ref, package_format, display = _package_lane(repo, kind, profile)
    client = _client(profile)
    try:
        items = collection_all(client, _format_path(repository_ref, package_format, "packages"))
    except (ApiError, ValueError) as exc:
        output.fatal(str(exc))

    if not items:
        output.info(f"No {package_format} packages found in '{display}'.")
        return

    output.table(
        ["Name", "Latest", "Versions", "Size", "Downloads", "Bandwidth"],
        [
            [
                item["name"],
                item.get("latest_version") or "",
                str(item.get("version_count", "")),
                str(item.get("total_size_bytes", "")),
                str(item.get("downloads", "0")),
                str(item.get("bandwidth_bytes", "0")),
            ]
            for item in items
        ],
        title=f"{package_format} packages in {display}",
        json_keys=["name", "latest", "versions", "size", "downloads", "bandwidth"],
    )


_DEFAULT_VERSION_LIMIT = 50
_MAX_VERSION_LIMIT = 100
# Formats whose packages have tags; npm calls them distribution tags.
_TAGGED_PACKAGE_FORMATS = ("npm",)


def _package_tags(item: dict) -> object:
    return item.get("tags")


def _tags_label(tags: object, details: list[dict] | None = None) -> str:
    """Show effective tags as ``tag=version``, noting tags that do not resolve
    to their stored version in this repository and tags an upstream defines."""
    if details:
        return ", ".join(_tag_detail_label(entry) for entry in details)
    if not isinstance(tags, dict):
        return ""
    order = sorted(tags.items(), key=lambda item: (item[0] != _LATEST_TAG, item[0]))
    return ", ".join(f"{tag}={version}" for tag, version in order)


_HIDDEN_REASONS = {"in_trash": "in the trash", "not_available": "not installable here"}
_REPOSITORY_TAG_SOURCE = "repository"
# Where a tag's version is defined: the repository itself, or one of its upstreams.
_TAG_SOURCES = {
    _REPOSITORY_TAG_SOURCE: "this repository",
    "upstream_repository": "upstream repository",
    "remote_cache": "remote cache",
    "upstream": "upstream",
}


def _tag_state(entry: dict) -> str:
    """The tag's state, with why its stored version is not installable."""
    state = str(entry.get("state") or "")
    reason = _HIDDEN_REASONS.get(str(entry.get("hidden_reason") or ""))
    return f"{state}: {reason}" if reason and state in {"fallback", "hidden"} else state


def _tag_source(entry: dict) -> str:
    """The source kind of a tag; an answer without one lists the repository's own."""
    source = entry.get("source")
    if isinstance(source, str) and source:
        return source
    return "upstream" if entry.get("read_only") else _REPOSITORY_TAG_SOURCE


def _tag_source_label(entry: dict) -> str:
    source = _tag_source(entry)
    return _TAG_SOURCES.get(source, source)


def _tag_source_cell(entry: dict) -> str:
    """The source with the upstream's position, as `repo upstream list` shows it."""
    label = _tag_source_label(entry)
    position = entry.get("upstream_position")
    return f"{label} (position {position})" if isinstance(position, int) else label


def _tag_detail_label(entry: dict) -> str:
    tag = str(entry.get("tag", ""))
    version = entry.get("version")
    stored = entry.get("stored_version")
    details: list[str] = []
    if entry.get("state") in {"fallback", "hidden"}:
        details.append(_tag_state(entry))
        if stored:
            details.append(f"set to {stored}")
    if _tag_source(entry) != _REPOSITORY_TAG_SOURCE:
        details.append(f"from {_tag_source_label(entry)}")
    label = f"{tag}={version or '-'}"
    return f"{label} ({'; '.join(details)})" if details else label


def _read_package_tags(
    client: ApiClient, repository_ref: str, package_format: str, name: str
) -> list[dict] | None:
    """Read the tag detail best effort; the package summary still lists tags."""
    if package_format not in _TAGGED_PACKAGE_FORMATS:
        return None
    try:
        return collection_all(
            client,
            _format_path(repository_ref, package_format, "package/tags"),
            {"package_name": name},
        )
    except ApiError, ValueError:
        return None


def _version_tags_label(entry: dict) -> str:
    tags = entry.get("tags")
    return ", ".join(str(tag) for tag in tags) if isinstance(tags, list) else ""


def _digests_label(digests: object) -> str:
    """Show every digest as ``algorithm:hex``; algorithms are an open set."""
    if not isinstance(digests, dict):
        return ""
    order = sorted(digests, key=lambda algorithm: (algorithm != "sha256", str(algorithm)))
    return "\n".join(f"{algorithm}:{digests[algorithm]}" for algorithm in order)


@package_app.command("show")
def package_show(
    account: str | None = typer.Option(None, "--account"),
    name: str = typer.Argument(..., help="Package name."),
    repo: str | None = typer.Option(None, "--target", "-t", help=_PACKAGE_TARGET_HELP),
    kind: str | None = typer.Option(None, "--format", "-f", help=_PACKAGE_FORMAT_HELP),
    version: str | None = typer.Option(
        None, "--version", help="Show one version with its files and digests."
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        min=1,
        max=_MAX_VERSION_LIMIT,
        help=f"Newest versions to list (default {_DEFAULT_VERSION_LIMIT}).",
    ),
    all_versions: bool = typer.Option(
        False, "--all-versions", help="List every version instead of only the newest."
    ),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Show a package summary and its newest versions, or one version in detail."""
    if version is not None and (limit is not None or all_versions):
        output.fatal("--version cannot be combined with --limit or --all-versions.")
    if limit is not None and all_versions:
        output.fatal("--limit and --all-versions are mutually exclusive.")
    repository_ref, package_format, _ = _package_lane(repo, kind, profile)
    client = _client(profile)
    try:
        if version is not None:
            coordinate = {"package_name": name, "version": version}
            detail = client.get(
                _format_path(repository_ref, package_format, "package/version"),
                params=coordinate,
            ).json()
            # A version's files are their own filename-ordered collection.
            files = collection_all(
                client,
                _format_path(repository_ref, package_format, "package/version/files"),
                coordinate,
            )
        else:
            item = client.get(
                _format_path(repository_ref, package_format, "package"),
                params={"package_name": name},
            ).json()
            versions = read_collection(
                client,
                _format_path(repository_ref, package_format, "package/versions"),
                {"package_name": name},
                max_items=None if all_versions else limit or _DEFAULT_VERSION_LIMIT,
            )
    except (ApiError, ValueError) as exc:
        output.fatal(str(exc))
    tag_details = (
        None
        if version is not None
        else _read_package_tags(client, repository_ref, package_format, name)
    )

    if version is not None:
        _print_package_version(repository_ref, name, package_format, detail, files)
        return

    # A cursor may be returned even when no version follows; the count settles it.
    version_count = item.get("version_count")
    more_versions = versions.next_cursor is not None and not (
        isinstance(version_count, int) and len(versions.items) >= version_count
    )
    if output.is_json():
        # One document with the wire fields; the cursor is null once every version is listed.
        click.echo(
            json.dumps(
                {
                    "repository": repository_ref,
                    "package": item,
                    "versions": versions.items,
                    "versions_next_cursor": versions.next_cursor if more_versions else None,
                    **({"tags": tag_details} if tag_details is not None else {}),
                }
            )
        )
        return

    output.kv(
        {
            "Repository": repository_ref,
            "Name": item["name"],
            "Status": str(item.get("status") or "active"),
            "Status reason": item.get("status_reason") or "",
            "Latest": item.get("latest_version") or "",
            "Latest stable": item.get("latest_stable_version") or "",
            "Versions": str(item.get("version_count", "")),
            "Size": str(item.get("total_size_bytes", "")),
            "Last upload": str(item.get("latest_uploaded_at") or ""),
            "Tags": _tags_label(_package_tags(item), tag_details),
        },
        title=name,
    )

    if versions.items:
        lifecycle_headings, _ = _package_lifecycle_columns(package_format, versions.items[0])
        tagged = any(entry.get("tags") for entry in versions.items)
        output.table(
            [
                "Version",
                *(["Tags"] if tagged else []),
                *lifecycle_headings,
                "Published",
                "Files",
                "Size",
                "Downloads",
            ],
            [
                [
                    str(entry.get("version", "")),
                    *([_version_tags_label(entry)] if tagged else []),
                    *_package_lifecycle_columns(package_format, entry)[1],
                    str(entry.get("published_at") or ""),
                    str(entry.get("file_count", "")),
                    str(entry.get("size_bytes", "")),
                    str(entry.get("downloads", "0")),
                ]
                for entry in versions.items
            ],
            title="Versions (newest first)",
        )
    if more_versions:
        output.warn(
            f"Showing the newest {len(versions.items)} of {version_count} versions. "
            "Pass --all-versions to list every version or --version VERSION for one version."
        )


def _print_package_version(
    repository_ref: str, name: str, package_format: str, detail: dict, files: list[dict]
) -> None:
    if output.is_json():
        click.echo(
            json.dumps({"repository": repository_ref, "package": name, **detail, "files": files})
        )
        return
    lifecycle: dict[str, str | None] = {}
    if package_format == "pypi":
        lifecycle = {
            "Yanked": _yes_no(detail.get("yanked")),
            "Yank reason": detail.get("yanked_reason") or "",
        }
    elif package_format == "npm":
        lifecycle = {
            "Deprecated": _yes_no(detail.get("deprecated")),
            "Deprecation message": detail.get("deprecated_reason") or "",
        }
    keywords = detail.get("keywords")
    output.kv(
        {
            "Repository": repository_ref,
            "Package": name,
            "Version": str(detail.get("version", "")),
            **({"Tags": _version_tags_label(detail)} if detail.get("tags") else {}),
            "Published": str(detail.get("published_at") or ""),
            **lifecycle,
            "Summary": detail.get("summary") or "",
            "License": detail.get("license") or "",
            "Home page": detail.get("home_page") or "",
            "Keywords": ", ".join(str(word) for word in keywords)
            if isinstance(keywords, list)
            else "",
            "Files": str(detail.get("file_count", len(files))),
            "Size": str(detail.get("size_bytes", "")),
            "Downloads": str(detail.get("downloads", "0")),
        },
        title=f"{name} {detail.get('version', '')}",
    )
    if files:
        output.table(
            ["File", "Size", "Published", "Digests"],
            [
                [
                    str(entry.get("filename", "")),
                    str(entry.get("size_bytes", "")),
                    str(entry.get("published_at") or ""),
                    _digests_label(entry.get("digests")),
                ]
                for entry in files
            ],
            title="Files",
        )


@package_app.command("delete-version")
def package_delete_version(
    account: str | None = typer.Option(None, "--account"),
    name: str = typer.Argument(..., help="Package name."),
    version: str = typer.Argument(..., help="Version to move to the trash."),
    repo: str | None = typer.Option(None, "--target", "-t", help=_PACKAGE_TARGET_HELP),
    kind: str | None = typer.Option(None, "--format", "-f", help=_PACKAGE_FORMAT_HELP),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Move one package version to the repository trash.

    It stops being installable, and the same version cannot be published again
    until it is permanently deleted. Restore it in the web app until then.
    """
    repository_ref, package_format, display = _package_lane(repo, kind, profile)
    if not yes:
        typer.confirm(
            f"Move {package_format} package version {name}@{version} in '{display}' to trash? "
            "It stops being installable and this version can't be published again until "
            "it is permanently deleted. You can restore it in the web app until then.",
            abort=True,
        )
    client = _client(profile)
    try:
        client.delete(
            _format_path(repository_ref, package_format, "package/version"),
            params={"package_name": name, "version": version},
        )
    except ApiError as exc:
        output.fatal(str(exc))
    output.success(moved_to_trash(f"{name}@{version}", f"'{display}'"))


def _update_package_version(
    client: ApiClient,
    repository_ref: str,
    package_format: str,
    name: str,
    version: str,
    body: dict[str, object],
) -> None:
    client.patch(
        _format_path(repository_ref, package_format, "package/version"),
        params={"package_name": name, "version": version},
        json=body,
    )


@package_app.command("yank")
def package_yank(
    account: str | None = typer.Option(None, "--account"),
    name: str = typer.Argument(..., help="Package name."),
    version: str = typer.Argument(..., help="Version to yank."),
    repo: str | None = typer.Option(None, "--target", "-t", help=_PACKAGE_TARGET_HELP),
    kind: str | None = typer.Option(None, "--format", "-f", hidden=True),
    reason: str | None = typer.Option(None, "--reason", "-m", help="Yank reason."),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Mark a package version as yanked."""
    repository_ref, package_format, display = _package_lane(
        repo, kind, profile, only="pypi", operation="Yanking a package version"
    )
    client = _client(profile)
    body: dict[str, object] = {"yanked": True}
    if reason is not None:
        body["yanked_reason"] = reason
    try:
        _update_package_version(client, repository_ref, package_format, name, version, body)
    except ApiError as exc:
        output.fatal(str(exc))
    output.success(f"Yanked {name}@{version} in '{display}'.")


@package_app.command("unyank")
def package_unyank(
    account: str | None = typer.Option(None, "--account"),
    name: str = typer.Argument(..., help="Package name."),
    version: str = typer.Argument(..., help="Version to unyank."),
    repo: str | None = typer.Option(None, "--target", "-t", help=_PACKAGE_TARGET_HELP),
    kind: str | None = typer.Option(None, "--format", "-f", hidden=True),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Make a yanked PyPI package version selectable again."""
    repository_ref, package_format, display = _package_lane(
        repo, kind, profile, only="pypi", operation="Unyanking a package version"
    )
    client = _client(profile)
    try:
        _update_package_version(
            client, repository_ref, package_format, name, version, {"yanked": False}
        )
    except ApiError as exc:
        output.fatal(str(exc))
    output.success(f"Unyanked {name}@{version} in '{display}'.")


@package_app.command("deprecate")
def package_deprecate(
    account: str | None = typer.Option(None, "--account"),
    name: str = typer.Argument(..., help="Package name."),
    version: str = typer.Argument(..., help="Version to deprecate."),
    repo: str | None = typer.Option(None, "--target", "-t", help=_PACKAGE_TARGET_HELP),
    kind: str | None = typer.Option(None, "--format", "-f", hidden=True),
    message: str = typer.Option(
        ...,
        "--message",
        "-m",
        help="Deprecation message shown by npm clients.",
    ),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Attach a warning message to an npm package version."""
    normalized_message = message.strip()
    if not normalized_message:
        output.fatal("--message cannot be empty. Use undeprecate to clear the message.")
    repository_ref, package_format, display = _package_lane(
        repo, kind, profile, only="npm", operation="Deprecating a package version"
    )
    client = _client(profile)
    try:
        _update_package_version(
            client,
            repository_ref,
            package_format,
            name,
            version,
            {"deprecated": True, "deprecated_reason": normalized_message},
        )
    except ApiError as exc:
        output.fatal(str(exc))
    output.success(f"Deprecated {name}@{version} in '{display}'.")


@package_app.command("undeprecate")
def package_undeprecate(
    account: str | None = typer.Option(None, "--account"),
    name: str = typer.Argument(..., help="Package name."),
    version: str = typer.Argument(..., help="Version to undeprecate."),
    repo: str | None = typer.Option(None, "--target", "-t", help=_PACKAGE_TARGET_HELP),
    kind: str | None = typer.Option(None, "--format", "-f", hidden=True),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """Clear the warning message from an npm package version."""
    repository_ref, package_format, display = _package_lane(
        repo, kind, profile, only="npm", operation="Undeprecating a package version"
    )
    client = _client(profile)
    try:
        _update_package_version(
            client, repository_ref, package_format, name, version, {"deprecated": False}
        )
    except ApiError as exc:
        output.fatal(str(exc))
    output.success(f"Undeprecated {name}@{version} in '{display}'.")


# ── package tag ──────────────────────────────────────────────────────────────

_LATEST_TAG = "latest"
_TAG_FORMAT_HELP = "Package format; package tags are available for npm."
_TAG_CONFLICT_HINTS = {
    "revision_mismatch": "The tag changed since you read it. Run `rvs art package tag list` "
    "and retry with its current --expect-revision.",
    "target_unavailable": "That version is not installable in this repository, for example "
    "because it is in the trash or held for checking.",
    "latest_required": "Every npm package keeps a `latest` tag while it has versions; move "
    "it with `rvs art package tag set` instead.",
    "tag_limit": "The package already has the maximum number of tags; delete one first.",
    "read_only_upstream": "`rvs art package tag list` shows where each tag comes from.",
}


def _tag_lane(
    repo: str | None, kind: str | None, profile: str | None, operation: str
) -> tuple[str, cfg_mod.Format, str]:
    return _package_lane(repo, kind, profile, only=_TAGGED_PACKAGE_FORMATS[0], operation=operation)


def _tag_error(exc: ApiError) -> str:
    reason = exc.detail.get("reason") if isinstance(exc.detail, dict) else None
    hint = _TAG_CONFLICT_HINTS.get(reason) if isinstance(reason, str) else None
    if not hint:
        return str(exc)
    # The API message is a sentence without its final period.
    message = str(exc).rstrip()
    if not message.endswith((".", "!", "?")):
        message += "."
    return f"{message} {hint}"


def _require_tag_name(tag: str) -> None:
    # HTTP clients resolve dot segments, so `..` would address another route.
    if not is_path_segment(tag):
        output.fatal("A package tag cannot be empty, '.', or '..'.")


@package_tag_app.command("list")
def package_tag_list(
    account: str | None = typer.Option(None, "--account", help=_ACCOUNT_HELP),
    name: str = typer.Argument(..., help="Package name."),
    repo: str | None = typer.Option(None, "--target", "-t", help=_PACKAGE_TARGET_HELP),
    kind: str | None = typer.Option(None, "--format", "-f", help=_TAG_FORMAT_HELP),
    profile: str | None = typer.Option(None, "--profile", "-p"),
) -> None:
    """List a package's tags as installs in this repository resolve them.

    The list holds the repository's own tags and the tags its upstreams define.
    An upstream's tag is read-only here and can only be changed in its source.
    A tag whose version is not installable here is marked: `latest` falls back
    to the best installable version, and other tags are hidden until it is.
    """
    repository_ref, package_format, display = _tag_lane(repo, kind, profile, "Listing package tags")
    client = _client(profile)
    try:
        items = collection_all(
            client,
            _format_path(repository_ref, package_format, "package/tags"),
            {"package_name": name},
        )
    except ApiError as exc:
        output.fatal(_tag_error(exc))
    except ValueError as exc:
        output.fatal(str(exc))
    if output.is_json():
        click.echo(json.dumps({"repository": repository_ref, "package": name, "items": items}))
        return
    if not items:
        output.info(f"{name} has no tags in '{display}'.")
        return
    output.table(
        ["Tag", "Version", "State", "Source", "Read-only", "Stored", "Revision"],
        [
            [
                str(item.get("tag", "")),
                str(item.get("version") or "-"),
                _tag_state(item),
                _tag_source_cell(item),
                _yes_no(item.get("read_only")),
                str(item.get("stored_version") or ""),
                "" if item.get("revision") is None else str(item["revision"]),
            ]
            for item in items
        ],
        title=f"Tags of {name} in {display}",
    )


@package_tag_app.command("set")
def package_tag_set(
    account: str | None = typer.Option(None, "--account", help=_ACCOUNT_HELP),
    name: str = typer.Argument(..., help="Package name."),
    tag: str = typer.Argument(..., help="Tag to create or move, such as latest or beta."),
    version: str = typer.Argument(..., help="Version the tag points at."),
    repo: str | None = typer.Option(None, "--target", "-t", help=_PACKAGE_TARGET_HELP),
    kind: str | None = typer.Option(None, "--format", "-f", help=_TAG_FORMAT_HELP),
    expect_revision: int | None = typer.Option(
        None,
        "--expect-revision",
        min=1,
        help="Change the tag only if its revision (see `tag list`) is still this one.",
    ),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Create TAG or move it to VERSION.

    Package tags move, unlike OCI tags. Moving `latest` changes what installs
    without a version or tag resolve to, so it asks for confirmation. A tag
    points only at the repository's own versions: a version or a package that
    comes from an upstream is refused.
    """
    _require_tag_name(tag)
    repository_ref, package_format, display = _tag_lane(
        repo, kind, profile, "Changing package tags"
    )
    if tag == _LATEST_TAG:
        confirm_question(
            f"Point '{_LATEST_TAG}' of {name} in '{display}' at {version}? Installs "
            "without a version or tag then get this version.",
            yes=yes,
            skip_option="--yes",
        )
    client = _client(profile)
    try:
        payload = client.put(
            _format_path(repository_ref, package_format, f"package/tags/{segment(tag)}"),
            json={"version": version, "expected_revision": expect_revision},
            params={"package_name": name},
        ).json()
    except ApiError as exc:
        output.fatal(_tag_error(exc))
    if output.is_json():
        click.echo(json.dumps({"repository": repository_ref, "package": name, **payload}))
        return
    result = payload.get("result")
    target = f"{name}@{payload.get('version', version)}"
    revision = payload.get("revision")
    if result == "created":
        output.success(f"Created tag '{tag}' at {target} in '{display}' (revision {revision}).")
    elif result == "unchanged":
        output.success(f"Tag '{tag}' already points at {target} in '{display}'.")
    else:
        output.success(f"Moved tag '{tag}' to {target} in '{display}' (revision {revision}).")


@package_tag_app.command("delete")
def package_tag_delete(
    account: str | None = typer.Option(None, "--account", help=_ACCOUNT_HELP),
    name: str = typer.Argument(..., help="Package name."),
    tag: str = typer.Argument(..., help="Tag to delete."),
    repo: str | None = typer.Option(None, "--target", "-t", help=_PACKAGE_TARGET_HELP),
    kind: str | None = typer.Option(None, "--format", "-f", help=_TAG_FORMAT_HELP),
    expect_revision: int | None = typer.Option(
        None,
        "--expect-revision",
        min=1,
        help="Delete the tag only if its revision (see `tag list`) is still this one.",
    ),
    profile: str | None = typer.Option(None, "--profile", "-p"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Delete TAG at once; the version it pointed at is unchanged.

    Installs by the tag stop working unless an upstream supplies the same tag.
    A tag an upstream defines is read-only here and cannot be deleted. `latest`
    cannot be deleted while the package has versions, unless an upstream
    supplies some of them: installs then resolve that upstream's `latest`.
    """
    _require_tag_name(tag)
    repository_ref, package_format, display = _tag_lane(
        repo, kind, profile, "Changing package tags"
    )
    confirm_question(
        f"Delete tag '{tag}' of {name} in '{display}'? Installs by this tag stop working "
        "unless an upstream supplies the same tag.",
        yes=yes,
        skip_option="--yes",
    )
    client = _client(profile)
    try:
        client.delete(
            _format_path(repository_ref, package_format, f"package/tags/{segment(tag)}"),
            params={
                "package_name": name,
                **({"expected_revision": expect_revision} if expect_revision else {}),
            },
        )
    except ApiError as exc:
        output.fatal(_tag_error(exc))
    output.success(f"Deleted tag '{tag}' of {name} in '{display}'.")
