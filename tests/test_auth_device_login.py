import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import ANY
from uuid import UUID

import pytest
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pathlib import Path

from rvs import client as client_mod
from rvs import config as cfg_mod
from rvs import interactive
from rvs.auth import commands as auth_cmd
from rvs.auth import credentials as auth_mod
from rvs.auth import device as login_mod


runner = CliRunner()

# Discovery shape: keyed by format, one nullable field set, unknown formats allowed.
NATIVE_REGISTRIES = {
    "pypi": {
        "read_base_url": "https://pypi.rvsta.sh",
        "push_base_url": "https://push.pypi.rvsta.sh",
        "mirror_base_url": "https://mirror.pypi.rvsta.sh",
        "registry_base_url": None,
    },
    "npm": {
        "read_base_url": "https://npm.rvsta.sh",
        "push_base_url": "https://push.npm.rvsta.sh",
        "mirror_base_url": "https://mirror.npm.rvsta.sh",
        "registry_base_url": None,
    },
    "maven": {
        "read_base_url": "https://maven.rvsta.sh",
        "push_base_url": "https://push.maven.rvsta.sh",
        "mirror_base_url": "https://mirror.maven.rvsta.sh",
        "registry_base_url": None,
    },
    "oci": {
        "read_base_url": None,
        "push_base_url": None,
        "mirror_base_url": None,
        "registry_base_url": "https://oci.rvsta.sh",
    },
    "cargo": {
        "read_base_url": None,
        "push_base_url": None,
        "mirror_base_url": None,
        "registry_base_url": None,
    },
}


class _FakeStdin:
    def __init__(self, value: str) -> None:
        self._chars = list(value)

    def read(self, size: int) -> str:
        del size
        if not self._chars:
            return ""
        return self._chars.pop(0)

    @property
    def has_pending(self) -> bool:
        return bool(self._chars)


class _FakeFdStdin:
    def fileno(self) -> int:
        return 0

    def read(self, _size: int) -> str:
        raise AssertionError("selector should use os.read for real stdin fds")


def _write_profiles_config(
    config_dir: Path,
    default_profile: str = "default",
    refresh_expires_at: str = "2099-01-01T04:00:00+00:00",
    credential_type: str = "expiring",
    default_extra: str = "",
) -> None:
    config_file = config_dir / "config.toml"
    config_dir.mkdir()
    config_file.write_text(
        f"""
schema_version = 1
default_profile = "{default_profile}"

[profiles.default]
api_url = "https://api.ravenstash.com"
{default_extra}
account_ref = "ac_23456789"
credential_type = "{credential_type}"
expires_at = "2099-01-01T00:00:00+00:00"
refresh_expires_at = "{refresh_expires_at}"

[profiles.work]
api_url = "https://api.work.example"
account_ref = "ac_3456789a"
credential_type = "{credential_type}"
expires_at = "2099-01-01T00:00:00+00:00"
refresh_expires_at = "{refresh_expires_at}"
""".strip(),
        encoding="utf-8",
    )


def test_rvs_token_takes_precedence_over_keyring(monkeypatch) -> None:
    monkeypatch.setenv("RVS_TOKEN", "rvs_ust" + "A" * 43)
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(auth_mod, "_kr_get", lambda profile: "keyring-token")

    assert auth_mod.get_token("default") == "rvs_ust" + "A" * 43
    assert auth_mod.token_source("default") == "RVS_TOKEN"


def test_rvs_token_prevents_expiring_refresh_attempt(monkeypatch) -> None:
    refreshed: list[str] = []

    monkeypatch.setenv("RVS_TOKEN", "rvs_ust" + "A" * 43)
    monkeypatch.setattr(auth_mod, "_stored_profile_expired", lambda profile: True)
    monkeypatch.setattr(
        auth_mod,
        "refresh_expiring_credential",
        lambda profile: refreshed.append(profile) or "refreshed-token",
    )

    assert auth_mod.get_token("default") == "rvs_ust" + "A" * 43
    assert refreshed == []


def test_expired_stored_token_can_be_loaded_without_refresh(monkeypatch) -> None:
    refreshed: list[str] = []

    monkeypatch.delenv("RVS_TOKEN", raising=False)
    monkeypatch.setattr(auth_mod, "_stored_profile_expired", lambda profile: True)
    monkeypatch.setattr(auth_mod, "selected_credential_store", lambda profile: "keyring")
    monkeypatch.setattr(auth_mod, "_store_get", lambda store, profile: "expired-access")
    monkeypatch.setattr(
        auth_mod,
        "refresh_expiring_credential",
        lambda profile: refreshed.append(profile) or "refreshed-token",
    )

    assert auth_mod.get_token("default", refresh=False) == "expired-access"
    assert auth_mod.credential_needs_refresh("default") is True
    assert refreshed == []


def test_config_token_is_ignored(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    config_file = config_dir / "config.toml"
    config_dir.mkdir()
    config_file.write_text(
        """
schema_version = 1
default_profile = "default"

[profiles.default]
api_url = "https://api.ravenstash.com"
token = "rvs_tok_old"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    monkeypatch.delenv("RVS_TOKEN", raising=False)
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: False)

    assert auth_mod.get_token("default") is None
    assert auth_mod.token_source("default") is None


def test_staging_profile_uses_env_api_url_when_not_configured(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    config_file.write_text('schema_version = 1\ndefault_profile = "default"\n', encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RVS_PROFILE_STAGING_API_URL", "https://staging.example.test")

    assert cfg_mod.load().active_profile("staging").api_url == "https://staging.example.test"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12h", 12 * 60 * 60),
        ("24hour", 24 * 60 * 60),
        ("36hours", 36 * 60 * 60),
        ("1d", 24 * 60 * 60),
        ("5day", 5 * 24 * 60 * 60),
        ("3days", 3 * 24 * 60 * 60),
        ("1month", 30 * 24 * 60 * 60),
        ("2 months", 60 * 24 * 60 * 60),
        ("6months", 180 * 24 * 60 * 60),
    ],
)
def test_parse_duration_seconds(raw: str, expected: int) -> None:
    assert login_mod.parse_duration_seconds(raw) == expected


@pytest.mark.parametrize("raw", ["59m", "0h", "11hours", "181days", "1year", "abc"])
def test_parse_duration_seconds_rejects_out_of_range_or_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        login_mod.parse_duration_seconds(raw)


def test_profile_use_sets_default_profile(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")

    result = runner.invoke(auth_cmd.profile_app, ["use", "work"])

    assert result.exit_code == 0
    assert cfg_mod.load().default_profile == "work"
    assert "Local profile 'work' selected for persisted default." in result.output


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        ("\x1b[A", interactive.KEY_UP),
        ("\x1b[B", interactive.KEY_DOWN),
        ("\x1bOA", interactive.KEY_UP),
        ("\x1bOB", interactive.KEY_DOWN),
        ("\x1b[1;5A", interactive.KEY_UP),
        ("\x1b[1;5B", interactive.KEY_DOWN),
    ],
)
def test_auth_switch_reads_arrow_key_sequences(
    monkeypatch,
    sequence: str,
    expected: str,
) -> None:
    import select as select_mod

    fake_stdin = _FakeStdin(sequence)

    def fake_select(
        readers: list[Any], *_args: Any, **_kwargs: Any
    ) -> tuple[list[Any], list[Any], list[Any]]:
        return (readers if fake_stdin.has_pending else [], [], [])

    monkeypatch.setattr(interactive.sys, "platform", "linux")
    monkeypatch.setattr(interactive.sys, "stdin", fake_stdin)
    monkeypatch.setattr(select_mod, "select", fake_select)

    assert interactive._read_selector_key() == expected


def test_auth_switch_reads_arrow_key_sequences_from_unbuffered_fd(monkeypatch) -> None:
    import select as select_mod

    pending = list(b"\x1b[B")

    def fake_os_read(fd: int, size: int) -> bytes:
        assert fd == 0
        assert size == 1
        if not pending:
            return b""
        return bytes([pending.pop(0)])

    def fake_select(
        readers: list[Any], *_args: Any, **_kwargs: Any
    ) -> tuple[list[Any], list[Any], list[Any]]:
        assert readers == [0]
        return (readers if pending else [], [], [])

    monkeypatch.setattr(interactive.sys, "platform", "linux")
    monkeypatch.setattr(interactive.sys, "stdin", _FakeFdStdin())
    monkeypatch.setattr(interactive.os, "read", fake_os_read)
    monkeypatch.setattr(select_mod, "select", fake_select)

    assert interactive._read_selector_key() == interactive.KEY_DOWN


def test_auth_logout_uses_current_profile(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir, default_profile="work")
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    deleted: list[str] = []
    monkeypatch.setattr(auth_cmd.auth_mod, "delete_token", lambda profile: deleted.append(profile))

    result = runner.invoke(auth_cmd.app, ["logout"])

    assert result.exit_code == 0
    assert deleted == ["work"]
    assert "Credentials removed for profile 'work'." in result.output


def test_auth_logout_all_profiles(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    deleted: list[str] = []
    monkeypatch.setattr(auth_cmd.auth_mod, "delete_token", lambda profile: deleted.append(profile))

    result = runner.invoke(auth_cmd.app, ["logout", "--all"])

    assert result.exit_code == 0
    assert deleted == ["default", "work"]
    assert "Credentials removed for all profiles." in result.output


def test_auth_delete_profile_removes_config_and_token(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir, default_profile="work")
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    deleted: list[str] = []
    monkeypatch.setattr(auth_cmd.auth_mod, "delete_token", lambda profile: deleted.append(profile))

    result = runner.invoke(auth_cmd.profile_app, ["delete"])
    cfg = cfg_mod.load()

    assert result.exit_code == 0
    assert deleted == ["work"]
    assert "work" not in cfg.profiles
    assert cfg.default_profile == "default"
    assert "Profile 'work' deleted." in result.output


def test_auth_delete_all_profiles(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    deleted: list[str] = []
    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "delete_token_from_all_stores",
        lambda profile: deleted.append(profile),
    )

    result = runner.invoke(auth_cmd.profile_app, ["delete", "-a"])
    cfg = cfg_mod.load()

    assert result.exit_code == 0
    assert deleted == ["default", "work"]
    assert cfg.profiles == {}
    assert cfg.default_profile == "default"
    assert "All profiles deleted." in result.output


def test_auth_delete_all_profiles_resets_config_before_v5(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    config_file = config_dir / "config.toml"
    config_file.write_text(
        config_file.read_text(encoding="utf-8").replace("schema_version = 1", "config_version = 4"),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    deleted: list[str] = []
    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "delete_token_from_all_stores",
        lambda profile: deleted.append(profile),
    )

    result = runner.invoke(auth_cmd.profile_app, ["delete", "--all"])

    assert result.exit_code == 0
    assert deleted == ["default", "work"]
    assert cfg_mod.load() == cfg_mod.RvsConfig()
    assert "All profiles deleted." in result.output


@dataclass
class _FakeResponse:
    status_code: int
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if not self.is_success:
            raise AssertionError(f"unexpected HTTP failure: {self.payload}")


ARTIFACTS_META = _FakeResponse(
    200, {"formats": ["pypi", "npm", "maven", "oci"], "native_registries": NATIVE_REGISTRIES}
)
META_URL = "https://api.ravenstash.com/v0/artifacts/meta"


class _FakeClient:
    responses: ClassVar[list[_FakeResponse]] = []
    requests: ClassVar[list[tuple[str, dict[str, Any] | None, dict[str, str] | None]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self.requests.append((url, json, headers))
        return self.responses.pop(0)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        assert headers is not None
        assert "Authorization" not in headers
        self.requests.append((url, None, headers))
        return self.responses.pop(0)


def test_device_login_handles_slow_down_and_stores_expiring_jwt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".rvs"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    _FakeClient.requests = []
    _FakeClient.responses = [
        _FakeResponse(
            200,
            {
                "device_code": "device-code",
                "user_code": "ABCD-1234",
                "verification_uri": "https://api.ravenstash.com/login/device",
                "verification_uri_complete": "https://api.ravenstash.com/login/device",
                "expires_in": 600,
                "interval": 5,
            },
        ),
        _FakeResponse(400, {"error": "authorization_pending"}),
        _FakeResponse(400, {"error": "slow_down"}),
        _FakeResponse(
            200,
            {
                "access_token": "jwt-token",
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_in": 900,
                "refresh_expires_in": 14400,
                "account_ref": "ac_23456789",
            },
        ),
        ARTIFACTS_META,
    ]
    stored_tokens: list[tuple[str, str]] = []
    stored_refresh_tokens: list[tuple[str, str]] = []
    metadata_writes: list[dict[str, Any]] = []
    sleeps: list[int] = []

    monkeypatch.setattr(login_mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(
        login_mod.auth_mod,
        "set_token",
        lambda profile, token: stored_tokens.append((profile, token)),
    )
    monkeypatch.setattr(
        login_mod.auth_mod,
        "set_refresh_token",
        lambda profile, token: stored_refresh_tokens.append((profile, token)),
    )
    monkeypatch.setattr(
        login_mod.cfg_mod,
        "set_profile_metadata",
        lambda profile, **kwargs: metadata_writes.append({"profile": profile, **kwargs}),
    )
    monkeypatch.setattr(login_mod.auth_mod, "has_active_expiring_session", lambda profile: False)
    monkeypatch.setattr(login_mod, "_rvs_version", lambda: "0.15.3")
    monkeypatch.setattr(login_mod, "_device_platform", lambda: "linux")
    monkeypatch.setattr(login_mod.time, "sleep", lambda seconds: sleeps.append(seconds))

    login_mod.perform_device_login(
        profile="default",
        api_url="https://api.ravenstash.com",
        no_browser=False,
        duration="12h",
    )

    assert stored_tokens == [("default", "jwt-token")]
    assert stored_refresh_tokens == [("default", "refresh-token")]
    assert len(metadata_writes) == 1
    assert metadata_writes[0]["profile"] == "default"
    assert metadata_writes[0]["api_url"] == "https://api.ravenstash.com"
    assert metadata_writes[0]["account_ref"] == "ac_23456789"
    assert "native_registries" not in metadata_writes[0]
    assert metadata_writes[0]["credential_type"] == "expiring"
    assert metadata_writes[0]["expires_at"]
    assert metadata_writes[0]["refresh_expires_at"]
    assert sleeps == [5, 10]
    assert [url for url, _payload, _headers in _FakeClient.requests] == [
        "https://api.ravenstash.com/v0/platform/auth/device/code",
        "https://api.ravenstash.com/v0/platform/auth/device/token",
        "https://api.ravenstash.com/v0/platform/auth/device/token",
        "https://api.ravenstash.com/v0/platform/auth/device/token",
        META_URL,
    ]
    first_payload = _FakeClient.requests[0][1]
    assert first_payload is not None
    assert first_payload["platform"] == "linux"
    assert first_payload["requested_duration_seconds"] == 12 * 60 * 60
    assert [headers for _url, _payload, headers in _FakeClient.requests[:4]] == [
        {"User-Agent": "rvs/0.15.3"},
        {"User-Agent": "rvs/0.15.3"},
        {"User-Agent": "rvs/0.15.3"},
        {"User-Agent": "rvs/0.15.3"},
    ]


def test_device_login_waits_for_retry_after_when_rate_limited(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".rvs"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    _FakeClient.requests = []
    _FakeClient.responses = [
        _FakeResponse(
            200,
            {
                "device_code": "device-code",
                "user_code": "ABCD-1234",
                "verification_uri": "https://api.ravenstash.com/login/device",
                "verification_uri_complete": "https://api.ravenstash.com/login/device",
                "expires_in": 600,
                "interval": 5,
            },
        ),
        _FakeResponse(
            429,
            {"error": {"code": "RateLimited", "message": "Too many requests"}},
            {"Retry-After": "12"},
        ),
        _FakeResponse(
            503,
            {"error": {"code": "CentralUnavailable", "message": "Try again"}},
        ),
        _FakeResponse(400, {"error": "authorization_pending"}),
        _FakeResponse(
            429,
            {"error": {"code": "RateLimited", "message": "Too many requests"}},
            {"Retry-After": "86400"},
        ),
        _FakeResponse(
            200,
            {
                "access_token": "jwt-token",
                "refresh_token": "refresh-token",
                "token_type": "Bearer",
                "expires_in": 900,
                "refresh_expires_in": 14400,
                "account_ref": "ac_23456789",
            },
        ),
        ARTIFACTS_META,
    ]
    sleeps: list[int] = []
    monkeypatch.setattr(login_mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(login_mod.auth_mod, "set_token", lambda profile, token: None)
    monkeypatch.setattr(login_mod.auth_mod, "set_refresh_token", lambda profile, token: None)
    monkeypatch.setattr(login_mod.cfg_mod, "set_profile_metadata", lambda profile, **kwargs: None)
    monkeypatch.setattr(login_mod.auth_mod, "has_active_expiring_session", lambda profile: False)
    monkeypatch.setattr(login_mod, "_rvs_version", lambda: "0.15.3")
    monkeypatch.setattr(login_mod, "_device_platform", lambda: "linux")
    monkeypatch.setattr(login_mod.time, "sleep", lambda seconds: sleeps.append(seconds))

    login_mod.perform_device_login(
        profile="default",
        api_url="https://api.ravenstash.com",
        no_browser=False,
        duration="12h",
    )

    # Each backoff applies once: a 429 waits as asked, a 503 without Retry-After
    # waits a little longer than usual, the next poll returns to the normal interval,
    # and no wait outlives the device session.
    assert sleeps[:3] == [12, 10, 5]
    assert 590 <= sleeps[3] <= 600


def test_device_login_replaces_active_profile_and_revokes_previous_refresh(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setenv("RVS_API_URL", "https://api.redirect.example.test")
    monkeypatch.setattr(login_mod.auth_mod, "has_active_expiring_session", lambda profile: True)
    monkeypatch.setattr(login_mod.auth_mod, "get_refresh_token", lambda profile: "old-refresh")
    _FakeClient.requests = []
    _FakeClient.responses = [
        _FakeResponse(
            200,
            {
                "device_code": "device-code",
                "user_code": "ABCD-1234",
                "verification_uri": "https://api.ravenstash.com/login/device",
                "verification_uri_complete": "https://api.ravenstash.com/login/device",
                "expires_in": 600,
                "interval": 5,
            },
        ),
        _FakeResponse(
            200,
            {
                "access_token": "jwt-token",
                "refresh_token": "new-refresh-token",
                "token_type": "Bearer",
                "expires_in": 900,
                "refresh_expires_in": 14400,
                "account_ref": "ac_23456789",
            },
        ),
        ARTIFACTS_META,
    ]
    stored_tokens: list[tuple[str, str]] = []
    stored_refresh_tokens: list[tuple[str, str]] = []
    revoked: list[tuple[str, str]] = []
    info_messages: list[str] = []
    warnings: list[str] = []

    monkeypatch.setattr(login_mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(login_mod.output, "warn", warnings.append)
    monkeypatch.setattr(
        login_mod.auth_mod,
        "set_token",
        lambda profile, token: stored_tokens.append((profile, token)),
    )
    monkeypatch.setattr(
        login_mod.auth_mod,
        "set_refresh_token",
        lambda profile, token: stored_refresh_tokens.append((profile, token)),
    )
    monkeypatch.setattr(
        login_mod.auth_mod,
        "revoke_device_refresh_token",
        lambda api_url, token: revoked.append((api_url, token)) or True,
    )
    monkeypatch.setattr(login_mod.output, "info", lambda message: info_messages.append(message))
    monkeypatch.setattr(login_mod, "_rvs_version", lambda: "0.15.3")
    monkeypatch.setattr(login_mod, "_device_platform", lambda: "linux")

    login_mod.perform_device_login(
        profile="default",
        api_url=None,
        no_browser=True,
    )

    assert stored_tokens == [("default", "jwt-token")]
    assert stored_refresh_tokens == [("default", "new-refresh-token")]
    assert revoked == [("https://api.ravenstash.com", "old-refresh")]
    assert any("already authenticated" in message for message in info_messages)
    assert any("RVS_API_URL is ignored" in message for message in warnings)
    profile = cfg_mod.load().profiles["default"]
    # An environment override never redirects a saved profile's login.
    assert profile.api_url == "https://api.ravenstash.com"
    assert profile.credential_api_url == "https://api.ravenstash.com"
    assert profile.refresh_expires_at is not None
    assert all(
        request[0].startswith("https://api.ravenstash.com/") for request in _FakeClient.requests
    )
    assert profile.native_registries.pypi.read_base_url == "https://pypi.rvsta.sh"
    assert profile.native_registries.npm.push_base_url == "https://push.npm.rvsta.sh"
    assert profile.native_registries.oci_registry_base_url == "https://oci.rvsta.sh"


def test_device_login_start_failure_does_not_replace_stored_api_origin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setenv("RVS_API_URL", "https://api.redirect.example.test")
    monkeypatch.setattr(login_mod.auth_mod, "has_active_expiring_session", lambda profile: False)
    _FakeClient.requests = []
    _FakeClient.responses = [_FakeResponse(503, {"error": "unavailable"})]
    monkeypatch.setattr(login_mod.httpx, "Client", _FakeClient)

    with pytest.raises(SystemExit):
        login_mod.perform_device_login(
            profile="default",
            api_url=None,
            no_browser=True,
        )

    assert cfg_mod.stored_profile_api_url("default") == "https://api.ravenstash.com"


def test_device_login_does_not_revoke_expired_previous_refresh(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(
        config_dir,
        refresh_expires_at="2000-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(login_mod.auth_mod, "has_active_expiring_session", lambda profile: False)
    _FakeClient.requests = []
    _FakeClient.responses = [
        _FakeResponse(
            200,
            {
                "device_code": "device-code",
                "user_code": "ABCD-1234",
                "verification_uri": "https://api.ravenstash.com/login/device",
                "verification_uri_complete": "https://api.ravenstash.com/login/device",
                "expires_in": 600,
                "interval": 5,
            },
        ),
        _FakeResponse(
            200,
            {
                "access_token": "jwt-token",
                "refresh_token": "new-refresh-token",
                "token_type": "Bearer",
                "expires_in": 900,
                "refresh_expires_in": 14400,
                "account_ref": "ac_23456789",
            },
        ),
    ]
    revoked: list[tuple[str, str]] = []
    info_messages: list[str] = []
    warnings: list[str] = []

    monkeypatch.setattr(login_mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(login_mod.output, "warn", warnings.append)
    monkeypatch.setattr(login_mod.auth_mod, "set_token", lambda _profile, _token: None)
    monkeypatch.setattr(
        login_mod.auth_mod,
        "set_refresh_token",
        lambda _profile, _token: None,
    )
    monkeypatch.setattr(
        login_mod.auth_mod,
        "revoke_device_refresh_token",
        lambda api_url, token: revoked.append((api_url, token)) or True,
    )
    monkeypatch.setattr(login_mod.output, "info", lambda message: info_messages.append(message))
    monkeypatch.setattr(login_mod, "_rvs_version", lambda: "0.15.3")
    monkeypatch.setattr(login_mod, "_device_platform", lambda: "linux")

    login_mod.perform_device_login(
        profile="default",
        api_url=None,
        no_browser=True,
    )

    assert revoked == []
    assert not any("already authenticated" in message for message in info_messages)


@pytest.mark.parametrize("error", [OSError("disk full"), ValueError("invalid schema")])
def test_store_expiring_credential_rolls_back_when_metadata_cannot_be_saved(
    monkeypatch, error: Exception
) -> None:
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(login_mod.auth_mod, "set_token", lambda *_args: None)
    monkeypatch.setattr(login_mod.auth_mod, "set_refresh_token", lambda *_args: None)
    monkeypatch.setattr(
        login_mod.auth_mod,
        "delete_token_from_store",
        lambda profile, store: deleted.append((profile, store)),
    )
    monkeypatch.setattr(
        login_mod.cfg_mod,
        "set_profile_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(RuntimeError, match=str(error)):
        login_mod._store_expiring_credential(
            profile="default",
            api_url="https://api.ravenstash.com",
            token="access",
            refresh_token="refresh",
            account_ref="ac_23456789",
            expires_in=900,
            refresh_expires_in=14400,
            credential_store="pass",
        )

    assert deleted == [("default", "pass")]


def test_refresh_expiring_credential_rotates_tokens(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(
        config_dir,
        credential_type="expiring",
        default_extra='repository_domain = "packages.enterprise.example"',
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    # A saved profile keeps its stored domain; the environment cannot redirect it.
    monkeypatch.setenv("RVS_REPOSITORY_DOMAIN", "attacker.example.test")
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(
        auth_mod,
        "_kr_get",
        lambda profile: "old-refresh" if profile == "default:refresh" else None,
    )
    stored: list[tuple[str, str]] = []
    _FakeClient.requests = []
    _FakeClient.responses = [
        _FakeResponse(
            200,
            {
                "access_token": "new-jwt",
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
                "expires_in": 900,
                "refresh_expires_in": 14400,
                "account_ref": "ac_23456789",
            },
        ),
        ARTIFACTS_META,
    ]

    monkeypatch.setattr(auth_mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(auth_mod, "_rvs_user_agent", lambda: "rvs/0.15.3")
    monkeypatch.setattr(auth_mod, "_device_platform", lambda: "linux")
    monkeypatch.setattr(auth_mod, "_kr_set", lambda profile, token: stored.append((profile, token)))

    shared_client = _FakeClient()
    assert auth_mod.refresh_expiring_credential("default", http_client=shared_client) == "new-jwt"
    assert stored == [
        ("default:refresh", "new-refresh"),
        ("default", "new-jwt"),
    ]
    assert _FakeClient.requests == [
        (
            "https://api.ravenstash.com/v0/platform/auth/device/refresh",
            {"refresh_token": "old-refresh", "platform": "linux", "operation_id": ANY},
            {"User-Agent": "rvs/0.15.3"},
        ),
        (META_URL, None, ANY),
    ]
    profile = cfg_mod.load().profiles["default"]
    assert profile.credential_type == "expiring"
    assert (
        profile.native_registries.pypi.read_base_url == "https://pypi.packages.enterprise.example"
    )
    assert (
        profile.native_registries.pypi.push_base_url
        == "https://push.pypi.packages.enterprise.example"
    )
    assert (
        profile.native_registries.oci_registry_base_url == "https://oci.packages.enterprise.example"
    )
    assert profile.refresh_expires_at is not None


def test_revoke_device_refresh_token_posts_to_the_api(monkeypatch) -> None:
    _FakeClient.requests = []
    _FakeClient.responses = [_FakeResponse(204, {})]

    monkeypatch.setattr(auth_mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(auth_mod, "_rvs_user_agent", lambda: "rvs/0.15.3")

    assert auth_mod.revoke_device_refresh_token(
        "https://api.ravenstash.com",
        "old-refresh",
    )
    assert _FakeClient.requests == [
        (
            "https://api.ravenstash.com/v0/platform/auth/device/revoke",
            {"refresh_token": "old-refresh"},
            {"User-Agent": "rvs/0.15.3"},
        )
    ]


def test_refresh_storage_failure_revokes_and_removes_partial_pair(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(
        auth_mod,
        "_kr_get",
        lambda profile: "old-refresh" if profile == "default:refresh" else None,
    )
    _FakeClient.requests = []
    _FakeClient.responses = [
        _FakeResponse(
            200,
            {
                "access_token": "new-jwt",
                "refresh_token": "new-refresh",
                "expires_in": 900,
                "refresh_expires_in": 14400,
            },
        )
    ]
    writes: list[str] = []
    deleted: list[tuple[str, str]] = []
    revoked: list[tuple[str, str]] = []

    def fail_second_write(profile: str, _token: str) -> None:
        writes.append(profile)
        if profile == "default":
            raise RuntimeError("locked during write")

    monkeypatch.setattr(auth_mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(
        auth_mod, "_store_set", lambda _store, profile, token: fail_second_write(profile, token)
    )
    monkeypatch.setattr(
        auth_mod,
        "delete_token_from_store",
        lambda profile, store: deleted.append((profile, store)),
    )
    monkeypatch.setattr(
        auth_mod,
        "revoke_device_refresh_token",
        lambda api_url, token: revoked.append((api_url, token)) or True,
    )

    assert auth_mod.refresh_expiring_credential("default") is None
    assert writes == ["default:refresh", "default"]
    assert deleted == [("default", "keyring")]
    assert revoked == [("https://api.ravenstash.com", "new-refresh")]
    assert cfg_mod.load().profiles["default"].credential_type is None


def test_refresh_expiring_credential_failure_clears_profile(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(
        auth_mod,
        "_kr_get",
        lambda profile: "old-refresh" if profile == "default:refresh" else None,
    )
    deleted: list[str] = []
    _FakeClient.requests = []
    _FakeClient.responses = [
        _FakeResponse(401, {"detail": "Invalid refresh token"}),
    ]

    monkeypatch.setattr(auth_mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(auth_mod, "_rvs_user_agent", lambda: "rvs/0.15.3")
    monkeypatch.setattr(auth_mod, "_device_platform", lambda: "linux")
    monkeypatch.setattr(auth_mod, "delete_token", lambda profile: deleted.append(profile))

    assert auth_mod.refresh_expiring_credential("default") is None
    assert deleted == ["default"]
    assert _FakeClient.requests == [
        (
            "https://api.ravenstash.com/v0/platform/auth/device/refresh",
            {"refresh_token": "old-refresh", "platform": "linux", "operation_id": ANY},
            {"User-Agent": "rvs/0.15.3"},
        )
    ]


def test_ambiguous_refresh_keeps_operation_across_invocations(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(
        auth_mod, "_kr_get", lambda profile: "old-refresh" if profile.endswith(":refresh") else None
    )
    deleted: list[str] = []
    monkeypatch.setattr(auth_mod, "delete_token", deleted.append)
    _FakeClient.requests = []
    _FakeClient.responses = [
        _FakeResponse(502, {}),
        _FakeResponse(
            409, {"detail": {"code": "refresh_response_unavailable", "recovery": "sign_in"}}
        ),
    ]
    monkeypatch.setattr(auth_mod.httpx, "Client", _FakeClient)

    assert auth_mod.refresh_expiring_credential("default") is None
    pending = list(config_dir.glob("*.pending.json"))
    assert len(pending) == 1
    if os.name != "nt":
        assert pending[0].stat().st_mode & 0o777 == 0o600
    metadata = pending[0].read_text()
    assert "old-refresh" not in metadata
    operation_id = json.loads(metadata)["operation_id"]
    assert str(UUID(operation_id)) == operation_id
    assert deleted == []

    assert auth_mod.refresh_expiring_credential("default") is None
    assert deleted == ["default"]
    assert not list(config_dir.glob("*.pending.json"))
    assert [body["operation_id"] for _, body, _ in _FakeClient.requests if body] == [
        operation_id,
        operation_id,
    ]


def test_concurrent_refresh_reuses_rotated_pair(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)

    credentials = {
        "default": "old-access",
        "default:refresh": "old-refresh",
    }
    credentials_guard = threading.Lock()
    first_has_lock = threading.Event()
    second_read_old_refresh = threading.Event()
    original_refresh_lock = auth_mod._profile_refresh_lock

    def credential_get(account: str) -> str | None:
        with credentials_guard:
            value = credentials.get(account)
        if (
            threading.current_thread().name == "refresh-two"
            and account == "default:refresh"
            and value == "old-refresh"
        ):
            second_read_old_refresh.set()
        return value

    def credential_set(account: str, value: str) -> None:
        with credentials_guard:
            credentials[account] = value

    @contextmanager
    def tracked_refresh_lock(profile: str):
        with original_refresh_lock(profile):
            if threading.current_thread().name == "refresh-one":
                first_has_lock.set()
            yield

    http_calls: list[str] = []

    class CoordinatedClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            del args

        def post(self, _url, *, headers, json):
            del headers
            http_calls.append(json["refresh_token"])
            assert second_read_old_refresh.wait(timeout=5)
            return _FakeResponse(
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 900,
                    "refresh_expires_in": 14400,
                },
            )

        def get(self, _url, *, headers, timeout=None):
            del headers, timeout
            return _FakeResponse(503, {})

    monkeypatch.setattr(auth_mod, "_kr_get", credential_get)
    monkeypatch.setattr(auth_mod, "_kr_set", credential_set)
    monkeypatch.setattr(auth_mod, "_profile_refresh_lock", tracked_refresh_lock)
    monkeypatch.setattr(auth_mod.httpx, "Client", CoordinatedClient)

    results: dict[str, str | None] = {}

    def refresh() -> None:
        results[threading.current_thread().name] = auth_mod.refresh_expiring_credential(
            "default", stale_access_token="old-access"
        )

    first = threading.Thread(target=refresh, name="refresh-one")
    second = threading.Thread(target=refresh, name="refresh-two")
    first.start()
    assert first_has_lock.wait(timeout=5)
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert results == {"refresh-one": "new-access", "refresh-two": "new-access"}
    assert http_calls == ["old-refresh"]


_DEVICE_SESSION = {
    "device_code": "device-code",
    "user_code": "ABCD-1234",
    "verification_uri": "https://api.ravenstash.com/login/device",
    "verification_uri_complete": "https://api.ravenstash.com/login/device",
    "expires_in": 600,
    "interval": 5,
}
_DEVICE_TOKEN = {
    "access_token": "jwt-token",
    "refresh_token": "new-refresh-token",
    "token_type": "bearer",
    "expires_in": 900,
    "refresh_expires_in": 14400,
    "account_ref": "ac_23456789",
}
_RETIRED = {
    "error": {
        "code": "ApiRouteRetired",
        "message": "This rvs release uses a retired API route. Upgrade rvs.",
        "details": None,
    },
    "request_id": "req_1",
}


def _isolated_login(monkeypatch, tmp_path: Path) -> Path:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.delenv("RVS_REPOSITORY_DOMAIN", raising=False)
    monkeypatch.delenv("RVS_API_URL", raising=False)
    monkeypatch.setattr(login_mod.auth_mod, "has_active_expiring_session", lambda profile: False)
    monkeypatch.setattr(login_mod.auth_mod, "set_token", lambda _profile, _token: None)
    monkeypatch.setattr(login_mod.auth_mod, "set_refresh_token", lambda _profile, _token: None)
    monkeypatch.setattr(login_mod.httpx, "Client", _FakeClient)
    monkeypatch.setattr(login_mod, "_rvs_version", lambda: "0.15.8")
    monkeypatch.setattr(login_mod, "_device_platform", lambda: "linux")
    _FakeClient.requests = []
    return config_dir


@pytest.mark.parametrize(
    "meta_response",
    [
        _FakeResponse(503, {"error": {"code": "ServiceUnavailable", "message": "down"}}),
        _FakeResponse(200, {"formats": ["pypi"]}),
        _FakeResponse(200, {"native_registries": {"pypi": {"read_base_url": "ftp://x"}}}),
    ],
)
def test_device_login_keeps_stored_registries_when_artifacts_discovery_fails(
    monkeypatch, tmp_path: Path, meta_response: _FakeResponse, capsys
) -> None:
    _isolated_login(monkeypatch, tmp_path)
    before = cfg_mod.load().profiles["default"].native_registries
    _FakeClient.responses = [
        _FakeResponse(200, _DEVICE_SESSION),
        _FakeResponse(200, _DEVICE_TOKEN),
        meta_response,
    ]

    login_mod.perform_device_login(profile="default", api_url=None, no_browser=True)

    profile = cfg_mod.load().profiles["default"]
    assert profile.credential_type == "expiring"
    assert profile.native_registries == before
    assert [url for url, _payload, _headers in _FakeClient.requests][-1] == META_URL
    assert "Could not refresh native registry endpoints" in " ".join(
        capsys.readouterr().err.split()
    )


def test_device_login_surfaces_retired_route_message(monkeypatch, tmp_path: Path, capsys) -> None:
    _isolated_login(monkeypatch, tmp_path)
    _FakeClient.responses = [_FakeResponse(410, _RETIRED)]

    with pytest.raises(SystemExit):
        login_mod.perform_device_login(profile="default", api_url=None, no_browser=True)

    err = " ".join(capsys.readouterr().err.split())
    assert "This rvs release is no longer supported by the Ravenstash API." in err
    assert "run `rvs update`" in err


def test_device_token_poll_surfaces_retired_route_message(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    _isolated_login(monkeypatch, tmp_path)
    _FakeClient.responses = [_FakeResponse(200, _DEVICE_SESSION), _FakeResponse(410, _RETIRED)]

    with pytest.raises(SystemExit):
        login_mod.perform_device_login(profile="default", api_url=None, no_browser=True)

    err = " ".join(capsys.readouterr().err.split())
    assert "This rvs release is no longer supported by the Ravenstash API." in err
    assert "run `rvs update`" in err


def test_device_login_reports_success_before_a_retired_post_login_route(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    _isolated_login(monkeypatch, tmp_path)
    _FakeClient.responses = [
        _FakeResponse(200, _DEVICE_SESSION),
        _FakeResponse(200, _DEVICE_TOKEN),
        _FakeResponse(410, _RETIRED),
    ]

    with pytest.raises(SystemExit) as raised:
        login_mod.perform_device_login(profile="default", api_url=None, no_browser=True)

    assert raised.value.code == 1
    captured = capsys.readouterr()
    text = " ".join((captured.out + captured.err).split())
    # The credential is stored, so the login is reported as a success first.
    assert "Authenticated profile 'default'" in text
    assert "This rvs release is no longer supported by the Ravenstash API." in text
    assert "Login failed" not in text


def _isolated_refresh(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.delenv("RVS_REPOSITORY_DOMAIN", raising=False)
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(
        auth_mod,
        "_kr_get",
        lambda profile: "old-refresh" if profile == "default:refresh" else None,
    )
    monkeypatch.setattr(auth_mod, "_kr_set", lambda _profile, _token: None)
    monkeypatch.setattr(auth_mod.httpx, "Client", _FakeClient)
    _FakeClient.requests = []


def test_refresh_updates_changed_native_registries_from_artifacts_meta(
    monkeypatch, tmp_path: Path
) -> None:
    _isolated_refresh(monkeypatch, tmp_path)
    moved = json.loads(json.dumps(NATIVE_REGISTRIES).replace("rvsta.sh", "packages.example.net"))
    _FakeClient.responses = [
        _FakeResponse(200, _DEVICE_TOKEN),
        _FakeResponse(
            200, {"formats": ["pypi", "npm", "maven", "oci"], "native_registries": moved}
        ),
    ]

    assert auth_mod.refresh_expiring_credential("default") == "jwt-token"

    registries = cfg_mod.load().profiles["default"].native_registries
    assert registries.pypi.read_base_url == "https://pypi.packages.example.net"
    assert registries.maven.mirror_base_url == "https://mirror.maven.packages.example.net"
    assert registries.oci_registry_base_url == "https://oci.packages.example.net"
    assert [url for url, _payload, _headers in _FakeClient.requests] == [
        "https://api.ravenstash.com/v0/platform/auth/device/refresh",
        META_URL,
    ]


def test_refresh_does_not_rewrite_config_when_registries_are_unchanged(
    monkeypatch, tmp_path: Path
) -> None:
    _isolated_refresh(monkeypatch, tmp_path)
    _FakeClient.responses = [_FakeResponse(200, _DEVICE_TOKEN), ARTIFACTS_META]
    assert auth_mod.refresh_expiring_credential("default") == "jwt-token"
    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cfg_mod, "set_profile_metadata", lambda profile, **kwargs: writes.append(kwargs)
    )
    _FakeClient.requests = []
    _FakeClient.responses = [ARTIFACTS_META]

    from rvs.artifacts.meta import sync_native_registries

    assert sync_native_registries("default", "https://api.ravenstash.com") == "unchanged"
    assert writes == []


@pytest.mark.parametrize(
    "meta_response",
    [
        _FakeResponse(503, {}),
        _FakeResponse(200, ["not", "an", "object"]),  # type: ignore[arg-type]
    ],
)
def test_refresh_keeps_stored_registries_when_artifacts_meta_fails(
    monkeypatch, tmp_path: Path, meta_response: _FakeResponse
) -> None:
    _isolated_refresh(monkeypatch, tmp_path)
    before = cfg_mod.load().profiles["default"].native_registries
    _FakeClient.responses = [_FakeResponse(200, _DEVICE_TOKEN), meta_response]

    assert auth_mod.refresh_expiring_credential("default") == "jwt-token"

    profile = cfg_mod.load().profiles["default"]
    assert profile.native_registries == before
    assert profile.credential_type == "expiring"


def test_refresh_cleanup_finishes_when_the_revoke_route_is_retired(
    monkeypatch, tmp_path: Path
) -> None:
    _isolated_refresh(monkeypatch, tmp_path)
    deleted: list[str] = []
    monkeypatch.setattr(
        cfg_mod,
        "set_profile_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        auth_mod, "delete_token_from_store", lambda profile, _store: deleted.append(profile)
    )
    _FakeClient.responses = [_FakeResponse(200, _DEVICE_TOKEN), _FakeResponse(410, _RETIRED)]

    # A partial rotated pair is still removed; the retired revoke is only logged.
    assert auth_mod.refresh_expiring_credential("default") is None
    assert deleted == ["default"]


def test_refresh_stops_after_storing_the_session_when_artifacts_meta_is_retired(
    monkeypatch, tmp_path: Path
) -> None:
    _isolated_refresh(monkeypatch, tmp_path)
    _FakeClient.responses = [_FakeResponse(200, _DEVICE_TOKEN), _FakeResponse(410, _RETIRED)]

    with pytest.raises(client_mod.ApiRouteRetiredError):
        auth_mod.refresh_expiring_credential("default")

    # The rotated session was stored before discovery reported the retired route.
    assert cfg_mod.load().profiles["default"].credential_type == "expiring"


@pytest.mark.parametrize(
    "response",
    [_FakeResponse(410, _RETIRED), _FakeResponse(410, {"detail": "Gone"})],
)
def test_refresh_reports_a_retired_route_instead_of_logging_it(
    monkeypatch, tmp_path: Path, response: _FakeResponse
) -> None:
    _isolated_refresh(monkeypatch, tmp_path)
    deleted: list[str] = []
    monkeypatch.setattr(auth_mod, "delete_token", deleted.append)
    _FakeClient.responses = [response]

    with pytest.raises(client_mod.ApiRouteRetiredError) as exc_info:
        auth_mod.refresh_expiring_credential("default")

    assert "no longer supported by the Ravenstash API" in str(exc_info.value)
    assert deleted == []


@pytest.mark.parametrize("status", [429, 503])
def test_refresh_honors_retry_after_and_resends_the_same_operation(
    monkeypatch, tmp_path: Path, capsys, status: int
) -> None:
    _isolated_refresh(monkeypatch, tmp_path)
    monkeypatch.setattr(client_mod, "_deprecation_warned", False)
    slept: list[float] = []
    monkeypatch.setattr("rvs.client.time.sleep", slept.append)
    deprecated = {"Deprecation": "@1767225600", "Sunset": "Thu, 01 Jan 2099 00:00:00 GMT"}
    _FakeClient.responses = [
        _FakeResponse(status, {}, {"Retry-After": "3"}),
        _FakeResponse(200, _DEVICE_TOKEN, deprecated),
        ARTIFACTS_META,
    ]

    assert auth_mod.refresh_expiring_credential("default") == "jwt-token"

    assert slept == [3.0]
    first, second = (payload for _url, payload, _headers in _FakeClient.requests[:2])
    assert first is not None and second is not None
    assert first["operation_id"] == second["operation_id"]
    err = " ".join(capsys.readouterr().err.split())
    assert "Ravenstash will retire on 2099-01-01" in err


def test_revoke_reports_a_retired_route(monkeypatch) -> None:
    _FakeClient.requests = []
    _FakeClient.responses = [_FakeResponse(410, {"detail": "not json either"})]
    monkeypatch.setattr(auth_mod.httpx, "Client", _FakeClient)

    with pytest.raises(client_mod.ApiRouteRetiredError):
        auth_mod.revoke_device_refresh_token("https://api.ravenstash.com", "old-refresh")


@pytest.mark.parametrize(
    ("app", "command"),
    [
        (auth_cmd.app, ["logout"]),
        (auth_cmd.app, ["logout", "--all"]),
        (auth_cmd.profile_app, ["delete", "work"]),
        (auth_cmd.profile_app, ["rename", "work", "renamed"]),
    ],
)
def test_signing_out_on_a_retired_route_removes_local_credentials_then_fails(
    monkeypatch, tmp_path: Path, app, command: list[str]
) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir, default_profile="work")
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    calls: list[str] = []

    def revoke(profile: str) -> bool:
        calls.append(f"revoke:{profile}")
        raise client_mod.ApiRouteRetiredError

    monkeypatch.setattr(auth_cmd.auth_mod, "revoke_stored_refresh_token", revoke)
    monkeypatch.setattr(
        auth_cmd.auth_mod, "delete_token", lambda profile: calls.append(f"delete:{profile}")
    )
    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "delete_token_from_all_stores",
        lambda profile: calls.append(f"delete:{profile}"),
    )

    result = runner.invoke(app, command)

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert calls[:2] == [f"revoke:{calls[0].split(':')[1]}", f"delete:{calls[0].split(':')[1]}"]
    output_text = " ".join(result.output.split())
    assert "Could not revoke the device session" in output_text
    assert "This rvs release is no longer supported by the Ravenstash API." in output_text
    assert "run `rvs update`" in output_text


def test_refresh_keeps_stored_registries_when_artifacts_meta_transport_fails(
    monkeypatch, tmp_path: Path
) -> None:
    _isolated_refresh(monkeypatch, tmp_path)
    before = cfg_mod.load().profiles["default"].native_registries

    class _BrokenMetaClient(_FakeClient):
        def get(self, url: str, **_kwargs: Any) -> _FakeResponse:
            raise auth_mod.httpx.ConnectError("offline")

    monkeypatch.setattr(auth_mod.httpx, "Client", _BrokenMetaClient)
    _FakeClient.responses = [_FakeResponse(200, _DEVICE_TOKEN)]

    assert auth_mod.refresh_expiring_credential("default") == "jwt-token"
    assert cfg_mod.load().profiles["default"].native_registries == before


def test_device_login_seeds_a_new_profile_from_environment_overrides(
    monkeypatch, tmp_path: Path
) -> None:
    config_dir = _isolated_login(monkeypatch, tmp_path)
    (config_dir / "config.toml").write_text("schema_version = 1\n", encoding="utf-8")
    monkeypatch.setenv("RVS_PROFILE_DEV_API_URL", "http://localhost:43100")
    monkeypatch.setenv("RVS_PROFILE_DEV_REPOSITORY_DOMAIN", "localhost")
    local_meta = json.loads(
        json.dumps(NATIVE_REGISTRIES).replace("rvsta.sh", "packages.example:43101")
    )
    _FakeClient.responses = [
        _FakeResponse(200, _DEVICE_SESSION),
        _FakeResponse(200, _DEVICE_TOKEN),
        _FakeResponse(200, {"formats": ["pypi"], "native_registries": local_meta}),
    ]

    login_mod.perform_device_login(profile="dev", api_url=None, no_browser=True)

    profile = cfg_mod.load().profiles["dev"]
    assert profile.api_url == "http://localhost:43100"
    assert profile.credential_api_url == "http://localhost:43100"
    assert profile.repository_domain == "localhost"
    # The stored domain, not the environment, rewrites discovered hosts from now on.
    assert profile.native_registries.pypi.read_base_url == "http://localhost:43101"
    assert all(url.startswith("http://localhost:43100/") for url, *_ in _FakeClient.requests)


def test_refresh_never_sends_a_refresh_token_to_another_api_origin(
    monkeypatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(
        config_dir, default_extra='credential_api_url = "https://api.issuer.example"'
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(auth_mod, "_kr_get", lambda profile: "stored-secret")
    monkeypatch.setattr(auth_mod.httpx, "Client", _FakeClient)
    _FakeClient.requests = []
    _FakeClient.responses = []

    assert auth_mod.refresh_expiring_credential("default") is None
    assert _FakeClient.requests == []
    message = auth_mod.credential_origin_error("default")
    assert message is not None
    assert "issued by https://api.issuer.example" in message
    assert "rvs auth login --profile default" in message


def test_api_client_refuses_to_send_a_stored_token_to_another_origin(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from rvs.client import ApiClient

    config_dir = tmp_path / ".rvs"
    _write_profiles_config(
        config_dir, default_extra='credential_api_url = "https://api.issuer.example"'
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.delenv("RVS_TOKEN", raising=False)
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(auth_mod, "_kr_get", lambda profile: "stored-secret")

    with pytest.raises(SystemExit):
        ApiClient.from_profile("default")

    assert "was issued by https://api.issuer.example" in " ".join(capsys.readouterr().err.split())


def test_api_client_uses_env_api_url_with_rvs_token_and_no_saved_profile(
    monkeypatch, tmp_path: Path
) -> None:
    from rvs.client import ApiClient

    config_dir = tmp_path / ".rvs"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    monkeypatch.delenv("RVS_ENV_FILE", raising=False)
    monkeypatch.delenv("RVS_PROFILE", raising=False)
    monkeypatch.setenv("RVS_TOKEN", "rvs_ust" + "A" * 43)
    monkeypatch.setenv("RVS_API_URL", "https://api.ci.example")

    client = ApiClient.from_profile()

    assert client._base == "https://api.ci.example"


def test_revoke_stored_refresh_token_uses_the_issuing_api(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(auth_mod, "_keyring_available", lambda: True)
    monkeypatch.setattr(
        auth_mod, "_kr_get", lambda profile: "old-refresh" if profile == "work:refresh" else None
    )
    revoked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        auth_mod,
        "revoke_device_refresh_token",
        lambda api_url, token: revoked.append((api_url, token)) or True,
    )

    assert auth_mod.revoke_stored_refresh_token("work") is True
    assert auth_mod.revoke_stored_refresh_token("default") is None
    assert revoked == [("https://api.work.example", "old-refresh")]


@pytest.mark.parametrize(
    ("command", "revocation", "expected"),
    [
        (["logout"], True, "revoked on the server"),
        (["logout"], False, "Could not revoke the device session"),
        (["logout", "--all"], False, "Could not revoke the device session"),
    ],
)
def test_logout_revokes_the_server_session_best_effort(
    monkeypatch, tmp_path: Path, command: list[str], revocation: bool, expected: str
) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir, default_profile="work")
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    calls: list[str] = []
    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "revoke_stored_refresh_token",
        lambda profile: calls.append(f"revoke:{profile}") or revocation,
    )
    monkeypatch.setattr(
        auth_cmd.auth_mod, "delete_token", lambda profile: calls.append(f"delete:{profile}")
    )

    result = runner.invoke(auth_cmd.app, command)

    assert result.exit_code == 0
    assert calls[:2] == [f"revoke:{calls[0].split(':')[1]}", f"delete:{calls[0].split(':')[1]}"]
    assert expected in " ".join(result.output.split())


@pytest.mark.parametrize("command", [["delete", "work"], ["rename", "work", "renamed"]])
def test_profile_delete_and_rename_revoke_the_server_session(
    monkeypatch, tmp_path: Path, command: list[str]
) -> None:
    config_dir = tmp_path / ".rvs"
    _write_profiles_config(config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    calls: list[str] = []
    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "revoke_stored_refresh_token",
        lambda profile: calls.append(f"revoke:{profile}") or False,
    )
    monkeypatch.setattr(
        auth_cmd.auth_mod, "delete_token", lambda profile: calls.append(f"delete:{profile}")
    )

    result = runner.invoke(auth_cmd.profile_app, command)

    assert result.exit_code == 0
    assert calls == ["revoke:work", "delete:work"]
    assert "Could not revoke the device session" in " ".join(result.output.split())
