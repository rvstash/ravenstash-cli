import json
from typing import TYPE_CHECKING, Any

from rvs import config as cfg_mod
from rvs.account import commands as account_cmd
from rvs.cli import app
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _identity(
    *,
    handle: str | None = "avery",
    display_name: str | None = "Avery Example",
    credential: dict[str, Any] | None = None,
    principal_type: str = "user",
) -> dict[str, Any]:
    """Return a wire ``Identity``; a device session has no credential."""
    return {
        "principal_type": principal_type,
        "user": {"handle": handle, "display_name": display_name} if handle else None,
        "email": "developer@example.test" if credential is None else None,
        "personal_account": (
            {"ref": "personal-user", "handle": "avery", "type": "personal"}
            if credential is None
            else None
        ),
        "credential": credential,
    }


AVERY = {
    "ref": "personal-user",
    "handle": "avery",
    "type": "personal",
    "display_name": "Avery Example",
    "is_admin": True,
    "organization_role": None,
    "authority_revision": 1,
}
ACME = {
    "ref": "acme",
    "handle": "acme",
    "type": "organization",
    "display_name": "Acme Incorporated",
    "is_admin": True,
    "organization_role": "admin",
    "authority_revision": 2,
}


def _serve(monkeypatch, identity: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    calls: list[str] = []

    class _Client:
        @staticmethod
        def get(path: str, params: dict | None = None) -> _Response:
            calls.append(path)
            if path == "/v0/platform/me":
                return _Response(identity)
            assert path == "/v0/platform/accounts"
            return _Response({"items": items, "next_cursor": None})

    monkeypatch.setattr(
        account_cmd.ApiClient, "from_profile", staticmethod(lambda profile=None: _Client())
    )
    return calls


def _isolate_config(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    config_file.write_text(
        """
schema_version = 1
default_profile = "work"

[profiles.work]
api_url = "https://api.work.example"
account_ref = "personal-user"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    for key in ("RVS_PROFILE", "RVS_ACCOUNT_REF", "RVS_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)


def _row(output: str, label: str) -> str:
    """Return one table row, including values a narrow console wraps onto
    indented continuation lines, with whitespace normalized."""
    lines = output.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(label))
    row = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith(" ") or not line.strip():
            break
        row.append(line)
    return " ".join(" ".join(row).split())


def test_status_names_the_person_and_selections_without_writing_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    calls = _serve(monkeypatch, _identity(), [AVERY, ACME])
    before = cfg_mod.CONFIG_FILE.read_text(encoding="utf-8")

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert calls == ["/v0/platform/me", "/v0/platform/accounts"]
    assert "user:avery (Avery Example)" in _row(result.output, "Signed in as")
    assert "developer@example.test" not in result.output
    assert _row(result.output, "Profile").split() == ["Profile", "work"]
    assert "user:avery (Avery Example)" in _row(result.output, "Account")
    assert "not selected" in _row(result.output, "Artifacts target")
    labels = ("Profile", "Signed in as", "Account", "Artifacts target", "Ravenstash API")
    rows = [line.split("  ")[0] for line in result.output.splitlines() if line.startswith(labels)]
    # The profile comes first: it decides which sign-in and selections apply.
    assert rows == list(labels)
    assert "https://api.work.example" in result.output
    assert cfg_mod.CONFIG_FILE.read_text(encoding="utf-8") == before
    assert not (cfg_mod.CONFIG_DIR / "sessions").exists()


def test_status_shows_the_target_and_non_default_sources(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    cfg_mod.set_active_account(profile="work", account=ACME)
    cfg_mod.set_selected_artifact_target(
        cfg_mod.ArtifactTarget(
            target_type="repository",
            account_ref="acme",
            stable_selector="in/ar_23456789",
            display_selector="repo:team/libs",
            format="pypi",
            namespace_realm="internal",
            namespace_ref="in_23456789",
            namespace_name_cache="team",
            repository_ref="ar_23456789",
            repository_name_cache="libs",
        ),
        profile="work",
        account_ref="acme",
    )
    monkeypatch.setenv("RVS_ACCOUNT_REF", "acme")
    _serve(monkeypatch, _identity(), [AVERY, ACME])

    result = runner.invoke(app, ["status", "--profile", "work"])

    assert result.exit_code == 0, result.output
    assert "work · command option (--profile)" in _row(result.output, "Profile")
    account = _row(result.output, "Account")
    assert "org:acme (Acme Incorporated) · environment (RVS_ACCOUNT_REF)" in account
    assert "repo:team/libs · pypi" in _row(result.output, "Artifacts target")


def test_status_json_nests_targets_by_product(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    cfg_mod.set_active_account(profile="work", account=ACME)
    cfg_mod.set_selected_artifact_target(
        cfg_mod.ArtifactTarget(
            target_type="official_cache",
            account_ref="acme",
            stable_selector="mirror:official-ref",
            display_selector="mirror:pypiorg",
            format="pypi",
        ),
        profile="work",
        account_ref="acme",
    )
    _serve(monkeypatch, _identity(), [AVERY, ACME])

    result = runner.invoke(app, ["--json", "status"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["values"] == {
        "signed_in_as": {
            "display": "user:avery (Avery Example)",
            "principal_type": "user",
            "user": "user:avery",
            "display_name": "Avery Example",
            "credential": None,
        },
        "profile": {"name": "work", "selected_by": "persisted default"},
        "account": {
            "ref": "acme",
            "handle": "org:acme",
            "display_name": "Acme Incorporated",
            "selected_by": "persisted profile",
        },
        "targets": {
            "artifacts": {"target": "mirror:pypiorg", "type": "official_cache", "format": "pypi"}
        },
        "api_url": "https://api.work.example",
    }


def test_status_does_not_repeat_the_organization_for_automation(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("RVS_ACCOUNT_REF", "acme")
    token_account = {"ref": "acme", "handle": "acme", "type": "organization"}
    identity = _identity(
        handle=None,
        principal_type="automation",
        credential={"scenario": "organization_workload", "account": token_account},
    )
    _serve(monkeypatch, identity, [ACME])

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    signed_in = _row(result.output, "Signed in as")
    assert "organization automation token" in signed_in
    assert "acme" not in signed_in
    assert "org:acme (Acme Incorporated)" in _row(result.output, "Account")
    assert "RVS_TOKEN acts only" not in result.output


def test_status_names_the_owner_of_an_organization_pat(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    token_account = {"ref": "acme", "handle": "acme", "type": "organization"}
    identity = _identity(
        credential={"scenario": "user_organization", "account": token_account},
    )
    _serve(monkeypatch, identity, [ACME])

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    signed_in = _row(result.output, "Signed in as")
    assert "user:avery (Avery Example) · organization-account PAT" in signed_in
    # The token's account replaces the persisted personal account.
    assert "org:acme (Acme Incorporated) · access token (RVS_TOKEN)" in _row(
        result.output, "Account"
    )
    assert result.stderr == ""


def test_status_warns_when_rvs_account_ref_names_another_account(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("RVS_ACCOUNT_REF", "personal-user")
    token_account = {"ref": "acme", "handle": "acme", "type": "organization"}
    _serve(
        monkeypatch,
        _identity(credential={"scenario": "user_organization", "account": token_account}),
        [ACME],
    )

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "org:acme (Acme Incorporated)" in _row(result.output, "Account")
    # Narrow consoles (Windows runners) wrap the warning; compare it normalized.
    assert "RVS_ACCOUNT_REF is personal-user, but RVS_TOKEN acts only for org:acme" in (
        " ".join(result.stderr.split())
    )


def test_status_json_never_reports_the_sign_in_email(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _serve(monkeypatch, _identity(display_name=None), [AVERY])

    result = runner.invoke(app, ["status"])
    as_json = runner.invoke(app, ["--json", "status"])

    assert result.exit_code == 0, result.output
    assert _row(result.output, "Signed in as").split() == ["Signed", "in", "as", "user:avery"]
    signed_in = json.loads(as_json.output)["values"]["signed_in_as"]
    assert signed_in["user"] == "user:avery"
    assert signed_in["display_name"] is None
    assert "developer@example.test" not in result.output + as_json.output


def test_art_status_leaves_a_command_chosen_format_empty(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    cfg_mod.set_active_account(profile="work", account=ACME)
    cfg_mod.set_selected_artifact_target(
        cfg_mod.ArtifactTarget(
            target_type="repository",
            account_ref="acme",
            stable_selector="in/ar_23456789",
            display_selector="repo:team/libs",
            # A repository with several formats and no --format keeps it open.
            format=None,
            namespace_realm="internal",
            namespace_ref="in_23456789",
            namespace_name_cache="team",
            repository_ref="ar_23456789",
            repository_name_cache="libs",
        ),
        profile="work",
        account_ref="acme",
    )
    _serve(monkeypatch, _identity(), [AVERY, ACME])
    before = cfg_mod.CONFIG_FILE.read_text(encoding="utf-8")

    result = runner.invoke(app, ["art", "status"])
    as_json = runner.invoke(app, ["--json", "art", "status"])

    assert result.exit_code == 0, result.output
    assert "org:acme (Acme Incorporated)" in _row(result.output, "Account")
    assert "repo:team/libs" in _row(result.output, "Target")
    assert _row(result.output, "Format").split() == ["Format", "-"]
    assert json.loads(as_json.output)["values"] == {
        "account": {
            "ref": "acme",
            "handle": "org:acme",
            "display_name": "Acme Incorporated",
            "selected_by": "persisted profile",
        },
        "target": "repo:team/libs",
        "type": "repository",
        "format": None,
    }
    assert cfg_mod.CONFIG_FILE.read_text(encoding="utf-8") == before


def test_profile_use_reports_persistence_scope(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)

    result = runner.invoke(app, ["profile", "use", "work"])

    assert result.exit_code == 0, result.output
    assert "Local profile 'work' selected for persisted default" in result.output
    assert cfg_mod.load().default_profile == "work"


def test_profile_current_owns_non_secret_endpoint_output(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)

    result = runner.invoke(app, ["profile", "current", "--verbose"])

    assert result.exit_code == 0, result.output
    assert "Current local Ravenstash profile" in result.output
    assert "https://api.work.example" in result.output
    assert "Repository domain" in result.output
    assert "https://pypi.rvsta.sh" in result.output
    assert "Owner ID" not in result.output
    assert "Authenticated" not in result.output


def test_account_switch_warns_when_environment_still_overrides_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    monkeypatch.setenv("RVS_ACCOUNT_REF", "forced-account")

    class _Client:
        @staticmethod
        def get(path: str, params: dict | None = None) -> _Response:
            assert path == "/v0/platform/accounts"
            return _Response(
                {
                    "items": [
                        {
                            "ref": "acme",
                            "handle": "acme",
                            "type": "organization",
                            "display_name": "Acme Incorporated",
                            "is_admin": True,
                            "organization_role": "admin",
                            "authority_revision": 2,
                        }
                    ],
                    "next_cursor": None,
                }
            )

    monkeypatch.setattr(
        account_cmd.ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: _Client()),
    )

    result = runner.invoke(app, ["account", "switch", "acme"])

    assert result.exit_code == 0, result.output
    assert "Account 'org:acme' selected for persisted profile" in result.output
    assert "RVS_ACCOUNT_REF is set and still overrides" in result.stderr
    assert cfg_mod.load().profiles["work"].active_account_ref == "acme"
    assert cfg_mod.current_account_ref("work") == "forced-account"


def test_account_list_displays_typed_user_and_organization_handles(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)

    class _Client:
        @staticmethod
        def get(path: str, params: dict | None = None) -> _Response:
            assert path == "/v0/platform/accounts"
            return _Response(
                {
                    "items": [
                        {
                            "ref": "personal-user",
                            "handle": "avery",
                            "type": "personal",
                            "display_name": "Avery Example",
                            "is_admin": True,
                            "organization_role": None,
                            "authority_revision": 1,
                        },
                        {
                            "ref": "acme",
                            "handle": "acme",
                            "type": "organization",
                            "display_name": "Acme Incorporated",
                            "is_admin": True,
                            "organization_role": "admin",
                            "authority_revision": 2,
                        },
                    ],
                    "next_cursor": None,
                }
            )

    monkeypatch.setattr(
        account_cmd.ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: _Client()),
    )

    result = runner.invoke(app, ["account", "list"])

    assert result.exit_code == 0, result.output
    assert "user:avery" in result.output
    assert "org:acme" in result.output
    assert "Acme Incorporated" in result.output


def test_account_list_follows_pages_and_shows_unknown_account_types(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    calls: list[dict | None] = []
    pages = [
        {
            "items": [
                {
                    "ref": "personal-user",
                    "handle": "avery",
                    "type": "personal",
                    "display_name": "Avery Example",
                    "is_admin": True,
                    "organization_role": None,
                    "authority_revision": 1,
                }
            ],
            "next_cursor": "page-2",
        },
        {
            "items": [
                {
                    "ref": "ac_23456789",
                    "handle": "ops",
                    "type": "example-type",
                    "display_name": "Ops",
                    "is_admin": False,
                    "organization_role": "auditor",
                    "authority_revision": 4,
                }
            ],
            "next_cursor": None,
        },
    ]

    class _Client:
        @staticmethod
        def get(path: str, params: dict | None = None) -> _Response:
            assert path == "/v0/platform/accounts"
            calls.append(params)
            return _Response(pages.pop(0))

    monkeypatch.setattr(
        account_cmd.ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: _Client()),
    )

    result = runner.invoke(app, ["account", "list"])

    assert result.exit_code == 0, result.output
    assert calls == [{"limit": 100}, {"limit": 100, "cursor": "page-2"}]
    assert "user:avery" in result.output
    # Account types and roles are open value sets: unknown values display as-is.
    assert "example-type:ops" in result.output
    assert "auditor" in result.output


def test_account_switch_without_selector_opens_account_picker(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    account_items = [
        {
            "ref": "acme",
            "handle": "acme",
            "type": "organization",
            "display_name": "Acme Incorporated",
            "is_admin": True,
            "organization_role": "admin",
            "authority_revision": 2,
        },
        {
            "ref": "personal-user",
            "handle": "avery",
            "type": "personal",
            "display_name": "Avery Example",
            "is_admin": True,
            "organization_role": None,
            "authority_revision": 1,
        },
    ]

    class _Client:
        @staticmethod
        def get(path: str, params: dict | None = None) -> _Response:
            assert path == "/v0/platform/accounts"
            return _Response({"items": account_items, "next_cursor": None})

    selector: dict[str, Any] = {}

    def select_index(**kwargs: Any) -> int:
        selector.update(kwargs)
        return 1

    monkeypatch.setattr(
        account_cmd.ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: _Client()),
    )
    monkeypatch.setattr(account_cmd, "select_index", select_index)

    result = runner.invoke(app, ["account", "switch"])

    assert result.exit_code == 0, result.output
    assert selector["initial_index"] == 0
    rendered = selector["render"](1)
    assert "user:avery" in rendered
    assert "[dim]Personal[/]" in rendered
    assert "org:acme" in rendered
    assert "Organization · admin" in rendered
    assert "Avery Example" not in rendered
    assert "Acme Incorporated" not in rendered
    assert "(active)" in rendered
    assert "Account 'org:acme' selected for persisted profile" in result.output
    assert cfg_mod.load().profiles["work"].active_account_ref == "acme"


def test_an_unknown_account_type_is_selectable_by_its_shown_handle(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    team = {"ref": "ac_teamteam", "type": "team", "handle": "Platform"}
    monkeypatch.setattr(
        account_cmd,
        "accounts",
        lambda profile=None: [{"ref": "ac_23456789", "type": "personal", "handle": "me"}, team],
    )
    monkeypatch.setattr(account_cmd.cfg_mod, "cache_account", lambda **kwargs: None)

    assert account_cmd.resolve_account("team:platform") == team
