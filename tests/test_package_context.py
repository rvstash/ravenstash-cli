from typing import TYPE_CHECKING, Any

import pytest
from rvs import config as cfg_mod
from rvs.artifacts.targets import parse_target, resolve_target
from rvs.cli import app
from rvs.client import ApiClient
from rvs.native import runner as native_runner
from rvs.runtime import tools as runtime_tools
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()


class Response:
    def __init__(self, value: Any) -> None:
        self.value = value

    def json(self) -> Any:
        return self.value


def account(
    account_ref: str,
    label: str,
    account_type: str = "organization",
) -> dict[str, Any]:
    return {
        "ref": account_ref,
        "handle": label,
        "type": account_type,
        "display_name": label,
        "is_admin": False,
        "organization_role": "member" if account_type == "organization" else None,
        "authority_revision": 4,
    }


def summary(account: dict[str, Any]) -> dict[str, Any]:
    """Return the embedded ``AccountSummary`` for one wire ``Account``."""
    return {"ref": account["ref"], "handle": account["handle"], "type": account["type"]}


def remote(
    *,
    owner: dict[str, Any],
    family: str,
    name: str,
    kind: str,
    suffix: str,
) -> dict[str, Any]:
    return {
        "ref": f"rc_{suffix.ljust(8, '2')}",
        "account": summary(owner),
        "format": kind,
        "source_type": family,
        # Official caches carry a display name; their selector is the source ref.
        "name": name if family == "custom" else f"{name} (official)",
        "official_source_ref": name if family == "official" else None,
        "min_age_hours": None,
        "max_age_hours": None,
        "consumer_repository_count": 0,
        "package_count": 0,
        "storage_bytes": 0,
        "last_accessed_at": None,
        "created_at": "2026-01-01T00:00:00Z",
    }


class FakeApi:
    def __init__(self, accounts: list[dict[str, Any]], remotes: list[dict[str, Any]]) -> None:
        self.accounts = accounts
        self.remotes = remotes
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> Response:
        self.calls.append(("GET", path, params))
        if path == "/v0/platform/accounts":
            return Response({"items": self.accounts, "next_cursor": None})
        if path == "/v0/platform/me":
            return Response({"principal_type": "user", "user": None, "credential": None})
        if path == "/v0/artifacts/remote-caches":
            selected = [
                item
                for item in self.remotes
                if params is None
                or (
                    (
                        not params.get("account_ref")
                        or item["account"]["ref"] == params["account_ref"]
                    )
                    and (not params.get("format") or item["format"] == params["format"])
                )
            ]
            return Response({"items": selected, "next_cursor": None})
        raise AssertionError(path)

    def issue_native(self, path: str, payload: dict):
        assert payload["duration_seconds"] == 14400
        return self.post(path, json=payload)

    def post(self, path: str, json: dict[str, Any] | None = None) -> Response:
        self.calls.append(("POST", path, json))
        for item in self.remotes:
            if path != f"/v0/artifacts/remote-caches/{item['ref']}/mint-token":
                continue
            # The cache ref travels in the path; the body carries only the duration.
            assert json == {"duration_seconds": 14400}
            official = item["source_type"] == "official"
            prefix = "o" if official else "c"
            public_name = item["official_source_ref"] if official else item["name"]
            return Response(
                {
                    "access_token": "rvs_sltEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEA",
                    "token_type": "bearer",
                    "expires_in": 14400,
                    "account_ref": item["account"]["ref"],
                    "remote_cache_ref": item["ref"],
                    "format": item["format"],
                    "native_path": f"/{prefix}/{public_name}",
                    "operations": ["read"],
                }
            )
        raise AssertionError(path)


def isolate(monkeypatch, tmp_path: Path, *, profiles: tuple[str, ...] = ("alice",)) -> None:
    config_dir = tmp_path / ".rvs"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    profile_tables = "\n".join(
        f"""
[profiles.{profile}]
api_url = "https://api.example.test"
account_ref = "personal-{profile}"
""".strip()
        for profile in profiles
    )
    config_file.write_text(
        f'schema_version = 1\ndefault_profile = "{profiles[0]}"\n\n{profile_tables}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    monkeypatch.delenv("RVS_PROFILE", raising=False)
    monkeypatch.delenv("RVS_ACCOUNT_REF", raising=False)
    monkeypatch.delenv("RVS_SESSION_ID", raising=False)


def test_target_parser_keeps_private_and_mirror_namespaces_disjoint() -> None:
    bare = parse_target("acme/backend")
    stable_internal = parse_target("in/ar_abcdefgh")

    assert bare.target_type == "repository"
    assert bare.namespace_realm is None
    assert stable_internal.namespace_realm == "internal"
    with pytest.raises(SystemExit):
        parse_target("gn_abcdefgh/ar_abcdefgh")
    assert parse_target("mirror:pypiorg").target_type == "official_cache"
    assert parse_target("custom-mirror:piwheels").target_type == "custom_cache"


def test_target_parser_accepts_the_canonical_repository_name() -> None:
    assert parse_target("repo:acme/backend") == parse_target("acme/backend")
    assert parse_target("repo:in/ar_abcdefgh") == parse_target("in/ar_abcdefgh")
    for selector in ("repo:", "repo:mirror:pypiorg", "repo:backend"):
        with pytest.raises(SystemExit):
            parse_target(selector)


@pytest.mark.parametrize("selector", ["other:acme/backend", "other:pypiorg", "@acme/backend"])
def test_unsupported_target_syntax_fails_before_repository_probe(
    monkeypatch, capsys, selector
) -> None:
    class NoProbeClient:
        def get(self, *_args, **_kwargs):
            raise AssertionError("an unsupported target must not probe private repositories")

    monkeypatch.setattr(
        ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: NoProbeClient()),
    )

    with pytest.raises(SystemExit):
        resolve_target(selector, profile="alice")

    assert "Unknown repository or mirror." in " ".join(capsys.readouterr().err.split())


def test_account_and_official_cache_selection_are_visible_and_clearable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    isolate(monkeypatch, tmp_path)
    personal = account("personal-alice", "Alice", "personal")
    acme = account("acme", "acme")
    fake = FakeApi(
        [personal, acme],
        [remote(owner=acme, family="official", name="pypiorg", kind="pypi", suffix="pypi")],
    )
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))

    switched = runner.invoke(app, ["account", "switch", "org:acme"])
    selected = runner.invoke(app, ["art", "select", "mirror:pypiorg"])

    assert switched.exit_code == 0, switched.output
    assert selected.exit_code == 0, selected.output
    saved = cfg_mod.selected_artifact_target("alice", "acme")
    assert saved is not None
    assert saved.target_type == "official_cache"
    assert saved.stable_selector == "mirror:rc_pypi2222"

    cleared = runner.invoke(app, ["art", "clear"])
    assert cleared.exit_code == 0, cleared.output
    assert cfg_mod.selected_artifact_target("alice", "acme") is None


def test_handle_selection_preserves_account_ref_and_canonical_case(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    owner = {**account("acme-id", "Acme"), "handle": "acmeHQ"}
    other = {**account("other-id", "acmeHQ"), "handle": "other-hq"}
    fake = FakeApi([owner, other], [])
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))
    selected = runner.invoke(app, ["account", "switch", "ACMEHQ"])
    assert selected.exit_code == 0, selected.output
    assert "org:acmeHQ" in selected.output
    assert cfg_mod.current_account_ref("alice") == "acme-id"
    cached = cfg_mod.cached_account("alice", "acme-id")
    assert cached is not None
    assert cached.account_handle == "acmeHQ"
    assert cached.account_ref == owner["ref"]


def test_typed_handle_selectors_disambiguate_user_and_organization(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    personal = account("personal-alice", "shared", "personal")
    organization = account("org-shared", "shared")
    fake = FakeApi([personal, organization], [])
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))

    selected_user = runner.invoke(app, ["account", "switch", "user:SHARED"])
    assert selected_user.exit_code == 0, selected_user.output
    assert "Account 'user:shared' selected" in selected_user.output
    assert cfg_mod.current_account_ref("alice") == "personal-alice"

    selected_org = runner.invoke(app, ["account", "switch", "org:shared"])
    assert selected_org.exit_code == 0, selected_org.output
    assert "Account 'org:shared' selected" in selected_org.output
    assert cfg_mod.current_account_ref("alice") == "org-shared"


def test_repository_resolution_rejects_cross_account_response(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    calls = []

    class ForeignRepositoryApi:
        def get(self, path, params):
            calls.append((path, params))
            return Response({"account": summary(account("foreign", "Foreign"))})

    monkeypatch.setattr(
        ApiClient, "from_profile", staticmethod(lambda profile=None: ForeignRepositoryApi())
    )
    with pytest.raises(SystemExit):
        resolve_target("main/packages", profile="alice", account_ref="selected")
    assert calls == [
        (
            "/v0/artifacts/repositories/resolve",
            {
                "selector": "main/packages",
                "account_ref": "selected",
            },
        )
    ]
    assert cfg_mod.cached_account("alice", "foreign") is None


def test_custom_cache_kind_collision_requires_disambiguation(monkeypatch, tmp_path: Path) -> None:
    isolate(monkeypatch, tmp_path)
    personal = account("personal-alice", "Alice", "personal")
    remotes = [
        remote(owner=personal, family="custom", name="piwheels", kind="pypi", suffix="py"),
        remote(owner=personal, family="custom", name="piwheels", kind="maven", suffix="mv"),
    ]
    fake = FakeApi([personal], remotes)
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))

    ambiguous = runner.invoke(app, ["art", "select", "custom-mirror:piwheels"])
    selected = runner.invoke(
        app,
        ["art", "select", "custom-mirror:piwheels", "--format", "pypi"],
    )

    assert ambiguous.exit_code == 1
    assert "ambiguous" in ambiguous.output
    assert selected.exit_code == 0, selected.output
    assert cfg_mod.selected_artifact_target("alice", "personal-alice").format == "pypi"


def test_two_login_profiles_keep_separate_actor_state_for_the_same_org(
    monkeypatch,
    tmp_path: Path,
) -> None:
    isolate(monkeypatch, tmp_path, profiles=("alice", "bob"))
    acme = account("acme", "acme")
    fake = FakeApi(
        [acme], [remote(owner=acme, family="official", name="pypiorg", kind="pypi", suffix="pypi")]
    )
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))

    assert (
        runner.invoke(app, ["account", "switch", "org:acme", "--profile", "alice"]).exit_code == 0
    )
    assert (
        runner.invoke(app, ["art", "select", "mirror:pypiorg", "--profile", "alice"]).exit_code == 0
    )
    assert runner.invoke(app, ["account", "switch", "org:acme", "--profile", "bob"]).exit_code == 0

    assert cfg_mod.selected_artifact_target("alice", "acme") is not None
    assert cfg_mod.selected_artifact_target("bob", "acme") is None


def test_one_shot_account_does_not_switch_the_active_account(monkeypatch, tmp_path: Path) -> None:
    isolate(monkeypatch, tmp_path)
    personal = account("personal-alice", "Alice", "personal")
    acme = account("acme", "acme")
    fake = FakeApi([personal, acme], [])
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))

    before = cfg_mod.CONFIG_FILE.read_text(encoding="utf-8")

    result = runner.invoke(app, ["art", "status", "--account", "org:acme"])

    assert result.exit_code == 0, result.output
    assert "org:acme · command option (--account)" in result.output
    assert cfg_mod.current_account_ref("alice") == "personal-alice"
    assert cfg_mod.CONFIG_FILE.read_text(encoding="utf-8") == before


def test_account_switch_is_local_to_an_integrated_shell(monkeypatch, tmp_path: Path) -> None:
    isolate(monkeypatch, tmp_path)
    personal = account("personal-alice", "Alice", "personal")
    acme = account("acme", "acme")
    fake = FakeApi([personal, acme], [])
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))
    monkeypatch.setenv("RVS_SESSION_ID", "terminal-a")

    switched = runner.invoke(app, ["account", "switch", "org:acme"])

    assert switched.exit_code == 0, switched.output
    assert cfg_mod.current_account_ref("alice") == "acme"
    monkeypatch.setenv("RVS_SESSION_ID", "terminal-b")
    assert cfg_mod.current_account_ref("alice") == "personal-alice"


def test_native_wrapper_uses_selected_cache_and_one_shot_does_not_mutate_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    isolate(monkeypatch, tmp_path)
    personal = account("personal-alice", "Alice", "personal")
    remotes = [
        remote(owner=personal, family="official", name="pypiorg", kind="pypi", suffix="py"),
        remote(owner=personal, family="custom", name="piwheels", kind="pypi", suffix="custom"),
    ]
    fake = FakeApi([personal], remotes)
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))
    assert runner.invoke(app, ["art", "select", "mirror:pypiorg"]).exit_code == 0

    calls: list[tuple[list[str], dict[str, str]]] = []

    class Completed:
        returncode = 0

    monkeypatch.setattr(runtime_tools, "pip_cmd", lambda: ["/bin/pip"])
    monkeypatch.setattr(
        native_runner.subprocess,
        "run",
        lambda cmd, *, env, check: calls.append((cmd, env.copy())) or Completed(),
    )

    result = runner.invoke(
        app,
        ["pip", "--rvs-target", "custom-mirror:piwheels", "install", "numpy"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == [
        "/bin/pip",
        "install",
        "--index-url",
        "https://mirror.pypi.rvsta.sh/c/piwheels/simple/",
        "numpy",
    ]
    assert cfg_mod.selected_artifact_target("alice", "personal-alice").display_selector == (
        "mirror:pypiorg"
    )


EXPECTED_TARGET = {
    "namespace_ref": "in_23456789",
    "namespace_name": "engineering",
    "namespace_realm": "internal",
    "repository_ref": "ar_abcdefgh",
    "repository_name": "packages",
}


class CrossAccountApi:
    def __init__(self):
        self.calls = []
        self.owner = summary(account("org-foreign", "OtherOrg"))

    def get(self, path, params=None):
        self.calls.append((path, params))
        assert path == "/v0/artifacts/repositories/resolve"
        return Response(
            {
                "ref": "ar_abcdefgh",
                "name": "packages",
                "account": self.owner,
                "namespace": {"ref": "in_23456789", "name": "engineering", "realm": "internal"},
                "formats": [{"format": "pypi"}],
                "allowed_actions": ["read"],
                "totals": {
                    "package_count": 0,
                    "version_count": 0,
                    "oci_path_count": 0,
                    "manifest_count": 0,
                    "storage_bytes": 0,
                },
                "latest_uploaded_at": None,
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )

    def issue_native(self, path, payload):
        assert payload["duration_seconds"] == 14400
        return self.post(path, json=payload)

    def post(self, path, json=None):
        self.calls.append((path, json))
        assert path == "/v0/artifacts/repositories/ar_abcdefgh/mint-token"
        assert "repository_ref" not in json
        assert json["formats"] == ["pypi"]
        assert json["operations"] == ["read"]
        assert json["expected_target"] == EXPECTED_TARGET
        return Response(
            {
                "access_token": "rvs_sltCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCA",
                "token_type": "bearer",
                "expires_in": 14400,
                "account_ref": "org-foreign",
                "target": EXPECTED_TARGET,
                "formats": ["pypi"],
                "operations": ["read"],
                "native_path": "/in/ar_abcdefgh",
            }
        )


def test_stable_cross_account_target_reports_owner_without_switching(monkeypatch, tmp_path, capsys):
    isolate(monkeypatch, tmp_path)
    fake = CrossAccountApi()
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))
    _, owner, target = resolve_target("in/ar_abcdefgh", profile="alice", kind="pypi")
    assert fake.calls == [
        (
            "/v0/artifacts/repositories/resolve",
            {
                "selector": "in/ar_abcdefgh",
                "format": "pypi",
            },
        )
    ]
    assert owner.account_ref == target.account_ref == "org-foreign"
    assert cfg_mod.current_account_ref("alice") == "personal-alice"
    assert "org:OtherOrg" in capsys.readouterr().out


def test_cross_account_selection_stays_in_current_context_and_uses_owner_credentials(
    monkeypatch, tmp_path
):
    from rvs.artifacts.targets import registry_context

    isolate(monkeypatch, tmp_path)
    fake = CrossAccountApi()
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))
    result = runner.invoke(app, ["art", "select", "in/ar_abcdefgh"])
    assert result.exit_code == 0, result.output
    assert "another account" in result.output
    assert cfg_mod.current_account_ref("alice") == "personal-alice"
    saved = cfg_mod.selected_artifact_target("alice", "personal-alice")
    assert saved is not None and saved.account_ref == "org-foreign"
    assert cfg_mod.selected_artifact_target("alice", "org-foreign") is None
    context = registry_context(kind="pypi", profile="alice")
    assert context.account_ref == "org-foreign"
    assert context.token == "rvs_sltCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCA"
    assert cfg_mod.current_account_ref("alice") == "personal-alice"


@pytest.mark.parametrize(
    "change",
    [
        {"target": {**EXPECTED_TARGET, "repository_ref": "ar_zzzzzzzz"}},
        {"native_path": "/in/../ar_zzzzzzzz"},
        {"native_path": 7},
    ],
)
def test_registry_context_requires_a_credential_for_the_selected_repository(
    monkeypatch, tmp_path, change
):
    from rvs.artifacts.targets import registry_context

    class _OtherRoute(CrossAccountApi):
        def post(self, path, json=None):
            response = super().post(path, json)
            return Response({**response.json(), **change})

    isolate(monkeypatch, tmp_path)
    fake = _OtherRoute()
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))
    with pytest.raises(SystemExit):
        registry_context(kind="pypi", target="in/ar_abcdefgh", profile="alice")


@pytest.mark.parametrize("selector", ["ar_abcdefgh", "in/ar_abcdefgh"])
def test_management_stable_reference_crosses_account_with_json_hint(
    monkeypatch, tmp_path, selector
):
    import json

    from rvs import output
    from rvs.artifacts.commands import _resolve_repository_entry

    isolate(monkeypatch, tmp_path)
    fake = CrossAccountApi()
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))
    messages = []
    monkeypatch.setattr(
        output.err_console, "print", lambda text, **kwargs: messages.append(json.loads(text))
    )
    output.set_json(True)
    try:
        entry = _resolve_repository_entry(selector, "alice")
    finally:
        output.set_json(False)
    assert entry["account"]["ref"] == "org-foreign"
    assert fake.calls == [("/v0/artifacts/repositories/resolve", {"selector": selector})]
    assert messages[0]["event"] == "cross_account_resource"
    assert messages[0]["selected_account_ref"] == "personal-alice"
    assert messages[0]["owner_account_ref"] == "org-foreign"
    assert messages[0]["owner_account"] == "org:OtherOrg"
    assert cfg_mod.current_account_ref("alice") == "personal-alice"


def test_management_readable_name_cannot_cross_account(monkeypatch, tmp_path):
    from rvs.artifacts.commands import _resolve_repository_entry

    isolate(monkeypatch, tmp_path)
    fake = CrossAccountApi()
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))
    with pytest.raises(SystemExit):
        _resolve_repository_entry("engineering/packages", "alice")
    assert fake.calls[0][1]["account_ref"] == "personal-alice"


def test_unauthorized_stable_reference_keeps_api_denial(monkeypatch, tmp_path):
    from rvs.client import ApiError

    isolate(monkeypatch, tmp_path)

    class DeniedApi:
        def get(self, path, params):
            raise ApiError(404, "Repository not found")

    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: DeniedApi()))
    with pytest.raises(SystemExit):
        resolve_target("in/ar_abcdefgh", profile="alice")
    assert cfg_mod.current_account_ref("alice") == "personal-alice"


@pytest.mark.parametrize("json_output", [False, True])
def test_cross_account_print_token_keeps_stdout_secret_only(monkeypatch, tmp_path, json_output):
    import json

    isolate(monkeypatch, tmp_path)
    fake = CrossAccountApi()
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))
    args = ["art", "token", "mint", "--target", "in/ar_abcdefgh"]
    if json_output:
        args.insert(0, "--json")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    secret = "rvs_sltCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCA"
    if json_output:
        assert json.loads(result.stdout)["access_token"] == secret
    else:
        assert result.stdout == secret + "\n"
    assert "another account" in result.stderr
    assert secret not in result.stderr
    assert cfg_mod.current_account_ref("alice") == "personal-alice"


def test_cross_account_issuance_denial_does_not_fallback_or_switch(monkeypatch, tmp_path):
    from rvs.artifacts.targets import registry_context
    from rvs.client import ApiError

    isolate(monkeypatch, tmp_path)

    class DeniedIssuance(CrossAccountApi):
        def issue_native(self, path, payload):
            self.calls.append((path, payload))
            raise ApiError(403, "Source grants do not allow the target")

    fake = DeniedIssuance()
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))
    with pytest.raises(SystemExit):
        registry_context(kind="pypi", target="in/ar_abcdefgh", profile="alice")
    assert [path for path, _ in fake.calls] == [
        "/v0/artifacts/repositories/resolve",
        "/v0/artifacts/repositories/ar_abcdefgh/mint-token",
    ]
    assert cfg_mod.current_account_ref("alice") == "personal-alice"


@pytest.mark.parametrize("automation", [False, True])
def test_hidden_repository_format_explains_automation_token_scope(
    monkeypatch, tmp_path, capsys, automation
):
    from rvs.client import ApiError

    isolate(monkeypatch, tmp_path)
    if automation:
        monkeypatch.setenv("RVS_TOKEN", "rvs_ust" + "A" * 43)
    else:
        monkeypatch.delenv("RVS_TOKEN", raising=False)

    class HiddenFormatApi:
        def get(self, path, params=None):
            if path == "/v0/platform/me":
                account = {"ref": "personal-alice", "handle": "alice", "type": "personal"}
                return Response(
                    {
                        "principal_type": "user",
                        "credential": {"scenario": "user_personal", "account": account},
                    }
                )
            assert params["format"] == "pypi"
            raise ApiError(404, "Repository not found")

    monkeypatch.setattr(
        ApiClient, "from_profile", staticmethod(lambda profile=None: HiddenFormatApi())
    )
    with pytest.raises(SystemExit):
        resolve_target("in/ar_abcdefgh", profile="alice", kind="pypi")
    message = " ".join(capsys.readouterr().err.split())
    assert "Repository not found" in message
    hint = (
        "Repository not found. Check that RVS_TOKEN includes this repository and the pypi format."
    )
    assert (hint in message) is automation


def test_unselected_format_explains_automation_token_scope(monkeypatch):
    from types import SimpleNamespace

    from rvs.artifacts.discovery import Discovery

    monkeypatch.setenv("RVS_TOKEN", "rvs_ust" + "A" * 43)

    def discovery(target_type: str) -> Discovery:
        target = SimpleNamespace(target_type=target_type)
        return Discovery("alice", target, ("npm",), ("in", "ar_abcdefgh"))  # type: ignore[arg-type]

    with pytest.raises(
        ValueError, match="Check that RVS_TOKEN includes this repository and the pypi format"
    ):
        discovery("repository").select_format("pypi")
    # A mirror, or a format the command cannot use, is not a token-scope problem.
    for target_type, applicable in (("official_cache", ("pypi", "npm")), ("repository", ("oci",))):
        with pytest.raises(ValueError) as raised:
            discovery(target_type).select_format("pypi", applicable)
        assert "RVS_TOKEN" not in str(raised.value)


def test_registry_context_routes_an_opaque_native_path_as_is(monkeypatch, tmp_path):
    from rvs.artifacts.targets import registry_context

    class _OpaqueRoute(CrossAccountApi):
        def post(self, path, json=None):
            response = super().post(path, json)
            return Response({**response.json(), "native_path": "/r/v2/ar_abcdefgh/"})

    isolate(monkeypatch, tmp_path)
    fake = _OpaqueRoute()
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))

    context = registry_context(kind="pypi", target="in/ar_abcdefgh", profile="alice")

    assert context.native_path == "r/v2/ar_abcdefgh"
