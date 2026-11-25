"""Per-format selection and the automatic default mirror of personal accounts."""

import json
from typing import TYPE_CHECKING, Any

import pytest
from rvs import config as cfg_mod
from rvs.artifacts import targets
from rvs.cli import app
from rvs.client import ApiClient, ApiError
from rvs.native import runner as native_runner
from rvs.runtime import tools as runtime_tools
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()

PERSONAL = {
    "ref": "ac_personal",
    "handle": "alice",
    "type": "personal",
    "display_name": "Alice",
    "is_admin": True,
    "organization_role": None,
    "authority_revision": 1,
}
ORGANIZATION = {
    "ref": "ac_acme",
    "handle": "acme",
    "type": "organization",
    "display_name": "Acme",
    "is_admin": False,
    "organization_role": "member",
    "authority_revision": 1,
}


class Response:
    def __init__(self, value: Any, status_code: int = 200) -> None:
        self.value = value
        self.status_code = status_code

    def json(self) -> Any:
        return self.value


def summary(account: dict[str, Any]) -> dict[str, Any]:
    return {"ref": account["ref"], "handle": account["handle"], "type": account["type"]}


def official(
    owner: dict[str, Any], kind: str, source: str, *, default: bool | None = True
) -> dict[str, Any]:
    item = {
        "ref": f"rc_{source.replace('-', '')[:8].ljust(8, '2')}",
        "account": summary(owner),
        "format": kind,
        "source_type": "official",
        "name": f"{source} (official)",
        "official_source_ref": source,
        "created_at": "2026-01-01T00:00:00Z",
    }
    if default is not None:
        item["is_default"] = default
    return item


def repository(owner: dict[str, Any], *kinds: str) -> dict[str, Any]:
    return {
        "ref": "ar_abcdefgh",
        "name": "packages",
        "account": summary(owner),
        "namespace": {"ref": "in_23456789", "name": "engineering", "realm": "internal"},
        "formats": [{"format": kind} for kind in kinds],
        "allowed_actions": ["content.read"],
    }


class FakeApi:
    def __init__(
        self,
        accounts: list[dict[str, Any]],
        remotes: list[dict[str, Any]] | None = None,
        repo: dict[str, Any] | None = None,
        sources: list[dict[str, Any]] | None = None,
        create: Any = None,
    ) -> None:
        self.accounts = accounts
        self.remotes = remotes or []
        self.repo = repo
        self.sources = sources or []
        self.create = create
        self.calls: list[tuple[str, str, Any]] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> Response:
        self.calls.append(("GET", path, params))
        params = params or {}
        if path == "/v0/platform/accounts":
            return Response({"items": self.accounts, "next_cursor": None})
        if path == "/v0/platform/me":
            return Response({"principal_type": "user", "user": None, "credential": None})
        if path == "/v0/artifacts/remote-caches":
            items = [
                item
                for item in self.remotes
                if item["account"]["ref"] == params.get("account_ref", item["account"]["ref"])
                and item["format"] == params.get("format", item["format"])
            ]
            return Response({"items": items, "next_cursor": None})
        if path == "/v0/artifacts/official-sources":
            return Response({"items": self.sources, "next_cursor": None})
        if path == "/v0/artifacts/repositories/resolve":
            assert self.repo is not None
            kind = params.get("format")
            if kind is not None and kind not in {item["format"] for item in self.repo["formats"]}:
                raise ApiError(404, "Repository not found.")
            return Response(self.repo)
        raise AssertionError(path)

    def issue_native(self, path: str, payload: dict) -> Response:
        return self.post(path, json=payload)

    def post(self, path: str, json: dict[str, Any] | None = None) -> Response:
        self.calls.append(("POST", path, json))
        if path == "/v0/artifacts/remote-caches":
            if isinstance(self.create, Exception):
                raise self.create
            created = self.create.json()
            if created not in self.remotes:
                self.remotes.append(created)
            return self.create
        for item in self.remotes:
            if path == f"/v0/artifacts/remote-caches/{item['ref']}/mint-token":
                return Response(
                    {
                        "access_token": "rvs_sltEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEA",
                        "token_type": "bearer",
                        "expires_in": 14400,
                        "account_ref": item["account"]["ref"],
                        "remote_cache_ref": item["ref"],
                        "format": item["format"],
                        "native_path": f"/o/{item['official_source_ref']}",
                        "operations": ["read"],
                    }
                )
        raise AssertionError(path)

    def paths(self) -> list[str]:
        return [path for _, path, _ in self.calls]


def isolate(monkeypatch, tmp_path: Path, account: dict[str, Any] = PERSONAL) -> None:
    config_dir = tmp_path / ".rvs"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    config_file.write_text(
        f"""schema_version = 2
default_profile = "alice"

[profiles.alice]
api_url = "https://api.example.test"
account_ref = "ac_personal"
active_account_ref = "{account["ref"]}"

[profiles.alice.accounts.{account["ref"]}]
account_type = "{account["type"]}"
account_handle = "{account["handle"]}"
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    for key in ("RVS_PROFILE", "RVS_ACCOUNT_REF", "RVS_SESSION_ID", "RVS_TOKEN"):
        monkeypatch.delenv(key, raising=False)


def serve(monkeypatch, fake: FakeApi) -> FakeApi:
    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: fake))
    return fake


def capture_pip(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    class Completed:
        returncode = 0

    monkeypatch.setattr(runtime_tools, "pip_cmd", lambda: ["/bin/pip"])
    monkeypatch.setattr(runtime_tools, "require", lambda name, *, install_kind: f"/bin/{name}")
    monkeypatch.setattr(
        native_runner.subprocess,
        "run",
        lambda cmd, *, env, check: calls.append(cmd) or Completed(),
    )
    return calls


def selected(account_ref: str = "ac_personal") -> dict[str, str]:
    return {
        kind: target.display_selector
        for kind, target in cfg_mod.selected_artifact_targets("alice", account_ref).items()
    }


def text(result) -> str:
    return " ".join(result.output.split())


# ── rvs art select / clear ────────────────────────────────────────────────────


def test_select_without_format_selects_every_format_the_repository_has(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    serve(
        monkeypatch,
        FakeApi(
            [PERSONAL],
            [official(PERSONAL, "maven", "maven-central")],
            repository(PERSONAL, "pypi", "npm", "oci"),
        ),
    )
    assert runner.invoke(app, ["art", "mirror", "select", "maven-central"]).exit_code == 0

    result = runner.invoke(app, ["art", "select", "engineering/packages"])

    assert result.exit_code == 0, result.output
    assert "for pypi, npm, oci" in text(result)
    # Formats the repository lacks keep their selection.
    assert selected() == {
        "pypi": "repo:engineering/packages",
        "npm": "repo:engineering/packages",
        "oci": "repo:engineering/packages",
        "maven": "mirror:maven-central",
    }
    stored = cfg_mod.selected_artifact_target("alice", "ac_personal", "npm")
    assert stored is not None and stored.format == "npm"


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (["--format", "pypi"], ["pypi"]),
        (["--format", "pypi,npm"], ["pypi", "npm"]),
        (["--format", "npm", "--format", "oci"], ["npm", "oci"]),
    ],
)
def test_select_with_formats_selects_only_those(monkeypatch, tmp_path, flags, expected):
    isolate(monkeypatch, tmp_path)
    serve(monkeypatch, FakeApi([PERSONAL], [], repository(PERSONAL, "pypi", "npm", "oci")))

    result = runner.invoke(app, ["art", "select", "engineering/packages", *flags])

    assert result.exit_code == 0, result.output
    assert sorted(selected()) == sorted(expected)


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (["--format", "maven"], "Repository not found"),
        (["--format", "pypi,maven"], "has no maven format. It has: pypi, npm"),
        (["--format", "all"], "Unknown or empty format 'all'"),
    ],
)
def test_select_refuses_formats_the_target_lacks(monkeypatch, tmp_path, flags, message):
    isolate(monkeypatch, tmp_path)
    serve(monkeypatch, FakeApi([PERSONAL], [], repository(PERSONAL, "pypi", "npm")))

    result = runner.invoke(app, ["art", "select", "engineering/packages", *flags])

    assert result.exit_code == 1
    assert message in text(result)
    assert selected() == {}


def test_mirror_select_replaces_only_its_format(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    serve(
        monkeypatch,
        FakeApi(
            [PERSONAL],
            [official(PERSONAL, "npm", "npmjs")],
            repository(PERSONAL, "pypi", "npm"),
        ),
    )
    assert runner.invoke(app, ["art", "select", "engineering/packages"]).exit_code == 0

    result = runner.invoke(app, ["art", "mirror", "select", "npmjs"])

    assert result.exit_code == 0, result.output
    assert selected() == {"pypi": "repo:engineering/packages", "npm": "mirror:npmjs"}


def test_clear_removes_one_format_or_all(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    serve(monkeypatch, FakeApi([PERSONAL], [], repository(PERSONAL, "pypi", "npm", "maven")))
    assert runner.invoke(app, ["art", "select", "engineering/packages"]).exit_code == 0

    one = runner.invoke(app, ["art", "clear", "--format", "npm"])

    assert one.exit_code == 0, one.output
    assert "for npm" in text(one)
    assert sorted(selected()) == ["maven", "pypi"]

    everything = runner.invoke(app, ["art", "clear"])

    assert everything.exit_code == 0, everything.output
    assert selected() == {}
    assert runner.invoke(app, ["art", "clear", "--format", "rubygems"]).exit_code == 1


def test_configuration_rejects_a_target_stored_for_another_format(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    mirror = targets._remote_target(official(PERSONAL, "pypi", "pypiorg"), "official_cache")

    with pytest.raises(ValueError, match="pypi target cannot be selected for npm"):
        cfg_mod.update_selected_artifact_targets(
            select={"npm": mirror}, profile="alice", account_ref="ac_personal"
        )


# ── Automatic default mirror ──────────────────────────────────────────────────


def test_personal_read_without_selection_uses_the_default_mirror(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    fake = serve(
        monkeypatch,
        FakeApi(
            [PERSONAL],
            [
                official(PERSONAL, "pypi", "pytorch", default=False),
                official(PERSONAL, "pypi", "pypiorg"),
            ],
        ),
    )
    calls = capture_pip(monkeypatch)

    result = runner.invoke(app, ["pip", "install", "requests"])

    assert result.exit_code == 0, result.output
    assert calls[0] == [
        "/bin/pip",
        "install",
        "--index-url",
        "https://mirror.pypi.rvsta.sh/o/pypiorg/simple/",
        "requests",
    ]
    note = " ".join(result.stderr.split())
    assert "Using mirror:pypiorg, the default pypi mirror (no pypi target selected" in note
    assert "/v0/artifacts/remote-caches/rc_pypiorg2/mint-token" in fake.paths()
    # Nothing is selected or created on the user's behalf.
    assert selected() == {}
    assert all(method == "GET" or "mint-token" in path for method, path, _ in fake.calls)


def test_server_without_the_default_marker_uses_the_well_known_source(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    serve(
        monkeypatch,
        FakeApi(
            [PERSONAL],
            [
                official(PERSONAL, "pypi", "pytorch", default=None),
                official(PERSONAL, "pypi", "pypiorg", default=None),
            ],
        ),
    )
    calls = capture_pip(monkeypatch)

    result = runner.invoke(app, ["pip", "download", "requests"])

    assert result.exit_code == 0, result.output
    assert "https://mirror.pypi.rvsta.sh/o/pypiorg/simple/" in calls[0]


def test_deleted_default_mirror_is_never_recreated(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    fake = serve(
        monkeypatch, FakeApi([PERSONAL], [official(PERSONAL, "pypi", "pytorch", default=False)])
    )
    calls = capture_pip(monkeypatch)

    result = runner.invoke(app, ["pip", "install", "requests"])

    assert result.exit_code == 1
    assert "has no default pypi mirror" in text(result)
    assert "rvs art mirror create --format pypi --select" in text(result)
    assert calls == []
    assert [method for method, _, _ in fake.calls] == ["GET"] * len(fake.calls)


def test_organization_never_falls_back_to_a_mirror(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path, ORGANIZATION)
    fake = serve(
        monkeypatch, FakeApi([PERSONAL, ORGANIZATION], [official(ORGANIZATION, "pypi", "pypiorg")])
    )
    calls = capture_pip(monkeypatch)

    result = runner.invoke(app, ["pip", "install", "requests"])

    assert result.exit_code == 1
    assert "No Ravenstash target is selected for pypi" in text(result)
    assert calls == []
    assert "/v0/artifacts/remote-caches" not in fake.paths()


def test_organization_package_read_reports_the_missing_selection(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path, ORGANIZATION)
    serve(monkeypatch, FakeApi([PERSONAL, ORGANIZATION]))

    with pytest.raises(SystemExit):
        targets.registry_context(kind="npm", profile="alice")


def test_automation_never_falls_back_to_a_mirror(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("RVS_TOKEN", "raw-control-token")
    fake = serve(monkeypatch, FakeApi([PERSONAL], [official(PERSONAL, "pypi", "pypiorg")]))
    calls = capture_pip(monkeypatch)

    result = runner.invoke(app, ["pip", "install", "requests"])

    assert result.exit_code == 1
    assert "No Ravenstash target is selected for pypi" in text(result)
    assert calls == []
    assert "/v0/artifacts/remote-caches" not in fake.paths()


def test_publish_never_falls_back_to_a_mirror(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    serve(monkeypatch, FakeApi([PERSONAL], [official(PERSONAL, "pypi", "pypiorg")]))
    calls = capture_pip(monkeypatch)

    result = runner.invoke(app, ["twine", "upload", "dist/demo.whl"])

    assert result.exit_code == 1
    assert "No Ravenstash target is selected for pypi" in text(result)
    assert calls == []
    with pytest.raises(SystemExit):
        targets.registry_context(
            kind="pypi", profile="alice", require_private=True, operations=("read", "publish")
        )


def test_publish_context_explains_that_mirrors_are_read_only(monkeypatch, tmp_path, capsys):
    isolate(monkeypatch, tmp_path)
    serve(monkeypatch, FakeApi([PERSONAL], [official(PERSONAL, "pypi", "pypiorg")]))

    with pytest.raises(SystemExit):
        targets.effective_target(kind="pypi", profile="alice", operations=("read", "publish"))

    assert "Mirrors are read-only" in " ".join(capsys.readouterr().err.split())


def test_not_signed_in_is_reported_before_any_fallback(monkeypatch, tmp_path):
    from rvs import client as client_mod

    isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(client_mod.auth_mod, "credential_origin_error", lambda profile: None)
    monkeypatch.setattr(client_mod.auth_mod, "credential_needs_refresh", lambda profile: False)
    monkeypatch.setattr(client_mod.auth_mod, "get_token", lambda profile, refresh=True: None)
    calls = capture_pip(monkeypatch)

    result = runner.invoke(app, ["pip", "install", "requests"])

    assert result.exit_code == 1
    assert "rvs auth login" in text(result)
    assert calls == []


def test_explicit_selection_wins_over_the_default_mirror(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    serve(
        monkeypatch,
        FakeApi(
            [PERSONAL],
            [
                official(PERSONAL, "pypi", "pypiorg"),
                official(PERSONAL, "pypi", "pytorch", default=False),
            ],
        ),
    )
    calls = capture_pip(monkeypatch)
    assert runner.invoke(app, ["art", "mirror", "select", "pytorch"]).exit_code == 0

    result = runner.invoke(app, ["pip", "install", "torch"])

    assert result.exit_code == 0, result.output
    assert "https://mirror.pypi.rvsta.sh/o/pytorch/simple/" in calls[0]
    assert "default pypi mirror" not in result.stderr


def test_container_commands_explain_how_to_choose_a_repository(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    fake = serve(monkeypatch, FakeApi([PERSONAL], [official(PERSONAL, "pypi", "pypiorg")]))

    result = runner.invoke(app, ["docker", "pull", "demo:1"])

    assert result.exit_code == 1
    message = text(result)
    assert "need a private repository with the oci format" in message
    assert "rvs art select NAMESPACE/REPOSITORY" in message
    assert "dashboard" in message
    assert "/v0/artifacts/remote-caches" not in fake.paths()


# ── Discovery and package commands use the selection of their format ─────────


def test_endpoint_uses_the_selection_of_the_requested_format(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    serve(
        monkeypatch,
        FakeApi([PERSONAL], [official(PERSONAL, "npm", "npmjs")], repository(PERSONAL, "pypi")),
    )
    assert runner.invoke(app, ["art", "select", "engineering/packages"]).exit_code == 0
    assert runner.invoke(app, ["art", "mirror", "select", "npmjs"]).exit_code == 0

    npm = runner.invoke(app, ["--json", "art", "endpoint", "--format", "npm"])
    ambiguous = runner.invoke(app, ["art", "endpoint"])
    maven = runner.invoke(app, ["art", "endpoint", "--format", "maven"])

    assert npm.exit_code == 0, npm.output
    assert json.loads(npm.output)["target"] == "mirror:npmjs"
    assert ambiguous.exit_code == 1
    assert "Different targets are selected for different formats" in text(ambiguous)
    assert maven.exit_code == 1
    assert "No maven target is selected" in text(maven)


def test_endpoint_without_any_selection_asks_for_one(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    serve(monkeypatch, FakeApi([PERSONAL]))

    result = runner.invoke(app, ["art", "endpoint"])

    assert result.exit_code == 1
    assert "No target is selected" in text(result)


def test_package_commands_need_one_repository_across_formats(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    fake = serve(
        monkeypatch,
        FakeApi([PERSONAL], [official(PERSONAL, "npm", "npmjs")], repository(PERSONAL, "pypi")),
    )
    assert runner.invoke(app, ["art", "select", "engineering/packages"]).exit_code == 0
    assert runner.invoke(app, ["art", "mirror", "select", "npmjs"]).exit_code == 0
    other = targets._repository_target(
        {**repository(PERSONAL, "maven"), "ref": "ar_zzzzzzzz", "name": "jars"}
    )
    cfg_mod.update_selected_artifact_targets(
        select={"maven": other}, profile="alice", account_ref="ac_personal"
    )

    several = runner.invoke(app, ["art", "package", "list"])
    mirror = runner.invoke(app, ["art", "package", "list", "--format", "npm"])
    missing = runner.invoke(app, ["art", "package", "list", "--format", "maven"])

    assert several.exit_code == 1
    assert "Different repositories are selected for maven, pypi" in text(several)
    assert mirror.exit_code == 1
    assert "'mirror:npmjs' is a private mirror" in text(mirror)
    assert missing.exit_code == 1
    assert "/v0/artifacts/repositories/resolve" in fake.paths()
    cfg_mod.update_selected_artifact_targets(
        clear=("maven", "pypi"), profile="alice", account_ref="ac_personal"
    )
    only_mirror = runner.invoke(app, ["art", "package", "list"])
    assert "'mirror:npmjs' is a private mirror" in text(only_mirror)
    cfg_mod.update_selected_artifact_targets(
        clear=("npm",), profile="alice", account_ref="ac_personal"
    )
    nothing = runner.invoke(app, ["art", "package", "list"])
    assert "No repository is selected" in text(nothing)


# ── rvs art mirror create ─────────────────────────────────────────────────────

SOURCES = [
    {"ref": "pypiorg", "format": "pypi", "display_name": "PyPI", "is_default": True},
    {"ref": "pytorch", "format": "pypi", "display_name": "PyTorch", "is_default": False},
]


def test_mirror_create_by_format_uses_the_default_source(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    created = official(PERSONAL, "pypi", "pypiorg")
    fake = serve(
        monkeypatch,
        FakeApi([PERSONAL], [], sources=SOURCES, create=Response(created, 201)),
    )

    result = runner.invoke(app, ["art", "mirror", "create", "--format", "pypi", "--select"])

    assert result.exit_code == 0, result.output
    assert "Added private mirror 'mirror:pypiorg'" in text(result)
    assert ("POST", "/v0/artifacts/remote-caches") in [(m, p) for m, p, _ in fake.calls]
    assert selected() == {"pypi": "mirror:pypiorg"}


def test_mirror_create_reuses_an_existing_mirror(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    existing = official(PERSONAL, "pypi", "pypiorg")
    fake = serve(
        monkeypatch, FakeApi([PERSONAL], [existing], sources=SOURCES, create=Response(existing))
    )

    result = runner.invoke(app, ["art", "mirror", "create", "pypiorg", "--min-age-hours", "48"])

    assert result.exit_code == 0, result.output
    assert "Private mirror 'mirror:pypiorg' already exists" in text(result)
    assert "Age options apply only to a new mirror" in text(result)
    assert "POST" not in [method for method, _path, _body in fake.calls]


def test_mirror_create_reports_a_server_answer_for_an_existing_mirror(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    existing = official(PERSONAL, "pypi", "pypiorg")
    serve(monkeypatch, FakeApi([PERSONAL], [], sources=SOURCES, create=Response(existing, 200)))

    result = runner.invoke(app, ["art", "mirror", "create", "pypiorg"])

    assert result.exit_code == 0, result.output
    assert "Private mirror 'mirror:pypiorg' already exists" in text(result)


def test_mirror_create_treats_an_older_server_conflict_as_existing(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    existing = official(PERSONAL, "pypi", "pypiorg", default=None)
    sources = [
        {key: value for key, value in item.items() if key != "is_default"} for item in SOURCES
    ]

    class _CreatedConcurrently(FakeApi):
        """The mirror appears between the first lookup and the request."""

        def post(self, path: str, json: dict[str, Any] | None = None) -> Response:
            if path == "/v0/artifacts/remote-caches":
                self.remotes.append(existing)
            return super().post(path, json)

    fake = serve(
        monkeypatch,
        _CreatedConcurrently([PERSONAL], [], sources=sources, create=ApiError(409, "Conflict")),
    )

    result = runner.invoke(app, ["art", "mirror", "create", "--format", "pypi", "--select"])

    assert result.exit_code == 0, result.output
    assert "already exists" in text(result)
    assert ("POST", "/v0/artifacts/remote-caches") in [(m, p) for m, p, _ in fake.calls]
    assert selected() == {"pypi": "mirror:pypiorg"}


@pytest.mark.parametrize(
    ("remotes", "error", "message"),
    [
        ([], ApiError(409, "Conflict"), "Conflict"),
        ([], ApiError(403, "Forbidden"), "Forbidden"),
    ],
)
def test_mirror_create_reports_other_failures(monkeypatch, tmp_path, remotes, error, message):
    isolate(monkeypatch, tmp_path)
    serve(monkeypatch, FakeApi([PERSONAL], remotes, sources=SOURCES, create=error))

    result = runner.invoke(app, ["art", "mirror", "create", "pypiorg"])

    assert result.exit_code == 1
    assert message in text(result)


def test_mirror_create_without_a_default_source_fails(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    sources = [{**item, "is_default": False} for item in SOURCES if item["ref"] != "pypiorg"]
    serve(monkeypatch, FakeApi([PERSONAL], [], sources=sources))

    result = runner.invoke(app, ["art", "mirror", "create", "--format", "pypi"])

    assert result.exit_code == 1
    assert "no default pypi mirror source" in text(result)


def test_default_official_cache_ignores_other_accounts_formats_and_families():
    assert targets.default_official_cache([], "pypi") is None
    custom = {**official(PERSONAL, "pypi", "pypiorg"), "source_type": "custom"}
    assert targets.default_official_cache([custom], "pypi") is None
    other_format = official(PERSONAL, "npm", "npmjs")
    assert targets.default_official_cache([other_format], "pypi") is None
    assert (
        targets.default_official_cache([official(PERSONAL, "oci", "x", default=None)], "oci")
        is None
    )


def test_an_unmarked_listing_falls_back_to_the_well_known_default():
    unmarked = official(PERSONAL, "pypi", "pypiorg", default=False)
    other = official(PERSONAL, "pypi", "pytorch", default=False)
    assert targets.default_official_cache([other, unmarked], "pypi") == unmarked
    marked = official(PERSONAL, "pypi", "pytorch", default=True)
    assert targets.default_official_cache([unmarked, marked], "pypi") == marked
