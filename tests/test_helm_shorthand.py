import pytest
from rvs.oci import helm
from rvs.oci.runner import _operations_for


ROOT = "registry.example/in/ar_xyzabcde"


@pytest.mark.parametrize(
    "args,expected",
    [
        (["push", "api-1.2.3.tgz"], ["push", "api-1.2.3.tgz", f"oci://{ROOT}"]),
        (["push", "api.tgz", "team/backend"], ["push", "api.tgz", f"oci://{ROOT}/team/backend"]),
        (
            ["pull", "api", "team/api", "--version", "1.2.3"],
            ["pull", f"oci://{ROOT}/api", f"oci://{ROOT}/team/api", "--version", "1.2.3"],
        ),
        (
            ["install", "push", "team/api", "-fvalues.yaml", "--set", "image=push"],
            ["install", "push", f"oci://{ROOT}/team/api", "-fvalues.yaml", "--set", "image=push"],
        ),
        (
            ["-n", "production", "upgrade", "--install", "release", "api"],
            ["-n", "production", "upgrade", "--install", "release", f"oci://{ROOT}/api"],
        ),
        (
            ["install", "--generate-name", "api"],
            ["install", "--generate-name", f"oci://{ROOT}/api"],
        ),
        (["template", "api"], ["template", f"oci://{ROOT}/api"]),
        (
            ["template", "release", "api", "--dry-run=client", "--new-option=value"],
            ["template", "release", f"oci://{ROOT}/api", "--dry-run=client", "--new-option=value"],
        ),
        (
            ["show", "values", "--version=1.2.3", "--", "team/api"],
            ["show", "values", "--version=1.2.3", "--", f"oci://{ROOT}/team/api"],
        ),
        (["pull", "api@sha256:" + "a" * 64], ["pull", f"oci://{ROOT}/api@sha256:" + "a" * 64]),
    ],
)
def test_expands_only_chart_or_destination(args, expected):
    assert helm.expand(helm.parse(args), ROOT) == expected


@pytest.mark.parametrize(
    "chart",
    [
        "./api",
        "../api",
        "/tmp/api",
        "api-1.2.3.tgz",
        ".",
        "oci://public.example/charts/api",
        "https://example.com/api.tgz",
    ],
)
def test_preserves_explicit_chart_references(chart):
    args = ["install", "release", chart]
    assert helm.expand(helm.parse(args), ROOT) == args


def test_bare_names_are_private_even_when_local_directory_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "api").mkdir()
    assert helm.expand(helm.parse(["template", "api"]), ROOT)[-1] == f"oci://{ROOT}/api"


@pytest.mark.parametrize(
    "args",
    [
        ["pull", "api", "--repo", "https://example.com"],
        ["push", "api.tgz", "oci://public.example/charts"],
        ["push", "--help"],
        ["show", "--help"],
        ["version", "--short"],
        ["dependency", "update", "./api"],
    ],
)
def test_preserves_explicit_repository_and_other_commands(args):
    assert helm.expand(helm.parse(args), ROOT) == args


@pytest.mark.parametrize(
    "args",
    [
        ["push"],
        ["push", "api.tgz", "dir", "extra"],
        ["pull"],
        ["pull", "api:1.2.3"],
        ["pull", "team//api"],
        ["push", "api.tgz", "team@sha256:" + "a" * 64],
        ["upgrade", "api"],
        ["pull", "api", "--version"],
        ["install", "--unknown", "release", "api"],
    ],
)
def test_rejects_ambiguous_arguments(args):
    with pytest.raises(SystemExit):
        helm.expand(helm.parse(args), ROOT)


def test_registry_config_is_source_not_native_override(tmp_path):
    config = tmp_path / "config.json"
    invocation = helm.parse(["pull", "--registry-config", str(config), "api"])
    assert invocation.config == config
    assert helm.expand(invocation, ROOT) == ["pull", f"oci://{ROOT}/api"]


def test_release_and_flag_values_do_not_request_publish_permission():
    assert _operations_for("helm", ["install", "push", "api"]) == ("read",)
    assert _operations_for("helm", ["show", "values", "api", "--version", "push"]) == ("read",)
    assert _operations_for("helm", ["push", "--help"]) == ("read",)
