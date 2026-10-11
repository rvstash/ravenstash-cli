from types import SimpleNamespace
from unittest.mock import Mock

import httpx2 as httpx
import pytest
from rvs import output
from rvs.api import remote_cache_mint_token_path, repository_mint_token_path
from rvs.artifacts import auth_commands
from rvs.artifacts.discovery import Discovery
from rvs.auth import credentials
from rvs.cli import app
from rvs.client import ApiClient, ApiError
from typer.testing import CliRunner


SECRET = "rvs_slt" + "A" * 43
runner = CliRunner()


@pytest.fixture
def issuer(monkeypatch):
    output.set_json(False)
    client = Mock()
    client.issue_native.return_value = httpx.Response(
        200,
        json={
            "access_token": SECRET,
            "expires_in": 14400,
            "operations": ["read"],
            "token_type": "bearer",
            "account_ref": "ac_23456789",
            "target": {
                "namespace_ref": "in_abcdefgh",
                "namespace_name": "space",
                "namespace_realm": "internal",
                "repository_ref": "ar_abcdefgh",
                "repository_name": "packages",
            },
            "formats": ["pypi"],
            "native_path": "/in/ar_abcdefgh",
        },
    )
    target = SimpleNamespace(
        target_type="repository",
        repository_ref="ar_abcdefgh",
        namespace_ref="in_abcdefgh",
        namespace_name_cache="space",
        repository_name_cache="packages",
        namespace_realm="internal",
        format="pypi",
        display_selector="space/packages",
    )

    def resolve(*args, **kwargs):
        print("Resolved fixture target")
        return Discovery("fixture", target, ("pypi",), "in/ar_abcdefgh")

    monkeypatch.setattr(auth_commands, "discover", resolve)
    monkeypatch.setattr(ApiClient, "from_profile", lambda *args, **kwargs: client)
    yield client
    output.set_json(False)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"target": {"repository_ref": "ar_zzzzzzzz"}}, "credential for a different repository"),
        ({"target": None}, "credential for a different repository"),
        ({"native_path": None}, "invalid native path"),
        ({"native_path": "/in/../ar_zzzzzzzz"}, "invalid native path"),
        ({"native_path": "/in/ar_abcdefgh?x=1"}, "invalid native path"),
        ({"native_path": "//evil.example/in"}, "invalid native path"),
    ],
)
def test_manual_rejects_a_credential_for_another_repository_or_an_unsafe_path(
    issuer, change, message
):
    payload = issuer.issue_native.return_value.json() | change
    issuer.issue_native.return_value = httpx.Response(200, json=payload)

    result = runner.invoke(app, ["art", "token", "mint", "--target", "space/packages"])

    assert result.exit_code == 1
    assert SECRET not in result.stdout
    assert message in " ".join(result.stderr.split())


class _Transport:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def __call__(self, *args, **kwargs) -> _Transport:
        return self

    def __enter__(self) -> _Transport:
        return self

    def __exit__(self, *args) -> None:
        return None

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return self.response


def _api_answers(monkeypatch, response: httpx.Response) -> None:
    monkeypatch.setattr(httpx, "Client", _Transport(response))
    monkeypatch.setattr(
        ApiClient,
        "from_profile",
        lambda *args, **kwargs: ApiClient("https://api.example", "token"),
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(410, json={"error": {"code": "ApiRouteRetired", "message": "Upgrade."}}),
        httpx.Response(410, text="gone"),
    ],
)
def test_manual_on_a_retired_route_fails_cleanly_without_stdout(issuer, monkeypatch, response):
    _api_answers(monkeypatch, response)

    result = runner.invoke(app, ["art", "token", "mint", "--target", "space/packages"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.stdout == ""
    stderr = " ".join(result.stderr.split())
    assert "This rvs release is no longer supported by the Ravenstash API." in stderr
    assert "run `rvs update`" in stderr


def test_manual_deprecation_warning_never_reaches_stdout(issuer, monkeypatch):
    from rvs import client as client_mod

    monkeypatch.setattr(client_mod, "_deprecation_warned", False)
    payload = issuer.issue_native.return_value.json()
    _api_answers(
        monkeypatch,
        httpx.Response(200, json=payload, headers={"Deprecation": "@1", "Sunset": "bogus"}),
    )

    result = runner.invoke(app, ["art", "token", "mint", "--target", "space/packages"])

    assert result.exit_code == 0, result.output
    assert result.stdout == SECRET + "\n"
    assert "Ravenstash will retire soon" in " ".join(result.stderr.split())


def test_manual_accepts_an_opaque_native_path_for_the_selected_repository(issuer):
    payload = issuer.issue_native.return_value.json() | {"native_path": "/r/v2/ar_abcdefgh"}
    issuer.issue_native.return_value = httpx.Response(200, json=payload)

    result = runner.invoke(app, ["art", "token", "mint", "--target", "space/packages"])

    assert result.exit_code == 0, result.output
    assert result.stdout == SECRET + "\n"


def test_manual_default_prints_only_the_secret_to_stdout(issuer):
    result = runner.invoke(app, ["art", "token", "mint", "--target", "space/packages"])
    assert result.exit_code == 0, result.output
    assert result.stdout == SECRET + "\n"
    assert "Resolved fixture target" in result.stderr
    path, payload = issuer.issue_native.call_args.args
    assert path == "/v0/artifacts/repositories/ar_abcdefgh/mint-token"
    assert payload == {
        "formats": ["pypi"],
        "operations": ["read"],
        "duration_seconds": 14400,
        "expected_target": {
            "namespace_ref": "in_abcdefgh",
            "namespace_name": "space",
            "namespace_realm": "internal",
            "repository_ref": "ar_abcdefgh",
            "repository_name": "packages",
        },
    }


def test_manual_kind_is_required_only_when_target_is_ambiguous(issuer, monkeypatch):
    target = SimpleNamespace(
        target_type="repository",
        repository_ref="ar_abcdefgh",
        namespace_ref="in_abcdefgh",
        namespace_name_cache="space",
        repository_name_cache="packages",
        namespace_realm="internal",
        format=None,
    )
    monkeypatch.setattr(
        auth_commands,
        "discover",
        lambda *args, **kwargs: Discovery("fixture", target, ("pypi", "oci"), "in/ar_abcdefgh"),
    )

    result = runner.invoke(app, ["art", "token", "mint", "--target", "space/packages"])

    assert result.exit_code == 1
    assert "Select one enabled format" in result.stderr
    issuer.issue_native.assert_not_called()


def test_manual_json_and_publish_are_explicit(issuer):
    import json

    result = runner.invoke(
        app,
        [
            "--json",
            "art",
            "token",
            "mint",
            "--target",
            "space/packages",
            "--format",
            "pypi",
            "--access",
            "publish",
            "--duration",
            "12h",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["access_token"] == SECRET
    assert issuer.issue_native.call_args.args[1]["duration_seconds"] == 43200
    assert issuer.issue_native.call_args.args[1]["operations"] == ["read", "publish"]


def test_manual_admin_requests_delete_without_changing_publish(issuer):
    result = runner.invoke(
        app,
        [
            "art",
            "token",
            "mint",
            "--target",
            "space/packages",
            "--format",
            "pypi",
            "--access",
            "admin",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == SECRET + "\n"
    assert issuer.issue_native.call_args.args[1]["operations"] == ["read", "publish", "delete"]


@pytest.mark.parametrize("duration", ["14m", "0h", "13h", "4", "forever"])
def test_invalid_lifetime_is_rejected_before_issuance(issuer, duration):
    result = runner.invoke(
        app,
        [
            "art",
            "token",
            "mint",
            "--target",
            "space/packages",
            "--format",
            "npm",
            "--duration",
            duration,
        ],
    )
    assert result.exit_code != 0
    assert result.stdout == ""
    issuer.issue_native.assert_not_called()


@pytest.mark.parametrize(
    "token", ["", "rst_old", "header.payload.signature", SECRET, "rvs_ust" + "A" * 42 + "B"]
)
def test_invalid_environment_override_never_uses_the_stored_login(monkeypatch, token):
    monkeypatch.setenv("RVS_TOKEN", token)
    stored = Mock(side_effect=AssertionError("Stored session must not be consulted"))
    monkeypatch.setattr(credentials, "selected_credential_store", stored)
    with pytest.raises(SystemExit):
        credentials.get_token("fixture")
    assert credentials.token_source("fixture") == "RVS_TOKEN"
    stored.assert_not_called()


@pytest.mark.parametrize("marker", ["rvs_ust", "rvs_uot", "rvs_oat"])
def test_current_automation_override_has_priority(monkeypatch, marker):
    token = marker + "A" * 43
    monkeypatch.setenv("RVS_TOKEN", token)
    assert credentials.get_token("fixture") == token


def test_native_issuance_retries_are_bounded_and_respect_retry_after(monkeypatch):
    client = ApiClient("https://api.example.test", "source")
    reply = httpx.Response(200, json={"access_token": SECRET})
    post = Mock(
        side_effect=[
            ApiError(503, "busy", retry_after=2),
            httpx.ReadTimeout("lost response"),
            reply,
        ]
    )
    monkeypatch.setattr(client, "post", post)
    sleep = Mock()
    monkeypatch.setattr("rvs.client.time.sleep", sleep)
    assert client.issue_native(repository_mint_token_path("ar_abcdefgh"), {}) is reply
    assert post.call_count == 3
    assert sleep.call_args_list[0].args[0] >= 2
    assert all(call.kwargs["retry"] is False for call in post.call_args_list)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_native_issuance_does_not_retry_denials(monkeypatch, status):
    client = ApiClient("https://api.example.test", "source")
    post = Mock(side_effect=ApiError(status, "denied"))
    monkeypatch.setattr(client, "post", post)
    with pytest.raises(ApiError):
        client.issue_native(remote_cache_mint_token_path("rc_abcdefgh"), {})
    assert post.call_count == 1


def test_issuance_retry_policy_cannot_be_used_for_an_arbitrary_mutation(monkeypatch):
    client = ApiClient("https://api.example.test", "source")
    post = Mock()
    monkeypatch.setattr(client, "post", post)
    for path in ("/repositories", "/package-credentials", "/v0/artifacts/repositories"):
        with pytest.raises(ValueError):
            client.issue_native(path, {})
    post.assert_not_called()


@pytest.mark.parametrize(
    ("access", "operations"),
    [
        ("read", ["read"]),
        ("publish", ["read", "publish"]),
        ("admin", ["read", "publish", "delete"]),
    ],
)
def test_access_levels_map_to_grant_operations(issuer, access, operations):
    result = runner.invoke(
        app, ["art", "token", "mint", "--target", "space/packages", "--access", access]
    )
    assert result.exit_code == 0, result.output
    assert issuer.issue_native.call_args.args[1]["operations"] == operations


def _multi_lane_issuer(monkeypatch, issuer, formats):
    target = SimpleNamespace(
        target_type="repository",
        repository_ref="ar_abcdefgh",
        namespace_ref="in_abcdefgh",
        namespace_name_cache="space",
        repository_name_cache="packages",
        namespace_realm="internal",
        format=None,
        display_selector="space/packages",
    )
    monkeypatch.setattr(
        auth_commands,
        "discover",
        lambda *args, **kwargs: Discovery("fixture", target, formats, "in/ar_abcdefgh"),
    )
    issuer.issue_native.return_value = httpx.Response(
        200,
        json={
            "access_token": SECRET,
            "expires_in": 14400,
            "operations": ["read", "publish"],
            "token_type": "bearer",
            "account_ref": "ac_23456789",
            "target": {
                "namespace_ref": "in_abcdefgh",
                "namespace_name": "space",
                "namespace_realm": "internal",
                "repository_ref": "ar_abcdefgh",
                "repository_name": "packages",
            },
            "formats": list(formats),
            "native_path": "/in/ar_abcdefgh",
        },
    )


def test_all_formats_mints_one_token_for_every_lane_including_oci(issuer, monkeypatch):
    import json

    _multi_lane_issuer(monkeypatch, issuer, ("pypi", "npm", "oci"))
    result = runner.invoke(
        app,
        [
            "--json",
            "art",
            "token",
            "mint",
            "--target",
            "space/packages",
            "--all-formats",
            "--access",
            "publish",
        ],
    )
    assert result.exit_code == 0, result.output
    path, payload = issuer.issue_native.call_args.args
    assert path == "/v0/artifacts/repositories/ar_abcdefgh/mint-token"
    assert payload["all_formats"] is True
    assert "formats" not in payload
    assert payload["operations"] == ["read", "publish"]
    assert json.loads(result.stdout)["formats"] == ["pypi", "npm", "oci"]


def test_explicit_multi_format_mint_includes_oci_lane(issuer, monkeypatch):
    _multi_lane_issuer(monkeypatch, issuer, ("pypi", "oci"))
    result = runner.invoke(
        app, ["art", "token", "mint", "--target", "space/packages", "--format", "pypi,oci"]
    )
    assert result.exit_code == 0, result.output
    payload = issuer.issue_native.call_args.args[1]
    assert payload["formats"] == ["pypi", "oci"]
    assert "all_formats" not in payload


def test_all_formats_and_format_are_mutually_exclusive(issuer):
    result = runner.invoke(
        app,
        ["art", "token", "mint", "--target", "space/packages", "--all-formats", "-f", "pypi"],
    )
    assert result.exit_code == 1
    issuer.issue_native.assert_not_called()


def test_mirror_mint_sends_only_duration_and_checks_format(issuer, monkeypatch):
    target = SimpleNamespace(
        target_type="official_cache",
        remote_cache_ref="rc_abcdefgh",
        format="pypi",
        display_selector="mirror:pypiorg",
    )
    monkeypatch.setattr(
        auth_commands,
        "discover",
        lambda *args, **kwargs: Discovery("fixture", target, ("pypi",), "o/pypiorg"),
    )
    issuer.issue_native.return_value = httpx.Response(
        200,
        json={
            "access_token": SECRET,
            "token_type": "bearer",
            "expires_in": 14400,
            "account_ref": "ac_23456789",
            "remote_cache_ref": "rc_abcdefgh",
            "format": "pypi",
            "native_path": "/o/pypiorg",
            "operations": ["read"],
        },
    )
    result = runner.invoke(app, ["art", "token", "mint", "--target", "mirror:pypiorg"])
    assert result.exit_code == 0, result.output
    assert result.stdout == SECRET + "\n"
    path, payload = issuer.issue_native.call_args.args
    assert path == "/v0/artifacts/remote-caches/rc_abcdefgh/mint-token"
    assert payload == {"duration_seconds": 14400}

    denied = runner.invoke(
        app, ["art", "token", "mint", "--target", "mirror:pypiorg", "--access", "publish"]
    )
    assert denied.exit_code == 1
    assert "read credentials only" in denied.stderr


@pytest.mark.parametrize(
    ("mismatch", "message"),
    [
        ({"format": "npm"}, "different private mirror"),
        ({"remote_cache_ref": "rc_23456789"}, "different private mirror"),
        ({"native_path": "/o/./pypiorg"}, "invalid native path"),
    ],
)
def test_mirror_mint_rejects_a_credential_for_another_mirror(
    issuer, monkeypatch, mismatch, message
):
    target = SimpleNamespace(
        target_type="official_cache",
        remote_cache_ref="rc_abcdefgh",
        format="pypi",
        display_selector="mirror:pypiorg",
    )
    monkeypatch.setattr(
        auth_commands,
        "discover",
        lambda *args, **kwargs: Discovery("fixture", target, ("pypi",), "o/pypiorg"),
    )
    issuer.issue_native.return_value = httpx.Response(
        200,
        json={
            "access_token": SECRET,
            "token_type": "bearer",
            "expires_in": 14400,
            "account_ref": "ac_23456789",
            "remote_cache_ref": "rc_abcdefgh",
            "format": "pypi",
            "native_path": "/o/pypiorg",
            "operations": ["read"],
        }
        | mismatch,
    )

    result = runner.invoke(app, ["art", "token", "mint", "--target", "mirror:pypiorg"])

    assert result.exit_code == 1
    assert message in " ".join(result.stderr.split())
    assert SECRET not in result.stdout
