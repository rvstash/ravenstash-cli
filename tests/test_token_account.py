"""RVS_TOKEN acts for exactly one account, which the server names."""

from typing import TYPE_CHECKING, Any

import pytest
from rvs import config as cfg_mod
from rvs.account import commands as account_cmd
from rvs.artifacts import discovery
from rvs.cli import app
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()
ACME = {"ref": "ac_acme2345", "handle": "acme", "type": "organization"}
PERSONAL = {"ref": "ac_ada23456", "handle": "ada", "type": "personal"}


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Api:
    def __init__(self, identity: dict[str, Any]) -> None:
        self.identity = identity
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, path: str, params: dict | None = None) -> _Response:
        self.calls.append((path, params))
        if path == "/v0/platform/me":
            return _Response(self.identity)
        if path == "/v0/platform/accounts":
            return _Response({"items": [ACME | {"display_name": "Acme Inc"}], "next_cursor": None})
        assert path == "/v0/artifacts/repositories/resolve"
        return _Response(
            {
                "ref": "ar_23456789",
                "name": "packages",
                "account": ACME if params and params["account_ref"] == ACME["ref"] else PERSONAL,
                "namespace": {"ref": "in_23456789", "name": "platform", "realm": "internal"},
                "formats": [{"format": "pypi"}],
            }
        )


def _organization_token() -> dict[str, Any]:
    return {
        "principal_type": "automation",
        "user": None,
        "email": None,
        "personal_account": None,
        "credential": {"scenario": "organization_workload", "account": ACME},
    }


@pytest.fixture
def api(monkeypatch, tmp_path: Path) -> _Api:
    config_dir = tmp_path / ".rvs"
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    for key in ("RVS_PROFILE", "RVS_ACCOUNT_REF", "RVS_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RVS_TOKEN", "rvs_oat" + "A" * 43)
    fake = _Api(_organization_token())
    monkeypatch.setattr(
        account_cmd.ApiClient, "from_profile", staticmethod(lambda profile=None: fake)
    )
    monkeypatch.setattr(
        discovery.ApiClient, "from_profile", staticmethod(lambda profile=None: fake)
    )
    return fake


def test_a_fresh_ci_runner_acts_for_the_token_account(api: _Api) -> None:
    found = discovery.discover("platform/packages", None, None, "pypi")

    assert found.target.account_ref == ACME["ref"]
    assert (
        "/v0/artifacts/repositories/resolve",
        {"selector": "platform/packages", "account_ref": ACME["ref"]},
    ) in api.calls
    assert not cfg_mod.CONFIG_FILE.exists()


def test_the_token_account_wins_over_a_saved_selection(api: _Api) -> None:
    cfg_mod.set_active_account(
        profile="default",
        account=PERSONAL | {"is_admin": True, "organization_role": None, "authority_revision": 1},
    )

    assert account_cmd.acting_account_ref() == ACME["ref"]
    profile, account = account_cmd.ensure_active_account()

    assert account.account_ref == ACME["ref"]
    assert account.account_handle == "acme"
    # The token never changes the saved selection.
    assert cfg_mod.load().profiles[profile].active_account_ref == PERSONAL["ref"]


def test_the_identity_is_verified_once_per_process(api: _Api) -> None:
    for _ in range(3):
        account_cmd.acting_account_ref()

    assert [path for path, _ in api.calls] == ["/v0/platform/me"]


def test_rvs_account_ref_may_only_repeat_the_token_account(api: _Api, monkeypatch) -> None:
    monkeypatch.setenv("RVS_ACCOUNT_REF", ACME["ref"])
    assert account_cmd.acting_account_ref() == ACME["ref"]

    monkeypatch.setenv("RVS_ACCOUNT_REF", PERSONAL["ref"])
    result = runner.invoke(app, ["art", "endpoint", "--target", "platform/packages"])

    assert result.exit_code == 1
    assert f"RVS_ACCOUNT_REF is {PERSONAL['ref']}, but RVS_TOKEN acts only for org:acme" in (
        result.output
    )


@pytest.mark.parametrize("session_token", [None, "eyJhbGciOiJFUzI1NiJ9.session.signature"])
def test_a_sign_in_session_keeps_the_local_selection_without_asking(
    api: _Api, monkeypatch, session_token: str | None
) -> None:
    if session_token is None:
        monkeypatch.delenv("RVS_TOKEN")
    else:
        monkeypatch.setenv("RVS_TOKEN", session_token)
    monkeypatch.setenv("RVS_ACCOUNT_REF", PERSONAL["ref"])

    assert account_cmd.acting_account_ref() == PERSONAL["ref"]
    assert api.calls == []


def test_status_names_the_token_as_the_account_source(api: _Api) -> None:
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "org:acme (Acme Inc) · access token (RVS_TOKEN)" in result.output
    assert "Warning" not in result.output
