"""CLI parsing and transport contracts for OCI graph management."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rvs.artifacts import oci_commands
from rvs.cli import app
from rvs.oci import oras
from rvs.oci import runner as oci_runner
from typer.testing import CliRunner


runner = CliRunner()
DIGEST = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("argv", "publishing", "deleting"),
    [
        (["pull", "example.test/push:delete"], False, False),
        (["manifest", "fetch", "example.test/image:push"], False, False),
        (["--debug", "manifest", "delete", "example.test/image@" + DIGEST], False, True),
        (["cp", "example.test/source:read", "example.test/dest:write"], True, False),
        (["push", "example.test/image:tag", "payload.txt"], True, False),
    ],
)
def test_oras_grants_follow_verbs_not_operands(argv, publishing, deleting):
    parsed = oras.parse(argv)
    assert parsed.publishing is publishing
    assert parsed.deleting is deleting


def test_oras_copy_config_files_are_overlaid_independently():
    argv, configs = oras.registry_configs(
        [
            "cp",
            "--from-registry-config=/tmp/source.json",
            "--to-registry-config",
            "/tmp/destination.json",
            "a.test/image:1",
            "b.test/image:1",
        ]
    )
    assert argv == ["cp", "a.test/image:1", "b.test/image:1"]
    assert configs == {
        "--from-registry-config": "/tmp/source.json",
        "--to-registry-config": "/tmp/destination.json",
    }
    assert oras.registry_configs(["logout", "oci.test"])[1] == {"--registry-config": None}
    assert oras.registry_configs(["version"])[1] == {}


@pytest.fixture
def transport(monkeypatch):
    found = SimpleNamespace(
        profile=object(),
        select_format=Mock(),
        target=SimpleNamespace(
            target_type="repository",
            repository_ref="ar_23456789",
            display_selector="main/packages",
        ),
    )
    monkeypatch.setattr(oci_commands, "discover", Mock(return_value=found))
    client = Mock()
    for method in ("get", "post", "put", "delete"):
        getattr(client, method).return_value.json.return_value = {
            "items": [],
            "next_cursor": "next",
        }
    monkeypatch.setattr(oci_commands.ApiClient, "from_profile", Mock(return_value=client))
    return client


def test_oci_list_has_one_format_and_preserves_cursor_envelope(transport):
    result = runner.invoke(
        app, ["--json", "art", "oci", "list", "--content-type", "helm_chart", "--limit", "1"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"items": [], "next_cursor": "next"}
    transport.get.assert_called_once_with(
        "/v0/artifacts/repositories/ar_23456789/formats/oci/paths",
        params={"content_type": "helm_chart", "limit": 1},
    )


@pytest.mark.parametrize(
    ("args", "method", "suffix", "params"),
    [
        (["show", "images/api"], "get", "path", {"path": "images/api"}),
        (
            ["manifest", "show", "images/api@" + DIGEST],
            "get",
            "manifests/sha256%3A" + "a" * 64,
            {"path": "images/api"},
        ),
        (
            ["manifest", "delete", "images/api@" + DIGEST, "--yes"],
            "delete",
            "manifests/sha256%3A" + "a" * 64,
            {"path": "images/api"},
        ),
        (
            ["tag", "delete", "images/api:1.2.3", "--yes"],
            "delete",
            "tags/1.2.3",
            {"path": "images/api"},
        ),
        (
            ["referrer", "list", "images/api@" + DIGEST, "--limit", "5"],
            "get",
            "manifests/sha256%3A" + "a" * 64 + "/referrers",
            {"path": "images/api", "limit": 5},
        ),
        (
            ["manifest", "list", "images/api"],
            "get",
            "manifests",
            {"path": "images/api", "limit": 50},
        ),
        (
            ["tag", "list", "images/api"],
            "get",
            "tags",
            {"path": "images/api", "limit": 50},
        ),
        (
            ["tag", "list", "images/api", "--digest", DIGEST],
            "get",
            "tags",
            {"path": "images/api", "digest": DIGEST, "limit": 50},
        ),
    ],
)
def test_oci_exact_references_reach_typed_routes(transport, args, method, suffix, params):
    result = runner.invoke(app, ["art", "oci", *args])
    assert result.exit_code == 0, result.output
    getattr(transport, method).assert_called_once_with(
        "/v0/artifacts/repositories/ar_23456789/formats/oci/" + suffix, params=params
    )


@pytest.mark.parametrize(
    ("status", "message"), [(201, "Created tag 'stable'"), (200, "Tag 'stable' already points at")]
)
def test_oci_tag_create_puts_the_tag_with_its_digest(transport, status, message):
    transport.put.return_value.status_code = status
    transport.put.return_value.json.return_value = {"tag": "stable", "digest": DIGEST}
    result = runner.invoke(app, ["art", "oci", "tag", "create", "images/api@" + DIGEST, "stable"])
    assert result.exit_code == 0, result.output
    transport.put.assert_called_once_with(
        "/v0/artifacts/repositories/ar_23456789/formats/oci/tags/stable",
        json={"digest": DIGEST},
        params={"path": "images/api"},
    )
    transport.post.assert_not_called()
    assert message in " ".join(result.stdout.split())


@pytest.mark.parametrize(("status", "created"), [(201, True), (200, False)])
def test_oci_tag_create_json_reports_whether_the_tag_was_created(transport, status, created):
    transport.put.return_value.status_code = status
    transport.put.return_value.json.return_value = {"tag": "v1", "digest": DIGEST}
    result = runner.invoke(
        app, ["--json", "art", "oci", "tag", "create", "images/api@" + DIGEST, "v1"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"tag": "v1", "digest": DIGEST, "created": created}


@pytest.mark.parametrize(
    ("args", "suffix", "message"),
    [
        (["tag", "delete", "images/api:1.2.3", "--yes"], "tags/1.2.3", "Deleted images/api:1.2.3"),
        (
            ["manifest", "delete", "images/api@" + DIGEST, "--yes"],
            "manifests/sha256%3A" + "a" * 64,
            f"Moved images/api@{DIGEST} in main/packages to trash. Restore it in the web "
            "app before it is permanently deleted.",
        ),
    ],
)
def test_oci_deletes_accept_an_empty_204(transport, args, suffix, message):
    # A 204 has no body; the CLI must not try to decode one.
    transport.delete.return_value.json.side_effect = ValueError("no body")
    result = runner.invoke(app, ["art", "oci", *args])
    assert result.exit_code == 0, result.output
    transport.delete.assert_called_once_with(
        "/v0/artifacts/repositories/ar_23456789/formats/oci/" + suffix,
        params={"path": "images/api"},
    )
    assert message in " ".join(result.stdout.split())


@pytest.mark.parametrize(
    ("args", "prompt"),
    [
        (["manifest", "delete", "images/api@" + DIGEST], "Move manifest images/api@"),
        (["tag", "delete", "images/api:1.2.3"], "Delete images/api:1.2.3 from"),
    ],
)
def test_oci_delete_prompts_describe_what_happens(transport, args, prompt):
    result = runner.invoke(app, ["art", "oci", *args], input="n\n")

    assert result.exit_code != 0
    assert prompt in " ".join(result.output.split())
    if args[0] == "manifest":
        assert "to trash?" in result.output
        assert "restore it in the web app" in " ".join(result.output.split())
    transport.delete.assert_not_called()


def test_oci_manifest_referenced_by_an_index_explains_itself(transport):
    from rvs.client import ApiError

    message = "This manifest is part of an image index (sha256:bb). Delete the index instead."
    transport.delete.side_effect = ApiError(
        409, {"code": "ManifestReferencedByIndex", "message": message}
    )
    result = runner.invoke(app, ["art", "oci", "manifest", "delete", "images/api@" + DIGEST, "-y"])

    assert result.exit_code == 1
    assert message in " ".join(result.stderr.split())
    assert "HTTP 409" not in result.stderr


def test_oci_delete_reports_an_absent_object(transport):
    from rvs.client import ApiError

    transport.delete.side_effect = ApiError(404, {"code": "NotFound", "message": "Not found"})
    result = runner.invoke(app, ["art", "oci", "tag", "delete", "images/api:gone", "--yes"])
    assert result.exit_code == 1
    assert "404" in result.stderr


def test_oci_explicit_limit_returns_exactly_one_page(transport):
    transport.get.return_value.json.return_value = {
        "items": [{"path": "images/api", "recent_tags": ["v2", "v1"], "tag_count": 7}],
        "next_cursor": "c2",
    }
    result = runner.invoke(app, ["--json", "art", "oci", "list", "--limit", "1", "--cursor", "c1"])
    assert result.exit_code == 0, result.output
    transport.get.assert_called_once_with(
        "/v0/artifacts/repositories/ar_23456789/formats/oci/paths",
        params={"limit": 1, "cursor": "c1"},
    )
    assert json.loads(result.stdout)["next_cursor"] == "c2"


def test_oci_tables_show_unknown_content_types_as_is(transport):
    transport.get.return_value.json.return_value = {
        "items": [{"path": "models/llm", "content_type": "ml_model", "tag_count": 1}],
        "next_cursor": None,
    }
    result = runner.invoke(app, ["art", "oci", "list"])
    assert result.exit_code == 0, result.output
    assert "ml_model" in result.stdout


def test_oci_list_rejects_a_non_envelope_collection(transport):
    transport.get.return_value.json.return_value = [{"path": "images/api"}]
    result = runner.invoke(app, ["art", "oci", "list"])
    assert result.exit_code == 1
    assert "collection response is invalid" in result.stderr


@pytest.mark.parametrize("reference", ["images/api", "images/api:latest", "../api@" + DIGEST])
def test_manifest_show_never_infers_a_digest(transport, reference):
    result = runner.invoke(app, ["art", "oci", "manifest", "show", reference])
    assert result.exit_code != 0
    transport.get.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [
        ["version"],
        ["manifest", "--help"],
        ["push", "--oci-layout", "./layout:tag", "file.txt"],
        ["cp", "--from-oci-layout", "--to-oci-layout", "./source:tag", "./dest:tag"],
    ],
)
def test_oras_local_operations_need_no_ravenstash_login(monkeypatch, argv):
    resolve = Mock(side_effect=AssertionError("No credential for local operation"))
    process = Mock()
    monkeypatch.setattr(oci_runner, "resolve_route", resolve)
    monkeypatch.setattr(oci_runner, "_executable", lambda _tool: "/usr/bin/oras")
    monkeypatch.setattr(oci_runner, "_run_process", process)
    oci_runner.run("oras", argv, oci_runner.OciOptions())
    resolve.assert_not_called()
    assert process.call_args.args[0] == ["/usr/bin/oras", *argv]


def test_oras_logout_mutates_only_a_temporary_overlay(monkeypatch, tmp_path):
    source = tmp_path / "persistent.json"
    original = '{"auths":{"oci.test":{"auth":"original"}}}'
    source.write_text(original)
    route = oci_runner.OciRoute(
        kind="oci",
        registry_url="https://oci.test",
        registry_host="oci.test",
        native_root="oci.test/main/packages",
        accepted_roots=frozenset({"main/packages"}),
        package_token="temporary",
    )
    monkeypatch.setattr(oci_runner, "resolve_route", lambda *_: route)
    monkeypatch.setattr(oci_runner, "_executable", lambda _: "/usr/bin/oras")
    monkeypatch.setattr(oci_runner, "warn_if_old", lambda *_: None)

    def logout(argv, env):
        from pathlib import Path

        overlay = Path(argv[argv.index("--registry-config") + 1])
        assert overlay != source
        assert json.loads(overlay.read_text())["credHelpers"]["oci.test"] == "rvs"
        overlay.write_text("{}")

    monkeypatch.setattr(oci_runner, "_run_process", logout)
    oci_runner.run(
        "oras", ["logout", "oci.test", "--registry-config", str(source)], oci_runner.OciOptions()
    )
    assert source.read_text() == original


def test_oci_tag_list_rejects_a_malformed_digest_filter(transport):
    result = runner.invoke(app, ["art", "oci", "tag", "list", "images/api", "--digest", "latest"])
    assert result.exit_code == 1
    assert "manifest digest" in result.stderr
    transport.get.assert_not_called()


def test_oci_manifest_list_shows_recent_tags_and_the_tag_count(transport):
    transport.get.return_value.json.return_value = {
        "items": [
            {
                "path": "images/api",
                "digest": DIGEST,
                "manifest_kind": "image",
                "recent_tags": ["v3", "v2"],
                "tag_count": 12,
            }
        ],
        "next_cursor": None,
    }
    result = runner.invoke(
        app, ["art", "oci", "manifest", "list", "images/api"], env={"COLUMNS": "300"}
    )
    assert result.exit_code == 0, result.output
    assert "recent_tags" in result.stdout
    assert "v3, v2" in result.stdout
    assert "12" in result.stdout
