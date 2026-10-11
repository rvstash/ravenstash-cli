from typing import TYPE_CHECKING

import pytest
from rvs import config as cfg_mod
from rvs.auth import credentials as auth_mod


if TYPE_CHECKING:
    from pathlib import Path


def _isolate_config(monkeypatch, tmp_path: Path, content: str) -> None:
    config_dir = tmp_path / ".rvs"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    normalized = content.strip()
    if "schema_version" not in normalized:
        normalized = f"schema_version = 1\n{normalized}"
    config_file.write_text(normalized, encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)


def test_expiring_credential_type_is_refreshable() -> None:
    assert auth_mod.is_refreshable_credential_type("expiring") is True
    assert auth_mod.is_refreshable_credential_type("token") is False
    assert auth_mod.display_credential_type("expiring") == "expiring"


def test_set_token_requires_configured_store(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path, 'credential_store = "auto"')
    monkeypatch.setattr(auth_mod, "_store_available", lambda _store: False)

    with pytest.raises(RuntimeError, match="No usable credential store"):
        auth_mod.set_token("default", "token")


def test_has_active_expiring_session_requires_valid_refresh_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(
        monkeypatch,
        tmp_path,
        """
default_profile = "default"

[profiles.default]
api_url = "https://api.ravenstash.com"
credential_type = "expiring"
refresh_expires_at = "2099-01-01T00:00:00+00:00"

[profiles.expired]
api_url = "https://api.ravenstash.com"
credential_type = "expiring"
refresh_expires_at = "2000-01-01T00:00:00+00:00"

[profiles.static]
api_url = "https://api.ravenstash.com"
credential_type = "token"
refresh_expires_at = "2099-01-01T00:00:00+00:00"
""",
    )
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(
        auth_mod,
        "_kr_get",
        lambda profile: "refresh-token" if profile == "default:refresh" else None,
    )

    assert auth_mod.has_active_expiring_session("default") is True
    assert auth_mod.has_active_expiring_session("expired") is False
    assert auth_mod.has_active_expiring_session("static") is False
    assert auth_mod.has_active_expiring_session("missing") is False


def test_delete_token_removes_access_and_refresh_tokens_and_clears_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(
        monkeypatch,
        tmp_path,
        """
schema_version = 1
default_profile = "default"

[profiles.default]
api_url = "https://api.ravenstash.com"
credential_store = "keyring"
account_ref = "ac_23456789"
credential_type = "expiring"
expires_at = "2099-01-01T00:00:00+00:00"
refresh_expires_at = "2099-01-02T00:00:00+00:00"
""",
    )
    deleted: list[str] = []
    monkeypatch.setattr(auth_mod, "_kr_delete", lambda profile: deleted.append(profile))

    auth_mod.delete_token("default")
    profile = cfg_mod.load().profiles["default"]

    assert deleted == ["default", "default:refresh"]
    assert profile.api_url == "https://api.ravenstash.com"
    assert profile.native_registries.pypi.read_base_url == "https://pypi.rvsta.sh"
    assert profile.native_registries.pypi.push_base_url == "https://push.pypi.rvsta.sh"
    assert profile.account_ref is None
    assert profile.credential_store == "keyring"
    assert profile.credential_type is None
    assert profile.expires_at is None
    assert profile.refresh_expires_at is None


def test_delete_token_from_all_stores_ignores_unavailable_store_os_errors(monkeypatch) -> None:
    deleted: list[tuple[str, str]] = []

    def system_delete(service: str, profile: str) -> None:
        assert service == "rvs"
        deleted.append(("keyring", profile))

    def pass_delete(profile: str) -> None:
        deleted.append(("pass", profile))

    def vault_delete(profile: str) -> None:
        deleted.append(("vault", profile))
        raise OSError("vault socket is unavailable")

    def plaintext_delete(profile: str) -> None:
        deleted.append(("plaintext", profile))

    monkeypatch.setattr(auth_mod.stores, "system_delete", system_delete)
    monkeypatch.setattr(auth_mod.stores, "pass_delete", pass_delete)
    monkeypatch.setattr(auth_mod.stores, "vault_delete", vault_delete)
    monkeypatch.setattr(auth_mod.stores, "plaintext_delete", plaintext_delete)

    auth_mod.delete_token_from_all_stores("obsolete")

    assert ("keyring", "obsolete") in deleted
    assert ("vault", "obsolete") in deleted
    assert ("plaintext", "obsolete:refresh") in deleted


def test_get_refresh_token_returns_none_when_keyring_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path, 'credential_store = "auto"')
    monkeypatch.setattr(auth_mod, "_store_available", lambda _store: False)

    assert auth_mod.get_refresh_token("default") is None


def test_selected_store_honors_profile_pass_choice(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(
        monkeypatch,
        tmp_path,
        """
default_profile = "default"
credential_store = "auto"

[profiles.default]
api_url = "https://api.ravenstash.com"
credential_store = "pass"
""",
    )
    monkeypatch.delenv("RVS_CREDENTIAL_STORE", raising=False)
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(
        auth_mod.stores,
        "pass_status",
        lambda: auth_mod.stores.StoreStatus("pass", True, "pass", "ready", ""),
    )

    assert auth_mod.selected_credential_store("default") == "pass"


def test_auto_store_prefers_usable_os_keyring(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path, 'credential_store = "auto"')
    monkeypatch.delenv("RVS_CREDENTIAL_STORE", raising=False)
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(
        auth_mod.stores,
        "pass_status",
        lambda: auth_mod.stores.StoreStatus("pass", True, "pass", "ready", ""),
    )

    assert auth_mod.selected_credential_store("default") == "keyring"


def test_preflight_round_trips_disposable_secret(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path, 'credential_store = "auto"')
    values: dict[str, str] = {}
    deleted: list[str] = []
    monkeypatch.setattr(auth_mod, "_store_available", lambda store: store == "pass")
    monkeypatch.setattr(
        auth_mod,
        "_store_set",
        lambda store, account, secret: values.__setitem__(account, secret),
    )
    monkeypatch.setattr(
        auth_mod,
        "_store_get",
        lambda store, account, strict=False: values.get(account),
    )
    monkeypatch.setattr(
        auth_mod,
        "_store_delete",
        lambda store, account, strict=False: (deleted.append(account), values.pop(account, None)),
    )

    assert auth_mod.preflight_credential_store("default") == "pass"
    assert values == {}
    assert len(deleted) == 1
    assert deleted[0].startswith("__probe__:")


def test_auto_preflight_falls_back_after_keyring_round_trip_failure(monkeypatch) -> None:
    attempts: list[str] = []
    monkeypatch.setattr(auth_mod, "_store_available", lambda store: store in {"keyring", "pass"})

    def preflight(store: str) -> None:
        attempts.append(store)
        if store == "keyring":
            raise RuntimeError("keyring is locked")

    monkeypatch.setattr(auth_mod, "_preflight_store", preflight)

    assert auth_mod.preflight_credential_store("default", "auto") == "pass"
    assert attempts == ["keyring", "pass"]


def test_auto_preflight_requests_setup_when_detected_store_fails(monkeypatch) -> None:
    monkeypatch.setattr(auth_mod, "_store_available", lambda store: store == "keyring")
    monkeypatch.setattr(
        auth_mod,
        "_preflight_store",
        lambda store: (_ for _ in ()).throw(RuntimeError("keyring is locked")),
    )

    with pytest.raises(auth_mod.NoCredentialStoreError, match="keyring is locked"):
        auth_mod.preflight_credential_store("default", "auto")


def test_auto_never_selects_plaintext_implicitly(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path, 'credential_store = "auto"')
    monkeypatch.setattr(auth_mod, "_store_available", lambda store: store == "plaintext")

    with pytest.raises(auth_mod.NoCredentialStoreError):
        auth_mod.preflight_credential_store("default", "auto")
