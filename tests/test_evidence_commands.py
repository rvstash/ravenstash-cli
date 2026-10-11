import hashlib
import json
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import ANY

import pytest
from click import unstyle
from rvs import output
from rvs.artifacts import evidence_commands
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def json(self) -> dict[str, object]:
        return self.payload


class _DocumentResponse:
    def __init__(self, payload: bytes, etag: str) -> None:
        self.content = payload
        self.headers = {"etag": etag}

    def json(self) -> dict[str, object]:
        import json

        return json.loads(self.content)


class _Client:
    def __init__(self) -> None:
        self.payload: dict[str, object] | None = None

    def post(self, path: str, *, json: dict[str, object]) -> _Response:
        assert path == "/v0/artifacts/package-evidence/intents"
        self.payload = json
        return _Response(
            {
                **json,
                "ref": "pe_23456789abcdefghijkmn",
                "state": "prepared",
                "analysis_state": "not_started",
                "bound_file_refs": [],
            }
        )


def test_hash_file_streams_and_records_identity(tmp_path: Path) -> None:
    path = tmp_path / "bom.cdx.json"
    path.write_bytes(b'{"bomFormat":"CycloneDX"}')

    result = evidence_commands._hash_file(path, max_bytes=1024)

    assert result.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.size == path.stat().st_size
    evidence_commands._require_unchanged(result)


def test_hash_file_rejects_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    link = tmp_path / "bom.cdx.json"
    link.symlink_to(source)

    with pytest.raises(SystemExit):
        evidence_commands._hash_file(link, max_bytes=1024)


def test_upload_identity_check_rejects_changed_file(tmp_path: Path) -> None:
    path = tmp_path / "bom.cdx.json"
    path.write_text("{}", encoding="utf-8")
    result = evidence_commands._hash_file(path, max_bytes=1024)
    path.write_text('{"changed":true}', encoding="utf-8")
    os.utime(path, ns=(result.modified_ns + 1, result.modified_ns + 1))

    with pytest.raises(SystemExit):
        evidence_commands._require_unchanged(result)


def test_stage_coordinate_validation_accepts_matching_python_release(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "demo_package-1.2.0-py3-none-any.whl"
    sdist = tmp_path / "demo_package-1.2.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")

    evidence_commands._validate_distribution_coordinates(
        [
            evidence_commands._hash_file(wheel, max_bytes=1024),
            evidence_commands._hash_file(sdist, max_bytes=1024),
        ],
        format="pypi",
        package="demo-package",
        version="1.2.0",
    )


def test_stage_coordinate_validation_rejects_mixed_python_release(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "other_package-1.2.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    with pytest.raises(SystemExit):
        evidence_commands._validate_distribution_coordinates(
            [evidence_commands._hash_file(wheel, max_bytes=1024)],
            format="pypi",
            package="demo-package",
            version="1.2.0",
        )


@pytest.mark.parametrize(
    ("format", "package", "version", "filename"),
    [
        ("npm", "@scope/demo-package", "2.0.0", "scope-demo-package-2.0.0.tgz"),
        ("maven", "org.example:demo", "3.1.0", "demo-3.1.0-sources.jar"),
    ],
)
def test_stage_coordinate_validation_accepts_native_filenames(
    tmp_path: Path,
    format: str,
    package: str,
    version: str,
    filename: str,
) -> None:
    artifact = tmp_path / filename
    artifact.write_bytes(b"artifact")

    evidence_commands._validate_distribution_coordinates(
        [evidence_commands._hash_file(artifact, max_bytes=1024)],
        format=format,
        package=package,
        version=version,
    )


def test_evidence_help_keeps_profile_and_analysis_context_distinct() -> None:
    result = runner.invoke(
        evidence_commands.app,
        ["stage", "--help"],
        color=True,
        env={"COLUMNS": "120"},
    )
    help_output = unstyle(result.output)

    assert result.exit_code == 0
    assert "--profile" in help_output
    assert "--analysis-context" in help_output
    assert "--analysis-profile" not in help_output


def test_stage_emits_stable_json_without_upload_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    distribution = tmp_path / "demo-1.0.0-py3-none-any.whl"
    distribution.write_bytes(b"wheel")
    evidence = tmp_path / "bom.cdx.json"
    evidence.write_text("{}", encoding="utf-8")
    client = _Client()
    monkeypatch.setattr(
        evidence_commands,
        "_repository_target",
        lambda **_kwargs: (client, "ar_23456789"),
    )

    async_uploads: list[Any] = []

    def fake_upload(
        _client: _Client,
        intent: dict[str, object],
        files: list[tuple[str, evidence_commands.FileDigest]],
    ) -> dict[str, object]:
        async_uploads.extend(files)
        return {**intent, "state": "evidence_ready"}

    monkeypatch.setattr(evidence_commands, "_upload_evidence", fake_upload)
    output.set_json(True)
    try:
        result = runner.invoke(
            evidence_commands.app,
            [
                "stage",
                str(distribution),
                "--target",
                "ar_23456789",
                "--format",
                "pypi",
                "--package",
                "demo",
                "--version",
                "1.0.0",
                "--evidence",
                f"cyclonedx={evidence}",
                "--analysis-context",
                "pypi-cpython313-linux-x86_64-base",
                "--profile",
                "work",
            ],
        )
    finally:
        output.set_json(False)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["ref"] == "pe_23456789abcdefghijkmn"
    assert "upload_request" not in result.output
    assert client.payload is not None
    assert client.payload["analysis_context"] == "pypi-cpython313-linux-x86_64-base"
    assert "analysis_context_id" not in client.payload
    assert "deadline_at" in client.payload
    assert "deadline" not in client.payload
    assert client.payload["repository_ref"] == "ar_23456789"
    assert client.payload["format"] == "pypi"
    assert client.payload["package_name"] == "demo"
    assert "target" not in client.payload
    assert "package" not in client.payload
    assert len(async_uploads) == 1


def test_list_forwards_cursor_and_preserves_page_in_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ListClient:
        params: dict[str, object] | None = None

        def get(self, path: str, *, params: dict[str, object]) -> _Response:
            assert path == "/v0/artifacts/package-evidence/intents"
            self.params = params
            return _Response(
                {
                    "items": [
                        {
                            "ref": "pe_23456789abcdefghijkmn",
                            "repository_ref": "ar_23456789",
                            "format": "pypi",
                            "package_name": "demo",
                            "version": "1.0.0",
                            "scope": "artifact",
                            "state": "active",
                            "analysis_state": "complete",
                        }
                    ],
                    "next_cursor": "next-page",
                }
            )

    client = ListClient()
    monkeypatch.setattr(
        evidence_commands,
        "_repository_target",
        lambda **_kwargs: (client, "ar_23456789"),
    )
    output.set_json(True)
    try:
        result = runner.invoke(
            evidence_commands.app,
            [
                "list",
                "--target",
                "ar_23456789",
                "--format",
                "pypi",
                "--limit",
                "25",
                "--cursor",
                "current-page",
            ],
        )
    finally:
        output.set_json(False)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["next_cursor"] == "next-page"
    assert client.params == {
        "repository_ref": "ar_23456789",
        "limit": 25,
        "cursor": "current-page",
    }


def test_sbom_download_verifies_semantic_snapshot_etag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import json

    snapshot = "a" * 64
    payload = json.dumps(
        {
            "metadata": {
                "timestamp": "2026-09-17T00:00:00Z",
                "properties": [
                    {
                        "name": "ravenstash:sbom:snapshot-digest",
                        "value": snapshot,
                    }
                ],
            },
        }
    ).encode()

    class DocumentClient:
        def get(self, _path: str, *, params: object = None) -> _DocumentResponse:
            return _DocumentResponse(payload, f'"sha256:{snapshot}"')

    monkeypatch.setattr(
        evidence_commands.ApiClient,
        "from_profile",
        lambda _profile: DocumentClient(),
    )
    destination = tmp_path / "bom.cdx.json"

    evidence_commands._download_document(
        artifact_ref="pa_23456789abcdefghijkmn",
        kind="sbom",
        destination=destination,
        profile="work",
        analysis_context="observed",
    )

    assert destination.read_bytes() == payload


def test_evidence_target_resolves_friendly_selector_to_repository_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from rvs import config as cfg_mod
    from rvs.artifacts import targets

    config_dir = tmp_path / ".rvs"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'schema_version = 1\ndefault_profile = "default"\n\n[profiles.default]\n'
        'account_ref = "ac_23456789"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(cfg_mod, "PROFILE_ENV_FILE", config_dir / "profiles.env")
    monkeypatch.setattr(
        evidence_commands,
        "_effective_account",
        lambda _profile, _account: ("default", "ac_23456789"),
    )
    calls: list[tuple[str, dict[str, object] | None]] = []

    class ResolveClient:
        def get(self, path: str, params: dict[str, object] | None = None) -> _Response:
            calls.append((path, params))
            return _Response(
                {
                    "ref": "ar_23456789",
                    "name": "packages",
                    "account": {"ref": "ac_23456789", "handle": "space", "type": "personal"},
                    "namespace": {"ref": "in_23456789", "name": "space", "realm": "internal"},
                    "formats": [{"format": "pypi"}],
                    "allowed_actions": ["content.read", "content.publish"],
                }
            )

    monkeypatch.setattr(targets.ApiClient, "from_profile", lambda *_args: ResolveClient())

    for selector in ("space/packages", "ar_23456789", "in/ar_23456789"):
        calls.clear()
        _client, repository_ref = evidence_commands._repository_target(
            target=selector, format="pypi", profile=None, account=None
        )
        assert repository_ref == "ar_23456789"
        expected_selector = "in/ar_23456789" if "ar_" in selector else selector
        assert calls[0][0] == "/v0/artifacts/repositories/resolve"
        assert calls[0][1] is not None
        assert calls[0][1]["selector"] == expected_selector
        assert calls[0][1]["format"] == "pypi"


def test_upload_resolves_release_artifacts_and_completes_uploads_by_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence = tmp_path / "bom.cdx.json"
    evidence.write_text("{}", encoding="utf-8")
    digest = "b" * 64
    calls: list[tuple[str, str, object]] = []

    class FlowClient:
        def get(self, path: str, params: dict[str, object] | None = None) -> _Response:
            calls.append(("GET", path, params))
            return _Response(
                {
                    "items": [
                        {
                            "ref": "af_23456789abcdefghijkmn",
                            "filename": "demo-1.0.0-py3-none-any.whl",
                            "size_bytes": 5,
                            "digests": {"sha256": digest, "md5": "c" * 32},
                            "artifact_type": "wheel",
                            "targetable": True,
                            "exclusion_reason": None,
                        }
                    ],
                    "next_cursor": None,
                }
            )

        def post(self, path: str, *, json: dict[str, object]) -> _Response:
            calls.append(("POST", path, json))
            intent = {
                "ref": "pe_23456789abcdefghijkmn",
                "state": "prepared",
                "repository_ref": "ar_23456789",
                "format": "pypi",
                "package_name": "demo",
                "version": "1.0.0",
                "evidence": [{"ref": "pu_23456789abcdefghijkmn", "filename": "bom.cdx.json"}],
            }
            if path.endswith("/uploads"):
                return _Response(
                    {
                        "uploads": [
                            {
                                "ref": "pu_23456789abcdefghijkmn",
                                "request": {
                                    "method": "PUT",
                                    "url": "https://uploads.example.test/x",
                                    "headers": {"Content-MD5": "ignored"},
                                    "expires_at": "2026-09-26T12:00:00Z",
                                },
                            }
                        ]
                    }
                )
            if path.endswith("/complete"):
                return _Response({**intent, "state": "evidence_ready"})
            return _Response(intent)

    class PutClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> PutClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def request(self, method: str, url: str, **kwargs: object) -> Any:
            calls.append((method, url, kwargs["headers"]))

            class _Ok:
                def raise_for_status(self) -> None:
                    return None

            return _Ok()

    monkeypatch.setattr(
        evidence_commands, "_repository_target", lambda **_kwargs: (FlowClient(), "ar_23456789")
    )
    monkeypatch.setattr(evidence_commands.httpx2, "Client", PutClient)

    result = runner.invoke(
        evidence_commands.app,
        [
            "upload",
            str(evidence),
            "--target",
            "space/packages",
            "--format",
            "pypi",
            "--package",
            "demo",
            "--version",
            "1.0.0",
            "--artifact-sha256",
            digest,
            "--type",
            "cyclonedx",
            "--analysis-context",
            "observed",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0] == (
        "GET",
        "/v0/artifacts/package-evidence/files",
        {
            "repository_ref": "ar_23456789",
            "format": "pypi",
            "package_name": "demo",
            "version": "1.0.0",
            "limit": 100,
        },
    )
    assert calls[1][:2] == ("POST", "/v0/artifacts/package-evidence/intents")
    create_body = calls[1][2]
    assert isinstance(create_body, dict)
    assert create_body["repository_ref"] == "ar_23456789"
    assert create_body["package_name"] == "demo"
    assert create_body["artifact_manifest"] == [
        {"sha256_digest": digest, "size_bytes": 5, "filename": "demo-1.0.0-py3-none-any.whl"}
    ]
    assert [call[:2] for call in calls[2:]] == [
        ("POST", "/v0/artifacts/package-evidence/intents/pe_23456789abcdefghijkmn/uploads"),
        ("PUT", "https://uploads.example.test/x"),
        (
            "POST",
            "/v0/artifacts/package-evidence/intents/pe_23456789abcdefghijkmn/uploads/"
            "pu_23456789abcdefghijkmn/complete",
        ),
    ]
    assert calls[2][2] == {
        "uploads": [{"upload_ref": "pu_23456789abcdefghijkmn", "content_md5": ANY}]
    }
    assert calls[4][2] == {"expected_size_bytes": 2, "sha256_digest": ANY}
    # The typed upload instruction supplies the method and headers.
    assert calls[3][2] == {"Content-MD5": "ignored"}
    assert "pe_23456789abcdefghijkmn" in result.output


def test_status_retire_and_documents_use_flat_evidence_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths: list[tuple[str, str]] = []
    intent = {"ref": "pe_23456789abcdefghijkmn", "state": "active"}

    class RefClient:
        def get(self, path: str, params: object = None) -> Any:
            paths.append(("GET", path))
            if path.endswith("/report"):
                return _DocumentResponse(b"{}", "")
            return _Response(intent)

        def post(self, path: str, json: object = None) -> _Response:
            assert json is None
            paths.append(("POST", path))
            return _Response({**intent, "state": "cancelled"})

    monkeypatch.setattr(evidence_commands.ApiClient, "from_profile", lambda _profile: RefClient())

    assert (
        runner.invoke(evidence_commands.app, ["status", "pe_23456789abcdefghijkmn"]).exit_code == 0
    )
    assert (
        runner.invoke(
            evidence_commands.app, ["retire", "pe_23456789abcdefghijkmn", "--yes"]
        ).exit_code
        == 0
    )
    report = tmp_path / "report.json"
    assert (
        runner.invoke(
            evidence_commands.app,
            ["report", "af_23456789abcdefghijkmn", "--output", str(report)],
        ).exit_code
        == 0
    )
    assert paths == [
        ("GET", "/v0/artifacts/package-evidence/intents/pe_23456789abcdefghijkmn"),
        ("POST", "/v0/artifacts/package-evidence/intents/pe_23456789abcdefghijkmn/retire"),
        ("GET", "/v0/artifacts/package-evidence/files/af_23456789abcdefghijkmn/report"),
    ]


def test_waiting_stops_at_a_state_it_does_not_know(monkeypatch) -> None:
    class _Client:
        def get(self, path: str) -> Any:
            raise AssertionError("an unknown state must not be polled")

    intent = {"ref": "ei_abcdefgh", "state": "archived"}

    assert evidence_commands._wait(_Client(), intent, timeout=60) == intent


def test_evidence_upload_failure_never_prints_the_presigned_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = tmp_path / "bom.cdx.json"
    evidence.write_text("{}", encoding="utf-8")
    presigned = "https://uploads.example.test/x?X-Amz-Signature=secret-signature"

    class FlowClient:
        def post(self, path: str, *, json: dict[str, object]) -> _Response:
            return _Response(
                {"uploads": [{"ref": "pu_23456789abcdefghijkmn", "request": {"url": presigned}}]}
            )

    class FailingPutClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> FailingPutClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def request(self, method: str, url: str, **kwargs: object) -> Any:
            request = evidence_commands.httpx2.Request(method, url)
            response = evidence_commands.httpx2.Response(403, request=request)
            raise evidence_commands.httpx2.HTTPStatusError(
                f"Client error '403 Forbidden' for url '{url}'",
                request=request,
                response=response,
            )

    monkeypatch.setattr(evidence_commands.httpx2, "Client", FailingPutClient)
    local = evidence_commands._hash_file(evidence, max_bytes=1024)
    intent: dict[str, object] = {
        "ref": "pe_23456789abcdefghijkmn",
        "evidence": [{"ref": "pu_23456789abcdefghijkmn"}],
    }

    with pytest.raises(SystemExit):
        evidence_commands._upload_evidence(
            FlowClient(),  # type: ignore[arg-type]
            intent,
            [("cyclonedx", local)],
        )

    printed = capsys.readouterr()
    assert "secret-signature" not in printed.out + printed.err
    assert "HTTP 403" in " ".join(printed.err.split())
    assert evidence_commands._upload_failure(
        evidence_commands.httpx2.ConnectError(f"failed to reach {presigned}")
    ) == ("ConnectError while sending the file to the storage service")


def test_evidence_upload_http_error_message_is_redacted() -> None:
    request = evidence_commands.httpx2.Request("PUT", "https://uploads.example.test/x?sig=secret")
    response = evidence_commands.httpx2.Response(403, request=request)
    error = evidence_commands.httpx2.HTTPStatusError(
        "boom sig=secret", request=request, response=response
    )

    message = evidence_commands._upload_failure(error)

    assert message == "the storage service answered HTTP 403"
    assert "secret" not in message
