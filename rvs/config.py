"""Config file management for rvs.

Config lives at ~/.rvs/config.toml and supports multiple named profiles.
The effective local profile is resolved by:
  1. --profile CLI flag  (highest priority)
  2. RVS_PROFILE environment variable
  3. default_profile key in config file  (default: "default")

Config file shape
-----------------
::

    schema_version = 1
    default_profile = "default"

    [profiles.default]
    api_url = "https://api.ravenstash.com"
    credential_store = "keyring"
    account_ref = "ac_23456789"
    credential_type = "expiring"
    credential_api_url = "https://api.ravenstash.com"
    expires_at = "2099-01-01T00:00:00+00:00"
    refresh_expires_at = "2099-01-02T00:00:00+00:00"

    [profiles.default.native_registries.pypi]
    read_base_url = "https://pypi.rvsta.sh"
    push_base_url = "https://push.pypi.rvsta.sh"
    mirror_base_url = "https://mirror.pypi.rvsta.sh"

    [profiles.default.native_registries.npm]
    read_base_url = "https://npm.rvsta.sh"
    push_base_url = "https://push.npm.rvsta.sh"
    mirror_base_url = "https://mirror.npm.rvsta.sh"

    [profiles.default.native_registries.maven]
    read_base_url = "https://maven.rvsta.sh"
    push_base_url = "https://push.maven.rvsta.sh"
    mirror_base_url = "https://mirror.maven.rvsta.sh"

    [profiles.default.native_registries.oci]
    registry_base_url = "https://oci.rvsta.sh"


Endpoint overrides from the process environment, ``~/.rvs/profiles.env``, or
an explicit ``RVS_ENV_FILE`` only seed a profile that is not saved yet. A saved
profile keeps its own API URL, optional ``repository_domain``, and discovered
native-registry endpoints; stored device credentials are bound to the Ravenstash API
URL that issued them (``credential_api_url``).

``schema_version`` numbers the stored shape independently of the rvs release
version. A file written with an older schema is upgraded when it is loaded,
after the original is kept next to it as ``config.toml.schema-<N>.bak``. A file
with a newer schema, or a configuration written by a pre-release version of
rvs, is reported and never modified.
"""

import hashlib
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit

import tomli_w

from . import config_migrations
from .config_migrations import SCHEMA_VERSION_KEY
from .paths import rvs_home


Format = Literal["pypi", "npm", "maven", "oci"]
ArtifactTargetType = Literal["repository", "official_cache", "custom_cache"]
NamespaceRealm = Literal["internal", "global"]

CONFIG_DIR = rvs_home()
CONFIG_FILE = CONFIG_DIR / "config.toml"
PROFILE_ENV_FILE = CONFIG_DIR / "profiles.env"
SESSIONS_DIR_NAME = "sessions"

DEFAULT_API_URL = "https://api.ravenstash.com"
DEFAULT_REPOSITORY_DOMAIN = "rvsta.sh"
REPOSITORY_TARGET_PREFIX = "repo:"
# Pre-release versions of rvs wrote this key, or no version at all, and never
# ``schema_version``.
_PRE_RELEASE_VERSION_KEY = "config_version"


class ConfigError(ValueError):
    """A user-actionable failure while reading the local configuration."""

    # The remedy the entry point prints after the message.
    hint = "Run `rvs profile delete --all` to reset the local configuration."


class PreReleaseConfigError(ConfigError):
    """The config file predates the first public schema; it is never modified."""

    hint = (
        "Run `rvs profile delete --all` to reset your local profiles, "
        "then sign in again with `rvs auth login`."
    )


class NewerConfigSchemaError(ConfigError):
    """The config file uses a schema newer than this release; it is never modified."""

    hint = "Run `rvs update` or reinstall rvs to use a release that supports it."


class ConfigMigrationError(ConfigError):
    """An older schema could not be upgraded; the config file is unchanged."""

    hint = (
        "Run `rvs update` and try again, or run `rvs profile delete --all` "
        "to reset your local profiles."
    )


_UNIQUE_ID_FRAGMENT = r"[23456789abcdefghijkmnpqrstuvwxyz]{8}"
_INTERNAL_NAMESPACE_REF_RE = re.compile(rf"^in_{_UNIQUE_ID_FRAGMENT}$")
_GLOBAL_NAMESPACE_REF_RE = re.compile(rf"^gn_{_UNIQUE_ID_FRAGMENT}$")
_REPOSITORY_REF_RE = re.compile(rf"^ar_{_UNIQUE_ID_FRAGMENT}$")


@dataclass(frozen=True)
class PackageRegistryEndpoints:
    read_base_url: str
    push_base_url: str
    mirror_base_url: str


@dataclass(frozen=True)
class NativeRegistryEndpoints:
    pypi: PackageRegistryEndpoints
    npm: PackageRegistryEndpoints
    maven: PackageRegistryEndpoints
    oci_registry_base_url: str

    def package(self, kind: Literal["pypi", "npm", "maven"]) -> PackageRegistryEndpoints:
        return cast("PackageRegistryEndpoints", getattr(self, kind))


def repository_domain_summary(endpoints: NativeRegistryEndpoints) -> str:
    """Summarize a conventional native-registry endpoint family by DNS suffix."""
    service_urls = (
        (("pypi",), endpoints.pypi.read_base_url),
        (("push.pypi",), endpoints.pypi.push_base_url),
        (("mirror.pypi",), endpoints.pypi.mirror_base_url),
        (("npm",), endpoints.npm.read_base_url),
        (("push.npm",), endpoints.npm.push_base_url),
        (("mirror.npm",), endpoints.npm.mirror_base_url),
        (("maven",), endpoints.maven.read_base_url),
        (("push.maven",), endpoints.maven.push_base_url),
        (("mirror.maven",), endpoints.maven.mirror_base_url),
        (("oci",), endpoints.oci_registry_base_url),
    )
    hosts = [urlsplit(url).hostname or "" for _services, url in service_urls]
    domains = {
        host.removeprefix(f"{service}.")
        for (services, _url), host in zip(service_urls, hosts, strict=True)
        for service in services
        if host.startswith(f"{service}.")
    }
    if len(domains) == 1 and all(
        any(host == f"{service}.{next(iter(domains))}" for service in services)
        for (services, _url), host in zip(service_urls, hosts, strict=True)
    ):
        return next(iter(domains))

    unique_hosts = set(hosts)
    if len(unique_hosts) == 1 and hosts[0] and _is_loopback_host(hosts[0]):
        return hosts[0]
    return "custom endpoints"


def _is_loopback_host(hostname: str) -> bool:
    """Return whether *hostname* is exactly ``localhost`` or a literal loopback IP.

    Other names, including ``*.localhost``, are resolved through DNS and are not
    trusted to stay on this machine, so they never qualify for plain HTTP.
    """
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def api_origin(url: str) -> str:
    """Return the ``scheme://host[:port]`` origin of a validated service URL."""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def validate_service_url(value: str, *, label: str) -> str:
    """Validate and normalize a credential-bearing Ravenstash service URL."""
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label} must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain a query string or fragment")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ValueError(
            f"{label} must use HTTPS unless it targets localhost or a loopback IP address"
        )

    hostname = parsed.hostname.rstrip(".").lower()
    if ":" in hostname:
        host = f"[{hostname}]"
    else:
        host = hostname
    if parsed.port is not None:
        default_port = 443 if parsed.scheme == "https" else 80
        if parsed.port != default_port:
            host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), host, path, "", ""))


@dataclass
class ProfileConfig:
    api_url: str = DEFAULT_API_URL
    native_registries: NativeRegistryEndpoints = field(
        default_factory=lambda: _native_registries_for_domain(None)
    )
    # The signed-in user's personal account.
    account_ref: str | None = None
    credential_store: str | None = None
    credential_type: str | None = None
    expires_at: str | None = None
    refresh_expires_at: str | None = None
    # The acting account chosen with `rvs account switch`; the personal account when unset.
    active_account_ref: str | None = None
    accounts: dict[str, AccountContext] = field(default_factory=dict)
    # DNS suffix that rewrites discovered native-registry hosts; seeded once from
    # the environment when the profile is created.
    repository_domain: str | None = None
    # API URL that issued the stored device credential.
    credential_api_url: str | None = None

    @property
    def credential_origin_url(self) -> str:
        """Return the Ravenstash API URL that stored device credentials belong to."""
        return self.credential_api_url or self.api_url

    def credential_origin_matches(self) -> bool:
        """Return whether stored credentials may be sent to this profile's API URL."""
        return api_origin(self.credential_origin_url) == api_origin(self.api_url)


@dataclass
class ArtifactTarget:
    target_type: ArtifactTargetType
    account_ref: str
    stable_selector: str
    display_selector: str
    format: Format | None = None
    namespace_realm: NamespaceRealm | None = None
    namespace_ref: str | None = None
    namespace_name_cache: str | None = None
    repository_ref: str | None = None
    repository_name_cache: str | None = None
    remote_cache_ref: str | None = None
    remote_name_cache: str | None = None


@dataclass
class AccountContext:
    account_ref: str
    # Account types are an open value set; personal and organization are known.
    account_type: str
    organization_role: str | None = None
    authority_revision: int | None = None
    selected_target: ArtifactTarget | None = None
    account_handle: str | None = None


@dataclass
class SessionContext:
    profile: str | None = None
    account_ref: str | None = None


@dataclass
class RvsConfig:
    default_profile: str = "default"
    credential_store: str = "auto"
    profiles: dict[str, ProfileConfig] = field(default_factory=dict)

    def active_profile(self, profile_name: str | None = None) -> ProfileConfig:
        name = profile_name or os.environ.get("RVS_PROFILE") or self.default_profile
        return self.profiles.get(name, _default_profile_config(name))


def current_profile_name(cfg: RvsConfig | None = None) -> str:
    """Return the effective named local CLI profile."""
    resolved_cfg = cfg or load()
    session = load_session()
    return os.environ.get("RVS_PROFILE") or session.profile or resolved_cfg.default_profile


def profile_selection_source() -> str:
    """Describe what selected the effective local CLI profile."""
    if os.environ.get("RVS_PROFILE"):
        return "environment (RVS_PROFILE)"
    if load_session().profile:
        return "shell session"
    return "persisted default"


def current_account_ref(
    profile_name: str | None = None,
    cfg: RvsConfig | None = None,
) -> str | None:
    """Return the active account for a profile, including this shell's override."""
    resolved_cfg = cfg or load()
    effective_profile = profile_name or current_profile_name(resolved_cfg)
    explicit = os.environ.get("RVS_ACCOUNT_REF")
    if explicit:
        return explicit
    session = load_session()
    if session.account_ref and (session.profile is None or session.profile == effective_profile):
        return session.account_ref
    profile = resolved_cfg.profiles.get(effective_profile)
    if profile is None:
        return None
    return profile.active_account_ref or profile.account_ref


def account_selection_source(
    profile_name: str | None = None,
    cfg: RvsConfig | None = None,
) -> str:
    """Describe what selected the effective acting account."""
    resolved_cfg = cfg or load()
    effective_profile = profile_name or current_profile_name(resolved_cfg)
    if os.environ.get("RVS_ACCOUNT_REF"):
        return "environment (RVS_ACCOUNT_REF)"
    session = load_session()
    if session.account_ref and (session.profile is None or session.profile == effective_profile):
        return "shell session"
    profile = resolved_cfg.profiles.get(effective_profile)
    if profile is None:
        return "not selected"
    if profile.active_account_ref:
        return "persisted profile"
    if profile.account_ref:
        return "personal account default"
    return "not selected"


def profile_selection_write_scope() -> str:
    """Return where a local-profile selection is persisted."""
    return "shell session" if _session_file() is not None else "persisted default"


def account_selection_write_scope() -> str:
    """Return where an acting-account selection is persisted."""
    return "shell session" if _session_file() is not None else "persisted profile"


def _session_file() -> Path | None:
    session_id = os.environ.get("RVS_SESSION_ID", "").strip()
    if not session_id:
        return None
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return CONFIG_DIR / SESSIONS_DIR_NAME / f"{digest}.toml"


def load_session() -> SessionContext:
    path = _session_file()
    if path is None or not path.exists():
        return SessionContext()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except OSError, tomllib.TOMLDecodeError:
        return SessionContext()
    return SessionContext(
        profile=raw.get("profile") if isinstance(raw.get("profile"), str) else None,
        account_ref=(raw.get("account_ref") if isinstance(raw.get("account_ref"), str) else None),
    )


def save_session(session: SessionContext) -> bool:
    """Persist non-secret shell-local context; return False without shell integration."""
    path = _session_file()
    if path is None:
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    raw = {
        key: value
        for key, value in {
            "profile": session.profile,
            "account_ref": session.account_ref,
        }.items()
        if value is not None
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            if hasattr(os, "fchmod"):
                os.fchmod(temporary.fileno(), 0o600)
            tomli_w.dump(raw, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return True


# ── Environment helpers ───────────────────────────────────────────────────────


def _profile_env_key(profile_name: str, suffix: str = "API_URL") -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", profile_name).strip("_").upper()
    return f"RVS_PROFILE_{normalized}_{suffix}"


def _parse_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists() or not path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key:
            values[key] = _parse_env_value(value)
    return values


def _require_private_env_file(path: Path) -> None:
    """Refuse an explicit env file that another local user could have written."""
    try:
        status = path.stat()
    except OSError as exc:
        raise ConfigError(f"RVS_ENV_FILE {path} cannot be read: {exc}") from exc
    if not stat.S_ISREG(status.st_mode):
        raise ConfigError(f"RVS_ENV_FILE {path} must be a regular file")
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # Windows ACLs are not modelled by POSIX mode bits.
        return
    if status.st_uid != getuid():
        raise ConfigError(f"RVS_ENV_FILE {path} must be owned by the current user")
    if status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            f"RVS_ENV_FILE {path} must not be writable by group or others (run: chmod go-w)"
        )


def _env_file_values() -> dict[str, str]:
    # Only files the user named or owns in ~/.rvs are read; rvs never discovers
    # an env file from the working directory.
    values = _read_env_file(PROFILE_ENV_FILE)
    explicit_env_file = os.environ.get("RVS_ENV_FILE")
    if explicit_env_file:
        path = Path(explicit_env_file).expanduser()
        _require_private_env_file(path)
        values.update(_read_env_file(path))
    return values


def _env_value(name: str) -> str | None:
    return os.environ.get(name) or _env_file_values().get(name)


def _override_keys(profile_name: str, suffix: str, default_alias: str) -> list[str]:
    keys = [_profile_env_key(profile_name, suffix)]
    if profile_name == "default":
        keys.append(default_alias)
    return keys


def _override_value(profile_name: str, suffix: str, default_alias: str) -> tuple[str, str] | None:
    for key in _override_keys(profile_name, suffix, default_alias):
        value = _env_value(key)
        if value:
            return key, value
    return None


def profile_api_url(profile_name: str) -> str:
    """Return the API URL that seeds *profile_name* when it is not saved yet."""
    return _profile_api_url_override(profile_name) or DEFAULT_API_URL


def _profile_api_url_override(profile_name: str) -> str | None:
    found = _override_value(profile_name, "API_URL", "RVS_API_URL")
    if found is None:
        return None
    return validate_service_url(found[1], label=f"{profile_name} API URL")


def validate_repository_domain(value: str, *, label: str) -> str:
    """Validate and normalize a package-repository DNS suffix."""
    candidate = value.strip().lower().rstrip(".")
    if candidate.startswith("*."):
        candidate = candidate[2:]
    if not candidate or len(candidate) > 253:
        raise ValueError(f"{label} must be a valid DNS domain")
    invalid_label = any(
        len(part) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", part)
        for part in candidate.split(".")
    )
    if invalid_label:
        raise ValueError(f"{label} must be a valid DNS domain without a scheme, port, or path")
    return candidate


def _profile_repository_domain_override(profile_name: str) -> str | None:
    found = _override_value(profile_name, "REPOSITORY_DOMAIN", "RVS_REPOSITORY_DOMAIN")
    if found is None:
        return None
    return validate_repository_domain(found[1], label=f"{profile_name} repository domain")


def ignored_endpoint_overrides(profile_name: str, cfg: RvsConfig | None = None) -> list[str]:
    """Name environment overrides that a saved profile does not follow.

    Overrides only seed a new profile. When one is set for a saved profile and
    differs from what the profile stores, callers can tell the user it is ignored.
    """
    resolved_cfg = cfg or load()
    profile = resolved_cfg.profiles.get(profile_name)
    if profile is None:
        return []
    ignored: list[str] = []
    api_url = _override_value(profile_name, "API_URL", "RVS_API_URL")
    if api_url is not None:
        try:
            differs = validate_service_url(api_url[1], label=api_url[0]) != profile.api_url
        except ValueError:
            differs = True
        if differs:
            ignored.append(api_url[0])
    domain = _override_value(profile_name, "REPOSITORY_DOMAIN", "RVS_REPOSITORY_DOMAIN")
    if domain is not None:
        try:
            differs = (
                validate_repository_domain(domain[1], label=domain[0]) != profile.repository_domain
            )
        except ValueError:
            differs = True
        if differs:
            ignored.append(domain[0])
    return ignored


def _repository_service_url(
    domain: str,
    service: str,
    *,
    source_url: str | None = None,
) -> str:
    source = urlsplit(source_url) if source_url is not None else None
    scheme = source.scheme if source is not None else "https"
    # A *.localhost suffix is rewritten to the literal host ``localhost`` so plain
    # HTTP never depends on how a resolver treats the name.
    is_localhost = domain == "localhost" or domain.endswith(".localhost")
    if is_localhost:
        scheme = "http"
    host = "localhost" if is_localhost else f"{service}.{domain}"
    if source is not None and source.port is not None:
        host = f"{host}:{source.port}"
    path = source.path if source is not None else ""
    return validate_service_url(
        urlunsplit((scheme, host, path, "", "")),
        label=f"{service} repository URL",
    )


def _package_registry_endpoints(
    domain: str | None,
    kind: Literal["pypi", "npm", "maven"],
    source: PackageRegistryEndpoints | None = None,
) -> PackageRegistryEndpoints:
    if source is not None and domain is None:
        return source
    effective_domain = domain or DEFAULT_REPOSITORY_DOMAIN
    return PackageRegistryEndpoints(
        read_base_url=_repository_service_url(
            effective_domain,
            kind,
            source_url=source.read_base_url if source is not None else None,
        ),
        push_base_url=_repository_service_url(
            effective_domain,
            f"push.{kind}",
            source_url=source.push_base_url if source is not None else None,
        ),
        mirror_base_url=_repository_service_url(
            effective_domain,
            f"mirror.{kind}",
            source_url=source.mirror_base_url if source is not None else None,
        ),
    )


def _native_registries_for_domain(domain: str | None) -> NativeRegistryEndpoints:
    return NativeRegistryEndpoints(
        pypi=_package_registry_endpoints(domain, "pypi"),
        npm=_package_registry_endpoints(domain, "npm"),
        maven=_package_registry_endpoints(domain, "maven"),
        oci_registry_base_url=_repository_service_url(
            domain or DEFAULT_REPOSITORY_DOMAIN,
            "oci",
        ),
    )


def _required_endpoint(raw: dict, key: str, *, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is missing")
    return validate_service_url(value, label=label)


def _native_registries_from_mapping(
    value: object,
    *,
    profile_name: str,
    repository_domain: str | None,
) -> NativeRegistryEndpoints:
    """Read stored or discovered endpoints; a format's unused fields may be null."""
    if value is None:
        return _native_registries_for_domain(repository_domain)
    if not isinstance(value, dict):
        raise ValueError(f"{profile_name} native registry endpoints must be a table")

    def package(kind: Literal["pypi", "npm", "maven"]) -> PackageRegistryEndpoints:
        raw = value.get(kind)
        if not isinstance(raw, dict):
            raise ValueError(f"{profile_name} {kind} registry endpoints are missing")
        discovered = PackageRegistryEndpoints(
            read_base_url=_required_endpoint(
                raw, "read_base_url", label=f"{profile_name} {kind} read URL"
            ),
            push_base_url=_required_endpoint(
                raw, "push_base_url", label=f"{profile_name} {kind} push URL"
            ),
            mirror_base_url=_required_endpoint(
                raw, "mirror_base_url", label=f"{profile_name} {kind} mirror URL"
            ),
        )
        return _package_registry_endpoints(repository_domain, kind, discovered)

    raw_oci = value.get("oci")
    if not isinstance(raw_oci, dict):
        raise ValueError(f"{profile_name} OCI registry endpoint is missing")
    discovered_oci_url = _required_endpoint(
        raw_oci, "registry_base_url", label=f"{profile_name} OCI registry URL"
    )
    return NativeRegistryEndpoints(
        pypi=package("pypi"),
        npm=package("npm"),
        maven=package("maven"),
        oci_registry_base_url=(
            _repository_service_url(repository_domain, "oci", source_url=discovered_oci_url)
            if repository_domain is not None
            else discovered_oci_url
        ),
    )


def parse_native_registries(
    value: object,
    *,
    profile_name: str,
    repository_domain: str | None = None,
) -> NativeRegistryEndpoints:
    """Validate a discovered native-registry projection for one profile.

    ``/v0/artifacts/meta`` returns a map keyed by format whose values share one
    shape with nullable ``read_base_url``, ``push_base_url``, ``mirror_base_url``,
    and ``registry_base_url``; each format must carry the fields rvs uses.
    """
    if value is None:
        raise ValueError(f"{profile_name} native registry endpoints are missing")
    return _native_registries_from_mapping(
        value, profile_name=profile_name, repository_domain=repository_domain
    )


def _native_registries_mapping(endpoints: NativeRegistryEndpoints) -> dict:
    return {
        "pypi": vars(endpoints.pypi),
        "npm": vars(endpoints.npm),
        "maven": vars(endpoints.maven),
        "oci": {"registry_base_url": endpoints.oci_registry_base_url},
    }


def _default_profile_config(profile_name: str) -> ProfileConfig:
    """Return the configuration an unsaved profile starts from.

    Environment and env-file overrides apply here only, so they can seed a new
    profile but never redirect one that is already saved.
    """
    api_url = profile_api_url(profile_name)
    repository_domain = _profile_repository_domain_override(profile_name)
    return ProfileConfig(
        api_url=api_url,
        native_registries=_native_registries_for_domain(repository_domain),
        repository_domain=repository_domain,
    )


def _artifact_target_from_mapping(value: object) -> ArtifactTarget | None:
    if not isinstance(value, dict):
        return None
    target_type = value.get("target_type")
    account_ref = value.get("account_ref")
    stable_selector = value.get("stable_selector")
    display_selector = value.get("display_selector")
    if target_type not in {"repository", "official_cache", "custom_cache"}:
        return None
    if not all(
        isinstance(item, str) and item for item in (account_ref, stable_selector, display_selector)
    ):
        return None
    stored_format = value.get("format")
    if stored_format not in {None, "pypi", "npm", "maven", "oci"}:
        return None
    return ArtifactTarget(
        target_type=cast("ArtifactTargetType", target_type),
        account_ref=cast("str", account_ref),
        stable_selector=cast("str", stable_selector),
        display_selector=cast("str", display_selector),
        format=cast("Format | None", stored_format),
        namespace_realm=value.get("namespace_realm"),
        namespace_ref=value.get("namespace_ref"),
        namespace_name_cache=value.get("namespace_name_cache"),
        repository_ref=value.get("repository_ref"),
        repository_name_cache=value.get("repository_name_cache"),
        remote_cache_ref=value.get("remote_cache_ref"),
        remote_name_cache=value.get("remote_name_cache"),
    )


def _account_contexts_from_mapping(value: object) -> dict[str, AccountContext]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, AccountContext] = {}
    for account_ref, raw in value.items():
        if not isinstance(account_ref, str) or not isinstance(raw, dict):
            continue
        account_type = raw.get("account_type")
        if not isinstance(account_type, str) or not account_type:
            continue
        if not account_ref:
            continue
        result[account_ref] = AccountContext(
            account_ref=account_ref,
            account_type=account_type,
            organization_role=raw.get("organization_role"),
            authority_revision=raw.get("authority_revision"),
            selected_target=_artifact_target_from_mapping(raw.get("selected_target")),
            account_handle=raw.get("account_handle"),
        )
    return result


def _artifact_target_mapping(target: ArtifactTarget) -> dict:
    return {key: value for key, value in vars(target).items() if value is not None}


def _account_context_mapping(account: AccountContext) -> dict:
    return {
        key: value
        for key, value in {
            "account_type": account.account_type,
            "account_handle": account.account_handle,
            "organization_role": account.organization_role,
            "authority_revision": account.authority_revision,
            "selected_target": (
                _artifact_target_mapping(account.selected_target)
                if account.selected_target is not None
                else None
            ),
        }.items()
        if value is not None
    }


# ── Serialisation helpers ─────────────────────────────────────────────────────


def _load_raw() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with CONFIG_FILE.open("rb") as f:
        return tomllib.load(f)


def _stored_schema_version(raw: dict) -> int | None:
    """Return the schema version of a parsed config file, or ``None`` for an empty one.

    An empty file is a fresh installation. Any other file without a usable
    ``schema_version`` is refused here, before anything is written.
    """
    if not raw:
        return None
    if _PRE_RELEASE_VERSION_KEY in raw or SCHEMA_VERSION_KEY not in raw:
        raise PreReleaseConfigError(
            f"{CONFIG_FILE} was written by a pre-release version of rvs and cannot be "
            "used by this release; it was left unchanged"
        )
    version = raw[SCHEMA_VERSION_KEY]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"{SCHEMA_VERSION_KEY} must be a positive integer")
    current = config_migrations.CURRENT_SCHEMA_VERSION
    if version > current:
        raise NewerConfigSchemaError(
            f"{CONFIG_FILE} uses configuration schema {version}, which is newer than "
            f"this rvs release supports (schema {current}); it was left unchanged"
        )
    return version


def _validate_repository_target_identity(value: dict, *, path: str) -> None:
    realm = value.get("namespace_realm")
    if realm not in {"internal", "global"}:
        raise ValueError(f"invalid namespace realm at {path}.namespace_realm")
    namespace_ref = value.get("namespace_ref")
    repository_ref = value.get("repository_ref")
    expected_namespace = (
        _INTERNAL_NAMESPACE_REF_RE if realm == "internal" else _GLOBAL_NAMESPACE_REF_RE
    )
    if namespace_ref is not None and (
        not isinstance(namespace_ref, str) or expected_namespace.fullmatch(namespace_ref) is None
    ):
        raise ValueError(f"invalid namespace reference at {path}.namespace_ref")
    if repository_ref is not None and (
        not isinstance(repository_ref, str) or _REPOSITORY_REF_RE.fullmatch(repository_ref) is None
    ):
        raise ValueError(f"invalid repository reference at {path}.repository_ref")

    selector = value.get("stable_selector")
    if not isinstance(selector, str) or not selector:
        raise ValueError(f"missing repository selector at {path}")
    if selector.split("/", 1)[-1].startswith("ar_"):
        parts = selector.split("/")
        if len(parts) != 2 or parts[0] != "in" or _REPOSITORY_REF_RE.fullmatch(parts[1]) is None:
            raise ValueError(f"invalid stable repository selector at {path}")
        if repository_ref is not None and repository_ref != parts[1]:
            raise ValueError(f"conflicting repository identity at {path}")
    elif selector.startswith("@") or ":" in selector or selector.count("/") != 1:
        raise ValueError(f"invalid repository selector at {path}")


def _validate_raw_config(raw: dict) -> None:
    """Fail closed on malformed current config before replacing the source file."""

    profiles = raw.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ValueError("profiles must be a table")
    for profile_name, profile in profiles.items():
        profile_path = f"profiles.{profile_name}"
        if not isinstance(profile_name, str) or not isinstance(profile, dict):
            raise ValueError(f"invalid profile table at {profile_path}")
        accounts = profile.get("accounts", {})
        if not isinstance(accounts, dict):
            raise ValueError(f"accounts must be a table at {profile_path}.accounts")
        for account_ref, account in accounts.items():
            account_path = f"{profile_path}.accounts.{account_ref}"
            if not isinstance(account_ref, str) or not account_ref or not isinstance(account, dict):
                raise ValueError(f"invalid account table at {account_path}")
            selected = account.get("selected_target")
            if selected is None:
                continue
            target_path = f"{account_path}.selected_target"
            if not isinstance(selected, dict) or _artifact_target_from_mapping(selected) is None:
                raise ValueError(f"invalid selected target at {target_path}")
            if selected.get("target_type") == "repository":
                _validate_repository_target_identity(selected, path=target_path)


def _write_private_file(path: Path, data: bytes) -> None:
    """Atomically replace *path*, a file in the config directory, with owner-only *data*."""
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=CONFIG_DIR,
            prefix=".config.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            if hasattr(os, "fchmod"):
                os.fchmod(temporary.fileno(), 0o600)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _write_raw(raw: dict) -> None:
    """Atomically persist an already validated config mapping."""
    _write_private_file(CONFIG_FILE, tomli_w.dumps(raw).encode("utf-8"))


def _migration_backup_path(version: int) -> Path:
    return CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.schema-{version}.bak")


def _migrate_config_file(original: bytes, raw: dict, version: int) -> dict:
    """Upgrade the config file to the current schema, keeping the original as a backup.

    The backup is written before the first step and is never replaced, and the
    upgraded file replaces the original atomically. An interrupted or failed
    migration therefore leaves the original file, which migrates again later.
    """
    current = config_migrations.CURRENT_SCHEMA_VERSION
    try:
        backup = _migration_backup_path(version)
        if not backup.exists():
            _write_private_file(backup, original)
        migrated = config_migrations.migrate(raw, from_version=version, to_version=current)
        _validate_raw_config(migrated)
        _write_raw(migrated)
    except Exception as exc:
        # A migration is arbitrary code; whatever it raises is reported the same way.
        raise ConfigMigrationError(
            f"could not migrate {CONFIG_FILE} from configuration schema {version} to "
            f"schema {current}: {exc}; it was left unchanged"
        ) from exc
    return migrated


def _load_current_raw() -> dict:
    """Read the config file in the current schema, upgrading an older one on disk."""
    if not CONFIG_FILE.exists():
        return {}
    original = CONFIG_FILE.read_bytes()
    raw = tomllib.loads(original.decode("utf-8"))
    version = _stored_schema_version(raw)
    if version is None or version == config_migrations.CURRENT_SCHEMA_VERSION:
        return raw
    return _migrate_config_file(original, raw, version)


def stored_profile_api_url(profile_name: str) -> str | None:
    """Return a saved profile's API URL, or ``None`` when it is not saved."""
    profile = load().profiles.get(profile_name)
    return profile.api_url if profile is not None else None


def stored_credential_api_url(profile_name: str) -> str | None:
    """Return the Ravenstash API URL that issued a saved profile's device credential."""
    profile = load().profiles.get(profile_name)
    return profile.credential_origin_url if profile is not None else None


def _optional_service_url(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return validate_service_url(value, label=label)


def _load_validated() -> RvsConfig:
    raw = _load_current_raw()
    _validate_raw_config(raw)
    cfg = RvsConfig(
        default_profile=raw.get("default_profile", "default"),
        credential_store=raw.get("credential_store", "auto"),
    )

    for name, vals in raw.get("profiles", {}).items():
        # A saved profile never follows environment or env-file overrides.
        raw_domain = vals.get("repository_domain")
        repository_domain = (
            validate_repository_domain(str(raw_domain), label=f"{name} repository domain")
            if raw_domain is not None
            else None
        )
        cfg.profiles[name] = ProfileConfig(
            api_url=validate_service_url(
                vals.get("api_url", DEFAULT_API_URL),
                label=f"{name} API URL",
            ),
            native_registries=_native_registries_from_mapping(
                vals.get("native_registries"),
                profile_name=name,
                repository_domain=repository_domain,
            ),
            account_ref=vals.get("account_ref"),
            credential_store=vals.get("credential_store"),
            credential_type=vals.get("credential_type"),
            expires_at=vals.get("expires_at"),
            refresh_expires_at=vals.get("refresh_expires_at"),
            active_account_ref=vals.get("active_account_ref"),
            accounts=_account_contexts_from_mapping(vals.get("accounts")),
            repository_domain=repository_domain,
            credential_api_url=_optional_service_url(
                vals.get("credential_api_url"), label=f"{name} credential API URL"
            ),
        )

    return cfg


def load() -> RvsConfig:
    """Load configuration and identify failures that users can repair locally."""
    try:
        return _load_validated()
    except ConfigError:
        raise
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise ConfigError(f"could not read {CONFIG_FILE}: {exc}") from exc


def save(cfg: RvsConfig) -> None:
    """Persist *cfg*; profiles carry only their own stored values, never overrides."""
    raw: dict = {
        SCHEMA_VERSION_KEY: config_migrations.CURRENT_SCHEMA_VERSION,
        "default_profile": cfg.default_profile,
        "credential_store": cfg.credential_store,
    }

    if cfg.profiles:
        raw["profiles"] = {
            name: {
                k: v
                for k, v in {
                    "api_url": p.api_url,
                    "repository_domain": p.repository_domain,
                    "native_registries": _native_registries_mapping(p.native_registries),
                    "account_ref": p.account_ref,
                    "credential_store": p.credential_store,
                    "credential_type": p.credential_type,
                    "credential_api_url": p.credential_api_url,
                    "expires_at": p.expires_at,
                    "refresh_expires_at": p.refresh_expires_at,
                    "active_account_ref": p.active_account_ref,
                    "accounts": {
                        account_ref: _account_context_mapping(account)
                        for account_ref, account in p.accounts.items()
                    }
                    or None,
                }.items()
                if v is not None
            }
            for name, p in cfg.profiles.items()
        }

    _write_raw(raw)


# ── Convenience setters ───────────────────────────────────────────────────────


def set_profile_value(profile: str, api_url: str | None = None) -> None:
    cfg = load()
    existing = cfg.profiles.get(profile, _default_profile_config(profile))
    if api_url is not None:
        existing.api_url = validate_service_url(api_url, label=f"{profile} API URL")
    cfg.profiles[profile] = existing
    save(cfg)


def clear_profile_credential_metadata(profile: str) -> None:
    cfg = load()
    existing = cfg.profiles.get(profile)
    if existing is None:
        return
    existing.account_ref = None
    existing.credential_type = None
    existing.credential_api_url = None
    existing.expires_at = None
    existing.refresh_expires_at = None
    save(cfg)


def set_profile_metadata(
    profile: str,
    *,
    api_url: str | None = None,
    native_registries: object | None = None,
    account_ref: str | None = None,
    credential_store: str | None = None,
    credential_type: str | None = None,
    credential_api_url: str | None = None,
    expires_at: str | None = None,
    refresh_expires_at: str | None = None,
) -> None:
    cfg = load()
    existing = cfg.profiles.get(profile, _default_profile_config(profile))
    if api_url is not None:
        existing.api_url = validate_service_url(api_url, label=f"{profile} API URL")
    if native_registries is not None:
        existing.native_registries = _native_registries_from_mapping(
            native_registries,
            profile_name=profile,
            repository_domain=existing.repository_domain,
        )
    if account_ref is not None:
        existing.account_ref = account_ref
    if credential_store is not None:
        existing.credential_store = credential_store
    if credential_type is not None:
        existing.credential_type = credential_type
    if credential_api_url is not None:
        existing.credential_api_url = validate_service_url(
            credential_api_url, label=f"{profile} credential API URL"
        )
    if expires_at is not None:
        existing.expires_at = expires_at
    if refresh_expires_at is not None:
        existing.refresh_expires_at = refresh_expires_at
    cfg.profiles[profile] = existing
    save(cfg)


def set_credential_store(store: str) -> None:
    """Set the preferred credential store for future device logins."""
    if store not in {"auto", "keyring", "pass", "vault", "plaintext"}:
        raise ValueError("Credential store must be auto, keyring, pass, vault, or plaintext")
    cfg = load()
    cfg.credential_store = store
    save(cfg)


def set_default_profile(profile: str) -> None:
    session = load_session()
    if _session_file() is not None:
        session.profile = profile
        session.account_ref = None
        save_session(session)
        return
    cfg = load()
    cfg.default_profile = profile
    save(cfg)


def set_active_account(
    *,
    profile: str,
    account: dict,
) -> AccountContext:
    """Select and cache one authorized account for a local profile."""
    return cache_account(profile=profile, account=account, activate=True)


def cache_account(
    *,
    profile: str,
    account: dict,
    activate: bool = False,
) -> AccountContext:
    """Cache safe account metadata, optionally making the account active.

    *account* is an API ``Account`` or embedded ``AccountSummary``. A summary
    carries only ``ref``, ``handle``, and ``type``; the role and authority
    revision already cached from the full account list are then retained.
    """
    cfg = load()
    profile_config = cfg.profiles.get(profile, _default_profile_config(profile))
    account_ref = str(account["ref"])
    existing = profile_config.accounts.get(account_ref)
    handle = account.get("handle")
    context = AccountContext(
        account_ref=account_ref,
        account_type=str(account["type"]),
        account_handle=handle
        if handle is not None
        else (existing.account_handle if existing is not None else None),
        organization_role=account["organization_role"]
        if "organization_role" in account
        else (existing.organization_role if existing is not None else None),
        authority_revision=account["authority_revision"]
        if "authority_revision" in account
        else (existing.authority_revision if existing is not None else None),
        selected_target=existing.selected_target if existing is not None else None,
    )
    profile_config.accounts[account_ref] = context
    session_scoped = _session_file() is not None
    if activate and not session_scoped:
        profile_config.active_account_ref = account_ref
    cfg.profiles[profile] = profile_config
    save(cfg)
    session = load_session()
    if activate and session_scoped:
        session.profile = profile
        session.account_ref = account_ref
        save_session(session)
    return context


def cached_account(
    profile: str | None = None,
    account_ref: str | None = None,
) -> AccountContext | None:
    cfg = load()
    profile_name = profile or current_profile_name(cfg)
    effective_account_ref = account_ref or current_account_ref(profile_name, cfg)
    if not effective_account_ref:
        return None
    profile_config = cfg.profiles.get(profile_name)
    return profile_config.accounts.get(effective_account_ref) if profile_config else None


def selected_artifact_target(
    profile: str | None = None,
    account_ref: str | None = None,
) -> ArtifactTarget | None:
    account = cached_account(profile, account_ref)
    return account.selected_target if account is not None else None


def set_selected_artifact_target(
    target: ArtifactTarget | None,
    *,
    profile: str | None = None,
    account_ref: str | None = None,
) -> None:
    cfg = load()
    profile_name = profile or current_profile_name(cfg)
    effective_account_ref = account_ref or current_account_ref(profile_name, cfg)
    if not effective_account_ref:
        raise ValueError("No account is selected")
    profile_config = cfg.profiles.get(profile_name, _default_profile_config(profile_name))
    account = profile_config.accounts.get(effective_account_ref)
    if account is None:
        raise ValueError("The selected account could not be found")
    account.selected_target = target
    profile_config.accounts[effective_account_ref] = account
    cfg.profiles[profile_name] = profile_config
    save(cfg)


def delete_profile(profile: str) -> bool:
    cfg = load()
    if profile not in cfg.profiles:
        return False

    del cfg.profiles[profile]
    if cfg.default_profile == profile:
        cfg.default_profile = next(iter(cfg.profiles), "default")
    save(cfg)
    session = load_session()
    if session.profile == profile:
        session.profile = cfg.default_profile
        session.account_ref = None
        save_session(session)
    return True


def profile_names_for_reset() -> list[str]:
    """Read profile names without requiring the config format to be supported."""
    try:
        raw = _load_raw()
    except OSError, tomllib.TOMLDecodeError:
        return []
    profiles = raw.get("profiles")
    if not isinstance(profiles, dict):
        return []
    return [name for name in profiles if isinstance(name, str)]


def delete_all_profiles() -> None:
    """Replace any local config, whatever wrote it, with an empty current-schema baseline."""
    save(RvsConfig())
    if _session_file() is not None:
        save_session(SessionContext(profile="default"))
