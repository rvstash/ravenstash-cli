"""Public native setup is side-effect-free and has predictable tool intent."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from rvs import config, output
from rvs.artifacts import auth_commands, primitives
from rvs.artifacts.discovery import Discovery
from rvs.artifacts.native_config import render
from rvs.cli import app
from rvs.oci.reference import qualify_reference
from typer.testing import CliRunner


runner = CliRunner()


@pytest.fixture
def found(monkeypatch):
    profile = config.ProfileConfig()
    monkeypatch.setattr(config, "load", lambda: Mock(active_profile=lambda *_: profile))
    target = config.ArtifactTarget(
        target_type="repository",
        account_ref="ac_23456789",
        stable_selector="in/ar_abcdefgh",
        display_selector="space/packages",
        namespace_ref="in_abcdefgh",
        repository_ref="ar_abcdefgh",
        namespace_name_cache="Space",
        repository_name_cache="Packages",
    )
    found = Discovery(
        "default",
        target,
        ("pypi", "npm", "maven", "oci"),
        "in/ar_abcdefgh",
    )
    monkeypatch.setattr(primitives, "discover", lambda *args: found)
    monkeypatch.setattr(config, "save", lambda *_: pytest.fail("template wrote configuration"))
    monkeypatch.setattr(
        auth_commands.ApiClient, "issue_native", lambda *_: pytest.fail("template minted a secret")
    )
    yield found
    output.set_json(False)


@pytest.mark.parametrize(
    "tool,formats",
    [
        ("pip", ["pypi"]),
        ("twine", ["pypi"]),
        ("uv", ["pypi"]),
        ("npm", ["npm"]),
        ("pnpm", ["npm"]),
        ("mvn", ["maven"]),
        ("maven", ["maven"]),
        ("docker", ["oci"]),
        ("helm", ["oci"]),
        ("oras", ["oci"]),
    ],
)
def test_templates_do_not_issue_credentials_or_write_config(found, tool, formats):
    result = runner.invoke(app, ["--json", "art", "native", "config", tool])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["formats"] == formats
    assert payload["access"] == ("read" if tool == "pip" else "publish")
    assert payload["snippets"]
    text = "\n".join(item["text"] for item in payload["snippets"])
    assert "rvs art token mint" in text
    assert "--access admin" not in text
    assert "rvs art endpoint" in text
    if tool == "oras":
        assert "--format oci" in text
        assert text.count("--password-stdin") == 1
    if tool == "helm":
        assert "push api-1.2.0.tgz oci://oci.rvsta.sh/in/ar_abcdefgh/charts" in text


@pytest.mark.parametrize("tool", ["pip", "twine"])
@pytest.mark.parametrize("access", ["read", "publish", "admin"])
def test_fixed_intent_tools_reject_access(found, tool, access):
    with pytest.raises(ValueError, match="fixed"):
        render(found, tool, None, access)


@pytest.mark.parametrize("tool", ["uv", "npm", "pnpm", "mvn", "docker", "helm", "oras"])
def test_read_templates_exclude_publish_instructions(found, tool):
    result = render(found, tool, None, "read")
    text = "\n".join(item["text"] for item in result["snippets"])
    assert "--access read" in text
    assert "--access publish" not in text
    assert "push api" not in text and "npm publish" not in text and "deploy:deploy-file" not in text


def test_mirrors_default_to_read_and_never_downgrade_explicit_publish(found):
    found.target.target_type = "official_cache"
    object.__setattr__(found, "formats", ("pypi",))
    assert render(found, "uv", None, None)["access"] == "read"
    for tool in ("uv", "twine"):
        with pytest.raises(ValueError, match="read-only"):
            render(found, tool, None, "publish" if tool == "uv" else None)


def test_oras_only_prints_enabled_formats(found):
    object.__setattr__(found, "formats", ("oci", "pypi"))
    assert render(found, "oras", None, None)["formats"] == ["oci"]
    with pytest.raises(ValueError):
        render(found, "oras", "container", None)


@pytest.mark.parametrize("tool", ["helm", "oras"])
def test_http_oci_templates_preserve_local_transport(found, tool, monkeypatch):
    registries = replace(
        config.load().active_profile().native_registries,
        oci_registry_base_url="http://localhost:5080",
    )
    monkeypatch.setattr(
        config, "load", lambda: Mock(active_profile=lambda *_: Mock(native_registries=registries))
    )
    text = "\n".join(item["text"] for item in render(found, tool, None, None)["snippets"])
    assert "localhost:5080" in text
    assert "--password-stdin --plain-http" in text
    if tool == "oras":
        assert "--to-plain-http" in text
        assert "--from-plain-http" not in text
    else:
        assert "oci://localhost:5080/in/ar_abcdefgh/charts --plain-http" in text


@pytest.mark.parametrize(
    "operand",
    [
        "../x",
        "/x",
        "x/",
        "X",
        "x:",
        "x@sha256:abc",
        "x:tag@sha256:" + "a" * 64,
        "https://registry/x",
    ],
)
def test_reference_rejects_invalid_operand(operand):
    with pytest.raises(ValueError):
        qualify_reference("registry/ns/repo", operand)


@pytest.mark.parametrize(
    "operand", [None, "backend", "backend:latest", "charts/api:1.2.0", "backend@sha256:" + "a" * 64]
)
def test_reference_is_one_value_without_implicit_tag(found, operand):
    args = ["art", "reference", "--format", "oci"] + ([operand] if operand else [])
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert (
        result.stdout == "oci.rvsta.sh/space/packages" + ("/" + operand if operand else "") + "\n"
    )


def test_endpoint_prints_read_and_publish_addresses_by_default(found):
    assert runner.invoke(app, ["art", "endpoint"]).exit_code == 1
    result = runner.invoke(app, ["art", "endpoint", "--format", "pypi"])
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "Read: https://pypi.rvsta.sh/in/ar_abcdefgh/simple/\n"
        "Publish: https://push.pypi.rvsta.sh/in/ar_abcdefgh/\n"
    )


def test_endpoint_json_adds_publish_address_and_access_selects_one_raw_value(found):
    result = runner.invoke(app, ["--json", "art", "endpoint", "--format", "oci"])
    assert json.loads(result.stdout) == {
        "target": "space/packages",
        "format": "oci",
        "access": "read",
        "endpoint": "oci.rvsta.sh",
        "publish_endpoint": "oci.rvsta.sh",
    }
    result = runner.invoke(app, ["art", "endpoint", "--format", "pypi", "--access", "publish"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "https://push.pypi.rvsta.sh/in/ar_abcdefgh/\n"
    assert runner.invoke(app, ["art", "endpoint", "--json"]).exit_code == 2


def test_endpoint_omits_publish_address_for_read_only_mirror(found):
    found.target.target_type = "official_cache"
    object.__setattr__(found, "formats", ("pypi",))

    result = runner.invoke(app, ["--json", "art", "endpoint", "--format", "pypi"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "target": "space/packages",
        "format": "pypi",
        "access": "read",
        "endpoint": "https://mirror.pypi.rvsta.sh/in/ar_abcdefgh/simple/",
    }


@pytest.mark.parametrize("values", [[""], ["pypi,"], ["all"], ["Pypi"], ["rpm"]])
def test_token_format_parser_rejects_empty_unknown_and_all(values):
    with pytest.raises(ValueError):
        auth_commands.flatten_formats(values)


def test_token_format_parser_flattens_and_deduplicates():
    assert auth_commands.flatten_formats(["pypi, npm", "pypi", "oci"]) == ["pypi", "npm", "oci"]
