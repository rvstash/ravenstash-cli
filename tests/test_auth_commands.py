from typing import TYPE_CHECKING, Any

from rvs import config as cfg_mod
from rvs.auth import commands as auth_cmd
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()
STAGING_API_URL = "https://staging.example.test"


def _write_config(config_dir: Path, content: str) -> Path:
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    normalized = content.strip()
    if "schema_version" not in normalized:
        normalized = f"schema_version = 1\n{normalized}"
    config_file.write_text(normalized, encoding="utf-8")
    return config_file


def _isolate_config(monkeypatch, tmp_path: Path, content: str) -> None:
    config_dir = tmp_path / ".rvs"
    config_file = _write_config(config_dir, content)
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RVS_ENV_FILE", raising=False)
    monkeypatch.delenv("RVS_PROFILE_STAGING_API_URL", raising=False)


def test_auth_status_reports_env_token_for_builtin_staging_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path, 'default_profile = "default"')
    monkeypatch.setenv("RVS_TOKEN", "rvs_ust" + "A" * 43)
    monkeypatch.setenv("RVS_PROFILE_STAGING_API_URL", STAGING_API_URL)
    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "selected_credential_store",
        lambda profile: (_ for _ in ()).throw(AssertionError("CI token must bypass stores")),
    )

    result = runner.invoke(auth_cmd.app, ["status", "--profile", "staging"])

    assert result.exit_code == 0
    assert "staging" in result.output
    assert "Authenticated" in result.output
    assert "yes" in result.output
    assert "RVS_TOKEN" in result.output
    assert "not used (RVS_TOKEN)" in result.output
    assert STAGING_API_URL not in result.output
    assert "Repository domain" not in result.output
    assert "Owner ID" not in result.output


def test_auth_status_rejects_profile_configuration_options(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path, 'default_profile = "default"')
    monkeypatch.setenv("RVS_TOKEN", "rvs_ust" + "A" * 43)

    result = runner.invoke(auth_cmd.app, ["status", "--verbose"])

    assert result.exit_code == 2
    assert "No such option" in result.output
    assert "verbose" in result.output


def test_auth_status_exits_one_when_profile_has_no_token(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path, 'default_profile = "default"')
    monkeypatch.delenv("RVS_TOKEN", raising=False)
    monkeypatch.setenv("RVS_PROFILE_STAGING_API_URL", STAGING_API_URL)
    monkeypatch.setattr(auth_cmd.auth_mod, "get_token", lambda profile: None)
    monkeypatch.setattr(auth_cmd.auth_mod, "token_source", lambda profile: None)

    result = runner.invoke(auth_cmd.app, ["status", "--profile", "staging"])

    assert result.exit_code == 1
    assert "Authenticated" in result.output
    assert "no" in result.output
    assert STAGING_API_URL not in result.output


def test_auth_whoami_reports_only_verified_user_identity(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(
        monkeypatch,
        tmp_path,
        """
schema_version = 1
default_profile = "work"

[profiles.work]
api_url = "https://api.work.example"
account_ref = "ac_23456789"
""",
    )
    calls: list[str] = []

    class _Response:
        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "principal_type": "user",
                "user": {"handle": "developer", "display_name": None},
                "email": "developer@example.test",
                "personal_account": {
                    "ref": "ac_23456789",
                    "handle": "developer",
                    "type": "personal",
                },
                "credential": None,
            }

    class _Client:
        @staticmethod
        def get(path: str) -> _Response:
            calls.append(path)
            return _Response()

    monkeypatch.setattr(
        auth_cmd.ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: _Client()),
    )

    result = runner.invoke(auth_cmd.app, ["whoami"])

    assert result.exit_code == 0
    assert calls == ["/v0/platform/me"]
    assert "work" in result.output
    assert "user:developer" in result.output
    assert "developer@example.test" not in result.output
    assert "ac_23456789" not in result.output
    assert "https://api.work.example" not in result.output
    assert "Repository domain" not in result.output


def test_auth_whoami_describes_automation_credential_without_email(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(
        monkeypatch,
        tmp_path,
        """
default_profile = "work"

[profiles.work]
api_url = "https://api.work.example"
""",
    )

    class _Response:
        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "principal_type": "automation",
                "email": None,
                "personal_account": None,
                "credential": {
                    "scenario": "organization_workload",
                    "account": {"ref": "ac_acme2345", "handle": "acme", "type": "organization"},
                },
            }

    class _Client:
        @staticmethod
        def get(path: str) -> _Response:
            assert path == "/v0/platform/me"
            return _Response()

    monkeypatch.setattr(
        auth_cmd.ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: _Client()),
    )

    result = runner.invoke(auth_cmd.app, ["whoami"])

    assert result.exit_code == 0, result.output
    assert "organization automation token for org:acme" in result.output


def test_auth_login_delegates_to_device_login_with_active_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(
        monkeypatch,
        tmp_path,
        """
default_profile = "work"

[profiles.work]
api_url = "https://api.work.example"
""",
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "preflight_credential_store",
        lambda profile, requested=None: "keyring",
    )
    monkeypatch.setattr(
        auth_cmd,
        "perform_device_login",
        lambda **kwargs: calls.append(kwargs),
    )

    result = runner.invoke(
        auth_cmd.app,
        ["login", "--api-url", "https://api.override.example", "--duration", "12h", "--no-browser"],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "profile": "work",
            "api_url": "https://api.override.example",
            "no_browser": True,
            "duration": "12h",
            "credential_store": "keyring",
        }
    ]


def test_auth_login_help_keeps_internal_api_override_hidden() -> None:
    result = runner.invoke(auth_cmd.app, ["login", "--help"])

    assert result.exit_code == 0
    assert "--api-url" not in result.output


def test_auth_login_stops_before_device_flow_when_store_preflight_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path, 'default_profile = "default"')
    called = False

    def unexpected_device_login(**_kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "preflight_credential_store",
        lambda profile, requested=None: (_ for _ in ()).throw(RuntimeError("store locked")),
    )
    monkeypatch.setattr(auth_cmd, "perform_device_login", unexpected_device_login)

    result = runner.invoke(auth_cmd.app, ["login"])

    assert result.exit_code == 1
    assert "store locked" in result.stderr
    assert called is False


def test_auth_login_offers_and_installs_dedicated_store_before_device_flow(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path, 'default_profile = "default"')
    calls: list[dict[str, Any]] = []
    initialized: list[str] = []
    preflights = iter(
        [
            auth_cmd.auth_mod.NoCredentialStoreError("no store"),
            "vault",
        ]
    )

    def preflight(profile: str, requested: str | None = None) -> str:
        result = next(preflights)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(auth_cmd, "_interactive_terminal_available", lambda: True)
    monkeypatch.setattr(auth_cmd.auth_mod, "preflight_credential_store", preflight)
    monkeypatch.setattr(auth_cmd.stores.vault, "exists", lambda: False)
    monkeypatch.setattr(
        auth_cmd.stores.vault,
        "initialize",
        lambda passphrase: initialized.append(passphrase),
    )
    monkeypatch.setattr(auth_cmd, "perform_device_login", lambda **kwargs: calls.append(kwargs))

    result = runner.invoke(
        auth_cmd.app,
        ["login", "--no-browser"],
        input="y\na sufficiently long passphrase\na sufficiently long passphrase\n",
    )

    assert result.exit_code == 0
    assert initialized == ["a sufficiently long passphrase"]
    assert calls[0]["credential_store"] == "vault"
    assert cfg_mod.load().credential_store == "vault"
    assert "Install a dedicated credential store for rvs?" in result.output
    assert "Installed dedicated credential store: Ravenstash encrypted vault." in result.output
    assert "vault, plaintext" not in result.output
    assert "before" not in result.output.lower()


def test_auth_login_stops_when_dedicated_store_install_is_declined(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path, 'default_profile = "default"')
    initialized: list[str] = []
    device_login_called = False

    def unexpected_device_login(**_kwargs: Any) -> None:
        nonlocal device_login_called
        device_login_called = True

    monkeypatch.setattr(auth_cmd, "_interactive_terminal_available", lambda: True)
    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "preflight_credential_store",
        lambda profile, requested=None: (_ for _ in ()).throw(
            auth_cmd.auth_mod.NoCredentialStoreError("no store")
        ),
    )
    monkeypatch.setattr(
        auth_cmd.stores.vault,
        "initialize",
        lambda passphrase: initialized.append(passphrase),
    )
    monkeypatch.setattr(auth_cmd, "perform_device_login", unexpected_device_login)

    result = runner.invoke(auth_cmd.app, ["login", "--no-browser"], input="n\n")

    assert result.exit_code == 1
    assert initialized == []
    assert device_login_called is False
    assert "Install a dedicated credential store for rvs?" in result.output
    assert "Login requires credential storage" in result.stderr


def test_auth_login_warns_but_accepts_short_recommended_vault_passphrase(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path, 'default_profile = "default"')
    initialized: list[str] = []
    preflights = iter(
        [
            auth_cmd.auth_mod.NoCredentialStoreError("no store"),
            "vault",
        ]
    )

    def preflight(profile: str, requested: str | None = None) -> str:
        result = next(preflights)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(auth_cmd, "_interactive_terminal_available", lambda: True)
    monkeypatch.setattr(auth_cmd.auth_mod, "preflight_credential_store", preflight)
    monkeypatch.setattr(auth_cmd.stores.vault, "exists", lambda: False)
    monkeypatch.setattr(
        auth_cmd.stores.vault,
        "initialize",
        lambda passphrase: initialized.append(passphrase),
    )
    monkeypatch.setattr(auth_cmd, "perform_device_login", lambda **kwargs: None)

    result = runner.invoke(
        auth_cmd.app,
        ["login", "--no-browser"],
        input="y\n12345678\n12345678\n",
    )

    assert result.exit_code == 0
    assert initialized == ["12345678"]
    assert "accepted" in result.output
    assert "12+ characters" in result.output


def test_auth_storage_setup_plaintext_requires_exact_acknowledgement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path, 'default_profile = "default"')
    initialized: list[bool] = []
    monkeypatch.setattr(auth_cmd, "_interactive_terminal_available", lambda: True)
    monkeypatch.setattr(auth_cmd.stores.plaintext_store, "exists", lambda: False)
    monkeypatch.setattr(
        auth_cmd.stores.plaintext_store,
        "initialize",
        lambda: initialized.append(True),
    )
    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "preflight_credential_store",
        lambda profile, requested=None: "plaintext",
    )

    rejected = runner.invoke(
        auth_cmd.app,
        ["storage", "setup", "--store", "plaintext"],
        input="yes\n",
    )
    accepted = runner.invoke(
        auth_cmd.app,
        ["storage", "setup", "--store", "plaintext"],
        input="STORE PLAINTEXT\n",
    )

    assert rejected.exit_code == 1
    assert initialized == [True]
    assert accepted.exit_code == 0
    assert cfg_mod.load().credential_store == "plaintext"
    assert "NOT ENCRYPTED" in accepted.stderr


def test_auth_storage_setup_plaintext_allows_explicit_noninteractive_acknowledgement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path, 'default_profile = "default"')
    initialized: list[bool] = []
    monkeypatch.setattr(auth_cmd, "_interactive_terminal_available", lambda: False)
    monkeypatch.setattr(auth_cmd.stores.plaintext_store, "exists", lambda: False)
    monkeypatch.setattr(
        auth_cmd.stores.plaintext_store,
        "initialize",
        lambda: initialized.append(True),
    )
    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "preflight_credential_store",
        lambda profile, requested=None: "plaintext",
    )

    result = runner.invoke(
        auth_cmd.app,
        [
            "storage",
            "setup",
            "--store",
            "plaintext",
            "--allow-insecure-storage",
        ],
    )

    assert result.exit_code == 0
    assert initialized == [True]


def test_auth_storage_doctor_performs_disposable_round_trip(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path, 'default_profile = "default"')
    checked: list[tuple[str, str | None]] = []
    status = auth_cmd.stores.StoreStatus(
        "keyring",
        True,
        "SecretService",
        "ready",
        "",
    )
    monkeypatch.setattr(auth_cmd.auth_mod, "credential_store_statuses", lambda: [status])
    monkeypatch.setattr(
        auth_cmd.auth_mod,
        "preflight_credential_store",
        lambda profile, requested=None: checked.append((profile, requested)) or "keyring",
    )

    result = runner.invoke(auth_cmd.app, ["storage", "doctor"])

    assert result.exit_code == 0
    assert checked == [("default", None)]
    assert "passed" in result.output


def test_auth_profile_list_reports_no_profiles(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path, 'default_profile = "default"')

    result = runner.invoke(auth_cmd.profile_app, ["list"])

    assert result.exit_code == 0
    assert "No profiles configured" in result.output


def test_auth_profile_rename_moves_metadata_and_deletes_old_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(
        monkeypatch,
        tmp_path,
        """
schema_version = 1
default_profile = "work"

[profiles.work]
api_url = "https://api.work.example"
account_ref = "ac_23456789"
""",
    )
    deleted: list[str] = []
    monkeypatch.setattr(auth_cmd.auth_mod, "delete_token", lambda profile: deleted.append(profile))

    result = runner.invoke(auth_cmd.profile_app, ["rename", "work", "staging"])
    cfg = cfg_mod.load()

    assert result.exit_code == 0
    assert deleted == ["work"]
    assert "work" not in cfg.profiles
    assert cfg.profiles["staging"].account_ref == "ac_23456789"
    assert cfg.default_profile == "staging"
    assert "renamed to 'staging'" in result.output


def test_profile_use_rejects_missing_profile(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(
        monkeypatch,
        tmp_path,
        """
default_profile = "default"

[profiles.default]
api_url = "https://api.ravenstash.com"
""",
    )

    result = runner.invoke(auth_cmd.profile_app, ["use", "missing"])

    assert result.exit_code == 1
    assert "Profile 'missing' is not configured" in result.stderr


def test_auth_logout_rejects_profile_and_all_together() -> None:
    result = runner.invoke(auth_cmd.app, ["logout", "--profile", "work", "--all"])

    assert result.exit_code == 1
    assert "Use either `--profile` or `--all`, not both" in result.stderr
