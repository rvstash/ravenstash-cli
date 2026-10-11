import io
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest
from rvs import config as cfg_mod
from rvs.cli import app
from rvs.oci import credential_helper
from rvs.oci import runner as oci_runner
from typer.testing import CliRunner


runner = CliRunner()


def test_friendly_oci_root_normalizes_display_case_without_changing_identity() -> None:
    assert oci_runner._friendly_oci_root("AcmeHQ", "images") == "acmehq/images"
    assert oci_runner._friendly_oci_root("Acme-HQ", "images") == "acme-hq/images"
    assert oci_runner._friendly_oci_root("Engineering", "Images") == "engineering/images"
    assert oci_runner._friendly_oci_root("Engineering", "IMAGES") == "engineering/images"
    assert oci_runner._stable_oci_root("ar_xyzabcde") == "in/ar_xyzabcde"


class _Response:
    def __init__(self, value: Any) -> None:
        self.value = value

    def json(self) -> Any:
        return self.value


EXPECTED_MINT_PATH = "/v0/artifacts/repositories/ar_xyzabcde/mint-token"
EXPECTED_TARGET = {
    "namespace_ref": "in_abcdefgh",
    "namespace_name": "main",
    "namespace_realm": "internal",
    "repository_ref": "ar_xyzabcde",
    "repository_name": "images",
}


class _Api:
    def __init__(self) -> None:
        self.mints: list[tuple[str, dict]] = []

    def get(self, path: str, params=None) -> _Response:
        assert path == "/v0/artifacts/repositories/resolve"
        if params.get("format") is None:
            assert "format" not in params
        else:
            assert params["format"] in {"oci"}
        if params["selector"].startswith(("in/", "ar_")):
            assert "account_ref" not in params
        else:
            assert params["account_ref"] == "ac_23456789"
        return _Response(
            {
                "ref": "ar_xyzabcde",
                "name": "images",
                "account": {"ref": "ac_23456789", "handle": "personal", "type": "personal"},
                "namespace": {"ref": "in_abcdefgh", "name": "main", "realm": "internal"},
                "formats": [
                    {"format": "oci"},
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
        self.mints.append((path, payload))
        return self.post(path, json=payload)

    def post(self, path: str, json=None) -> _Response:
        assert path == EXPECTED_MINT_PATH
        assert set(json) == {"formats", "operations", "duration_seconds", "expected_target"}
        assert json["formats"] == ["oci"]
        assert json["operations"] in (["read"], ["read", "publish"], ["read", "delete"])
        assert json["expected_target"] == EXPECTED_TARGET
        return _Response(
            {
                "access_token": "rvs_sltDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDA",
                "token_type": "Bearer",
                "expires_in": 14400,
                "account_ref": "ac_23456789",
                "target": dict(EXPECTED_TARGET),
                "formats": ["oci"],
                "operations": list(json["operations"]),
                "native_path": "/in/ar_xyzabcde",
            }
        )


def test_oci_commands_request_only_the_operations_they_need() -> None:
    assert oci_runner._operations_for("docker", ["pull", "image:tag"]) == ("read",)
    assert oci_runner._operations_for("docker", ["push", "image:tag"]) == ("read", "publish")
    assert oci_runner._operations_for("helm", ["show", "chart", "oci://chart"]) == ("read",)
    assert oci_runner._operations_for("helm", ["push", "chart.tgz"]) == ("read", "publish")
    assert oci_runner._operations_for("oras", ["copy", "source", "target"]) == (
        "read",
        "publish",
    )


def _setup(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / ".rvs"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    config_file.write_text(
        """
schema_version = 1
default_profile = "default"

[profiles.default]
api_url = "https://api.example.test"
account_ref = "ac_23456789"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    monkeypatch.setenv("RVS_TOKEN", "raw-control-token")
    monkeypatch.setattr(
        oci_runner.ApiClient, "from_profile", staticmethod(lambda profile=None: _Api())
    )
    monkeypatch.setattr(
        oci_runner.tools,
        "require",
        lambda name, *, install_kind: f"/usr/bin/{name}",
    )


@pytest.mark.parametrize(
    "tool,args",
    [
        ("docker", ["push", "oci.rvsta.sh/main/images/backend:latest"]),
        ("helm", ["push", "chart.tgz", "oci://oci.rvsta.sh/main/images"]),
        (
            "oras",
            [
                "push",
                "oci.rvsta.sh/main/images/backend:latest",
                "demo.txt",
            ],
        ),
    ],
)
def test_oci_publish_decline_does_not_launch(monkeypatch, tmp_path: Path, tool, args) -> None:
    _setup(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(oci_runner.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))
    result = runner.invoke(app, [tool, "--rvs-target", "main/images", *args], input="\n")
    assert result.exit_code != 0
    assert "Publish to repo:main/images (user:personal)" in result.output
    assert not calls


@pytest.mark.parametrize("root", ["main/images", "Main/Images"])
def test_docker_uses_exact_ephemeral_helper_without_secret_in_argv(
    monkeypatch, tmp_path: Path, root
) -> None:
    _setup(monkeypatch, tmp_path)
    captured: dict[str, Any] = {}

    class FakeProcess:
        def __init__(self, cmd, *, env):
            captured["cmd"] = cmd
            captured["env"] = env.copy()
            assert "RVS_TOKEN" not in env
            broker = Path(env["RVS_OCI_CREDENTIAL_FILE"])
            config = Path(env["DOCKER_CONFIG"]) / "config.json"
            if os.name != "nt":
                assert broker.stat().st_mode & 0o077 == 0
                assert config.stat().st_mode & 0o077 == 0
            assert json.loads(config.read_text())["credHelpers"] == {"oci.rvsta.sh": "rvs"}
            assert json.loads(broker.read_text())["username"] == "__token__"

        def poll(self):
            return None

        def send_signal(self, _signum):
            raise AssertionError("successful execution must not forward a signal")

        def wait(self):
            return 0

    monkeypatch.setattr(oci_runner.subprocess, "Popen", FakeProcess)
    result = runner.invoke(
        app,
        [
            "docker",
            "--rvs-target",
            root,
            "push",
            f"oci.rvsta.sh/{root}/backend:Latest",
        ],
        input="y\n",
    )

    assert result.exit_code == 0, result.output
    assert captured["cmd"] == [
        "/usr/bin/docker",
        "push",
        "oci.rvsta.sh/main/images/backend:Latest",
    ]
    assert "rvs_sltDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDA" not in " ".join(captured["cmd"])
    assert not Path(captured["env"]["RVS_OCI_CREDENTIAL_FILE"]).exists()


def test_namespace_target_accepts_current_friendly_native_root(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)

    class StablePathApi(_Api):
        def post(self, path: str, json=None) -> _Response:
            response = super().post(path, json)
            response.value["native_path"] = "/in/ar_xyzabcde"
            return response

    api = StablePathApi()
    monkeypatch.setattr(
        oci_runner.ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: api),
    )

    route = oci_runner.resolve_route(
        "docker",
        oci_runner.OciOptions(target="main/images"),
        ("read", "publish"),
    )

    assert route.native_root == "oci.rvsta.sh/in/ar_xyzabcde"
    assert route.accepted_roots == frozenset({"main/images", "in/ar_xyzabcde"})
    assert api.mints == [
        (
            "/v0/artifacts/repositories/ar_xyzabcde/mint-token",
            {
                "formats": ["oci"],
                "operations": ["read", "publish"],
                "duration_seconds": 14400,
                "expected_target": {
                    "namespace_ref": "in_abcdefgh",
                    "namespace_name": "main",
                    "namespace_realm": "internal",
                    "repository_ref": "ar_xyzabcde",
                    "repository_name": "images",
                },
            },
        )
    ]


def test_oci_route_rejects_a_token_minted_for_another_repository(
    monkeypatch, tmp_path: Path
) -> None:
    _setup(monkeypatch, tmp_path)

    class OtherRepositoryApi(_Api):
        def post(self, path: str, json=None) -> _Response:
            response = super().post(path, json)
            response.value["native_path"] = "/in/ar_23456789"
            response.value["target"] = response.value["target"] | {"repository_ref": "ar_23456789"}
            return response

    api = OtherRepositoryApi()
    monkeypatch.setattr(
        oci_runner.ApiClient, "from_profile", staticmethod(lambda profile=None: api)
    )

    with pytest.raises(SystemExit):
        oci_runner.resolve_route("docker", oci_runner.OciOptions(target="main/images"))


def test_oci_route_on_a_retired_api_fails_cleanly_on_stderr(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    import httpx2
    from rvs.client import ApiClient

    _setup(monkeypatch, tmp_path)

    class GoneTransport:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> GoneTransport:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def request(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
            return httpx2.Response(410, text="<html>Gone</html>")

    monkeypatch.setattr(httpx2, "Client", GoneTransport)
    monkeypatch.setattr(
        oci_runner.ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: ApiClient("https://api.example", "token")),
    )

    with pytest.raises(SystemExit) as exc_info:
        oci_runner.resolve_route("docker", oci_runner.OciOptions(target="main/images"))

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no longer supported by the Ravenstash API" in " ".join(captured.err.split())


def test_oci_read_route_mints_read_only_repository_token(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    api = _Api()
    monkeypatch.setattr(
        oci_runner.ApiClient, "from_profile", staticmethod(lambda profile=None: api)
    )

    oci_runner.resolve_route("docker", oci_runner.OciOptions(target="main/images"))

    assert api.mints == [
        (
            EXPECTED_MINT_PATH,
            {
                "formats": ["oci"],
                "operations": ["read"],
                "duration_seconds": 14400,
                "expected_target": EXPECTED_TARGET,
            },
        )
    ]


def test_docker_preserves_other_native_credentials_but_replaces_ravenstash(
    monkeypatch, tmp_path: Path
) -> None:
    _setup(monkeypatch, tmp_path)
    docker_config = tmp_path / "native-docker"
    docker_config.mkdir()
    (docker_config / "config.json").write_text(
        json.dumps(
            {
                "auths": {
                    "private.example.test": {"auth": "external-secret"},
                    "https://oci.rvsta.sh/v1/": {"auth": "stale-ravenstash-secret"},
                },
                "credHelpers": {
                    "helpers.example.test": "desktop",
                    "oci.rvsta.sh": "stale-helper",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(docker_config))
    captured: dict[str, Any] = {}

    class FakeProcess:
        def __init__(self, _cmd, *, env):
            generated = json.loads((Path(env["DOCKER_CONFIG"]) / "config.json").read_text())
            captured.update(generated)

        def poll(self):
            return None

        def wait(self):
            return 0

    monkeypatch.setattr(oci_runner.subprocess, "Popen", FakeProcess)

    result = runner.invoke(
        app,
        [
            "docker",
            "--rvs-target",
            "main/images",
            "pull",
            "private.example.test/base:1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["auths"] == {"private.example.test": {"auth": "external-secret"}}
    assert captured["credHelpers"] == {
        "helpers.example.test": "desktop",
        "oci.rvsta.sh": "rvs",
    }


@pytest.mark.skipif(os.name == "nt", reason="Windows os.kill(SIGTERM) terminates the test process")
def test_native_signal_is_forwarded_with_shell_exit_code_and_secret_cleanup(
    monkeypatch, tmp_path: Path
) -> None:
    _setup(monkeypatch, tmp_path)
    captured: dict[str, Any] = {"signals": []}

    class FakeProcess:
        def __init__(self, _cmd, *, env):
            captured["broker"] = Path(env["RVS_OCI_CREDENTIAL_FILE"])

        def poll(self):
            return None

        def send_signal(self, signum):
            captured["signals"].append(signum)

        def wait(self):
            os.kill(os.getpid(), signal.SIGTERM)
            return -signal.SIGTERM

    monkeypatch.setattr(oci_runner.subprocess, "Popen", FakeProcess)

    result = runner.invoke(
        app,
        [
            "docker",
            "--rvs-target",
            "main/images",
            "push",
            "oci.rvsta.sh/in/ar_xyzabcde/backend:latest",
            "--rvs-yes",
        ],
    )

    assert result.exit_code == 128 + signal.SIGTERM
    assert captured["signals"] == [signal.SIGTERM]
    assert not captured["broker"].exists()


def test_oras_manifest_delete_explains_the_trash_before_oras_confirms(
    monkeypatch, tmp_path: Path
) -> None:
    _setup(monkeypatch, tmp_path)
    launches = []
    monkeypatch.setattr(oci_runner, "_run_process", lambda cmd, env: launches.append(cmd))

    result = runner.invoke(
        app,
        [
            "oras",
            "--rvs-target",
            "main/images",
            "manifest",
            "delete",
            "oci.rvsta.sh/main/images/backend@sha256:" + "a" * 64,
        ],
    )

    assert result.exit_code == 0, result.output
    assert launches[0][1:3] == ["manifest", "delete"]
    # ORAS asks for confirmation itself and exits 0 when it is declined, so the
    # trash is explained before it runs rather than claimed afterwards.
    notice = " ".join(result.stderr.split())
    assert "moves it to the repository trash" in notice
    assert "Restore it in the web app before it is permanently deleted." in notice


def test_oras_pull_prints_no_trash_notice(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(oci_runner, "_run_process", lambda cmd, env: None)

    result = runner.invoke(
        app,
        ["oras", "--rvs-target", "main/images", "pull", "oci.rvsta.sh/main/images/a:one"],
    )

    assert result.exit_code == 0, result.output
    assert "trash" not in result.output


@pytest.mark.parametrize("other_root", ["in/ar_23456789", "Main/Other"])
def test_oras_rejects_second_ravenstash_target(monkeypatch, tmp_path: Path, other_root) -> None:
    _setup(monkeypatch, tmp_path)

    rejected = runner.invoke(
        app,
        [
            "oras",
            "--rvs-target",
            "main/images",
            "cp",
            "oci.rvsta.sh/main/images/a:one",
            f"oci.rvsta.sh/{other_root}/b:two",
        ],
    )
    assert rejected.exit_code != 0
    assert "another Ravenstash logical repository" in rejected.output


def test_credential_helper_is_exact_host_and_read_only(monkeypatch, tmp_path: Path, capsys) -> None:
    broker = tmp_path / "broker.json"
    broker.write_text(
        json.dumps(
            {
                "server": "oci.rvsta.sh",
                "username": "helm",
                "secret": "ephemeral-secret",
            }
        ),
        encoding="utf-8",
    )
    broker.chmod(0o600)
    monkeypatch.setenv("RVS_OCI_CREDENTIAL_FILE", str(broker))
    monkeypatch.setattr(sys, "argv", ["docker-credential-rvs", "get"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("https://oci.rvsta.sh"))

    credential_helper.main()

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"Username": "helm", "Secret": "ephemeral-secret"}


def test_oci_reference_prints_stable_root_without_v2(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)

    class StablePathApi(_Api):
        def post(self, path: str, json=None) -> _Response:
            response = super().post(path, json)
            response.value["native_path"] = "/in/ar_xyzabcde"
            return response

    monkeypatch.setattr(
        oci_runner.ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: StablePathApi()),
    )
    result = runner.invoke(
        app,
        [
            "art",
            "reference",
            "team/api:1.2.3",
            "--format",
            "oci",
            "--target",
            "main/charts",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == ("oci.rvsta.sh/main/images/team/api:1.2.3")
    assert "/v2/" not in result.output


@pytest.mark.parametrize("native_path", ["/in/../images", "/in/ar_xyzabcde?x=1", "", None])
def test_oci_capability_rejects_an_unsafe_native_path(
    monkeypatch, tmp_path: Path, native_path: str | None
) -> None:
    _setup(monkeypatch, tmp_path)

    class InvalidPathApi(_Api):
        def post(self, path: str, json=None) -> _Response:
            response = super().post(path, json)
            response.value["native_path"] = native_path
            return response

    monkeypatch.setattr(
        oci_runner.ApiClient,
        "from_profile",
        staticmethod(lambda profile=None: InvalidPathApi()),
    )

    result = runner.invoke(
        app,
        ["docker", "--rvs-target", "main/images", "pull", "backend:latest"],
    )

    assert result.exit_code != 0
    assert "invalid native path" in " ".join(result.output.split())


def test_oci_route_uses_an_opaque_native_path_as_is(monkeypatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)

    class OpaquePathApi(_Api):
        def post(self, path: str, json=None) -> _Response:
            response = super().post(path, json)
            response.value["native_path"] = "/r/v2/ar_xyzabcde"
            return response

    monkeypatch.setattr(
        oci_runner.ApiClient, "from_profile", staticmethod(lambda profile=None: OpaquePathApi())
    )

    route = oci_runner.resolve_route("docker", oci_runner.OciOptions(target="main/images"))

    assert route.native_root == "oci.rvsta.sh/r/v2/ar_xyzabcde"
    assert "r/v2/ar_xyzabcde" in route.accepted_roots
    # A reference below the opaque root is this target; another repository is not.
    oci_runner._assert_exact_targets(["oci.rvsta.sh/r/v2/ar_xyzabcde/api:1"], route)
    with pytest.raises(SystemExit):
        oci_runner._assert_exact_targets(["oci.rvsta.sh/in/ar_23456789/api:1"], route)


def test_package_and_mirror_commands_reject_oci_kinds() -> None:
    package = runner.invoke(
        app,
        [
            "art",
            "package",
            "list",
            "--target",
            "images",
            "--format",
            "oci",
        ],
    )
    remote = runner.invoke(
        app,
        ["art", "mirror", "create", "--format", "helm"],
    )

    assert package.exit_code != 0
    assert remote.exit_code != 0
    assert "Use rvs docker, rvs helm, or rvs oras" in package.output
    assert "--format pypi, npm, or maven" in remote.output


@pytest.mark.parametrize("command", [["push"], ["image", "push"]])
def test_short_push_tags_exact_source_before_push(monkeypatch, tmp_path, command):
    _setup(monkeypatch, tmp_path)
    identity = "sha256:" + "a" * 64
    inspections = []
    launches = []
    destination = "oci.rvsta.sh/in/ar_xyzabcde/team/api:RC1"

    def inspect(cmd, **kwargs):
        inspections.append(cmd)
        assert "RVS_TOKEN" not in kwargs["env"]
        if cmd[-1] == "team/api:RC1":
            return oci_runner.subprocess.CompletedProcess(cmd, 0, identity + "\n", "")
        assert cmd[-1] == destination
        return oci_runner.subprocess.CompletedProcess(
            cmd, 1, "", "Error: No such image: " + destination
        )

    monkeypatch.setattr(oci_runner.subprocess, "run", inspect)
    monkeypatch.setattr(oci_runner, "_run_process", lambda cmd, env: launches.append((cmd, env)))
    result = runner.invoke(
        app,
        [
            "docker",
            "--rvs-target",
            "main/images",
            "--context",
            "remote",
            *command,
            "--platform=linux/arm64",
            "team/api:RC1",
        ],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert len(inspections) == 2
    assert all(cmd[1:3] == ["--context", "remote"] for cmd in inspections)
    assert launches[0][0] == [
        "/usr/bin/docker",
        "--context",
        "remote",
        "image",
        "tag",
        "team/api:RC1",
        destination,
    ]
    assert launches[1][0] == [
        "/usr/bin/docker",
        "--context",
        "remote",
        *command,
        "--platform=linux/arm64",
        destination,
    ]
    assert destination in result.output
    assert not Path(launches[0][1]["RVS_OCI_CREDENTIAL_FILE"]).exists()


@pytest.mark.parametrize(
    "failure", ["collision", "missing-source", "daemon", "tag-failure", "push-failure", "decline"]
)
def test_short_push_failure_does_not_push_wrong_image(monkeypatch, tmp_path, failure):
    _setup(monkeypatch, tmp_path)
    launches = []
    inspections = []

    def inspect(cmd, **kwargs):
        inspections.append(cmd)
        if failure == "missing-source":
            return oci_runner.subprocess.CompletedProcess(cmd, 1, "", "No such image: api:latest")
        if len(inspections) == 2 and failure == "daemon":
            return oci_runner.subprocess.CompletedProcess(
                cmd, 1, "", "Cannot connect to Docker daemon"
            )
        letter = "b" if len(inspections) == 2 and failure == "collision" else "a"
        return oci_runner.subprocess.CompletedProcess(cmd, 0, "sha256:" + letter * 64, "")

    def launch(cmd, env):
        launches.append(cmd)
        if failure == "tag-failure" or (failure == "push-failure" and len(launches) == 2):
            import typer

            raise typer.Exit(17)

    monkeypatch.setattr(oci_runner.subprocess, "run", inspect)
    monkeypatch.setattr(oci_runner, "_run_process", launch)
    result = runner.invoke(
        app,
        ["docker", "--rvs-target", "main/images", "push", "api"],
        input="n\n" if failure == "decline" else "y\n",
    )
    assert result.exit_code != 0
    if failure in {"collision", "missing-source", "daemon", "decline"}:
        assert not launches
    if failure == "decline":
        assert not inspections
    if failure == "tag-failure":
        assert len(launches) == 1
        assert result.exit_code == 17
    if failure == "push-failure":
        assert len(launches) == 2
        assert "retained if push fails" in result.output
        assert result.exit_code == 17


def test_short_pull_uses_saved_target_without_local_alias(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    selected = runner.invoke(app, ["art", "select", "main/images"])
    assert selected.exit_code == 0, selected.output
    launches = []
    monkeypatch.setattr(oci_runner, "_run_process", lambda cmd, env: launches.append(cmd))
    monkeypatch.setattr(
        oci_runner.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("No local tagging on pull"),
    )
    result = runner.invoke(app, ["docker", "image", "pull", "--", "team/api"])
    assert result.exit_code == 0, result.output
    assert launches == [
        ["/usr/bin/docker", "image", "pull", "oci.rvsta.sh/in/ar_xyzabcde/team/api:latest"]
    ]


def test_short_tag_keeps_source_from_another_repo_local(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    source = "oci.rvsta.sh/other/repo/api:dev"
    identity = "sha256:" + "a" * 64
    inspections = []
    launches = []

    def inspect(cmd, **kwargs):
        inspections.append(cmd[-1])
        return oci_runner.subprocess.CompletedProcess(cmd, 0, identity, "")

    monkeypatch.setattr(oci_runner.subprocess, "run", inspect)
    monkeypatch.setattr(oci_runner, "_run_process", lambda cmd, env: launches.append(cmd))
    result = runner.invoke(
        app, ["docker", "--rvs-target", "main/images", "image", "tag", source, "api:release"]
    )
    assert result.exit_code == 0, result.output
    assert inspections[0] == source
    assert launches == [
        ["/usr/bin/docker", "image", "tag", source, "oci.rvsta.sh/in/ar_xyzabcde/api:release"]
    ]


def test_docker_explicit_config_preserves_context_plugins_and_buildx(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    native = tmp_path / "docker-config"
    native.mkdir()
    original = '{"currentContext":"remote","auths":{"docker.io":{"auth":"existing"}}}'
    (native / "config.json").write_text(original)
    for directory in ("contexts", "cli-plugins", "buildx"):
        (native / directory).mkdir()
        (native / directory / "sentinel").write_text(directory)
    monkeypatch.delenv("BUILDX_CONFIG", raising=False)
    seen = []

    def launch(cmd, env):
        seen.append(cmd)
        overlay = Path(env["DOCKER_CONFIG"])
        assert overlay != native
        data = json.loads((overlay / "config.json").read_text())
        assert data["currentContext"] == "remote"
        assert data["auths"]["docker.io"]["auth"] == "existing"
        assert data["credHelpers"]["oci.rvsta.sh"] == "rvs"
        for directory in ("contexts", "cli-plugins"):
            assert (overlay / directory / "sentinel").read_text() == directory
        assert env["BUILDX_CONFIG"] == str(native / "buildx")

    monkeypatch.setattr(oci_runner, "_run_process", launch)
    result = runner.invoke(
        app, ["docker", "--rvs-target", "main/images", f"--config={native}", "pull", "api"]
    )
    assert result.exit_code == 0, result.output
    assert seen == [["/usr/bin/docker", "pull", "oci.rvsta.sh/in/ar_xyzabcde/api:latest"]]
    assert (native / "config.json").read_text() == original


@pytest.mark.parametrize(
    "arguments,tail",
    [
        (
            ["pull", "api", "--version", "1.2.3"],
            ["pull", "oci://oci.rvsta.sh/in/ar_xyzabcde/api", "--version", "1.2.3"],
        ),
        (
            ["--rvs-yes", "push", "api.tgz"],
            ["push", "api.tgz", "oci://oci.rvsta.sh/in/ar_xyzabcde"],
        ),
        (
            ["--rvs-yes", "push", "--plain-http", "api.tgz"],
            ["push", "--plain-http", "api.tgz", "oci://oci.rvsta.sh/in/ar_xyzabcde"],
        ),
    ],
)
def test_helm_saved_target_shorthand_uses_temporary_config(monkeypatch, tmp_path, arguments, tail):
    _setup(monkeypatch, tmp_path)
    assert runner.invoke(app, ["art", "select", "main/images"]).exit_code == 0
    source = tmp_path / "helm.json"
    source.write_text('{"auths":{"public.example":{"auth":"public-auth"}}}')
    captured = []

    def run_process(argv, env):
        captured.append(argv)
        config_path = Path(env["HELM_REGISTRY_CONFIG"])
        assert config_path != source
        config = json.loads(config_path.read_text())
        assert config["auths"]["public.example"]["auth"] == "public-auth"
        assert config["credHelpers"]["oci.rvsta.sh"] == "rvs"
        assert "--registry-config" not in argv
        assert "rvs_sltDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDA" not in " ".join(argv)

    publish_arguments = []
    native_artifacts = oci_runner.oci_artifacts

    def artifacts(tool, argv, host):
        publish_arguments.append(argv)
        return native_artifacts(tool, argv, host)

    monkeypatch.setattr(oci_runner, "oci_artifacts", artifacts)
    monkeypatch.setattr(oci_runner, "_run_process", run_process)
    result = runner.invoke(app, ["helm", "--registry-config", str(source), *arguments])
    assert result.exit_code == 0, result.output
    assert captured == [["/usr/bin/helm", *tail]]
    if "push" in arguments:
        assert publish_arguments == [["push", "api.tgz", "oci://oci.rvsta.sh/in/ar_xyzabcde"]]
    assert source.read_text() == '{"auths":{"public.example":{"auth":"public-auth"}}}'
