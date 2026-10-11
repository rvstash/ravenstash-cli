import json
import re
from typing import TYPE_CHECKING, Any

import pytest
from rvs import config as cfg_mod
from rvs import output
from rvs.artifacts import commands as artifacts_cmd
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()
STAGING_API_URL = "https://control.example.test"
PYPI_READ_URL = "https://pypi.packages.example.test"
PYPI_READ_HOST = "pypi.packages.example.test"
PYPI_PUSH_URL = "https://push.pypi.packages.example.test"
NPM_READ_URL = "https://npm.packages.example.test"
NPM_READ_HOST = "npm.packages.example.test"
NPM_PUSH_URL = "https://push.npm.packages.example.test"
MAVEN_READ_URL = "https://maven.packages.example.test"
MAVEN_PUSH_URL = "https://push.maven.packages.example.test"


class _JsonResponse:
    def __init__(
        self, payload: Any, *, headers: dict[str, str] | None = None, status_code: int = 200
    ) -> None:
        self._payload = payload
        self.headers = headers or {}
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class _FakeApiClient:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = responses or []
        self.calls: list[tuple[str, str, Any]] = []
        self.request_headers: list[tuple[str, str, dict[str, str] | None]] = []

    def _next(self, default: Any = None) -> _JsonResponse:
        payload = self.responses.pop(0) if self.responses else default
        if isinstance(payload, _JsonResponse):
            return payload
        if isinstance(payload, list):
            payload = {"items": payload, "next_cursor": None}
        return _JsonResponse(payload)

    def get(self, path: str, params: dict[str, Any] | None = None) -> _JsonResponse:
        self.calls.append(("GET", path, params))
        if path == "/v0/artifacts/repositories/resolve" and not self.responses:
            return _JsonResponse(_repository_entry())
        return self._next()

    def issue_native(self, path: str, payload: dict):
        assert payload["duration_seconds"] == 14400
        return self.post(path, json=payload)

    def post(self, path: str, json: Any = None, **kwargs: Any) -> _JsonResponse:
        payload = {"json": json, **kwargs} if json is not None and kwargs else json or kwargs
        self.calls.append(("POST", path, payload))
        if path == "/v0/artifacts/repositories/ar_xyzabcde/mint-token":
            assert isinstance(json, dict)
            return _JsonResponse(
                {
                    "access_token": "rvs_sltAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "native_path": "/in/ar_xyzabcde",
                }
            )
        return _JsonResponse(self.responses.pop(0) if self.responses else {})

    def patch(
        self,
        path: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _JsonResponse:
        payload = {"json": json, "params": params} if params is not None else json
        self.calls.append(("PATCH", path, payload))
        self.request_headers.append(("PATCH", path, headers))
        return self._next({})

    def put(
        self, path: str, json: Any = None, params: dict[str, Any] | None = None
    ) -> _JsonResponse:
        self.calls.append(("PUT", path, {"json": json, "params": params} if params else json))
        return self._next({})

    def delete(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _JsonResponse:
        self.calls.append(("DELETE", path, params))
        self.request_headers.append(("DELETE", path, headers))
        return self._next(None)


_ACCOUNT_SUMMARY = {"ref": "ac_23456789", "handle": "test-account", "type": "organization"}
_ACCOUNT = {
    **_ACCOUNT_SUMMARY,
    "display_name": "Test account",
    "is_admin": True,
    "organization_role": "owner",
    "authority_revision": 3,
}
_NAMESPACE = {
    "ref": "in_abcdefgh",
    "name": "test-account",
    "realm": "internal",
    "is_default": True,
    "account": dict(_ACCOUNT_SUMMARY),
    "created_at": "2026-09-01T00:00:00Z",
    "updated_at": "2026-09-01T00:00:00Z",
}


def _repository_entry(name: str = "repo", **overrides: Any) -> dict[str, Any]:
    return {
        "ref": "ar_xyzabcde",
        "name": name,
        "account": dict(_ACCOUNT_SUMMARY),
        "namespace": {"ref": "in_abcdefgh", "name": "test-account", "realm": "internal"},
        "formats": [{"format": kind} for kind in ("pypi", "npm", "maven")],
        "allowed_actions": ["content.read", "content.publish", "repository.write"],
        "totals": {
            "package_count": 0,
            "version_count": 0,
            "oci_path_count": 0,
            "manifest_count": 0,
            "storage_bytes": 0,
        },
        "latest_uploaded_at": None,
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
        **overrides,
    }


def _isolate_config(monkeypatch, tmp_path: Path, content: str | None = None) -> None:
    output.set_json(False)
    config_dir = tmp_path / ".rvs"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    config_file.write_text(
        content
        or """
schema_version = 1
default_profile = "default"

[profiles.default]
api_url = "https://api.ravenstash.com"
account_ref = "ac_23456789"

[profiles.staging]
account_ref = "ac_23456789"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RVS_ENV_FILE", raising=False)
    monkeypatch.setenv("RVS_PROFILE_STAGING_API_URL", STAGING_API_URL)
    monkeypatch.setenv("RVS_PROFILE_STAGING_REPOSITORY_DOMAIN", "packages.example.test")
    _use_fake_client(monkeypatch, _FakeApiClient())


def _use_fake_client(monkeypatch, fake: _FakeApiClient) -> None:
    monkeypatch.setattr(
        artifacts_cmd.ApiClient, "from_profile", staticmethod(lambda profile=None: fake)
    )


def test_artifacts_repo_list_filters_by_kind_and_uses_profile_account(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            [
                _repository_entry(
                    "repo-pypi",
                    formats=[
                        {"format": "pypi"},
                        {"format": "npm"},
                    ],
                ),
            ]
        ]
    )
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, ["repo", "list", "--format", "pypi"])

    assert result.exit_code == 0
    assert "pypi, npm" in result.output
    assert "Maintainer" in result.output
    assert fake.calls == [
        (
            "GET",
            "/v0/artifacts/repositories",
            {"account_ref": "ac_23456789", "format": "pypi", "limit": 100},
        )
    ]
    assert "repo-pypi" in result.output
    assert "Account" in result.output
    assert "test-account" in result.output
    assert "Test account" not in result.output
    assert "test-account/repo-pypi" in result.output
    assert "ID-based target" in result.output
    assert "in/ar_xyzabcde" in result.output
    assert "Namespace" not in result.output
    assert "Repository ID" not in result.output


def test_artifacts_repo_list_omits_unset_query_filters(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([[], [_ACCOUNT]])
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, ["repo", "list"])

    assert result.exit_code == 0
    assert fake.calls == [
        ("GET", "/v0/artifacts/repositories", {"account_ref": "ac_23456789", "limit": 100}),
        ("GET", "/v0/platform/accounts", {"limit": 100}),
    ]
    assert "Account" in result.output
    assert "test-account" in result.output
    assert "No repositories found" in result.output


@pytest.mark.parametrize(
    ("allowed_actions", "label"),
    [
        (["content.read", "repository.read", "upstream.read"], "Reader"),
        (["content.read", "content.publish"], "Publisher"),
        (["content.read", "content.publish", "content.delete", "upstream.write"], "Maintainer"),
        (["content.read", "repository.write", "repository.security.write"], "Maintainer"),
        (["content.read", "repository.settings.write"], "Admin"),
        (["content.read", "content.delete", "repository.delete"], "Admin"),
        ([], "Unknown"),
    ],
)
def test_repository_access_label_names_the_four_levels(
    allowed_actions: list[str], label: str
) -> None:
    repository = _repository_entry(allowed_actions=allowed_actions)
    assert artifacts_cmd._repository_access_label(repository) == label


def test_artifacts_repo_list_preserves_json_shape_and_uses_typed_account_handle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([[_repository_entry("repo-pypi", allowed_actions=[])]])
    _use_fake_client(monkeypatch, fake)

    output.set_json(True)
    try:
        result = runner.invoke(artifacts_cmd.app, ["repo", "list"])
    finally:
        output.set_json(False)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["items"] == [
        {
            "account": "org:test-account",
            "namespace": "test-account",
            "repository": "repo-pypi",
            "id_based_target": "in/ar_xyzabcde",
            "formats": "pypi, npm, maven",
            "access": "Unknown",
        }
    ]


@pytest.mark.parametrize(
    "flags",
    [
        ["-f", "pypi,npm,maven,oci"],
        ["-f", "pypi, npm", "--format", "maven", "-f", "oci,pypi"],
    ],
)
def test_repo_create_accepts_comma_separated_and_repeated_formats(monkeypatch, tmp_path, flags):
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([[_NAMESPACE], _repository_entry("packages")])
    _use_fake_client(monkeypatch, fake)
    result = runner.invoke(artifacts_cmd.app, ["repo", "create", "packages", *flags])
    assert result.exit_code == 0, result.output
    assert fake.calls[-1][2]["formats"] == ["pypi", "npm", "maven", "oci"]


@pytest.mark.parametrize("value", ["pypi,", ",npm", "pypi,unknown", " "])
def test_repo_create_rejects_invalid_format_set_before_api(monkeypatch, value):
    monkeypatch.setattr(
        artifacts_cmd, "_client", lambda *_: pytest.fail("Invalid formats reached API")
    )
    result = runner.invoke(artifacts_cmd.app, ["repo", "create", "packages", "-f", value])
    assert result.exit_code != 0
    assert "Unknown or empty format" in result.output


@pytest.mark.parametrize("selector", ["Engineering", "in_abcdefgh"])
@pytest.mark.parametrize("repo_name", ["new-node", "New-Node"])
def test_create_resolves_namespace_inside_selected_account(
    monkeypatch, tmp_path, selector, repo_name
):
    _isolate_config(monkeypatch, tmp_path)
    namespace = {**_NAMESPACE, "name": "engineering", "is_default": False}
    fake = _FakeApiClient(
        [
            [
                {
                    **namespace,
                    "ref": "in_foreign1",
                    "account": {"ref": "ac_foreign1", "handle": "x", "type": "personal"},
                },
                namespace,
            ],
            _repository_entry(repo_name),
        ]
    )
    _use_fake_client(monkeypatch, fake)
    result = runner.invoke(
        artifacts_cmd.app, ["repo", "create", f"{selector}/{repo_name}", "-f", "npm"]
    )
    assert result.exit_code == 0, result.output
    assert fake.calls == [
        ("GET", "/v0/platform/namespaces", {"account_ref": "ac_23456789", "limit": 100}),
        (
            "POST",
            "/v0/artifacts/repositories",
            {
                "namespace_ref": "in_abcdefgh",
                "name": repo_name,
                "formats": ["npm"],
            },
        ),
    ]
    assert f"Created repository '{repo_name}'" in result.output


def test_create_with_deferred_personal_namespace_requests_onboarding(monkeypatch, tmp_path):
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([[]])
    _use_fake_client(monkeypatch, fake)
    result = runner.invoke(artifacts_cmd.app, ["repo", "create", "new-node", "-f", "npm"])
    assert result.exit_code != 0
    assert "finish onboarding" in result.output
    assert fake.calls == [
        ("GET", "/v0/platform/namespaces", {"account_ref": "ac_23456789", "limit": 100})
    ]


@pytest.mark.parametrize("flags", [["--public"], ["--scope", "public"]])
def test_public_scope_never_runs_private_mutation(monkeypatch, flags):
    def unexpected_client(*_args, **_kwargs):
        raise AssertionError("Unavailable public scope must not reach the API")

    monkeypatch.setattr(artifacts_cmd.ApiClient, "from_profile", unexpected_client)
    result = runner.invoke(artifacts_cmd.app, [*flags, "repo", "create", "demo", "-f", "npm"])
    assert result.exit_code != 0
    assert "No such option" in result.output


def test_artifacts_repo_create_rejects_unknown_kind(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)

    result = runner.invoke(artifacts_cmd.app, ["repo", "create", "bad", "--format", "gem"])

    assert result.exit_code == 1
    assert "Unknown or empty format 'gem'" in result.stderr


def test_artifacts_repo_show_renders_repository_details(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _repository_entry(
                "repo-pypi",
                totals={
                    "package_count": 7,
                    "version_count": 13,
                    "oci_path_count": 2,
                    "manifest_count": 5,
                    "storage_bytes": 4096,
                },
                created_at="2026-06-20T00:00:00Z",
            )
        ]
    )
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, ["repo", "show", "repo-pypi"])

    assert result.exit_code == 0
    assert fake.calls == [
        (
            "GET",
            "/v0/artifacts/repositories/resolve",
            {"selector": "repo-pypi", "account_ref": "ac_23456789"},
        )
    ]
    assert "repo-pypi" in result.output
    assert "test-account/repo-pypi" in result.output
    assert "Test account" not in result.output
    assert "ID-based target" in result.output
    assert "in/ar_xyzabcde" in result.output
    assert "Repository ID" not in result.output
    assert "Packages" in result.output
    assert "7" in result.output
    assert "Versions" in result.output
    assert "13" in result.output
    assert "OCI paths" in result.output
    assert "2" in result.output
    assert "Manifests" in result.output
    assert "5" in result.output
    assert "4096" in result.output


def _remote_cache(**overrides: Any) -> dict[str, Any]:
    return {
        "ref": "rc_abcdefgh",
        "account": dict(_ACCOUNT_SUMMARY),
        "format": "pypi",
        "source_type": "official",
        "name": "PyPI",
        "official_source_ref": "pypiorg",
        "min_age_hours": None,
        "max_age_hours": None,
        "consumer_repository_count": 0,
        "package_count": 0,
        "storage_bytes": 0,
        "last_accessed_at": None,
        "created_at": "2026-09-01T00:00:00Z",
        **overrides,
    }


def _upstream(position: int, **source: Any) -> dict[str, Any]:
    return {
        "position": position,
        "source": {
            "kind": "remote_cache",
            "ref": "rc_abcdefgh",
            "display_name": "pypiorg",
            "namespace_ref": None,
            "namespace_name": None,
            "source_type": "official",
            **source,
        },
        "publication_control": "externally_controlled",
        "resolution_tier": "externally_controlled",
        "min_age_hours": 5.0,
        "max_age_hours": None,
    }


def test_artifacts_mirror_create_uses_the_default_official_source(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [[{"ref": "pypiorg", "format": "pypi", "display_name": "PyPI"}], _remote_cache()]
    )
    _use_fake_client(monkeypatch, fake)

    create_result = runner.invoke(artifacts_cmd.app, ["mirror", "create", "--format", "pypi"])

    assert create_result.exit_code == 0, create_result.output
    assert "mirror:pypiorg" in create_result.output
    assert fake.calls == [
        (
            "GET",
            "/v0/artifacts/official-sources",
            {"account_ref": "ac_23456789", "limit": 100},
        ),
        (
            "POST",
            "/v0/artifacts/remote-caches",
            {"account_ref": "ac_23456789", "source_ref": "pypiorg"},
        ),
    ]


def test_artifacts_official_remote_add_uses_public_source_ref(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            {
                "items": [
                    {
                        "ref": "pypiorg",
                        "display_name": "Python Package Index",
                        "format": "pypi",
                        "recommended_min_age_hours": None,
                        "recommended_max_age_hours": None,
                    }
                ],
                "next_cursor": None,
            },
            _remote_cache(ref="rc_23456789"),
        ]
    )
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app, ["mirror", "create", "pypiorg", "--min-age-hours", "24"]
    )

    assert result.exit_code == 0
    assert fake.calls == [
        (
            "GET",
            "/v0/artifacts/official-sources",
            {"account_ref": "ac_23456789", "limit": 100},
        ),
        (
            "POST",
            "/v0/artifacts/remote-caches",
            {"account_ref": "ac_23456789", "source_ref": "pypiorg", "min_age_hours": 24.0},
        ),
    ]


def test_artifacts_official_remote_list_reports_external_publication_control(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([[_remote_cache(ref="rc_23456789", official_source_ref="pypi")]])
    _use_fake_client(monkeypatch, fake)
    tables: list[tuple[list[str], list[list[str]]]] = []
    monkeypatch.setattr(
        artifacts_cmd.output,
        "table",
        lambda headers, rows, **_kwargs: tables.append((headers, rows)),
    )

    result = runner.invoke(artifacts_cmd.app, ["mirror", "list", "--format", "pypi"])

    assert result.exit_code == 0
    assert fake.calls == [
        (
            "GET",
            "/v0/artifacts/remote-caches",
            {"account_ref": "ac_23456789", "format": "pypi", "limit": 100},
        )
    ]
    assert tables[0][1][0][0] == "org:test-account"
    assert tables[0][0][1] == "Remote-cache ref"
    assert tables[0][1][0][1] == "rc_23456789"
    assert tables[0][0][3] == "Publication"
    assert tables[0][1][0][3] == "externally_controlled"
    assert tables[0][1][0][4] == "mirror:pypi"
    assert tables[0][1][0][-1] == "No minimum"


def test_artifacts_mirror_show_formats_absent_age_bounds(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([_remote_cache()])
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, ["mirror", "show", "rc_abcdefgh"])

    assert result.exit_code == 0, result.output
    assert fake.calls == [("GET", "/v0/artifacts/remote-caches/rc_abcdefgh", None)]
    assert "No minimum" in result.output
    assert "No maximum" in result.output
    assert "None hours" not in result.output
    assert "mirror:pypiorg" in result.output
    assert "ac_23456789" in result.output


def _private_source() -> dict[str, Any]:
    return _repository_entry(
        "shared",
        ref="ar_shared23",
        namespace={"ref": "in_libs2345", "name": "libraries", "realm": "internal"},
        formats=[{"format": "pypi"}],
    )


def test_artifacts_repo_upstream_list_is_addressed_by_position(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _repository_entry("application"),
            [
                _upstream(3),
                _upstream(
                    1,
                    kind="repository",
                    ref="ar_shared23",
                    display_name="shared",
                    namespace_name="libraries",
                ),
            ],
        ]
    )
    _use_fake_client(monkeypatch, fake)

    output.set_json(True)
    try:
        result = runner.invoke(
            artifacts_cmd.app, ["repo", "upstream", "list", "application", "pypi"]
        )
    finally:
        output.set_json(False)

    assert result.exit_code == 0, result.output
    assert fake.calls[-1] == (
        "GET",
        "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/upstreams",
        {"limit": 100},
    )
    rows = json.loads(result.output)["items"]
    assert [row["position"] for row in rows] == ["1", "3"]
    assert rows[0]["type"] == "repository"
    assert rows[0]["source"] == "libraries/shared"
    assert rows[0]["source_ref"] == "ar_shared23"
    assert rows[1]["source"] == "pypiorg"
    assert "id" not in rows[0]


@pytest.mark.parametrize(
    "argv",
    [
        ["repo", "delete", "platform/app", "--yes"],
        ["repo", "rename", "platform/app", "renamed"],
        ["repo", "upstream", "add", "platform/app", "pypi"],
        ["repo", "upstream", "update", "platform/app", "pypi", "1"],
        ["repo", "upstream", "remove", "platform/app", "pypi", "1"],
        ["repo", "upstream", "reorder", "platform/app", "npm", "1", "2"],
        ["mirror", "set-age", "rc_abcdefgh", "--min-age-hours", "1"],
        ["mirror", "delete", "rc_abcdefgh", "--yes"],
        ["package", "delete", "demo", "--yes"],
    ],
)
def test_danger_zone_operations_are_browser_only(
    monkeypatch, tmp_path: Path, argv: list[str]
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient()
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, argv)

    assert result.exit_code == 2
    assert fake.calls == []


def _package_summary(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "demo",
        "normalized_name": "demo",
        "status": "quarantined",
        "status_reason": None,
        "latest_version": "1.2.3",
        "latest_stable_version": "1.2.3",
        "version_count": 3,
        "total_size_bytes": 1234,
        "latest_uploaded_at": "2026-09-01T00:00:00Z",
        "tags": {},
        **overrides,
    }


def _version_summary(version: str, **overrides: Any) -> dict[str, Any]:
    return {
        "version": version,
        "published_at": "2026-09-01T00:00:00Z",
        "yanked": False,
        "deprecated": False,
        "file_count": 1,
        "size_bytes": 1000,
        "downloads": 4,
        **overrides,
    }


def test_artifacts_package_list_follows_every_page(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _repository_entry("repo-pypi"),
            {"items": [{"name": "alpha", "version_count": 1}], "next_cursor": "c1"},
            {"items": [{"name": "beta", "version_count": 2}], "next_cursor": "c2"},
            {"items": [{"name": "gamma", "version_count": 3}], "next_cursor": None},
        ]
    )
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app, ["package", "list", "--target", "repo-pypi", "--format", "pypi"]
    )

    assert result.exit_code == 0, result.output
    path = "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/packages"
    assert fake.calls[1:] == [
        ("GET", path, {"limit": 100}),
        ("GET", path, {"limit": 100, "cursor": "c1"}),
        ("GET", path, {"limit": 100, "cursor": "c2"}),
    ]
    for name in ("alpha", "beta", "gamma"):
        assert name in result.output


def test_artifacts_package_show_lists_the_newest_versions_and_says_when_more_exist(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _repository_entry("repo-pypi"),
            _package_summary(),
            {
                "items": [_version_summary("1.2.3"), _version_summary("1.2.2", yanked=True)],
                "next_cursor": "v2",
            },
        ]
    )
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app,
        ["package", "show", "demo", "--target", "repo-pypi", "--format", "pypi", "--limit", "2"],
    )

    assert result.exit_code == 0, result.output
    assert fake.calls[1:] == [
        (
            "GET",
            "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/package",
            {"package_name": "demo"},
        ),
        (
            "GET",
            "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/package/versions",
            {"package_name": "demo", "limit": 2},
        ),
    ]
    assert "quarantined" in result.stdout
    assert "1.2.3" in result.stdout
    assert "1.2.2" in result.stdout
    assert "Yanked" in result.stdout
    assert "ar_xyzabcde" in result.stdout
    note = " ".join(result.stderr.split())
    assert "Showing the newest 2 of 3 versions" in note
    assert "--all-versions" in note


def test_artifacts_package_show_default_reads_one_page_of_versions(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _repository_entry("repo-pypi"),
            _package_summary(version_count=1),
            {"items": [_version_summary("1.2.3")], "next_cursor": None},
        ]
    )
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app,
        ["package", "show", "demo", "--target", "repo-pypi", "--format", "pypi"],
    )

    assert result.exit_code == 0, result.output
    assert fake.calls[-1][2] == {"package_name": "demo", "limit": 50}
    assert "Showing the newest" not in result.stderr


def test_artifacts_package_show_all_versions_follows_every_page_as_json(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _repository_entry("repo-npm"),
            _package_summary(tags={"latest": "1.2.3", "next": "2.0.0-rc.1"}),
            {"items": [_version_summary("2.0.0-rc.1")], "next_cursor": "v1"},
            {
                "items": [_version_summary("1.2.3"), _version_summary("1.0.0", deprecated=True)],
                "next_cursor": None,
            },
        ]
    )
    _use_fake_client(monkeypatch, fake)

    output.set_json(True)
    try:
        result = runner.invoke(
            artifacts_cmd.app,
            ["package", "show", "demo", "-t", "repo-npm", "-f", "npm", "--all-versions"],
        )
    finally:
        output.set_json(False)

    assert result.exit_code == 0, result.output
    versions_path = "/v0/artifacts/repositories/ar_xyzabcde/formats/npm/package/versions"
    assert [call for call in fake.calls if call[1] == versions_path] == [
        ("GET", versions_path, {"package_name": "demo", "limit": 100}),
        ("GET", versions_path, {"package_name": "demo", "limit": 100, "cursor": "v1"}),
    ]
    (document,) = (json.loads(line) for line in result.stdout.splitlines())
    assert document["repository"] == "ar_xyzabcde"
    assert document["package"]["tags"] == {"latest": "1.2.3", "next": "2.0.0-rc.1"}
    assert document["package"]["latest_stable_version"] == "1.2.3"
    assert [item["version"] for item in document["versions"]] == ["2.0.0-rc.1", "1.2.3", "1.0.0"]
    assert document["versions"][2] == _version_summary("1.0.0", deprecated=True)
    assert document["versions_next_cursor"] is None


def test_artifacts_package_show_json_reports_where_a_truncated_listing_resumes(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _repository_entry("repo-pypi"),
            _package_summary(),
            {"items": [_version_summary("1.2.3")], "next_cursor": "v1"},
        ]
    )
    _use_fake_client(monkeypatch, fake)

    output.set_json(True)
    try:
        result = runner.invoke(
            artifacts_cmd.app,
            ["package", "show", "demo", "-t", "repo-pypi", "-f", "pypi", "--limit", "1"],
        )
    finally:
        output.set_json(False)

    assert result.exit_code == 0, result.output
    (document,) = (json.loads(line) for line in result.stdout.splitlines())
    assert document["versions_next_cursor"] == "v1"


def test_artifacts_package_show_ignores_a_cursor_once_every_version_is_listed(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _repository_entry("repo-pypi"),
            _package_summary(version_count=1),
            {"items": [_version_summary("1.2.3")], "next_cursor": "v1"},
        ]
    )
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app,
        ["package", "show", "demo", "-t", "repo-pypi", "-f", "pypi", "--limit", "1"],
    )

    assert result.exit_code == 0, result.output
    assert "Showing the newest" not in result.stderr


def _version_detail() -> dict[str, Any]:
    return {
        "version": "1.2.3",
        "published_at": "2026-09-01T00:00:00Z",
        "yanked": True,
        "yanked_reason": "broken wheel",
        "deprecated": False,
        "deprecated_reason": None,
        "summary": "Demo package",
        "description": "Long description",
        "license": "MIT",
        "home_page": "https://example.test/demo",
        "keywords": ["demo", "example"],
        "size_bytes": 2000,
        "downloads": 9,
        "file_count": 2,
    }


def _version_files() -> list[_JsonResponse]:
    """The version's files as two collection pages."""
    return [
        _JsonResponse(
            {
                "items": [
                    {
                        "filename": "demo-1.2.3-py3-none-any.whl",
                        "size_bytes": 1500,
                        "published_at": "2026-09-01T00:00:00Z",
                        "digests": {"md5": "d" * 32, "sha256": "a" * 64, "blake3_256": "b" * 64},
                    }
                ],
                "next_cursor": "files-page-2",
            }
        ),
        _JsonResponse(
            {
                "items": [
                    {
                        "filename": "demo-1.2.3.tar.gz",
                        "size_bytes": 500,
                        "published_at": None,
                        "digests": {"sha256": "c" * 64},
                    }
                ],
                "next_cursor": None,
            }
        ),
    ]


def test_artifacts_package_show_version_prints_files_and_every_digest(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([_repository_entry("repo-pypi"), _version_detail(), *_version_files()])
    _use_fake_client(monkeypatch, fake)
    tables: list[tuple[list[str], list[list[str]]]] = []
    monkeypatch.setattr(
        artifacts_cmd.output,
        "table",
        lambda headers, rows, **_kwargs: tables.append((headers, rows)),
    )

    result = runner.invoke(
        artifacts_cmd.app,
        ["package", "show", "demo", "-t", "repo-pypi", "-f", "pypi", "--version", "1.2.3"],
    )

    assert result.exit_code == 0, result.output
    files_path = "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/package/version/files"
    assert fake.calls[1:] == [
        (
            "GET",
            "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/package/version",
            {"package_name": "demo", "version": "1.2.3"},
        ),
        ("GET", files_path, {"package_name": "demo", "version": "1.2.3", "limit": 100}),
        (
            "GET",
            files_path,
            {"package_name": "demo", "version": "1.2.3", "limit": 100, "cursor": "files-page-2"},
        ),
    ]
    assert "broken wheel" in result.stdout
    assert "MIT" in result.stdout
    assert "demo, example" in result.stdout
    assert tables == [
        (
            ["File", "Size", "Published", "Digests"],
            [
                [
                    "demo-1.2.3-py3-none-any.whl",
                    "1500",
                    "2026-09-01T00:00:00Z",
                    f"sha256:{'a' * 64}\nblake3_256:{'b' * 64}\nmd5:{'d' * 32}",
                ],
                ["demo-1.2.3.tar.gz", "500", "", f"sha256:{'c' * 64}"],
            ],
        )
    ]


def test_artifacts_package_show_version_json_keeps_the_digest_map(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([_repository_entry("repo-pypi"), _version_detail(), *_version_files()])
    _use_fake_client(monkeypatch, fake)
    output.set_json(True)
    try:
        result = runner.invoke(
            artifacts_cmd.app,
            ["package", "show", "demo", "-t", "repo-pypi", "-f", "pypi", "--version", "1.2.3"],
        )
    finally:
        output.set_json(False)

    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["repository"] == "ar_xyzabcde"
    assert document["package"] == "demo"
    assert document["file_count"] == 2
    assert [item["filename"] for item in document["files"]] == [
        "demo-1.2.3-py3-none-any.whl",
        "demo-1.2.3.tar.gz",
    ]
    assert document["files"][0]["digests"]["blake3_256"] == "b" * 64


@pytest.mark.parametrize(
    "extra", [["--version", "1.0", "--limit", "2"], ["--limit", "2", "--all-versions"]]
)
def test_artifacts_package_show_rejects_conflicting_version_options(
    monkeypatch, tmp_path: Path, extra: list[str]
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient()
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app, ["package", "show", "demo", "-t", "repo-pypi", "-f", "pypi", *extra]
    )

    assert result.exit_code == 1
    assert fake.calls == []


def test_package_lifecycle_columns_follow_format_semantics() -> None:
    assert artifacts_cmd._package_lifecycle_columns("npm", {"deprecated": True}) == (
        ["Deprecated"],
        ["yes"],
    )
    assert artifacts_cmd._package_lifecycle_columns("pypi", {"yanked": False}) == (
        ["Yanked"],
        ["no"],
    )
    assert artifacts_cmd._package_lifecycle_columns("maven", {}) == ([], [])


def test_artifacts_package_mutations_call_expected_api_paths(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient()
    _use_fake_client(monkeypatch, fake)

    delete_result = runner.invoke(
        artifacts_cmd.app,
        ["package", "delete", "demo", "--target", "repo-pypi", "--format", "pypi"],
    )
    delete_version_result = runner.invoke(
        artifacts_cmd.app,
        [
            "package",
            "delete-version",
            "demo",
            "1.0.0",
            "--target",
            "repo-pypi",
            "--format",
            "pypi",
            "--yes",
        ],
    )
    yank_result = runner.invoke(
        artifacts_cmd.app,
        [
            "package",
            "yank",
            "demo",
            "1.0.1",
            "--target",
            "repo-pypi",
            "--format",
            "pypi",
            "--reason",
            "bad build",
        ],
    )
    unyank_result = runner.invoke(
        artifacts_cmd.app,
        [
            "package",
            "unyank",
            "demo",
            "1.0.1",
            "--target",
            "repo-pypi",
            "--format",
            "pypi",
        ],
    )
    deprecate_result = runner.invoke(
        artifacts_cmd.app,
        [
            "package",
            "deprecate",
            "demo",
            "2.0.0",
            "--target",
            "repo-npm",
            "--format",
            "npm",
            "--message",
            "use version 3",
        ],
    )
    undeprecate_result = runner.invoke(
        artifacts_cmd.app,
        [
            "package",
            "undeprecate",
            "demo",
            "2.0.0",
            "--target",
            "repo-npm",
            "--format",
            "npm",
        ],
    )

    # Deleting a whole package is browser-only.
    assert delete_result.exit_code == 2
    assert delete_version_result.exit_code == 0
    # A deleted version goes to the repository trash, not away for good.
    assert (
        "Moved demo@1.0.0 in 'repo:test-account/repo' to trash. Restore it in the web "
        "app before it is permanently deleted." in " ".join(delete_version_result.output.split())
    )
    assert yank_result.exit_code == 0
    assert unyank_result.exit_code == 0
    assert deprecate_result.exit_code == 0
    assert undeprecate_result.exit_code == 0
    assert fake.calls == [
        (
            "GET",
            "/v0/artifacts/repositories/resolve",
            {
                "selector": "repo-pypi",
                "account_ref": "ac_23456789",
                "format": "pypi",
            },
        ),
        (
            "DELETE",
            "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/package/version",
            {"package_name": "demo", "version": "1.0.0"},
        ),
        (
            "GET",
            "/v0/artifacts/repositories/resolve",
            {
                "selector": "repo-pypi",
                "account_ref": "ac_23456789",
                "format": "pypi",
            },
        ),
        (
            "PATCH",
            "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/package/version",
            {
                "json": {"yanked": True, "yanked_reason": "bad build"},
                "params": {"package_name": "demo", "version": "1.0.1"},
            },
        ),
        (
            "GET",
            "/v0/artifacts/repositories/resolve",
            {
                "selector": "repo-pypi",
                "account_ref": "ac_23456789",
                "format": "pypi",
            },
        ),
        (
            "PATCH",
            "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/package/version",
            {
                "json": {"yanked": False},
                "params": {"package_name": "demo", "version": "1.0.1"},
            },
        ),
        (
            "GET",
            "/v0/artifacts/repositories/resolve",
            {
                "selector": "repo-npm",
                "account_ref": "ac_23456789",
                "format": "npm",
            },
        ),
        (
            "PATCH",
            "/v0/artifacts/repositories/ar_xyzabcde/formats/npm/package/version",
            {
                "json": {"deprecated": True, "deprecated_reason": "use version 3"},
                "params": {"package_name": "demo", "version": "2.0.0"},
            },
        ),
        (
            "GET",
            "/v0/artifacts/repositories/resolve",
            {
                "selector": "repo-npm",
                "account_ref": "ac_23456789",
                "format": "npm",
            },
        ),
        (
            "PATCH",
            "/v0/artifacts/repositories/ar_xyzabcde/formats/npm/package/version",
            {
                "json": {"deprecated": False},
                "params": {"package_name": "demo", "version": "2.0.0"},
            },
        ),
    ]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (
            [
                "package",
                "yank",
                "demo",
                "1.0.0",
                "--target",
                "repo-npm",
                "--format",
                "npm",
            ],
            "only for pypi",
        ),
        (
            [
                "package",
                "deprecate",
                "demo",
                "1.0.0",
                "--target",
                "repo-pypi",
                "--format",
                "pypi",
                "--message",
                "obsolete",
            ],
            "only for npm",
        ),
        (
            [
                "package",
                "deprecate",
                "demo",
                "1.0.0",
                "--target",
                "repo-npm",
                "--format",
                "npm",
                "--message",
                "   ",
            ],
            "cannot be empty",
        ),
    ],
)
def test_artifacts_package_lifecycle_rejects_wrong_format_or_empty_message(
    monkeypatch,
    tmp_path: Path,
    args: list[str],
    message: str,
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient()
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, args)

    assert result.exit_code != 0
    assert message in result.output
    assert fake.calls == []


def test_artifacts_yank_without_reason_omits_the_reason(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient()
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app,
        ["package", "yank", "demo", "1.0.1", "--target", "repo-pypi", "--format", "pypi"],
    )

    assert result.exit_code == 0, result.output
    assert fake.calls[-1] == (
        "PATCH",
        "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/package/version",
        {"json": {"yanked": True}, "params": {"package_name": "demo", "version": "1.0.1"}},
    )


def test_artifacts_command_surfaces_retired_route_message(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)

    class RetiredClient(_FakeApiClient):
        def get(self, path: str, params: dict[str, Any] | None = None) -> _JsonResponse:
            raise artifacts_cmd.ApiError(
                410,
                {
                    "code": "ApiRouteRetired",
                    "message": "This rvs release uses a retired API route. Upgrade rvs.",
                },
            )

    _use_fake_client(monkeypatch, RetiredClient())

    result = runner.invoke(artifacts_cmd.app, ["repo", "list"])

    assert result.exit_code == 1
    output_text = " ".join(result.output.split())
    assert "This rvs release is no longer supported by the Ravenstash API." in output_text
    assert "run `rvs update`" in output_text
    assert "HTTP 410" not in result.output


def test_scoped_npm_package_names_stay_in_the_query(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _repository_entry("repo-npm"),
            _package_summary(name="@scope/pkg", normalized_name="@scope/pkg", status="active"),
            {"items": [], "next_cursor": None},
            {"items": [], "next_cursor": None},
            _repository_entry("repo-npm"),
            {"version": "2.0.0", "deprecated": True, "file_count": 0},
        ]
    )
    _use_fake_client(monkeypatch, fake)

    show = runner.invoke(
        artifacts_cmd.app,
        ["package", "show", "@scope/pkg", "--target", "repo-npm", "--format", "npm"],
    )
    deprecate = runner.invoke(
        artifacts_cmd.app,
        [
            "package",
            "deprecate",
            "@scope/pkg",
            "2.0.0",
            "--target",
            "repo-npm",
            "--format",
            "npm",
            "--message",
            "use version 3",
        ],
    )

    assert show.exit_code == 0, show.output
    assert deprecate.exit_code == 0, deprecate.output
    package_calls = [call for call in fake.calls if "/formats/npm/" in call[1]]
    assert [call[1] for call in package_calls] == [
        "/v0/artifacts/repositories/ar_xyzabcde/formats/npm/package",
        "/v0/artifacts/repositories/ar_xyzabcde/formats/npm/package/versions",
        "/v0/artifacts/repositories/ar_xyzabcde/formats/npm/package/tags",
        "/v0/artifacts/repositories/ar_xyzabcde/formats/npm/package/version",
    ]
    assert package_calls[0][2] == {"package_name": "@scope/pkg"}
    assert package_calls[1][2] == {"package_name": "@scope/pkg", "limit": 50}
    assert package_calls[2][2] == {"package_name": "@scope/pkg", "limit": 100}
    assert package_calls[3][2]["params"] == {"package_name": "@scope/pkg", "version": "2.0.0"}


def test_unknown_open_enum_values_are_displayed_as_is(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    repository = _repository_entry(
        "future",
        formats=[
            {"format": "pypi"},
            {"format": "cargo"},
        ],
        allowed_actions=["content.read", "repository.audit"],
    )
    fake = _FakeApiClient(
        [
            [repository],
            repository,
            [
                _upstream(
                    1,
                    kind="partner_feed",
                    ref=None,
                    display_name="partner",
                    source_type="partner",
                )
                | {"resolution_tier": "partner_tier", "publication_control": "partner_signed"}
            ],
            [_remote_cache(source_type="partner", name="vendor", official_source_ref=None)],
        ]
    )
    _use_fake_client(monkeypatch, fake)

    repos = runner.invoke(artifacts_cmd.app, ["repo", "list"])
    upstreams = runner.invoke(artifacts_cmd.app, ["repo", "upstream", "list", "future", "pypi"])
    mirrors = runner.invoke(artifacts_cmd.app, ["mirror", "list"])

    assert repos.exit_code == 0, repos.output
    assert "pypi, cargo" in repos.output
    assert upstreams.exit_code == 0, upstreams.output
    assert "partner_feed" in upstreams.output
    assert mirrors.exit_code == 0, mirrors.output
    assert "partner" in mirrors.output


def test_a_target_with_values_rvs_cannot_store_is_refused() -> None:
    from rvs.artifacts import targets

    single_unknown = _repository_entry(formats=[{"format": "cargo"}])
    assert targets._repository_target(single_unknown).format is None
    with pytest.raises(ValueError, match="namespace realm"):
        targets._repository_target(
            _repository_entry(namespace={"ref": "pb_abcdefgh", "name": "x", "realm": "public"})
        )
    with pytest.raises(ValueError, match="cargo"):
        targets._remote_target(_remote_cache(format="cargo"), "official_cache")


_SELECTED_REPOSITORY_CONFIG = """
schema_version = 1
default_profile = "default"

[profiles.default]
api_url = "https://api.ravenstash.com"
account_ref = "ac_23456789"
active_account_ref = "ac_23456789"

[profiles.default.accounts.ac_23456789]
account_type = "personal"
organization_role = "owner"

[profiles.default.accounts.ac_23456789.selected_target]
target_type = "repository"
stable_selector = "in/ar_xyzabcde"
display_selector = "test-account/repo"
namespace_realm = "internal"
namespace_ref = "in_abcdefgh"
namespace_name_cache = "test-account"
repository_ref = "ar_xyzabcde"
repository_name_cache = "repo"
account_ref = "ac_23456789"
""".strip()


def _formats(*kinds: str) -> list[dict[str, Any]]:
    return [{"format": kind} for kind in kinds]


def test_package_commands_use_the_selected_repository_and_its_one_package_format(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path, _SELECTED_REPOSITORY_CONFIG)
    fake = _FakeApiClient([_repository_entry(formats=_formats("pypi", "oci")), [{"name": "demo"}]])
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, ["package", "list"])

    assert result.exit_code == 0, result.output
    assert fake.calls[0][2]["selector"] == "in/ar_xyzabcde"
    assert fake.calls[1][1] == "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/packages"
    assert "demo" in result.output


def test_package_commands_ask_for_a_format_when_several_apply(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path, _SELECTED_REPOSITORY_CONFIG)
    fake = _FakeApiClient([_repository_entry(formats=_formats("pypi", "npm", "oci"))])
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, ["package", "list"])

    assert result.exit_code == 1
    assert "--format pypi | npm" in " ".join(result.output.split())
    assert [call[0] for call in fake.calls] == ["GET"]


def test_yank_needs_neither_target_nor_format(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path, _SELECTED_REPOSITORY_CONFIG)
    fake = _FakeApiClient([_repository_entry(formats=_formats("pypi", "npm")), {}])
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, ["package", "yank", "demo", "1.0"])

    assert result.exit_code == 0, result.output
    assert fake.calls[-1][1] == (
        "/v0/artifacts/repositories/ar_xyzabcde/formats/pypi/package/version"
    )


def test_package_commands_need_a_selection_or_target(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient()
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, ["package", "list", "--format", "pypi"])

    assert result.exit_code == 1
    assert "rvs art select" in result.output
    assert fake.calls == []


_SELECTED_MIRROR_CONFIG = _SELECTED_REPOSITORY_CONFIG.replace(
    'target_type = "repository"', 'target_type = "official_cache"'
).replace('display_selector = "test-account/repo"', 'display_selector = "mirror:pypiorg"')


@pytest.mark.parametrize(
    ("config", "argv", "responses", "message"),
    [
        (
            _SELECTED_MIRROR_CONFIG,
            ["package", "list"],
            [],
            "is a private mirror",
        ),
        (
            _SELECTED_REPOSITORY_CONFIG,
            ["package", "list"],
            [_repository_entry(formats=_formats("oci"))],
            "has no pypi, npm, or maven format",
        ),
        (
            _SELECTED_REPOSITORY_CONFIG,
            ["package", "list", "--format", "oci"],
            [],
            "support only pypi, npm",
        ),
        (
            _SELECTED_REPOSITORY_CONFIG,
            ["package", "yank", "demo", "1.0", "--format", "npm"],
            [],
            "supported only for pypi packages",
        ),
        (
            _SELECTED_REPOSITORY_CONFIG,
            ["package", "yank", "demo", "1.0"],
            [_repository_entry(formats=_formats("npm"))],
            "has no pypi format",
        ),
    ],
)
def test_package_commands_explain_a_target_or_format_they_cannot_use(
    monkeypatch, tmp_path: Path, config, argv, responses, message
) -> None:
    _isolate_config(monkeypatch, tmp_path, config)
    fake = _FakeApiClient(list(responses))
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, argv)

    assert result.exit_code == 1
    assert message in " ".join(result.output.split())
    # Nothing beyond the repository lookup is attempted.
    assert all(call[1].endswith("/resolve") for call in fake.calls)


def test_package_delete_version_confirms_the_move_to_trash(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient()
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app,
        [
            "package",
            "delete-version",
            "demo",
            "1.0.0",
            "--target",
            "repo-pypi",
            "--format",
            "pypi",
        ],
        input="n\n",
    )

    prompt = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "Move pypi package version demo@1.0.0 in 'repo:test-account/repo' to trash?" in prompt
    assert "can't be published again until it is permanently deleted" in prompt
    assert "restore it in the web app" in prompt
    assert all(method != "DELETE" for method, _path, _params in fake.calls)


def test_self_explaining_errors_show_their_own_message() -> None:
    from rvs.client import ApiError

    message = "This manifest is part of an image index (sha256:bb). Delete the index instead."

    error = ApiError(
        409,
        {
            "code": "ManifestReferencedByIndex",
            "message": message,
            "details": {"parent_digests": ["sha256:bb"]},
        },
    )

    assert str(error) == message
    assert error.code == "ManifestReferencedByIndex"
    # Other conflicts keep the HTTP status and code prefix.
    assert str(ApiError(409, {"code": "Conflict", "message": "Tag exists"})) == (
        "HTTP 409 Conflict: Tag exists"
    )


# ── package tags ─────────────────────────────────────────────────────────────

_TAGS_PATH = "/v0/artifacts/repositories/ar_xyzabcde/formats/npm/package/tags"


def _package_tag(tag: str, version: str | None, **overrides: Any) -> dict[str, Any]:
    return {
        "tag": tag,
        "version": version,
        "stored_version": version,
        "state": "effective",
        "revision": 1,
        **overrides,
    }


def _npm_repository() -> dict[str, Any]:
    return _repository_entry("repo-npm", formats=_formats("pypi", "npm"))


def test_package_tag_list_shows_effective_and_stored_versions(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _npm_repository(),
            {
                "items": [
                    _package_tag("beta", "2.0.0-beta.1"),
                    _package_tag(
                        "latest", "1.9.0", stored_version="2.0.0", state="fallback", revision=3
                    ),
                ],
                "next_cursor": "c1",
            },
            {
                "items": [_package_tag("next", None, stored_version="3.0.0-rc.1", state="hidden")],
                "next_cursor": None,
            },
        ]
    )
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app, ["package", "tag", "list", "@scope/pkg", "--target", "repo-npm"]
    )

    assert result.exit_code == 0, result.output
    assert fake.calls[1:] == [
        ("GET", _TAGS_PATH, {"package_name": "@scope/pkg", "limit": 100}),
        ("GET", _TAGS_PATH, {"package_name": "@scope/pkg", "limit": 100, "cursor": "c1"}),
    ]
    text = " ".join(result.stdout.split())
    for expected in ("Tag", "Version", "State", "Stored", "fallback", "2.0.0", "hidden"):
        assert expected in text
    assert "3.0.0-rc.1" in text


def test_package_tag_list_json_keeps_the_wire_items(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    items = [_package_tag("latest", "1.0.0")]
    fake = _FakeApiClient([_npm_repository(), {"items": items, "next_cursor": None}])
    _use_fake_client(monkeypatch, fake)
    output.set_json(True)
    try:
        result = runner.invoke(
            artifacts_cmd.app, ["package", "tag", "list", "demo", "--target", "repo-npm"]
        )
    finally:
        output.set_json(False)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "repository": "ar_xyzabcde",
        "package": "demo",
        "items": items,
    }


def test_package_tag_list_says_when_a_package_has_none(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    _use_fake_client(monkeypatch, _FakeApiClient([_npm_repository(), []]))

    result = runner.invoke(
        artifacts_cmd.app, ["package", "tag", "list", "demo", "--target", "repo-npm"]
    )

    assert result.exit_code == 0, result.output
    assert "demo has no tags" in result.output


def _upstream_tag(
    tag: str, version: str, source: str = "upstream_repository", position: int | None = 1
) -> dict[str, Any]:
    """A tag an upstream defines: read-only here, without a stored version or revision."""
    return {
        "tag": tag,
        "version": version,
        "stored_version": None,
        "state": "effective",
        "hidden_reason": None,
        "revision": None,
        "updated_at": None,
        "read_only": True,
        "source": source,
        "upstream_position": position,
    }


def _table_rows(stdout: str) -> dict[str, list[str]]:
    """Body rows of a printed table by their first cell.

    The header is the first row; its separator depends on the terminal, and
    Windows draws it like a body row.
    """
    lines = [
        [cell.strip() for cell in re.split("[┃│]", line.strip().strip("┃│"))]
        for line in stdout.splitlines()
        if "│" in line or "┃" in line
    ]
    return {cells[0]: cells for cells in lines[1:]}


def test_package_tag_list_names_each_tag_source_and_marks_read_only_tags(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    items = [
        # The repository's own `latest`, served by an upstream's version meanwhile.
        _package_tag(
            "latest",
            "2.1.0",
            stored_version="1.0.0",
            state="fallback",
            hidden_reason="not_available",
            revision=3,
            read_only=False,
            source="upstream_repository",
            upstream_position=1,
        ),
        _package_tag(
            "beta", "1.1.0-beta.1", read_only=False, source="repository", upstream_position=None
        ),
        _upstream_tag("canary", "2.2.0-canary.4"),
        _upstream_tag("next", "3.0.0-rc.1", source="remote_cache", position=3),
        # An upstream Ravenstash could not identify has no position.
        _upstream_tag("lts", "1.8.2", source="upstream", position=None),
        _upstream_tag("edge", "4.0.0", source="some_future_source", position=None),
    ]
    _use_fake_client(monkeypatch, _FakeApiClient([_npm_repository(), items]))
    monkeypatch.setattr(output.console, "width", 200)

    result = runner.invoke(
        artifacts_cmd.app, ["package", "tag", "list", "demo", "--target", "repo-npm"]
    )

    assert result.exit_code == 0, result.output
    header = " ".join(result.stdout.split())
    # The header separator depends on the terminal: Windows draws a lighter box.
    assert re.search(r"Tag\W+Version\W+State\W+Source\W+Read-only\W+Stored\W+Revision", header)
    assert _table_rows(result.stdout) == {
        "latest": [
            "latest",
            "2.1.0",
            "fallback: not installable here",
            "upstream repository (position 1)",
            "no",
            "1.0.0",
            "3",
        ],
        "beta": ["beta", "1.1.0-beta.1", "effective", "this repository", "no", "1.1.0-beta.1", "1"],
        "canary": [
            "canary",
            "2.2.0-canary.4",
            "effective",
            "upstream repository (position 1)",
            "yes",
            "",
            "",
        ],
        "next": ["next", "3.0.0-rc.1", "effective", "remote cache (position 3)", "yes", "", ""],
        "lts": ["lts", "1.8.2", "effective", "upstream", "yes", "", ""],
        # A source `rvs` does not know yet is shown as Ravenstash reports it.
        "edge": ["edge", "4.0.0", "effective", "some_future_source", "yes", "", ""],
    }
    for placeholder in ("None", "?", "hidden"):
        assert placeholder not in result.stdout


def test_package_tag_list_without_source_fields_lists_the_repository_tags(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    # An answer without `source`: every tag is the repository's own unless it
    # says it is read-only.
    items = [_package_tag("latest", "1.0.0"), {**_package_tag("beta", "2.0.0"), "read_only": True}]
    _use_fake_client(monkeypatch, _FakeApiClient([_npm_repository(), items]))
    monkeypatch.setattr(output.console, "width", 200)

    result = runner.invoke(
        artifacts_cmd.app, ["package", "tag", "list", "demo", "--target", "repo-npm"]
    )

    assert result.exit_code == 0, result.output
    rows = _table_rows(result.stdout)
    assert rows["latest"][3:5] == ["this repository", "no"]
    assert rows["beta"][3:5] == ["upstream", "yes"]


def test_package_tag_list_json_passes_the_source_fields_through(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    items = [
        _package_tag("latest", "1.0.0", read_only=False, source="repository"),
        _upstream_tag("next", "3.0.0-rc.1", source="remote_cache"),
    ]
    _use_fake_client(monkeypatch, _FakeApiClient([_npm_repository(), items]))
    output.set_json(True)
    try:
        result = runner.invoke(
            artifacts_cmd.app, ["package", "tag", "list", "demo", "--target", "repo-npm"]
        )
    finally:
        output.set_json(False)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["items"] == items


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            "created",
            "Created tag 'beta' at demo@2.0.0 in 'repo:test-account/repo-npm' (revision 1).",
        ),
        ("moved", "Moved tag 'beta' to demo@2.0.0"),
        ("unchanged", "Tag 'beta' already points at demo@2.0.0"),
    ],
)
def test_package_tag_set_creates_or_moves_a_tag_without_confirmation(
    monkeypatch, tmp_path: Path, result, message
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _npm_repository(),
            {"tag": "beta", "version": "2.0.0", "revision": 1, "result": result},
        ]
    )
    _use_fake_client(monkeypatch, fake)

    invoked = runner.invoke(
        artifacts_cmd.app,
        ["package", "tag", "set", "demo", "beta", "2.0.0", "--target", "repo-npm"],
    )

    assert invoked.exit_code == 0, invoked.output
    assert message in " ".join(invoked.stdout.split())
    assert fake.calls[-1] == (
        "PUT",
        f"{_TAGS_PATH}/beta",
        {
            "json": {"version": "2.0.0", "expected_revision": None},
            "params": {"package_name": "demo"},
        },
    )


def test_package_tag_set_confirms_moving_latest_and_sends_the_revision(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _npm_repository(),
            _npm_repository(),
            {"tag": "latest", "version": "1.0.1", "revision": 4, "result": "moved"},
        ]
    )
    _use_fake_client(monkeypatch, fake)
    argv = ["package", "tag", "set", "@scope/pkg", "latest", "1.0.1", "--target", "repo-npm"]

    declined = runner.invoke(artifacts_cmd.app, argv, input="n\n")
    accepted = runner.invoke(artifacts_cmd.app, [*argv, "--expect-revision", "3"], input="y\n")

    assert declined.exit_code == 1
    prompt = " ".join(declined.output.split())
    assert "Point 'latest' of @scope/pkg in 'repo:test-account/repo-npm' at 1.0.1?" in prompt
    assert accepted.exit_code == 0, accepted.output
    puts = [call for call in fake.calls if call[0] == "PUT"]
    assert puts == [
        (
            "PUT",
            f"{_TAGS_PATH}/latest",
            {
                "json": {"version": "1.0.1", "expected_revision": 3},
                "params": {"package_name": "@scope/pkg"},
            },
        )
    ]


def test_package_tag_delete_always_confirms(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([_npm_repository(), _npm_repository(), _npm_repository()])
    _use_fake_client(monkeypatch, fake)
    argv = ["package", "tag", "delete", "demo", "beta", "--target", "repo-npm"]

    declined = runner.invoke(artifacts_cmd.app, argv, input="n\n")
    accepted = runner.invoke(artifacts_cmd.app, argv, input="y\n")
    forced = runner.invoke(artifacts_cmd.app, [*argv, "--yes", "--expect-revision", "2"])

    assert declined.exit_code == 1
    assert "Delete tag 'beta' of demo in 'repo:test-account/repo-npm'?" in " ".join(
        declined.output.split()
    )
    assert accepted.exit_code == 0, accepted.output
    assert forced.exit_code == 0, forced.output
    assert "Deleted tag 'beta' of demo" in forced.stdout
    deletes = [call for call in fake.calls if call[0] == "DELETE"]
    assert deletes == [
        ("DELETE", f"{_TAGS_PATH}/beta", {"package_name": "demo"}),
        ("DELETE", f"{_TAGS_PATH}/beta", {"package_name": "demo", "expected_revision": 2}),
    ]


def test_package_tag_confirmation_shows_control_characters_as_placeholders(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([_npm_repository()])
    _use_fake_client(monkeypatch, fake)

    declined = runner.invoke(
        artifacts_cmd.app,
        ["package", "tag", "delete", "demo\x1b[2J", "beta", "--target", "repo-npm"],
        input="n\n",
    )

    assert declined.exit_code == 1
    # Click colors "Aborted." when CI forces color; only the name's sequence matters.
    assert "\x1b[2J" not in declined.output
    assert "Delete tag 'beta' of demo?[2J" in declined.output


def test_package_show_names_why_a_tag_does_not_resolve() -> None:
    assert (
        artifacts_cmd._tag_detail_label(
            _package_tag(
                "latest",
                "1.1.0",
                stored_version="1.2.0",
                state="fallback",
                hidden_reason="in_trash",
            )
        )
        == "latest=1.1.0 (fallback: in the trash; set to 1.2.0)"
    )
    assert (
        artifacts_cmd._tag_detail_label(
            _package_tag(
                "stable",
                None,
                stored_version="1.2.0",
                state="hidden",
                hidden_reason="not_available",
            )
        )
        == "stable=- (hidden: not installable here; set to 1.2.0)"
    )
    # A fallback without a reason still names its state, never a placeholder.
    assert (
        artifacts_cmd._tag_detail_label(
            _package_tag("latest", "1.1.0", stored_version=None, state="fallback")
        )
        == "latest=1.1.0 (fallback)"
    )
    # Readers without publish authority see a fallback `latest` as effective.
    assert artifacts_cmd._tag_detail_label(_reader_tag("latest", "1.1.0")) == "latest=1.1.0"


def test_package_show_names_the_source_of_an_upstream_tag() -> None:
    assert (
        artifacts_cmd._tag_detail_label(_upstream_tag("beta", "2.0.0-beta.1"))
        == "beta=2.0.0-beta.1 (from upstream repository)"
    )
    assert (
        artifacts_cmd._tag_detail_label(_upstream_tag("next", "3.0.0", source="remote_cache"))
        == "next=3.0.0 (from remote cache)"
    )
    # The repository's own tags carry no source note, for publishers and readers.
    assert (
        artifacts_cmd._tag_detail_label(
            _package_tag("latest", "1.0.0", read_only=False, source="repository")
        )
        == "latest=1.0.0"
    )
    assert (
        artifacts_cmd._tag_detail_label(
            {**_reader_tag("latest", "1.0.0"), "read_only": False, "source": "repository"}
        )
        == "latest=1.0.0"
    )
    # The repository's `latest` whose versions an upstream serves meanwhile.
    assert (
        artifacts_cmd._tag_detail_label(
            _package_tag(
                "latest",
                "2.1.0",
                stored_version="1.0.0",
                state="fallback",
                hidden_reason="not_available",
                read_only=False,
                source="upstream_repository",
            )
        )
        == "latest=2.1.0 (fallback: not installable here; set to 1.0.0; from upstream repository)"
    )


def _reader_tag(tag: str, version: str) -> dict[str, Any]:
    """A tag as callers without publish authority receive it."""
    return {
        "tag": tag,
        "version": version,
        "stored_version": None,
        "state": "effective",
        "hidden_reason": None,
        "revision": None,
        "updated_at": None,
    }


def test_package_tag_list_for_readers_prints_no_placeholders(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    items = [_reader_tag("latest", "1.1.0"), _reader_tag("beta", "2.0.0-beta.1")]
    _use_fake_client(monkeypatch, _FakeApiClient([_npm_repository(), items]))

    result = runner.invoke(
        artifacts_cmd.app, ["package", "tag", "list", "demo", "--target", "repo-npm"]
    )

    assert result.exit_code == 0, result.output
    text = " ".join(result.stdout.split())
    for expected in ("latest", "1.1.0", "beta", "2.0.0-beta.1", "effective"):
        assert expected in text
    for placeholder in ("None", "?", "fallback", "hidden"):
        assert placeholder not in text


@pytest.mark.parametrize("command", ["set", "delete"])
@pytest.mark.parametrize("tag", ["", ".", ".."])
def test_package_tag_changes_refuse_dot_segment_names_before_asking(
    monkeypatch, tmp_path: Path, command: str, tag: str
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([_npm_repository()])
    _use_fake_client(monkeypatch, fake)
    arguments = [tag, "1.0.0"] if command == "set" else [tag]

    result = runner.invoke(
        artifacts_cmd.app,
        ["package", "tag", command, "demo", *arguments, "--target", "repo-npm"],
        input="y\n",
    )

    assert result.exit_code == 1
    assert "A package tag cannot be empty, '.', or '..'." in " ".join(result.output.split())
    assert "?" not in result.output
    assert fake.calls == []


def test_package_tag_names_are_percent_encoded_and_round_trip(monkeypatch, tmp_path: Path) -> None:
    import httpx2
    from rvs.api import api_url

    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _npm_repository(),
            {"tag": "a@b", "version": "1.0.0", "revision": 1, "result": "created"},
            _npm_repository(),
        ]
    )
    _use_fake_client(monkeypatch, fake)

    created = runner.invoke(
        artifacts_cmd.app,
        ["package", "tag", "set", "demo", "a@b", "1.0.0", "--target", "repo-npm"],
    )
    deleted = runner.invoke(
        artifacts_cmd.app,
        ["package", "tag", "delete", "demo", "a@b", "--target", "repo-npm", "--yes"],
    )

    assert created.exit_code == 0, created.output
    assert deleted.exit_code == 0, deleted.output
    changes = [call for call in fake.calls if call[0] in {"PUT", "DELETE"}]
    assert [call[:2] for call in changes] == [
        ("PUT", f"{_TAGS_PATH}/a%40b"),
        ("DELETE", f"{_TAGS_PATH}/a%40b"),
    ]
    for _method, path, _params in changes:
        url = httpx2.URL(api_url("https://api.example.test", path))
        assert url.raw_path == f"{_TAGS_PATH}/a%40b".encode()
        assert url.path.rsplit("/", 1)[1] == "a@b"


def test_package_tag_confirmation_without_an_answer_names_yes(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([_npm_repository()])
    _use_fake_client(monkeypatch, fake)

    # No input: like CI, the run cannot answer the question.
    result = runner.invoke(
        artifacts_cmd.app,
        ["package", "tag", "delete", "demo", "beta", "--target", "repo-npm"],
    )

    assert result.exit_code == 1
    message = " ".join(result.output.split())
    assert "no answer could be read" in message
    assert "Pass --yes in non-interactive runs" in message
    assert all(call[0] == "GET" for call in fake.calls)


def test_package_tag_changes_in_json_mode_need_yes(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([_npm_repository()])
    _use_fake_client(monkeypatch, fake)
    output.set_json(True)
    try:
        result = runner.invoke(
            artifacts_cmd.app,
            ["package", "tag", "delete", "demo", "beta", "--target", "repo-npm"],
        )
    finally:
        output.set_json(False)

    assert result.exit_code == 1
    assert "Pass --yes" in result.output
    assert all(call[0] == "GET" for call in fake.calls)


def test_package_tag_set_json_reports_the_mutation(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    mutation = {"tag": "latest", "version": "1.0.1", "revision": 4, "result": "moved"}
    _use_fake_client(monkeypatch, _FakeApiClient([_npm_repository(), mutation]))
    output.set_json(True)
    try:
        result = runner.invoke(
            artifacts_cmd.app,
            ["package", "tag", "set", "demo", "latest", "1.0.1", "-t", "repo-npm", "--yes"],
        )
    finally:
        output.set_json(False)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "repository": "ar_xyzabcde",
        "package": "demo",
        **mutation,
    }


@pytest.mark.parametrize(
    ("reason", "hint"),
    [
        ("revision_mismatch", "retry with its current --expect-revision"),
        ("target_unavailable", "not installable in this repository"),
        ("latest_required", "move it with `rvs art package tag set`"),
        ("tag_limit", "maximum number of tags"),
        ("read_only_upstream", "Refused. `rvs art package tag list` shows where each tag"),
        ("some_future_reason", "HTTP 409 Conflict: Refused"),
    ],
)
def test_package_tag_conflicts_explain_their_reason(
    monkeypatch, tmp_path: Path, reason, hint
) -> None:
    _isolate_config(monkeypatch, tmp_path)

    class ConflictClient(_FakeApiClient):
        def delete(self, path, params=None, headers=None):
            raise artifacts_cmd.ApiError(
                409, {"code": "Conflict", "message": "Refused", "reason": reason}
            )

    _use_fake_client(monkeypatch, ConflictClient([_npm_repository()]))

    result = runner.invoke(
        artifacts_cmd.app,
        ["package", "tag", "delete", "demo", "latest", "--target", "repo-npm", "--yes"],
    )

    assert result.exit_code == 1
    assert hint in " ".join(result.output.split())


@pytest.mark.parametrize(
    ("argv", "method", "message"),
    [
        (
            ["package", "tag", "set", "demo", "beta", "2.1.0", "--target", "repo-npm"],
            "put",
            "Version '2.1.0' comes from an upstream; a tag here can only point at this "
            "repository's own versions",
        ),
        (
            ["package", "tag", "delete", "demo", "canary", "--target", "repo-npm", "--yes"],
            "delete",
            "Tag 'canary' comes from an upstream; it is read-only here and can only be "
            "changed in its source",
        ),
    ],
)
def test_package_tag_changes_to_upstream_content_show_the_refusal(
    monkeypatch, tmp_path: Path, argv, method, message
) -> None:
    _isolate_config(monkeypatch, tmp_path)

    def refuse(*_args, **_kwargs):
        raise artifacts_cmd.ApiError(
            409, {"code": "Conflict", "message": message, "reason": "read_only_upstream"}
        )

    fake = _FakeApiClient([_npm_repository()])
    monkeypatch.setattr(fake, method, refuse)
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, argv)

    assert result.exit_code == 1
    text = " ".join(result.output.split())
    assert (
        f"HTTP 409 Conflict: {message}. `rvs art package tag list` shows where each tag comes from."
    ) in text
    assert "Deleted" not in text and "Created" not in text


def test_package_tag_delete_says_an_upstream_tag_may_take_over(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient([_npm_repository()])
    _use_fake_client(monkeypatch, fake)

    declined = runner.invoke(
        artifacts_cmd.app,
        ["package", "tag", "delete", "demo", "latest", "--target", "repo-npm"],
        input="n\n",
    )

    assert declined.exit_code == 1
    assert (
        "Installs by this tag stop working unless an upstream supplies the same tag."
        in " ".join(declined.output.split())
    )
    assert all(call[0] == "GET" for call in fake.calls)


@pytest.mark.parametrize(
    ("argv", "responses", "message"),
    [
        (
            ["package", "tag", "list", "demo", "--target", "repo-pypi", "--format", "pypi"],
            [],
            "Listing package tags is supported only for npm packages.",
        ),
        (
            ["package", "tag", "set", "demo", "beta", "1.0", "--target", "repo-pypi"],
            [_repository_entry("repo-pypi", formats=_formats("pypi", "maven"))],
            "has no npm format. Changing package tags applies only to npm packages.",
        ),
    ],
)
def test_package_tags_name_the_format_they_need(
    monkeypatch, tmp_path: Path, argv, responses, message
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(list(responses))
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(artifacts_cmd.app, argv)

    assert result.exit_code == 1
    assert message in " ".join(result.output.split())
    assert all(call[1].endswith("/resolve") for call in fake.calls)


def test_package_show_prints_effective_tags_and_marks_fallbacks(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _npm_repository(),
            _package_summary(tags={"latest": "1.9.0", "beta": "2.0.0-beta.1"}),
            {
                "items": [
                    _version_summary("2.0.0-beta.1", tags=["beta"]),
                    _version_summary("1.9.0", tags=["latest"]),
                ],
                "next_cursor": None,
            },
            [
                _package_tag("beta", "2.0.0-beta.1"),
                _package_tag("latest", "1.9.0", stored_version="2.0.0", state="fallback"),
            ],
        ]
    )
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app, ["package", "show", "demo", "--target", "repo-npm", "--format", "npm"]
    )

    assert result.exit_code == 0, result.output
    text = " ".join(result.stdout.split())
    assert "beta=2.0.0-beta.1, latest=1.9.0 (fallback; set to 2.0.0)" in text
    assert "Tags" in text


def test_package_show_reads_summary_tags_when_the_detail_is_unavailable(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)

    class NoTagDetail(_FakeApiClient):
        def get(self, path, params=None):
            if path.endswith("/package/tags"):
                raise artifacts_cmd.ApiError(404, {"code": "NotFound", "message": "Not found"})
            return super().get(path, params)

    summary = _package_summary()
    summary["tags"] = {"beta": "2.0.0-beta.1", "latest": "1.2.3"}
    fake = NoTagDetail([_npm_repository(), summary, {"items": [], "next_cursor": None}])
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app, ["package", "show", "demo", "--target", "repo-npm", "--format", "npm"]
    )

    assert result.exit_code == 0, result.output
    assert "latest=1.2.3, beta=2.0.0-beta.1" in result.stdout


def test_package_show_version_lists_its_tags(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    fake = _FakeApiClient(
        [
            _npm_repository(),
            {"version": "1.9.0", "tags": ["latest", "lts"], "file_count": 0},
            [],
        ]
    )
    _use_fake_client(monkeypatch, fake)

    result = runner.invoke(
        artifacts_cmd.app,
        ["package", "show", "demo", "-t", "repo-npm", "-f", "npm", "--version", "1.9.0"],
    )

    assert result.exit_code == 0, result.output
    assert "latest, lts" in result.stdout
    assert not any(call[1].endswith("/package/tags") for call in fake.calls)


def test_package_show_lists_the_tags_of_an_upstream_supplied_package(
    monkeypatch, tmp_path: Path
) -> None:
    _isolate_config(monkeypatch, tmp_path)
    tags = [
        _upstream_tag("latest", "4.17.21", source="remote_cache"),
        _upstream_tag("next", "5.0.0-rc.2", source="remote_cache"),
    ]
    fake = _FakeApiClient(
        [
            _npm_repository(),
            _package_summary(tags={"latest": "4.17.21", "next": "5.0.0-rc.2"}),
            {"items": [_version_summary("4.17.21", tags=["latest"])], "next_cursor": None},
            tags,
        ]
    )
    _use_fake_client(monkeypatch, fake)
    monkeypatch.setattr(output.console, "width", 200)

    result = runner.invoke(
        artifacts_cmd.app, ["package", "show", "demo", "--target", "repo-npm", "--format", "npm"]
    )

    assert result.exit_code == 0, result.output
    text = " ".join(result.stdout.split())
    assert "latest=4.17.21 (from remote cache), next=5.0.0-rc.2 (from remote cache)" in text
    for placeholder in ("None", "?", "hidden", "fallback"):
        assert placeholder not in text


def test_package_show_json_carries_the_tag_source_fields(monkeypatch, tmp_path: Path) -> None:
    _isolate_config(monkeypatch, tmp_path)
    tags = [_upstream_tag("latest", "4.17.21", source="remote_cache")]
    fake = _FakeApiClient(
        [
            _npm_repository(),
            _package_summary(tags={"latest": "4.17.21"}),
            {"items": [], "next_cursor": None},
            tags,
        ]
    )
    _use_fake_client(monkeypatch, fake)
    output.set_json(True)
    try:
        result = runner.invoke(
            artifacts_cmd.app, ["package", "show", "demo", "-t", "repo-npm", "-f", "npm"]
        )
    finally:
        output.set_json(False)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["tags"] == tags
