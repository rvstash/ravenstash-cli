import json
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from rvs import config as cfg_mod
from rvs.cli import app
from rvs.client import ApiClient
from rvs.native import runner as native_runner
from rvs.native.adapters import ADAPTERS
from rvs.native.npm import NPM_GRAMMAR, spec_has_version
from rvs.runtime import tools as runtime_tools
from typer.testing import CliRunner


if TYPE_CHECKING:
    from collections.abc import Callable


runner = CliRunner()
STAGING_API_URL = "https://control.example.test"
PYPI_READ_URL = "https://python-read.example.test"
PYPI_PUSH_URL = "https://python-write.example.test"
PYPI_MIRROR_URL = "https://python-mirror.example.test"
NPM_READ_URL = "https://javascript-read.example.test"
NPM_PUSH_URL = "https://javascript-write.example.test"
NPM_MIRROR_URL = "https://javascript-mirror.example.test"
MAVEN_READ_URL = "https://java-read.example.test"
MAVEN_PUSH_URL = "https://java-write.example.test"
MAVEN_MIRROR_URL = "https://java-mirror.example.test"
MAVEN_ID = "rvs-0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _fixed_maven_server_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the per-run random Maven repository ID so settings can be asserted."""
    monkeypatch.setattr(secrets, "token_hex", lambda _size: MAVEN_ID[4:])


PYPI_READ_HOST = "python-read.example.test"
PYPI_MIRROR_HOST = "python-mirror.example.test"
NPM_READ_HOST = "javascript-read.example.test"
NPM_PUSH_HOST = "javascript-write.example.test"


def _native_endpoints() -> cfg_mod.NativeRegistryEndpoints:
    return cfg_mod.NativeRegistryEndpoints(
        pypi=cfg_mod.PackageRegistryEndpoints(PYPI_READ_URL, PYPI_PUSH_URL, PYPI_MIRROR_URL),
        npm=cfg_mod.PackageRegistryEndpoints(NPM_READ_URL, NPM_PUSH_URL, NPM_MIRROR_URL),
        maven=cfg_mod.PackageRegistryEndpoints(MAVEN_READ_URL, MAVEN_PUSH_URL, MAVEN_MIRROR_URL),
        oci_registry_base_url="https://images.example.test",
    )


class _Completed:
    returncode = 0


class _JsonResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def json(self) -> Any:
        return self.payload


REPOSITORY_MINT_PATH = "/v0/artifacts/repositories/ar_xyzabcde/mint-token"
REMOTE_CACHE_MINT_PREFIX = "/v0/artifacts/remote-caches/"
EXPECTED_REPOSITORY_TARGET = {
    "namespace_ref": "in_abcdefgh",
    "namespace_name": "staging",
    "namespace_realm": "internal",
    "repository_ref": "ar_xyzabcde",
    "repository_name": "repo",
}


class _FakeApi:
    def get(self, path: str, params=None) -> _JsonResponse:
        if path == "/v0/platform/accounts":
            return _JsonResponse(
                {
                    "items": [
                        {
                            "ref": "ac_23456789",
                            "handle": "staging",
                            "type": "personal",
                            "display_name": None,
                            "is_admin": True,
                            "organization_role": None,
                            "authority_revision": 1,
                        }
                    ],
                    "next_cursor": None,
                }
            )
        assert path == "/v0/artifacts/repositories/resolve"
        return _JsonResponse(
            {
                "ref": "ar_xyzabcde",
                "name": "repo",
                "account": {"ref": "ac_23456789", "handle": "staging", "type": "personal"},
                "namespace": {"ref": "in_abcdefgh", "name": "staging", "realm": "internal"},
                "formats": [
                    {"format": "pypi"},
                    {"format": "npm"},
                    {"format": "maven"},
                ],
                "allowed_actions": [],
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

    def issue_native(self, path: str, payload: dict):
        assert payload["duration_seconds"] == 14400
        return self.post(path, json=payload)

    def post(self, path: str, json: dict[str, Any] | None = None) -> _JsonResponse:
        if path.startswith(REMOTE_CACHE_MINT_PREFIX):
            assert json == {"duration_seconds": 14400}
            remote_cache_ref = path.removeprefix(REMOTE_CACHE_MINT_PREFIX).removesuffix(
                "/mint-token"
            )
            assert remote_cache_ref
            assert path == f"{REMOTE_CACHE_MINT_PREFIX}{remote_cache_ref}/mint-token"
            namespace = "o" if remote_cache_ref == "pypiorg" else "c"
            return _JsonResponse(
                {
                    "access_token": "rvs_sltBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBA",
                    "token_type": "Bearer",
                    "expires_in": 14400,
                    "account_ref": "ac_23456789",
                    "remote_cache_ref": remote_cache_ref,
                    "format": "pypi",
                    "native_path": f"/{namespace}/{remote_cache_ref}",
                    "operations": ["read"],
                }
            )
        assert path == REPOSITORY_MINT_PATH
        assert json is not None
        assert set(json) == {"formats", "operations", "duration_seconds", "expected_target"}
        assert json["operations"] in (
            ["read"],
            ["publish"],
            ["read", "publish"],
            ["read", "publish", "delete"],
        )
        assert json["expected_target"] == EXPECTED_REPOSITORY_TARGET
        return _JsonResponse(
            {
                "access_token": "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "token_type": "Bearer",
                "expires_in": 14400,
                "account_ref": "ac_23456789",
                "target": dict(EXPECTED_REPOSITORY_TARGET),
                "formats": list(json["formats"]),
                "operations": list(json["operations"]),
                "native_path": "/in/ar_xyzabcde",
            }
        )


def _isolate_config(monkeypatch: Any, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    config_file.write_text(
        f"""
schema_version = 1
default_profile = "staging"

[profiles.staging]
api_url = "{STAGING_API_URL}"
account_ref = "ac_23456789"
active_account_ref = "ac_23456789"

[profiles.staging.accounts.ac_23456789]
account_type = "personal"
account_handle = "staging"

[profiles.staging.accounts.ac_23456789.selected_target]
target_type = "repository"
account_ref = "ac_23456789"
stable_selector = "in/ar_xyzabcde"
display_selector = "staging/repo"
namespace_realm = "internal"
namespace_ref = "in_abcdefgh"
namespace_name_cache = "staging"
repository_ref = "ar_xyzabcde"
repository_name_cache = "repo"

[profiles.staging.native_registries.pypi]
read_base_url = "{PYPI_READ_URL}"
push_base_url = "{PYPI_PUSH_URL}"
mirror_base_url = "{PYPI_MIRROR_URL}"

[profiles.staging.native_registries.npm]
read_base_url = "{NPM_READ_URL}"
push_base_url = "{NPM_PUSH_URL}"
mirror_base_url = "{NPM_MIRROR_URL}"

[profiles.staging.native_registries.maven]
read_base_url = "{MAVEN_READ_URL}"
push_base_url = "{MAVEN_PUSH_URL}"
mirror_base_url = "{MAVEN_MIRROR_URL}"

[profiles.staging.native_registries.oci]
registry_base_url = "https://images.example.test"

""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RVS_TOKEN", "raw-control-token")
    monkeypatch.setattr(
        ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: _FakeApi()),
    )


def _mock_native_tools(monkeypatch: Any) -> None:
    monkeypatch.setattr(runtime_tools, "pip_cmd", lambda: ["/bin/pip"])
    monkeypatch.setattr(runtime_tools, "npm", lambda: "/bin/npm")
    monkeypatch.setattr(runtime_tools, "python", lambda: "/bin/python")
    monkeypatch.setattr(
        runtime_tools,
        "require",
        lambda name, *, install_kind: f"/bin/{name}",
    )


def _capture_run(
    monkeypatch: Any,
    calls: list[dict[str, Any]],
    hook: Callable[[list[str], dict[str, str]], None] | None = None,
) -> None:
    def fake_run(cmd: list[str], *, env: dict[str, str], check: bool) -> _Completed:
        del check
        assert "RVS_TOKEN" not in env
        if hook:
            hook(cmd, env)
        calls.append({"cmd": cmd, "env": env.copy()})
        return _Completed()

    monkeypatch.setattr(native_runner.subprocess, "run", fake_run)


@pytest.mark.parametrize(
    "wrong_token", ["rvs_ust" + "A" * 43, "rvs_xyz" + "C" * 43, "rvs_slt" + "B" * 43]
)
def test_native_wrapper_rejects_wrong_type_before_starting_child(
    monkeypatch, tmp_path, wrong_token
):
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    original_post = _FakeApi.post

    def wrong_credential(self, path, json=None):
        response = original_post(self, path, json)
        payload = response.json()
        payload["access_token"] = wrong_token
        return _JsonResponse(payload)

    monkeypatch.setattr(_FakeApi, "post", wrong_credential)
    calls = []
    _capture_run(monkeypatch, calls)
    result = runner.invoke(app, ["pip", "install", "demo"])
    assert result.exit_code != 0
    assert "Invalid public credential" in result.output
    assert wrong_token not in result.output
    assert calls == []


def test_native_commands_request_only_the_operations_they_need() -> None:
    assert native_runner._operations_for("pip", ["install", "demo"]) == ("read",)
    assert native_runner._operations_for("uv", ["publish", "dist/demo.whl"]) == ("publish",)
    assert native_runner._operations_for("twine", ["upload", "dist/*"]) == ("publish",)
    assert native_runner._operations_for("python", ["-m", "build"]) == ("read",)
    assert native_runner._operations_for("npm", ["ci"]) == ("read",)
    assert native_runner._operations_for("npm", ["publish"]) == ("publish",)
    # Publish never implies deletion: unpublish requests delete explicitly.
    assert native_runner._operations_for("npm", ["unpublish", "demo@1.0.0"]) == (
        "read",
        "publish",
        "delete",
    )
    assert native_runner._operations_for("npm", ["deprecate", "demo@1", "old"]) == (
        "read",
        "publish",
    )
    assert native_runner._operations_for("npm", ["undeprecate", "demo@1"]) == (
        "read",
        "publish",
    )
    assert native_runner._operations_for("npm", ["dist-tag", "add", "demo@1", "next"]) == (
        "read",
        "publish",
    )
    # Listing dist-tags is a read; only changes need publish.
    assert native_runner._operations_for("npm", ["dist-tag", "ls", "demo"]) == ("read",)
    assert native_runner._operations_for("npm", ["dist-tags", "ls", "demo"]) == ("read",)
    assert native_runner._operations_for("mvn", ["test"]) == ("read",)
    assert native_runner._operations_for("mvn", ["deploy"]) == ("read", "publish")
    assert native_runner._operations_for(
        "mvn",
        ["org.apache.maven.plugins:maven-deploy-plugin:3.1.3:deploy-file"],
    ) == ("read", "publish")


def test_native_npm_operations_come_from_the_command_not_package_names() -> None:
    operations = native_runner._operations_for
    # Only the npm command counts; later positionals are package specs or arguments.
    for argv in (
        ["install", "unpublish"],
        ["install", "publish"],
        ["view", "demo", "dist-tag"],
        ["install", "tag", "deprecate"],
        # npm has had no `tag` command since npm 7; it never needs publish.
        ["tag", "demo@1.0.0", "beta"],
        ["exec", "--", "npm", "unpublish", "demo"],
    ):
        assert operations("npm", argv) == ("read",), argv
        assert not native_runner._is_publishing("npm", argv), argv
    # An option value before the command is not the command.
    assert operations("npm", ["--loglevel", "verbose", "unpublish", "demo@1.0.0"]) == (
        "read",
        "publish",
        "delete",
    )
    assert operations("npm", ["--workspace=pkg", "publish"]) == ("publish",)
    assert operations("npm", ["--tag", "unpublish", "install", "demo"]) == ("read",)
    assert NPM_GRAMMAR.command(["--force"]) is None


def test_native_npm_respects_project_npmrc_and_injects_path_scoped_auth(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    (tmp_path / ".npmrc").write_text(
        f"@acme:registry={NPM_READ_URL}/in/ar_xyzabcde/\n",
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(app, ["npm", "install", "@acme/widgets"])

    assert result.exit_code == 0
    assert calls[0]["cmd"] == [
        "/bin/npm",
        "install",
        "--registry",
        f"{NPM_READ_URL}/in/ar_xyzabcde/",
        "@acme/widgets",
    ]
    assert calls[0]["env"]["NPM_CONFIG_REGISTRY"] == f"{NPM_READ_URL}/in/ar_xyzabcde/"
    assert (
        calls[0]["env"][f"NPM_CONFIG_//{NPM_READ_HOST}/in/ar_xyzabcde/:_authToken"]
        == "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )


@pytest.mark.parametrize(
    "tool,args",
    [
        ("npm", ["publish"]),
        ("uv", ["publish", "dist/demo.whl"]),
        ("twine", ["upload", "dist/demo.whl"]),
        ("mvn", ["deploy"]),
    ],
)
def test_native_publish_decline_does_not_launch(monkeypatch, tmp_path: Path, tool, args) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls = []
    _capture_run(monkeypatch, calls)
    result = runner.invoke(app, [tool, "--rvs-target", "staging/repo", *args], input="\n")
    assert result.exit_code != 0
    assert "Publish to repo:staging/repo (user:staging)" in result.output
    assert not calls


@pytest.mark.parametrize(
    "flags,answer,published", [([], "n\n", False), ([], "y\n", True), (["--rvs-yes"], "", True)]
)
def test_native_detected_registry_confirms_resolved_account(
    monkeypatch, tmp_path: Path, flags, answer, published
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    monkeypatch.setenv("NPM_CONFIG_REGISTRY", f"{NPM_PUSH_URL}/in/ar_xyzabcde/")

    class OrgApi(_FakeApi):
        def get(self, path, params=None):
            payload = super().get(path, params).json()
            payload["account"].update(type="organization")
            return _JsonResponse(payload)

    monkeypatch.setattr(ApiClient, "from_profile", staticmethod(lambda profile=None: OrgApi()))
    calls = []
    _capture_run(monkeypatch, calls)
    result = runner.invoke(app, ["npm", *flags, "publish"], input=answer)
    assert (result.exit_code == 0) == published, result.output
    assert bool(calls) == published
    if not flags:
        assert "Publish to repo:staging/repo (org:staging)" in result.output
    if calls:
        assert "--rvs-yes" not in calls[0]["cmd"]


def test_native_npm_repo_override_uses_upload_registry_for_publish(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(app, ["npm", "--rvs-target", "staging/repo-npm", "publish"], input="y\n")

    assert result.exit_code == 0
    assert calls[0]["cmd"] == [
        "/bin/npm",
        "publish",
        "--registry",
        f"{NPM_PUSH_URL}/in/ar_xyzabcde/",
    ]
    assert calls[0]["env"]["NPM_CONFIG_REGISTRY"] == (f"{NPM_PUSH_URL}/in/ar_xyzabcde/")
    assert (
        calls[0]["env"][f"NPM_CONFIG_//{NPM_PUSH_HOST}/in/ar_xyzabcde/:_authToken"]
        == "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )


def test_native_npm_repo_override_uses_upload_registry_for_unpublish(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["npm", "--rvs-target", "staging/repo-npm", "unpublish", "demo@1.0.0"],
    )

    assert result.exit_code == 0
    assert calls[0]["cmd"] == [
        "/bin/npm",
        "unpublish",
        "--registry",
        f"{NPM_PUSH_URL}/in/ar_xyzabcde/",
        "demo@1.0.0",
    ]
    assert calls[0]["env"]["NPM_CONFIG_REGISTRY"] == f"{NPM_PUSH_URL}/in/ar_xyzabcde/"
    assert (
        calls[0]["env"][f"NPM_CONFIG_//{NPM_PUSH_HOST}/in/ar_xyzabcde/:_authToken"]
        == "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    assert "moves the version to the repository trash" in " ".join(result.stderr.split())


def test_native_npm_unpublish_mints_delete_authority_and_explains_refusals(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    minted: list[list[str]] = []
    original_post = _FakeApi.post

    def recording_post(self, path, json=None):
        if json is not None and "operations" in json:
            minted.append(list(json["operations"]))
        return original_post(self, path, json)

    monkeypatch.setattr(_FakeApi, "post", recording_post)

    class _Refused:
        returncode = 1

    monkeypatch.setattr(
        native_runner.subprocess,
        "run",
        lambda cmd, *, env, check: _Refused(),
    )

    result = runner.invoke(
        app,
        ["npm", "--rvs-target", "staging/repo-npm", "unpublish", "demo", "--force"],
    )

    assert result.exit_code == 1
    assert minted == [["read", "publish", "delete"]]
    stderr = " ".join(result.stderr.split())
    assert "E405" in stderr
    assert "works only while it has one version left" in stderr
    assert "rvs art package delete-version NAME VERSION" in stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["dist-tag"],
        ["dist-tag", "demo"],
        ["dist-tag", "ls", "demo"],
        ["dist-tags", "list", "demo"],
        ["dist-tag", "--loglevel", "verbose", "l", "demo"],
    ],
)
def test_npm_dist_tag_classifies_reads_and_changes_by_subcommand(argv) -> None:
    assert NPM_GRAMMAR.dist_tag_action(argv) == "ls"
    assert not NPM_GRAMMAR.is_mutation(argv)
    for subcommand in ("add", "a", "set", "s"):
        change = ["dist-tag", subcommand, "demo@1.0.0", "beta"]
        assert NPM_GRAMMAR.dist_tag_action(change) == "add"
        assert native_runner._operations_for("npm", change) == ("read", "publish")
    for subcommand in ("rm", "r", "del", "d", "remove"):
        change = ["dist-tag", subcommand, "demo", "beta"]
        assert NPM_GRAMMAR.dist_tag_action(change) == "rm"
        assert native_runner._operations_for("npm", change) == ("read", "publish")
    assert NPM_GRAMMAR.dist_tag_action(["install", "demo"]) is None


def _record_mints(monkeypatch: Any) -> list[list[str]]:
    minted: list[list[str]] = []
    original_post = _FakeApi.post

    def recording_post(self, path, json=None):
        if json is not None and "operations" in json:
            minted.append(list(json["operations"]))
        return original_post(self, path, json)

    monkeypatch.setattr(_FakeApi, "post", recording_post)
    return minted


def test_native_npm_dist_tag_list_reads_the_download_registry_with_read_only_access(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    minted = _record_mints(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["npm", "--rvs-target", "staging/repo-npm", "dist-tag", "ls", "demo"],
    )

    assert result.exit_code == 0, result.output
    assert minted == [["read"]]
    assert calls[0]["cmd"] == [
        "/bin/npm",
        "dist-tag",
        "--registry",
        f"{NPM_READ_URL}/in/ar_xyzabcde/",
        "ls",
        "demo",
    ]
    assert calls[0]["env"]["NPM_CONFIG_REGISTRY"] == f"{NPM_READ_URL}/in/ar_xyzabcde/"
    assert (
        calls[0]["env"][f"NPM_CONFIG_//{NPM_READ_HOST}/in/ar_xyzabcde/:_authToken"]
        == "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    assert "Target:" not in result.output


@pytest.mark.parametrize(
    ("argv", "prompt"),
    [
        (["dist-tag", "add", "demo@1.0.0"], "Point npm dist-tag 'latest' at demo@1.0.0?"),
        (["dist-tag", "set", "demo@1.0.0", "latest"], "Point npm dist-tag 'latest'"),
        (["dist-tag", "add", "demo@1.0.0", "--tag", "latest"], "Point npm dist-tag 'latest'"),
        (["dist-tag", "rm", "demo", "beta"], "Remove npm dist-tag 'beta' from demo?"),
        (["dist-tags", "remove", "demo", "latest"], "Remove npm dist-tag 'latest' from demo?"),
        # npm reads a spec without a version as `*` and sends that to `latest`.
        (
            ["dist-tag", "add", "demo"],
            "Point npm dist-tag 'latest' at demo (no version given; npm sends '*')?",
        ),
        (["dist-tag", "add", "demo@"], "at demo@ (no version given; npm sends '*')?"),
        (["dist-tag", "add", "@scope/demo", "latest"], "at @scope/demo (no version given"),
    ],
)
def test_native_npm_dist_tag_changes_to_latest_and_removals_are_confirmed(
    monkeypatch: Any, tmp_path: Path, argv, prompt
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    minted = _record_mints(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)
    base = ["npm", "--rvs-target", "staging/repo-npm", *argv]

    declined = runner.invoke(app, base, input="n\n")
    accepted = runner.invoke(app, base, input="y\n")
    skipped = runner.invoke(app, [*base[:1], "--rvs-yes", *base[1:]])

    assert declined.exit_code != 0
    assert prompt in " ".join(declined.output.split())
    assert "Target: repo:staging/repo" in declined.output
    assert accepted.exit_code == 0, accepted.output
    assert skipped.exit_code == 0, skipped.output
    assert "Target:" not in skipped.output
    assert len(calls) == 2
    for call in calls:
        assert call["cmd"][2:4] == ["--registry", f"{NPM_PUSH_URL}/in/ar_xyzabcde/"]
        assert "--rvs-yes" not in call["cmd"]
    assert minted[-1] == ["read", "publish"]


@pytest.mark.parametrize(
    "argv",
    [
        ["dist-tag", "add", "demo@2.0.0-beta.1", "beta"],
        ["dist-tag", "add", "demo@2.0.0-beta.1", "--tag", "beta"],
        ["dist-tag", "add", "demo", "beta"],
        # npm refuses these with its usage message before any request.
        ["dist-tag", "add"],
        ["dist-tag", "rm", "demo"],
    ],
)
def test_native_npm_dist_tag_additions_of_other_tags_are_not_confirmed(
    monkeypatch: Any, tmp_path: Path, argv
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(app, ["npm", "--rvs-target", "staging/repo-npm", *argv])

    assert result.exit_code == 0, result.output
    assert "Target:" not in result.output
    assert calls[0]["cmd"][2:4] == ["--registry", f"{NPM_PUSH_URL}/in/ar_xyzabcde/"]


@pytest.mark.parametrize(
    ("spec", "has_version"),
    [
        ("demo@1.0.0", True),
        ("@scope/demo@1.0.0", True),
        ("demo", False),
        ("demo@", False),
        ("@scope/demo", False),
    ],
)
def test_native_npm_dist_tag_spec_needs_a_version(spec: str, has_version: bool) -> None:
    assert spec_has_version(spec) is has_version


def test_native_npm_dist_tag_default_tag_follows_npm_configuration() -> None:
    argv = ["dist-tag", "add", "demo@1.0.0"]

    assert NPM_GRAMMAR.dist_tag_confirmation(argv, {}) is not None
    assert NPM_GRAMMAR.dist_tag_confirmation(argv, {"NPM_CONFIG_TAG": "next"}) is None
    assert NPM_GRAMMAR.dist_tag_confirmation(argv, {"npm_config_tag": "next"}) is None
    assert NPM_GRAMMAR.dist_tag_confirmation(["install", "demo"], {}) is None


def test_native_npm_dist_tag_removal_says_an_upstream_tag_may_take_over() -> None:
    # Removing the repository's tag uncovers an upstream's tag of the same name.
    assert NPM_GRAMMAR.dist_tag_confirmation(["dist-tag", "rm", "demo", "beta"], {}) == (
        "Remove npm dist-tag 'beta' from demo? Installs by this tag stop working "
        "unless an upstream supplies the same tag."
    )


def test_native_npm_dist_tag_tag_option_before_the_command_selects_the_tag(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    minted = _record_mints(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)
    argv = ["dist-tag", "add", "demo@1.0.0"]

    beta = runner.invoke(app, ["npm", "--rvs-target", "staging/repo-npm", "--tag", "beta", *argv])
    latest = runner.invoke(
        app, ["npm", "--rvs-target", "staging/repo-npm", "--tag", "latest", *argv], input="y\n"
    )

    assert beta.exit_code == 0, beta.output
    assert "Target:" not in beta.output
    assert latest.exit_code == 0, latest.output
    assert "Point npm dist-tag 'latest' at demo@1.0.0?" in " ".join(latest.output.split())
    assert minted == [["read", "publish"], ["read", "publish"]]
    for call, tag in zip(calls, ("beta", "latest"), strict=True):
        assert call["cmd"][1:3] == ["--tag", tag]
        assert call["cmd"][-2:] == ["--registry", f"{NPM_PUSH_URL}/in/ar_xyzabcde/"]


@pytest.mark.parametrize(
    "argv", [["dist-tag", "add", "demo@1.0.0"], ["dist-tag", "rm", "demo", "beta"]]
)
def test_native_npm_dist_tag_confirmation_without_an_answer_needs_rvs_yes(
    monkeypatch: Any, tmp_path: Path, argv
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    # No input and no terminal, as in CI: the question cannot be answered.
    result = runner.invoke(app, ["npm", "--rvs-target", "staging/repo-npm", *argv])

    assert result.exit_code == 1
    message = " ".join(result.output.split())
    assert "no answer could be read" in message
    assert "Pass --rvs-yes in non-interactive runs such as CI." in message
    assert calls == []


def test_native_npm_dist_tag_confirmation_in_json_mode_needs_rvs_yes(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["--json", "npm", "--rvs-target", "staging/repo-npm", "dist-tag", "rm", "demo", "beta"],
    )

    assert result.exit_code == 1
    assert "--rvs-yes" in result.output
    assert calls == []


def test_native_npm_repo_override_replaces_conflicting_registry_flag(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(
        app,
        [
            "npm",
            "--rvs-target",
            "staging/repo-npm",
            "install",
            "--registry",
            "https://registry.npmjs.org/",
            "@acme/widgets",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["cmd"] == [
        "/bin/npm",
        "install",
        "--registry",
        f"{NPM_READ_URL}/in/ar_xyzabcde/",
        "@acme/widgets",
    ]


def test_native_npm_isolate_uses_distinct_empty_config_files(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    config_snapshots: list[tuple[Path, Path, str, str]] = []

    def hook(_cmd: list[str], env: dict[str, str]) -> None:
        user_config = Path(env["NPM_CONFIG_USERCONFIG"])
        global_config = Path(env["NPM_CONFIG_GLOBALCONFIG"])
        config_snapshots.append(
            (
                user_config,
                global_config,
                user_config.read_text(encoding="utf-8"),
                global_config.read_text(encoding="utf-8"),
            )
        )

    _capture_run(monkeypatch, calls, hook)

    result = runner.invoke(
        app,
        [
            "npm",
            "--rvs-target",
            "staging/repo-npm",
            "--rvs-native-config",
            "isolate",
            "install",
            "@acme/widgets",
        ],
    )

    assert result.exit_code == 0
    user_config, global_config, user_text, global_text = config_snapshots[0]
    assert user_config != global_config
    assert user_text == global_text == ""
    assert not user_config.exists()
    assert not global_config.exists()


def test_native_pip_respects_existing_index_and_injects_temp_netrc(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    monkeypatch.delenv("PIP_KEYRING_PROVIDER", raising=False)
    monkeypatch.setenv(
        "PIP_INDEX_URL",
        f"{PYPI_READ_URL}/in/ar_xyzabcde/simple/",
    )
    calls: list[dict[str, Any]] = []
    netrc_texts: list[str] = []

    def hook(_cmd: list[str], env: dict[str, str]) -> None:
        netrc_texts.append(Path(env["NETRC"]).read_text(encoding="utf-8"))

    _capture_run(monkeypatch, calls, hook)

    result = runner.invoke(app, ["pip", "install", "demo"])

    assert result.exit_code == 0
    assert calls[0]["cmd"] == [
        "/bin/pip",
        "install",
        "--index-url",
        f"{PYPI_READ_URL}/in/ar_xyzabcde/simple/",
        "demo",
    ]
    assert calls[0]["env"]["PIP_INDEX_URL"] == (f"{PYPI_READ_URL}/in/ar_xyzabcde/simple/")
    assert "PIP_KEYRING_PROVIDER" not in calls[0]["env"]
    assert netrc_texts == [
        f"machine {PYPI_READ_HOST} login __token__ password rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
    ]
    assert not Path(calls[0]["env"]["NETRC"]).exists()


def test_native_pip_exchanges_profile_token_for_scoped_remote_credential(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    index_url = f"{PYPI_MIRROR_URL}/c/piwheels/simple/"
    monkeypatch.setenv("PIP_INDEX_URL", index_url)
    monkeypatch.setattr(
        native_runner,
        "_resolve_route",
        lambda *a, **kw: native_runner.RegistryRoute(
            "pypi",
            PYPI_MIRROR_URL,
            None,
            "c/piwheels",
            "rvs_sltBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBA",
        ),
    )
    calls: list[dict[str, Any]] = []
    netrc_texts: list[str] = []

    def hook(_cmd: list[str], env: dict[str, str]) -> None:
        netrc_texts.append(Path(env["NETRC"]).read_text(encoding="utf-8"))

    _capture_run(monkeypatch, calls, hook)

    result = runner.invoke(app, ["pip", "download", "--no-deps", "simple-range==0.0.3"])

    assert result.exit_code == 0
    assert calls[0]["cmd"] == [
        "/bin/pip",
        "download",
        "--index-url",
        index_url,
        "--no-deps",
        "simple-range==0.0.3",
    ]
    assert calls[0]["env"]["PIP_INDEX_URL"] == index_url
    assert netrc_texts == [
        f"machine {PYPI_MIRROR_HOST} login __token__ password rvs_sltBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBA\n"
    ]


def test_native_pip_local_remote_cache_netrc_uses_hostname_without_port(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    config_path = cfg_mod.CONFIG_FILE
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            PYPI_MIRROR_URL,
            "http://localhost:43101/registry/pypi",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "PIP_INDEX_URL",
        "http://localhost:43101/registry/pypi/c/piwheels/simple/",
    )
    monkeypatch.setattr(
        native_runner,
        "_resolve_route",
        lambda *a, **kw: native_runner.RegistryRoute(
            "pypi",
            "http://localhost:43101/registry/pypi",
            None,
            "c/piwheels",
            "rvs_sltBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBA",
        ),
    )
    calls: list[dict[str, Any]] = []
    netrc_texts: list[str] = []

    def hook(_cmd: list[str], env: dict[str, str]) -> None:
        netrc_texts.append(Path(env["NETRC"]).read_text(encoding="utf-8"))

    _capture_run(monkeypatch, calls, hook)

    result = runner.invoke(app, ["pip", "download", "--no-deps", "simple-range==0.0.3"])

    assert result.exit_code == 0
    assert netrc_texts == [
        "machine localhost login __token__ password rvs_sltBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBA\n"
    ]


def test_native_pip_exchanges_official_remote_credential(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    monkeypatch.setenv(
        "PIP_INDEX_URL",
        f"{PYPI_MIRROR_URL}/o/pypiorg/simple/",
    )
    monkeypatch.setattr(
        native_runner,
        "_resolve_route",
        lambda *a, **kw: native_runner.RegistryRoute(
            "pypi",
            PYPI_MIRROR_URL,
            None,
            "o/pypiorg",
            "rvs_sltBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBA",
        ),
    )
    captured: list[str] = []

    def hook(_cmd: list[str], env: dict[str, str]) -> None:
        captured.append(Path(env["NETRC"]).read_text(encoding="utf-8"))

    _capture_run(monkeypatch, [], hook)

    result = runner.invoke(app, ["pip", "download", "demo"])

    assert result.exit_code == 0
    assert captured == [
        f"machine {PYPI_MIRROR_HOST} login __token__ password rvs_sltBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBA\n"
    ]


def test_native_pip_isolate_overrides_index_without_writing_credentials(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    netrc_texts: list[str] = []

    def hook(_cmd: list[str], env: dict[str, str]) -> None:
        netrc_texts.append(Path(env["NETRC"]).read_text(encoding="utf-8"))

    _capture_run(monkeypatch, calls, hook)

    result = runner.invoke(
        app,
        [
            "pip",
            "--rvs-target",
            "staging/repo-pypi",
            "--rvs-native-config",
            "isolate",
            "install",
            "demo",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["cmd"] == [
        "/bin/pip",
        "install",
        "--index-url",
        f"{PYPI_READ_URL}/in/ar_xyzabcde/simple/",
        "demo",
    ]
    assert calls[0]["env"]["PIP_CONFIG_FILE"] == os.devnull
    assert netrc_texts == [
        f"machine {PYPI_READ_HOST} login __token__ password rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
    ]


def test_native_pip_preserves_multi_part_pip_command_prefix(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    monkeypatch.setattr(runtime_tools, "pip_cmd", lambda: ["/bin/python", "-m", "pip"])
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(app, ["pip", "--rvs-target", "staging/repo-pypi", "install", "demo"])

    assert result.exit_code == 0
    assert calls[0]["cmd"] == [
        "/bin/python",
        "-m",
        "pip",
        "install",
        "--index-url",
        f"{PYPI_READ_URL}/in/ar_xyzabcde/simple/",
        "demo",
    ]


def test_native_uv_repo_override_sets_index_publish_env_and_netrc(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(app, ["uv", "--rvs-target", "staging/repo-pypi", "sync", "--locked"])

    assert result.exit_code == 0
    assert calls[0]["cmd"] == ["/bin/uv", "sync", "--locked"]
    assert calls[0]["env"]["UV_DEFAULT_INDEX"] == (f"{PYPI_READ_URL}/in/ar_xyzabcde/simple/")
    assert "UV_PUBLISH_PASSWORD" not in calls[0]["env"]
    assert "NETRC" in calls[0]["env"]
    assert not Path(calls[0]["env"]["NETRC"]).exists()


def test_native_python_build_injects_read_access_for_pip_and_uv_installers(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    minted = _record_mints(monkeypatch)
    calls: list[dict[str, Any]] = []
    netrc_texts: list[str] = []

    def hook(_cmd: list[str], env: dict[str, str]) -> None:
        netrc_texts.append(Path(env["NETRC"]).read_text(encoding="utf-8"))

    _capture_run(monkeypatch, calls, hook)

    result = runner.invoke(
        app, ["python", "-m", "build", "--sdist", "--outdir", "out", "--installer", "uv", "."]
    )

    assert result.exit_code == 0, result.output
    assert minted == [["read"]]
    assert calls[0]["cmd"] == [
        "/bin/python",
        "-m",
        "build",
        "--sdist",
        "--outdir",
        "out",
        "--installer",
        "uv",
        ".",
    ]
    index_url = f"{PYPI_READ_URL}/in/ar_xyzabcde/simple/"
    assert calls[0]["env"]["PIP_INDEX_URL"] == index_url
    assert calls[0]["env"]["UV_DEFAULT_INDEX"] == index_url
    assert calls[0]["env"]["UV_INDEX_URL"] == index_url
    assert "PIP_CONFIG_FILE" not in calls[0]["env"]
    assert "UV_NO_CONFIG" not in calls[0]["env"]
    assert netrc_texts == [
        f"machine {PYPI_READ_HOST} login __token__ password rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
    ]
    assert not Path(calls[0]["env"]["NETRC"]).exists()


def test_native_python_build_isolate_ignores_pip_and_uv_config(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(
        app, ["python", "--rvs-native-config", "isolate", "-m", "build", "--wheel"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["cmd"] == ["/bin/python", "-m", "build", "--wheel"]
    assert calls[0]["env"]["PIP_CONFIG_FILE"] == os.devnull
    assert calls[0]["env"]["UV_NO_CONFIG"] == "1"


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["script.py"],
        ["-c", "print(1)"],
        ["-m", "pip", "install", "demo"],
        ["-I", "-m", "build"],
        ["-m", "builder"],
    ],
)
def test_native_python_refuses_anything_but_the_build_module(
    monkeypatch: Any,
    tmp_path: Path,
    args: list[str],
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    minted = _record_mints(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(app, ["python", *args])

    assert result.exit_code != 0
    assert "rvs python only runs `python -m build`" in result.output
    assert minted == []
    assert calls == []


@pytest.mark.parametrize(
    "args",
    [["--version"], ["-h"], ["--no-isolation"], ["-n", "--wheel"]],
)
def test_native_python_build_without_installs_needs_no_target_or_access(
    monkeypatch: Any,
    tmp_path: Path,
    args: list[str],
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    cfg_mod.set_selected_artifact_target(None, profile="staging", account_ref="ac_23456789")
    monkeypatch.delenv("PIP_INDEX_URL", raising=False)
    monkeypatch.delenv("NETRC", raising=False)
    minted = _record_mints(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(app, ["python", "-m", "build", *args])

    assert result.exit_code == 0, result.output
    assert minted == []
    assert calls[0]["cmd"] == ["/bin/python", "-m", "build", *args]
    assert "PIP_INDEX_URL" not in calls[0]["env"]
    assert "NETRC" not in calls[0]["env"]


def test_native_twine_repo_override_sets_ephemeral_upload_credentials(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(
        app,
        ["twine", "--rvs-yes", "--rvs-target", "staging/repo-pypi", "upload", "dist/demo.whl"],
    )

    assert result.exit_code == 0
    assert calls[0]["cmd"] == ["/bin/twine", "upload", "dist/demo.whl"]
    assert calls[0]["env"]["TWINE_REPOSITORY_URL"] == (f"{PYPI_PUSH_URL}/in/ar_xyzabcde/")
    assert calls[0]["env"]["TWINE_USERNAME"] == "__token__"
    assert calls[0]["env"]["TWINE_PASSWORD"] == "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def test_native_maven_repo_override_generates_temp_settings(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    settings_texts: list[str] = []

    def hook(cmd: list[str], _env: dict[str, str]) -> None:
        settings_path = Path(cmd[cmd.index("--settings") + 1])
        settings_texts.append(settings_path.read_text(encoding="utf-8"))

    _capture_run(monkeypatch, calls, hook)

    result = runner.invoke(
        app, ["mvn", "--rvs-yes", "--rvs-target", "staging/repo-maven", "deploy"]
    )

    assert result.exit_code == 0
    assert calls[0]["cmd"][0:2] == ["/bin/mvn", "--settings"]
    assert calls[0]["cmd"][-1] == (
        f"-DaltDeploymentRepository={MAVEN_ID}::default::{MAVEN_PUSH_URL}/in/ar_xyzabcde/"
    )
    assert f"<id>{MAVEN_ID}</id>" in settings_texts[0]
    assert "<mirrorOf>central</mirrorOf>" in settings_texts[0]
    assert "<username>__token__</username>" in settings_texts[0]
    assert (
        "<password>rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA</password>"
        in settings_texts[0]
    )
    assert not Path(calls[0]["cmd"][calls[0]["cmd"].index("--settings") + 1]).exists()


def test_native_maven_read_only_mirror_uses_only_download_route(tmp_path: Path) -> None:
    route = native_runner.RegistryRoute(
        kind="maven",
        read_base_url=MAVEN_MIRROR_URL,
        push_base_url=None,
        native_path="o/maven-central",
        package_token="rvs_sltBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBA",
    )
    cmd = [
        "/bin/mvn",
        "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:get",
        "-Dartifact=com.example:demo:1.0.0",
    ]

    native_runner._inject_override(
        "mvn",
        cmd,
        1,
        {},
        route,
        tmp_path,
        isolate=True,
    )

    settings_path = Path(cmd[cmd.index("--settings") + 1])
    settings_text = settings_path.read_text(encoding="utf-8")
    expected_url = f"{MAVEN_MIRROR_URL}/o/maven-central/"
    assert expected_url in settings_text
    assert "<mirrorOf>*</mirrorOf>" in settings_text
    assert MAVEN_PUSH_URL not in settings_text


def test_native_maven_passthrough_accepts_dependency_get_flags(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    monkeypatch.setattr(
        native_runner,
        "_resolve_route",
        lambda *_args, **_kwargs: native_runner.RegistryRoute(
            "maven",
            MAVEN_MIRROR_URL,
            None,
            "o/maven-central",
            "download-token",
        ),
    )
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)
    native_args = [
        "--batch-mode",
        "--no-transfer-progress",
        "-U",
        f"-Dmaven.repo.local={tmp_path / 'm2'}",
        "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:get",
        "-Dartifact=org.apache.commons:commons-lang3:3.14.0",
        "-Dtransitive=false",
    ]
    result = runner.invoke(
        app,
        [
            "mvn",
            "--rvs-target",
            "mirror:maven-central",
            "--rvs-native-config",
            "isolate",
            *native_args,
        ],
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    command = calls[0]["cmd"]
    assert command[0:2] == ["/bin/mvn", "--settings"]
    assert command[-len(native_args) :] == native_args


def test_native_maven_deploy_file_injects_upload_destination(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(
        app,
        [
            "mvn",
            "--rvs-target",
            "staging/repo-maven",
            "org.apache.maven.plugins:maven-deploy-plugin:3.1.3:deploy-file",
            "--rvs-yes",
            "-Dfile=demo.jar",
            "-DpomFile=demo.pom",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["cmd"][-2:] == [
        f"-DrepositoryId={MAVEN_ID}",
        f"-Durl={MAVEN_PUSH_URL}/in/ar_xyzabcde/",
    ]


@pytest.mark.parametrize(
    "tool,args",
    [
        ("pip", ["install", "demo"]),
        ("uv", ["lock"]),
        ("npm", ["ci"]),
        ("mvn", ["verify"]),
        ("twine", ["upload", "dist/demo.whl"]),
        ("python", ["-m", "build"]),
    ],
)
def test_missing_target_never_launches_or_infers_from_native_config(
    monkeypatch, tmp_path, tool, args
):
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    cfg_mod.set_selected_artifact_target(None, profile="staging", account_ref="ac_23456789")
    monkeypatch.setenv("PIP_INDEX_URL", f"{PYPI_READ_URL}/in/ar_xyzabcde/simple/")
    calls = []
    _capture_run(monkeypatch, calls)
    result = runner.invoke(app, [tool, *args])
    assert result.exit_code != 0
    assert "No Ravenstash target is selected" in result.output
    assert not calls


@pytest.mark.parametrize(
    "tool,args,name,content",
    [
        (
            "uv",
            ["sync", "--locked"],
            "uv.lock",
            """version = 1
[[package]]
name = "demo"
version = "1.0"
source = { registry = "https://vendor.test/simple" }
wheels = [{url = "https://user:do-not-print@vendor.test/secret-path/demo.whl?token=secret-query"}]
""",
        ),
        (
            "npm",
            ["ci"],
            "package-lock.json",
            '{"packages":{"node_modules/demo":{"resolved":"https://vendor.test/demo.tgz","integrity":"unchanged"}}}',
        ),
        ("pip", ["install", "-r", "requirements.txt"], "requirements.txt", "-r nested.txt\n"),
    ],
)
def test_foreign_lock_sources_warn_without_changes(
    monkeypatch, tmp_path, tool, args, name, content
):
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    path = tmp_path / name
    path.write_text(content)
    (tmp_path / "nested.txt").write_text(
        "--extra-index-url https://vendor.test/simple\ndemo==1.0\n"
    )
    calls = []
    _capture_run(monkeypatch, calls)
    result = runner.invoke(app, [tool, "--rvs-target", "staging/repo", *args])
    assert result.exit_code == 0, result.output
    assert "Additional native sources" in result.output
    assert "do-not-print" not in result.output
    assert "secret-path" not in result.output
    assert "secret-query" not in result.output
    assert path.read_text() == content
    assert len(calls) == 1
    if tool == "uv":
        assert calls[0]["cmd"] == ["/bin/uv", "sync", "--locked"]


@pytest.mark.parametrize(
    ("source", "warns"),
    [
        (f"{NPM_READ_URL}/in/ar_xyzabcde/demo.tgz", False),
        (f"{NPM_READ_URL}.attacker.example/secret-path", True),
        (f"{NPM_READ_URL}@attacker.example/secret-path", True),
        (f"{NPM_READ_URL.replace('https://', 'http://')}/staging/repo/demo.tgz", True),
        (f"{NPM_READ_URL}:444/staging/repo/demo.tgz", True),
        (f"{NPM_READ_URL}/in/ar_mnpqrstu/demo.tgz", True),
    ],
)
def test_native_source_warning_uses_exact_target_origin(monkeypatch, tmp_path, source, warns):
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    lock = tmp_path / "package-lock.json"
    content = json.dumps({"packages": {"node_modules/demo": {"resolved": source}}})
    lock.write_text(content)
    calls = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(app, ["npm", "--rvs-target", "staging/repo", "ci"])

    assert result.exit_code == 0, result.output
    assert ("Additional native sources" in result.output) is warns
    assert "secret-path" not in result.output
    assert lock.read_text() == content
    assert len(calls) == 1


def test_uv_lock_keeps_named_sources_and_third_party_auth(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    project = """[project]
name = "demo"
version = "1.0"
[[tool.uv.index]]
name = "vendor"
url = "https://vendor.test/simple"
explicit = true
[tool.uv.sources]
demo = {index = "vendor"}
"""
    (tmp_path / "pyproject.toml").write_text(project)
    monkeypatch.setenv("UV_INDEX_VENDOR_PASSWORD", "vendor-secret")
    calls = []
    _capture_run(monkeypatch, calls)
    result = runner.invoke(app, ["uv", "--rvs-target", "staging/repo", "lock"])
    assert result.exit_code == 0, result.output
    assert "Additional native sources" in result.output
    assert calls[0]["cmd"] == ["/bin/uv", "lock"]
    assert calls[0]["env"]["UV_INDEX_VENDOR_PASSWORD"] == "vendor-secret"
    assert (tmp_path / "pyproject.toml").read_text() == project
    assert not (tmp_path / "uv.lock").exists()  # mocked native tool creates nothing


def test_uv_read_only_mirror_does_not_request_upload_url(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    monkeypatch.setattr(
        native_runner,
        "_resolve_route",
        lambda *a, **kw: native_runner.RegistryRoute(
            "pypi", PYPI_MIRROR_URL, None, "o/pypiorg", "download-token"
        ),
    )
    calls = []
    _capture_run(monkeypatch, calls)
    result = runner.invoke(app, ["uv", "--rvs-target", "mirror:pypiorg", "lock"])
    assert result.exit_code == 0, result.output
    assert calls[0]["env"]["UV_DEFAULT_INDEX"] == f"{PYPI_MIRROR_URL}/o/pypiorg/simple/"
    assert "UV_PUBLISH_PASSWORD" not in calls[0]["env"]


def test_pip_preserves_extra_index_and_existing_netrc(monkeypatch, tmp_path):
    import netrc

    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    original = tmp_path / "original-netrc"
    content = 'machine vendor.test login vendor password "vendor password"\n'
    original.write_text(content)
    monkeypatch.setenv("NETRC", str(original))
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://vendor.test/simple/")
    captured = []

    def hook(cmd, env):
        parsed = netrc.netrc(env["NETRC"])
        assert parsed.authenticators("vendor.test") == ("vendor", "", "vendor password")
        assert parsed.authenticators(PYPI_READ_HOST) == (
            "__token__",
            "",
            "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )
        assert parsed.authenticators("unrelated.test") is None
        captured.append(env["NETRC"])

    calls = []
    _capture_run(monkeypatch, calls, hook)
    result = runner.invoke(app, ["pip", "--rvs-target", "staging/repo", "install", "demo"])
    assert result.exit_code == 0, result.output
    assert "pip combines candidates" in result.output
    assert calls[0]["env"]["PIP_EXTRA_INDEX_URL"] == "https://vendor.test/simple/"
    assert original.read_text() == content
    assert not Path(captured[0]).exists()


def test_npm_preserves_foreign_scope_and_injects_only_selected_auth(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    content = (
        "@vendor:registry=https://vendor.test/npm/\n//vendor.test/npm/:_authToken=vendor-secret\n"
    )
    (tmp_path / ".npmrc").write_text(content)
    calls = []
    _capture_run(monkeypatch, calls)
    result = runner.invoke(app, ["npm", "--rvs-target", "staging/repo", "install", "@vendor/demo"])
    assert result.exit_code == 0, result.output
    assert "Additional native sources" in result.output
    assert (tmp_path / ".npmrc").read_text() == content
    injected = {
        k: v
        for k, v in calls[0]["env"].items()
        if v == "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    }
    assert injected == {
        f"NPM_CONFIG_//{NPM_READ_HOST}/in/ar_xyzabcde/:_authToken": "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    }


def test_maven_preserves_other_repositories_and_mirror_credentials(monkeypatch, tmp_path):
    import xml.etree.ElementTree as ET

    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    original = tmp_path / "settings.xml"
    content = """<settings><mirrors><mirror><id>vendor</id><url>https://vendor.test/maven/</url>
<mirrorOf>*</mirrorOf></mirror></mirrors><servers><server><id>vendor</id>
<username>vendor</username><password>vendor-secret</password></server></servers></settings>"""
    original.write_text(content)
    calls = []

    def hook(cmd, env):
        root = ET.parse(cmd[cmd.index("--settings") + 1]).getroot()
        mirrors = {el.findtext("id"): el for el in root.findall("./mirrors/mirror")}
        assert mirrors["vendor"].findtext("url") == "https://vendor.test/maven/"
        assert mirrors["vendor"].findtext("mirrorOf") == f"*,!central,!{MAVEN_ID}"
        assert mirrors[MAVEN_ID].findtext("mirrorOf") == "central"
        passwords = {
            el.findtext("id"): el.findtext("password") for el in root.findall("./servers/server")
        }
        assert passwords == {
            "vendor": "vendor-secret",
            MAVEN_ID: "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        }

    _capture_run(monkeypatch, calls, hook)
    result = runner.invoke(
        app, ["mvn", "--rvs-target", "staging/repo", "-s", str(original), "verify"]
    )
    assert result.exit_code == 0, result.output
    assert "Existing Maven mirrors" in result.output
    assert original.read_text() == content


def test_maven_repository_id_is_random_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    first = native_runner.RegistryRoute("maven", MAVEN_MIRROR_URL, None, "o/maven-central", "t")
    second = native_runner.RegistryRoute("maven", MAVEN_MIRROR_URL, None, "o/maven-central", "t")

    assert first.maven_server_id != second.maven_server_id
    assert first.maven_server_id.startswith("rvs-")
    assert len(first.maven_server_id) == len("rvs-") + 24
    assert "rvs-private" not in {first.maven_server_id, second.maven_server_id}


SLT = "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NPM_READ_REGISTRY = f"{NPM_READ_URL}/in/ar_xyzabcde/"
NPM_PUSH_REGISTRY = f"{NPM_PUSH_URL}/in/ar_xyzabcde/"
NPM_READ_AUTH_KEY = f"//{NPM_READ_HOST}/in/ar_xyzabcde/:_authToken"
NPM_PUSH_AUTH_KEY = f"//{NPM_PUSH_HOST}/in/ar_xyzabcde/:_authToken"


def _pnpm_home(monkeypatch: Any, tmp_path: Path) -> Path:
    """Give pnpm runs a private home so the developer's ~/.npmrc is never read."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in ("NPM_CONFIG_USERCONFIG", "npm_config_userconfig"):
        monkeypatch.delenv(name, raising=False)
    return home


def _run_pnpm(
    monkeypatch: Any, tmp_path: Path, argv: list[str], **invoke: Any
) -> tuple[Any, list[dict[str, Any]], list[str]]:
    """Run `rvs pnpm` and capture the child command, environment, and user config."""
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    user_configs: list[str] = []

    def hook(_cmd: list[str], env: dict[str, str]) -> None:
        path = Path(env["NPM_CONFIG_USERCONFIG"])
        if os.name != "nt":  # Windows has no POSIX permission bits.
            assert oct(path.stat().st_mode & 0o777) == "0o600"
        user_configs.append(path.read_text(encoding="utf-8"))

    _capture_run(monkeypatch, calls, hook)
    result = runner.invoke(app, ["pnpm", "--rvs-target", "staging/repo-npm", *argv], **invoke)
    return result, calls, user_configs


@pytest.mark.parametrize(
    ("argv", "operations"),
    [
        (["install"], ("read",)),
        (["add", "publish"], ("read",)),
        (["exec", "npm", "publish"], ("read",)),
        (["dlx", "create-vite"], ("read",)),
        (["publish"], ("publish",)),
        (["publish", "--no-git-checks"], ("publish",)),
        (["-r", "publish"], ("publish",)),
        (["--recursive", "publish", "--tag", "next"], ("publish",)),
        # `--filter` takes a value, so it is not the command.
        (["--filter", "publish", "install"], ("read",)),
        (["-F", "app", "publish"], ("publish",)),
        # pnpm's `-w` (workspace root) takes no value, unlike npm's `-w WORKSPACE`.
        (["-w", "publish"], ("publish",)),
        (["unpublish", "demo@1.0.0"], ("read", "publish", "delete")),
        (["deprecate", "demo@1", "old"], ("read", "publish")),
        (["undeprecate", "demo@1"], ("read", "publish")),
        (["dist-tag", "add", "demo@1.0.0", "beta"], ("read", "publish")),
        (["dist-tag", "rm", "demo", "beta"], ("read", "publish")),
        (["dist-tag", "ls", "demo"], ("read",)),
        (["dist-tags", "ls", "demo"], ("read",)),
    ],
)
def test_pnpm_operations_come_from_the_command(argv, operations) -> None:
    assert native_runner._operations_for("pnpm", argv) == operations
    assert native_runner._is_publishing("pnpm", argv) is (operations == ("publish",))


def test_pnpm_install_sets_both_variable_families_and_a_user_config(
    monkeypatch: Any, tmp_path: Path
) -> None:
    home = _pnpm_home(monkeypatch, tmp_path)
    (home / ".npmrc").write_text(
        "@vendor:registry=https://vendor.test/npm/\n"
        f"{NPM_READ_AUTH_KEY}=stale-token\n"
        "//vendor.test/npm/:_authToken=${VENDOR_TOKEN}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("npm_config_registry", "https://registry.npmjs.org/")
    monkeypatch.setenv("pnpm_config_registry", "https://registry.npmjs.org/")

    result, calls, user_configs = _run_pnpm(
        monkeypatch,
        tmp_path,
        ["install", "--registry", "https://registry.npmjs.org/", "--frozen-lockfile"],
    )

    assert result.exit_code == 0, result.output
    # The registry comes from the environment: an inserted `--registry` could
    # land among the arguments `pnpm run` passes to a script.
    assert calls[0]["cmd"] == ["/bin/pnpm", "install", "--frozen-lockfile"]
    env = calls[0]["env"]
    assert env["NPM_CONFIG_REGISTRY"] == NPM_READ_REGISTRY
    assert env["PNPM_CONFIG_REGISTRY"] == NPM_READ_REGISTRY
    assert env[f"NPM_CONFIG_{NPM_READ_AUTH_KEY}"] == SLT
    assert env[f"PNPM_CONFIG_{NPM_READ_AUTH_KEY}"] == SLT
    assert "npm_config_registry" not in env
    assert "pnpm_config_registry" not in env
    # pnpm 9, and pnpm 10 for a registry with a port, lose the variable form of
    # the credential, so the user config carries it too and keeps everything else.
    assert user_configs[0] == (
        "@vendor:registry=https://vendor.test/npm/\n"
        "//vendor.test/npm/:_authToken=${VENDOR_TOKEN}\n"
        f"{NPM_READ_AUTH_KEY}={SLT}\n"
    )
    assert "stale-token" not in user_configs[0]
    assert (home / ".npmrc").read_text(encoding="utf-8").count("stale-token") == 1
    assert not Path(env["NPM_CONFIG_USERCONFIG"]).exists()
    assert "--registry" not in calls[0]["cmd"]


def test_pnpm_keeps_a_user_global_config_unless_isolated(monkeypatch: Any, tmp_path: Path) -> None:
    _pnpm_home(monkeypatch, tmp_path)
    monkeypatch.setenv("npm_config_globalconfig", str(tmp_path / "global.npmrc"))

    result, calls, _ = _run_pnpm(monkeypatch, tmp_path, ["install"])

    assert result.exit_code == 0, result.output
    # Windows keeps environment names case-insensitively, in upper case.
    global_configs = [
        value for key, value in calls[0]["env"].items() if key.lower() == "npm_config_globalconfig"
    ]
    assert global_configs == [str(tmp_path / "global.npmrc")]


def test_pnpm_isolate_uses_only_the_injected_credential(monkeypatch: Any, tmp_path: Path) -> None:
    home = _pnpm_home(monkeypatch, tmp_path)
    (home / ".npmrc").write_text("registry=https://registry.npmjs.org/\n", encoding="utf-8")
    monkeypatch.setenv("npm_config_globalconfig", str(tmp_path / "global.npmrc"))
    global_texts: list[str] = []

    result, calls, user_configs = _run_pnpm(
        monkeypatch, tmp_path, ["--rvs-native-config", "isolate", "install"]
    )
    global_texts.append(Path(calls[0]["env"]["NPM_CONFIG_GLOBALCONFIG"]).name)

    assert result.exit_code == 0, result.output
    assert user_configs == [f"{NPM_READ_AUTH_KEY}={SLT}\n"]
    assert "npm_config_globalconfig" not in calls[0]["env"]
    assert global_texts == ["global.npmrc"]
    assert calls[0]["env"]["NPM_CONFIG_GLOBALCONFIG"] != str(tmp_path / "global.npmrc")


def test_pnpm_publish_uses_the_upload_registry_and_previews_the_package(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _pnpm_home(monkeypatch, tmp_path)
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "@acme/widgets", "version": "1.2.3"}), encoding="utf-8"
    )

    result, calls, user_configs = _run_pnpm(
        monkeypatch, tmp_path, ["publish", "--no-git-checks", "--tag", "next"], input="y\n"
    )

    assert result.exit_code == 0, result.output
    assert "Publish to repo:staging/repo (user:staging)" in result.output
    assert "npm @acme/widgets@1.2.3" in result.output
    assert "npm tag: next" in result.output
    assert calls[0]["cmd"] == ["/bin/pnpm", "publish", "--no-git-checks", "--tag", "next"]
    env = calls[0]["env"]
    assert env["NPM_CONFIG_REGISTRY"] == env["PNPM_CONFIG_REGISTRY"] == NPM_PUSH_REGISTRY
    assert env[f"PNPM_CONFIG_{NPM_PUSH_AUTH_KEY}"] == SLT
    assert user_configs == [f"{NPM_PUSH_AUTH_KEY}={SLT}\n"]


@pytest.mark.parametrize(
    "argv",
    [
        ["-r", "publish"],
        ["publish", "--recursive"],
        ["--filter", "@acme/*", "publish"],
        ["-C", "packages/app", "publish"],
    ],
)
def test_pnpm_workspace_publish_leaves_selection_to_pnpm(
    monkeypatch: Any, tmp_path: Path, argv
) -> None:
    _pnpm_home(monkeypatch, tmp_path)

    result, calls, _ = _run_pnpm(monkeypatch, tmp_path, ["--rvs-yes", *argv])

    assert result.exit_code == 0, result.output
    assert calls[0]["env"]["PNPM_CONFIG_REGISTRY"] == NPM_PUSH_REGISTRY
    preview = ADAPTERS["pnpm"].publish_items(argv)
    assert [item.identity for item in preview] == ["pnpm workspace/project publish"]
    assert "pnpm skips versions the registry already has." in preview[0].details[0]


def test_pnpm_publish_decline_does_not_launch(monkeypatch: Any, tmp_path: Path) -> None:
    _pnpm_home(monkeypatch, tmp_path)

    result, calls, _ = _run_pnpm(monkeypatch, tmp_path, ["publish"], input="\n")

    assert result.exit_code != 0
    assert calls == []


@pytest.mark.parametrize(
    ("argv", "prompt"),
    [
        (["dist-tag", "add", "demo@1.0.0"], "Point pnpm dist-tag 'latest' at demo@1.0.0?"),
        # pnpm 11 and later ignore `--tag` here and move `latest`.
        (["dist-tag", "add", "demo@1.0.0", "--tag", "beta"], "Point pnpm dist-tag 'latest'"),
        (["dist-tag", "add", "demo", "latest"], "at demo (no version given)?"),
        (["dist-tag", "rm", "demo", "beta"], "Remove pnpm dist-tag 'beta' from demo?"),
    ],
)
def test_pnpm_dist_tag_changes_to_latest_and_removals_are_confirmed(
    monkeypatch: Any, tmp_path: Path, argv, prompt
) -> None:
    _pnpm_home(monkeypatch, tmp_path)
    monkeypatch.setenv("NPM_CONFIG_TAG", "beta")

    declined, calls, _ = _run_pnpm(monkeypatch, tmp_path, argv, input="n\n")

    assert declined.exit_code != 0
    assert prompt in " ".join(declined.output.split())
    assert calls == []


def test_pnpm_dist_tag_addition_of_another_tag_is_not_confirmed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _pnpm_home(monkeypatch, tmp_path)

    result, calls, _ = _run_pnpm(monkeypatch, tmp_path, ["dist-tag", "add", "demo@1.0.0", "beta"])

    assert result.exit_code == 0, result.output
    assert "Target:" not in result.output
    assert calls[0]["env"]["PNPM_CONFIG_REGISTRY"] == NPM_PUSH_REGISTRY


def test_pnpm_unpublish_explains_trash_and_refusals(monkeypatch: Any, tmp_path: Path) -> None:
    _pnpm_home(monkeypatch, tmp_path)
    _isolate_config(monkeypatch, tmp_path)
    _mock_native_tools(monkeypatch)

    class _Refused:
        returncode = 1

    monkeypatch.setattr(native_runner.subprocess, "run", lambda cmd, *, env, check: _Refused())

    result = runner.invoke(
        app, ["pnpm", "--rvs-target", "staging/repo-npm", "unpublish", "demo", "--force"]
    )

    assert result.exit_code == 1
    stderr = " ".join(result.stderr.split())
    assert "moves the version to the repository trash" in stderr
    assert "refused the unpublish with status 405" in stderr
    assert "rvs art package delete-version NAME VERSION" in stderr


@pytest.mark.parametrize("argv", [["--version"], ["-h"], ["help"]])
def test_pnpm_help_and_version_need_no_target(monkeypatch: Any, tmp_path: Path, argv) -> None:
    _mock_native_tools(monkeypatch)
    calls: list[dict[str, Any]] = []
    _capture_run(monkeypatch, calls)

    result = runner.invoke(app, ["pnpm", *argv])

    assert result.exit_code == 0, result.output
    assert calls[0]["cmd"] == ["/bin/pnpm", *argv]
    assert "PNPM_CONFIG_REGISTRY" not in calls[0]["env"]


@pytest.mark.parametrize(
    ("pin", "running", "warns"),
    [
        ("pnpm@9.15.9", "10.34.7", True),
        ("pnpm@10.34.7+sha512.abc", "10.34.7", False),
        ("yarn@4.5.0", "10.34.7", False),
        (None, "10.34.7", False),
    ],
)
def test_pnpm_warns_when_the_project_pins_another_pnpm(
    monkeypatch: Any, tmp_path: Path, pin, running, warns
) -> None:
    from rvs import tool_advisories

    _pnpm_home(monkeypatch, tmp_path)
    tool_advisories._version_output.cache_clear()
    tool_advisories._advisory_message.cache_clear()
    monkeypatch.setattr(tool_advisories, "_read_version", lambda command: f"{running}\n")
    # A workspace package inherits the pin from the workspace root manifest.
    root_manifest: dict[str, Any] = {"name": "root", "private": True}
    if pin:
        root_manifest["packageManager"] = pin
    (tmp_path / "package.json").write_text(json.dumps(root_manifest), encoding="utf-8")
    package = tmp_path / "packages" / "app"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"name": "app"}', encoding="utf-8")

    result, calls, _ = _run_pnpm(monkeypatch, tmp_path, ["-C", str(package), "install"])

    assert result.exit_code == 0, result.output
    message = " ".join(result.output.split())
    assert ("but pnpm 10.34.7 is running" in message) is warns
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("name", "content", "warns"),
    [
        ("pnpm-workspace.yaml", "registry: https://vendor.test/npm/\n", True),
        (
            "pnpm-workspace.yaml",
            "registries:\n  default: https://registry.npmjs.org/\n  '@acme': https://vendor.test/\n",
            True,
        ),
        ("pnpm-workspace.yaml", "packages:\n  - 'packages/*'\nminimumReleaseAge: 0\n", False),
        (
            "pnpm-lock.yaml",
            "lockfileVersion: '9.0'\npackages:\n  demo@1.0.0:\n"
            "    resolution: {integrity: sha512-x, tarball: https://vendor.test/demo-1.0.0.tgz}\n",
            True,
        ),
        (
            "pnpm-lock.yaml",
            "lockfileVersion: '9.0'\npackages:\n  demo@1.0.0:\n"
            f"    resolution: {{tarball: {NPM_READ_REGISTRY}demo/-/demo-1.0.0.tgz}}\n",
            False,
        ),
        ("pnpm-lock.yaml", "lockfileVersion: '9.0'\npackages: {}\n", False),
    ],
)
def test_pnpm_source_warning_reads_pnpm_settings_and_lockfile(
    monkeypatch: Any, tmp_path: Path, name, content, warns
) -> None:
    _pnpm_home(monkeypatch, tmp_path)
    (tmp_path / name).write_text(content, encoding="utf-8")

    result, calls, _ = _run_pnpm(monkeypatch, tmp_path, ["install"])

    assert result.exit_code == 0, result.output
    assert ("Additional native sources" in result.output) is warns
    assert (tmp_path / name).read_text(encoding="utf-8") == content
    assert len(calls) == 1
