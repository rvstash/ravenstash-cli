import os
import stat
from typing import TYPE_CHECKING

import pytest
from rvs import config as cfg_mod


if TYPE_CHECKING:
    from pathlib import Path


def _point_config(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / ".rvs"
    config_file = config_dir / "config.toml"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    monkeypatch.chdir(tmp_path)
    for key in (
        "RVS_API_URL",
        "RVS_ENV_FILE",
        "RVS_PROFILE_DEV_API_URL",
        "RVS_PROFILE_STAGING_API_URL",
        "RVS_REPOSITORY_DOMAIN",
        "RVS_PROFILE_DEV_REPOSITORY_DOMAIN",
        "RVS_PROFILE_STAGING_REPOSITORY_DOMAIN",
    ):
        monkeypatch.delenv(key, raising=False)
    return config_dir, config_file


def test_load_missing_config_uses_production_default_without_profile_env(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _config_dir, config_file = _point_config(monkeypatch, tmp_path)

    cfg = cfg_mod.load()

    assert cfg.default_profile == "default"
    assert cfg.profiles == {}
    assert cfg.active_profile().api_url == "https://api.ravenstash.com"
    assert cfg.active_profile("dev").api_url == "https://api.ravenstash.com"
    assert cfg.active_profile("staging").api_url == "https://api.ravenstash.com"
    assert cfg.active_profile().native_registries.pypi.read_base_url == "https://pypi.rvsta.sh"
    assert cfg.active_profile().native_registries.pypi.push_base_url == "https://push.pypi.rvsta.sh"
    assert (
        cfg.active_profile().native_registries.pypi.mirror_base_url
        == "https://mirror.pypi.rvsta.sh"
    )
    assert not config_file.exists()


def test_saved_profile_without_api_url_ignores_env_override(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    monkeypatch.setenv("RVS_PROFILE_STAGING_API_URL", "https://staging.example.test")
    config_dir.mkdir()
    config_file.write_text(
        """
schema_version = 1
default_profile = "staging"

[profiles.staging]
""".strip(),
        encoding="utf-8",
    )

    profile = cfg_mod.load().active_profile()

    assert profile.api_url == "https://api.ravenstash.com"
    assert profile.account_ref is None


def test_env_api_url_never_replaces_saved_profile_url(monkeypatch, tmp_path: Path) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    monkeypatch.setenv("RVS_PROFILE_STAGING_API_URL", "https://api.staging.example.test")
    config_dir.mkdir()
    config_file.write_text(
        """
schema_version = 1
[profiles.staging]
api_url = "https://api.ravenstash.com"
""".strip(),
        encoding="utf-8",
    )

    cfg = cfg_mod.load()

    assert cfg.active_profile("staging").api_url == "https://api.ravenstash.com"
    assert cfg_mod.ignored_endpoint_overrides("staging", cfg) == ["RVS_PROFILE_STAGING_API_URL"]


def test_env_overrides_seed_only_an_unsaved_profile(monkeypatch, tmp_path: Path) -> None:
    _point_config(monkeypatch, tmp_path)
    monkeypatch.setenv("RVS_PROFILE_STAGING_API_URL", "https://staging.example.test")
    monkeypatch.setenv("RVS_PROFILE_STAGING_REPOSITORY_DOMAIN", "packages.example.test")

    cfg_mod.set_profile_metadata("staging", account_ref="ac_23456789")
    monkeypatch.setenv("RVS_PROFILE_STAGING_API_URL", "https://attacker.example.test")
    monkeypatch.setenv("RVS_PROFILE_STAGING_REPOSITORY_DOMAIN", "attacker.example.test")
    profile = cfg_mod.load().profiles["staging"]

    assert profile.api_url == "https://staging.example.test"
    assert profile.repository_domain == "packages.example.test"
    assert profile.native_registries.pypi.read_base_url == "https://pypi.packages.example.test"
    assert profile.native_registries.oci_registry_base_url == "https://oci.packages.example.test"


def test_save_never_persists_env_overrides_into_saved_profiles(monkeypatch, tmp_path: Path) -> None:
    _config_dir, config_file = _point_config(monkeypatch, tmp_path)
    cfg_mod.save(cfg_mod.RvsConfig(profiles={"default": cfg_mod.ProfileConfig()}))
    monkeypatch.setenv("RVS_API_URL", "https://attacker.example.test")
    monkeypatch.setenv("RVS_REPOSITORY_DOMAIN", "attacker.example.test")

    cfg_mod.save(cfg_mod.load())
    cfg_mod.set_profile_metadata("default", account_ref="ac_23456789")

    text = config_file.read_text(encoding="utf-8")
    assert "attacker" not in text
    assert cfg_mod.load().profiles["default"].api_url == "https://api.ravenstash.com"


def test_local_rvs_env_file_is_not_discovered_from_the_working_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _point_config(monkeypatch, tmp_path)
    nested = tmp_path / "checkout" / "src"
    nested.mkdir(parents=True)
    (tmp_path / "checkout" / ".rvs.env").write_text(
        "RVS_API_URL=https://attacker.example.test\nRVS_REPOSITORY_DOMAIN=attacker.example.test\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(nested)

    profile = cfg_mod.load().active_profile("default")

    assert profile.api_url == "https://api.ravenstash.com"
    assert profile.native_registries.pypi.read_base_url == "https://pypi.rvsta.sh"


def test_profile_api_url_can_be_declared_in_explicit_env_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _point_config(monkeypatch, tmp_path)
    env_file = tmp_path / "rvs.env"
    env_file.write_text(
        """
RVS_PROFILE_STAGING_API_URL=https://staging.example.test
RVS_PROFILE_DEV_API_URL='http://localhost:43100'
""".strip(),
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("RVS_ENV_FILE", str(env_file))

    assert cfg_mod.profile_api_url("staging") == "https://staging.example.test"
    assert cfg_mod.profile_api_url("dev") == "http://localhost:43100"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_explicit_env_file_must_not_be_writable_by_others(monkeypatch, tmp_path: Path) -> None:
    _point_config(monkeypatch, tmp_path)
    env_file = tmp_path / "rvs.env"
    env_file.write_text("RVS_API_URL=https://staging.example.test\n", encoding="utf-8")
    env_file.chmod(0o664)
    monkeypatch.setenv("RVS_ENV_FILE", str(env_file))

    with pytest.raises(cfg_mod.ConfigError, match="must not be writable by group or others"):
        cfg_mod.profile_api_url("default")


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership")
def test_explicit_env_file_must_be_owned_by_the_current_user(monkeypatch, tmp_path: Path) -> None:
    _point_config(monkeypatch, tmp_path)
    env_file = tmp_path / "rvs.env"
    env_file.write_text("RVS_API_URL=https://staging.example.test\n", encoding="utf-8")
    env_file.chmod(0o600)
    monkeypatch.setenv("RVS_ENV_FILE", str(env_file))
    monkeypatch.setattr(cfg_mod.os, "getuid", lambda: env_file.stat().st_uid + 1)

    with pytest.raises(cfg_mod.ConfigError, match="must be owned by the current user"):
        cfg_mod.profile_api_url("default")


@pytest.mark.parametrize(
    "url",
    ["http://dev.example.test", "http://sandbox.localhost:8080", "http://localhost.example.test"],
)
def test_profile_api_url_rejects_non_loopback_plain_http(
    monkeypatch, tmp_path: Path, url: str
) -> None:
    _point_config(monkeypatch, tmp_path)
    monkeypatch.setenv("RVS_PROFILE_DEV_API_URL", url)

    with pytest.raises(ValueError, match="must use HTTPS"):
        cfg_mod.profile_api_url("dev")


@pytest.mark.parametrize(
    "url", ["http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"]
)
def test_plain_http_is_allowed_for_localhost_and_loopback_addresses(url: str) -> None:
    assert cfg_mod.validate_service_url(url, label="API URL") == url


def test_repository_domain_formats_every_registry_service_for_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _point_config(monkeypatch, tmp_path)
    monkeypatch.setenv("RVS_PROFILE_STAGING_REPOSITORY_DOMAIN", "packages.example.test")

    profile = cfg_mod.load().active_profile("staging")

    assert profile.native_registries.pypi.read_base_url == "https://pypi.packages.example.test"
    assert profile.native_registries.pypi.push_base_url == "https://push.pypi.packages.example.test"
    assert (
        profile.native_registries.pypi.mirror_base_url
        == "https://mirror.pypi.packages.example.test"
    )
    assert profile.native_registries.npm.read_base_url == "https://npm.packages.example.test"
    assert (
        profile.native_registries.maven.push_base_url == "https://push.maven.packages.example.test"
    )
    assert profile.native_registries.oci_registry_base_url == "https://oci.packages.example.test"


@pytest.mark.parametrize(
    "repository_domain",
    ["localhost", "sandbox.localhost", "*.sandbox.localhost"],
)
def test_localhost_repository_domain_uses_literal_host_and_keeps_routes(
    monkeypatch,
    tmp_path: Path,
    repository_domain: str,
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    monkeypatch.setenv("RVS_PROFILE_DEV_REPOSITORY_DOMAIN", "attacker.example.test")
    config_dir.mkdir()
    config_file.write_text(
        f"""
schema_version = 1
[profiles.dev]
repository_domain = "{repository_domain}"

[profiles.dev.native_registries.pypi]
read_base_url = "https://download.example.test:43101/registry/pypi"
push_base_url = "https://upload.example.test:43102/registry/pypi"
mirror_base_url = "https://mirror.example.test:43101/registry/pypi"

[profiles.dev.native_registries.npm]
read_base_url = "https://download.example.test:43101/registry/npm"
push_base_url = "https://upload.example.test:43102/registry/npm"
mirror_base_url = "https://mirror.example.test:43101/registry/npm"

[profiles.dev.native_registries.maven]
read_base_url = "https://download.example.test:43101/registry/maven"
push_base_url = "https://upload.example.test:43102/registry/maven"
mirror_base_url = "https://mirror.example.test:43101/registry/maven"

[profiles.dev.native_registries.oci]
registry_base_url = "https://images.example.test:43101"
""".strip(),
        encoding="utf-8",
    )

    endpoints = cfg_mod.load().active_profile("dev").native_registries

    assert endpoints.pypi.read_base_url == "http://localhost:43101/registry/pypi"
    assert endpoints.pypi.push_base_url == "http://localhost:43102/registry/pypi"
    assert endpoints.npm.mirror_base_url == "http://localhost:43101/registry/npm"
    assert endpoints.oci_registry_base_url == "http://localhost:43101"


def test_discovered_heterogeneous_host_family_is_retained_without_domain_override(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    config_dir.mkdir()
    config_file.write_text(
        """
schema_version = 1
[profiles.work.native_registries.pypi]
read_base_url = "https://python-download.example.test"
push_base_url = "https://python-upload.example.test"
mirror_base_url = "https://python-mirror.example.test"

[profiles.work.native_registries.npm]
read_base_url = "https://javascript-download.example.test"
push_base_url = "https://javascript-upload.example.test"
mirror_base_url = "https://javascript-mirror.example.test"

[profiles.work.native_registries.maven]
read_base_url = "https://java-download.example.test"
push_base_url = "https://java-upload.example.test"
mirror_base_url = "https://java-mirror.example.test"

[profiles.work.native_registries.oci]
registry_base_url = "https://images.example.test"
""".strip(),
        encoding="utf-8",
    )

    endpoints = cfg_mod.load().active_profile("work").native_registries

    assert endpoints.pypi.read_base_url == "https://python-download.example.test"
    assert endpoints.pypi.push_base_url == "https://python-upload.example.test"
    assert endpoints.npm.mirror_base_url == "https://javascript-mirror.example.test"
    assert endpoints.oci_registry_base_url == "https://images.example.test"


def test_repository_domain_rejects_urls_and_ports(monkeypatch, tmp_path: Path) -> None:
    _point_config(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "RVS_PROFILE_STAGING_REPOSITORY_DOMAIN",
        "https://packages.example.test:43103",
    )

    with pytest.raises(ValueError, match="without a scheme, port, or path"):
        cfg_mod.load().active_profile("staging")


def test_save_uses_private_modes_and_atomic_replacement(monkeypatch, tmp_path: Path) -> None:
    config_dir, config_file = _point_config(monkeypatch, tmp_path)
    cfg = cfg_mod.RvsConfig(
        profiles={"default": cfg_mod.ProfileConfig()},
    )

    cfg_mod.save(cfg)
    first_inode = config_file.stat().st_ino
    cfg.default_profile = "work"
    cfg_mod.save(cfg)

    if os.name != "nt":
        assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert config_file.stat().st_ino != first_inode
    assert not list(config_dir.glob(".config.*.tmp"))


def test_process_env_overrides_env_files(monkeypatch, tmp_path: Path) -> None:
    _point_config(monkeypatch, tmp_path)
    env_file = tmp_path / "rvs.env"
    env_file.write_text(
        "RVS_PROFILE_STAGING_API_URL=https://local.example.test\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("RVS_ENV_FILE", str(env_file))
    monkeypatch.setenv("RVS_PROFILE_STAGING_API_URL", "https://process.example.test")

    assert cfg_mod.profile_api_url("staging") == "https://process.example.test"


def test_save_and_load_round_trips_profiles(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _point_config(monkeypatch, tmp_path)
    cfg = cfg_mod.RvsConfig(
        default_profile="work",
        credential_store="pass",
        profiles={
            "work": cfg_mod.ProfileConfig(
                api_url="https://api.work.example",
                native_registries=cfg_mod.NativeRegistryEndpoints(
                    pypi=cfg_mod.PackageRegistryEndpoints(
                        "https://pypi.work.example",
                        "https://push.pypi.work.example",
                        "https://mirror.pypi.work.example",
                    ),
                    npm=cfg_mod.PackageRegistryEndpoints(
                        "https://npm.work.example",
                        "https://push.npm.work.example",
                        "https://mirror.npm.work.example",
                    ),
                    maven=cfg_mod.PackageRegistryEndpoints(
                        "https://maven.work.example",
                        "https://push.maven.work.example",
                        "https://mirror.maven.work.example",
                    ),
                    oci_registry_base_url="https://oci.work.example",
                ),
                account_ref="ac_abcdefgh",
                credential_store="pass",
                credential_type="expiring",
                expires_at="2099-01-01T00:00:00+00:00",
                refresh_expires_at="2099-01-02T00:00:00+00:00",
            )
        },
    )

    cfg_mod.save(cfg)
    loaded = cfg_mod.load()

    assert loaded.default_profile == "work"
    assert loaded.credential_store == "pass"
    assert loaded.profiles["work"].api_url == "https://api.work.example"
    assert (
        loaded.profiles["work"].native_registries.pypi.read_base_url == "https://pypi.work.example"
    )
    assert (
        loaded.profiles["work"].native_registries.npm.push_base_url
        == "https://push.npm.work.example"
    )
    assert (
        loaded.profiles["work"].native_registries.oci_registry_base_url
        == "https://oci.work.example"
    )
    assert loaded.profiles["work"].account_ref == "ac_abcdefgh"
    assert loaded.profiles["work"].credential_store == "pass"


def test_current_profile_name_prefers_environment(monkeypatch) -> None:
    monkeypatch.setenv("RVS_PROFILE", "staging")

    assert cfg_mod.current_profile_name(cfg_mod.RvsConfig(default_profile="work")) == "staging"


def test_set_profile_metadata_preserves_existing_values(monkeypatch, tmp_path: Path) -> None:
    _point_config(monkeypatch, tmp_path)
    cfg_mod.save(
        cfg_mod.RvsConfig(
            profiles={
                "default": cfg_mod.ProfileConfig(
                    api_url="https://api.example",
                    account_ref="ac_abcdefgh",
                    credential_type="expiring",
                )
            }
        )
    )

    cfg_mod.set_profile_metadata("default", account_ref="ac_23456789")
    profile = cfg_mod.load().profiles["default"]

    assert profile.api_url == "https://api.example"
    assert profile.account_ref == "ac_23456789"
    assert profile.credential_type == "expiring"


def test_delete_profile_updates_default_profile(monkeypatch, tmp_path: Path) -> None:
    _point_config(monkeypatch, tmp_path)
    cfg_mod.save(
        cfg_mod.RvsConfig(
            default_profile="work",
            profiles={
                "default": cfg_mod.ProfileConfig(),
                "work": cfg_mod.ProfileConfig(api_url="https://api.work.example"),
            },
        )
    )

    assert cfg_mod.delete_profile("work") is True
    loaded = cfg_mod.load()

    assert "work" not in loaded.profiles
    assert loaded.default_profile == "default"
    assert cfg_mod.delete_profile("missing") is False


def test_cache_account_summary_keeps_fields_known_from_the_full_account(
    monkeypatch, tmp_path: Path
) -> None:
    _point_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg_mod, "_session_file", lambda: None)
    cfg_mod.cache_account(
        profile="default",
        account={
            "ref": "ac_23456789",
            "handle": "acme",
            "type": "organization",
            "display_name": "Acme Inc.",
            "is_admin": True,
            "organization_role": "admin",
            "authority_revision": 4,
        },
    )

    summary = cfg_mod.cache_account(
        profile="default",
        account={"ref": "ac_23456789", "handle": "acme-renamed", "type": "organization"},
    )

    assert summary.account_handle == "acme-renamed"
    assert summary.organization_role == "admin"
    assert summary.authority_revision == 4
    stored = cfg_mod.load().profiles["default"].accounts["ac_23456789"]
    assert stored.account_handle == "acme-renamed"
    assert stored.organization_role == "admin"


def test_cache_account_summary_without_history_keeps_only_the_summary(
    monkeypatch, tmp_path: Path
) -> None:
    _point_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cfg_mod, "_session_file", lambda: None)

    account = cfg_mod.cache_account(
        profile="default",
        account={"ref": "ac_23456789", "handle": "dev", "type": "personal"},
    )

    assert account.account_handle == "dev"
    assert account.organization_role is None
    assert account.authority_revision is None
