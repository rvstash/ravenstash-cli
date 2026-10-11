"""Read-only discovery shared by endpoint, reference, token and setup commands."""

from dataclasses import dataclass
from typing import cast

from .. import config as cfg
from ..account.commands import acting_account_ref, resolve_account
from ..api import artifacts_path, collection_all
from ..client import ApiClient
from ..oci.registry import normalized_registry_host
from ..oci.runner import _friendly_oci_root
from .formats import FORMATS
from .routing import CanonicalRouter
from .targets import (
    PackageFormat,
    _remote_target,
    _repository_target,
    matching_remote_caches,
    parse_target,
    remote_public_name,
    repository_formats,
    resolve_repository_entry,
    token_scope_hint,
)


@dataclass(frozen=True)
class Discovery:
    profile: str
    target: cfg.ArtifactTarget
    formats: tuple[str, ...]
    # Credential-free native path of the target, without outer slashes.
    native_path: str

    @property
    def oci_plain_http(self) -> bool:
        from urllib.parse import urlsplit

        registries = cfg.load().active_profile(self.profile).native_registries
        return urlsplit(registries.oci_registry_base_url).scheme == "http"

    def select_format(self, value: str | None, applicable: tuple[str, ...] = FORMATS) -> str:
        choices = tuple(item for item in self.formats if item in applicable)
        if value is not None:
            if value not in choices:
                # Only a repository format can be hidden by the token's scope.
                hidden = value in applicable and self.target.target_type == "repository"
                raise ValueError(
                    f"Format '{value}' is not enabled or applicable to this target."
                    + (token_scope_hint(value) if hidden else "")
                )
            return value
        if len(choices) != 1:
            raise ValueError("Select one enabled format with --format.")
        return choices[0]

    def endpoint(self, kind: str, access: str = "read") -> str:
        self.select_format(kind)
        if access not in {"read", "publish"}:
            raise ValueError("Endpoint access must be read or publish.")
        if access == "publish" and self.target.target_type != "repository":
            raise ValueError("Private mirrors are read-only.")
        registries = cfg.load().active_profile(self.profile).native_registries
        if kind in {"oci"}:
            return normalized_registry_host(registries.oci_registry_base_url)
        endpoints = registries.package(cast("PackageFormat", kind))
        base = (
            endpoints.push_base_url
            if access == "publish"
            else (
                endpoints.read_base_url
                if self.target.target_type == "repository"
                else endpoints.mirror_base_url
            )
        )
        router = CanonicalRouter()
        if kind == "pypi":
            route = router.pypi_index_url if access == "read" else router.pypi_upload_url
        elif kind == "maven":
            route = router.maven_repo_url if access == "read" else router.maven_upload_url
        else:
            route = router.npm_registry_url if access == "read" else router.npm_upload_registry_url
        return route(base, self.native_path)

    def reference(self, kind: str, operand: str | None = None) -> str:
        from ..oci.reference import qualify_reference

        self.select_format(kind, ("oci",))
        root = _friendly_oci_root(
            self.target.namespace_name_cache, self.target.repository_name_cache
        )
        return qualify_reference(f"{self.endpoint(kind)}/{root}", operand)

    def native_reference(self, kind: str, operand: str | None = None) -> str:
        """Return the immutable realm-qualified reference used by native clients."""

        from ..oci.reference import qualify_reference

        self.select_format(kind, ("oci",))
        root = self.native_path
        return qualify_reference(f"{self.endpoint(kind)}/{root}", operand)


def discover(
    target: str | None, profile: str | None, account: str | None, kind: str | None = None
) -> Discovery:
    profile_name = profile or cfg.current_profile_name()
    account_ref = (
        str(resolve_account(account, profile_name)["ref"])
        if account
        else acting_account_ref(profile_name)
    )
    if not account_ref:
        raise ValueError("No account is selected. Run rvs account switch.")
    if target is None:
        saved = cfg.selected_artifact_target(profile_name, account_ref)
        if saved is None:
            raise ValueError("No target is selected. Pass --target or run rvs art select.")
        target = saved.stable_selector
    spec = parse_target(target)
    client = ApiClient.from_profile(profile_name)
    if spec.target_type == "repository":
        repository = resolve_repository_entry(client, spec.selector, account_ref)
        selected = _repository_target(repository)
        formats = tuple(item for item in repository_formats(repository) if item in FORMATS)
        path = f"in/{selected.repository_ref}"
    else:
        params = {"account_ref": account_ref}
        if kind is not None:
            params["format"] = kind
        remotes = collection_all(client, artifacts_path("remote-caches"), params)
        matches = matching_remote_caches(remotes, spec.target_type, spec.selector)
        if len(matches) != 1:
            raise ValueError(
                "Mirror was not found or is ambiguous; specify --account and --format."
            )
        remote = matches[0]
        selected = _remote_target(remote, spec.target_type)
        formats = (str(remote["format"]),)
        prefix = "o" if spec.target_type == "official_cache" else "c"
        path = f"{prefix}/{remote_public_name(remote)}"
    if not formats:
        raise ValueError("The target has no enabled formats.")
    return Discovery(profile_name, selected, formats, path)
