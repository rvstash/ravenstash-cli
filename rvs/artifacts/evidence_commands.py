"""Package-evidence staging, upload, status, and document export commands."""

import base64
import datetime
import hashlib
import json
import os
import pathlib
import stat
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, cast

import click
import httpx2
import typer
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from .. import config as cfg_mod
from .. import output
from ..account.commands import ensure_active_account, resolve_account
from ..api import artifacts_path, collection_all, collection_page, segment
from ..client import ApiClient, ApiError
from .targets import resolve_target


if TYPE_CHECKING:
    from collections.abc import Sequence


app = typer.Typer(
    help="Stage lockfiles and SBOMs beside native package publication.",
    no_args_is_help=True,
)

_EVIDENCE_TYPES = {"cyclonedx", "pypi_lock", "npm_lock", "maven_dependency_graph"}
# Intent states are an open set; wait only while the intent is known to progress.
_PENDING_STATES = {
    "prepared",
    "uploading",
    "evidence_ready",
    "partially_bound",
    "subject_bound",
    "processing",
}


@dataclass(frozen=True, slots=True)
class FileDigest:
    path: pathlib.Path
    filename: str
    size: int
    sha256: str
    content_md5: str
    device: int
    inode: int
    modified_ns: int


def _hash_file(path: pathlib.Path, *, max_bytes: int) -> FileDigest:
    try:
        before = path.lstat()
    except OSError as exc:
        output.fatal(f"Cannot read {path}: {exc}")
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        output.fatal(f"{path} must be a regular file, not a symlink.")
    if before.st_size < 1 or before.st_size > max_bytes:
        output.fatal(f"{path} is outside the allowed size range.")
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
            ):
                output.fatal(f"{path} changed before it could be hashed.")
            while chunk := stream.read(1024 * 1024):
                sha256.update(chunk)
                md5.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        output.fatal(f"Cannot read {path}: {exc}")
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    ):
        output.fatal(f"{path} changed while it was being hashed.")
    return FileDigest(
        path=path,
        filename=path.name,
        size=after.st_size,
        sha256=sha256.hexdigest(),
        content_md5=base64.b64encode(md5.digest()).decode(),
        device=after.st_dev,
        inode=after.st_ino,
        modified_ns=after.st_mtime_ns,
    )


def _require_unchanged(item: FileDigest) -> None:
    try:
        current = item.path.lstat()
    except OSError as exc:
        output.fatal(f"Cannot read {item.path}: {exc}")
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        output.fatal(f"{item.path} must remain a regular file, not a symlink.")
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
        item.device,
        item.inode,
        item.size,
        item.modified_ns,
    ):
        output.fatal(f"{item.path} changed after it was hashed; stage it again.")


def _validate_distribution_coordinates(
    files: Sequence[FileDigest], *, format: str, package: str, version: str
) -> None:
    """Reject a staged release whose native filenames contradict its coordinate."""

    if not files:
        output.fatal("At least one distribution artifact is required.")
    if format == "pypi":
        try:
            expected_name = canonicalize_name(package)
            expected_version = Version(version)
        except InvalidVersion:
            output.fatal(f"{version!r} is not a valid Python package version.")
        for item in files:
            try:
                if item.filename.lower().endswith(".whl"):
                    found_name, found_version, _build, _tags = parse_wheel_filename(item.filename)
                else:
                    found_name, found_version = parse_sdist_filename(item.filename)
            except InvalidSdistFilename, InvalidWheelFilename:
                output.fatal(f"{item.filename} is not a recognized Python wheel or source archive.")
            if found_name != expected_name or found_version != expected_version:
                output.fatal(
                    f"{item.filename} declares {found_name} {found_version}, not "
                    f"{expected_name} {expected_version}."
                )
        return
    if format == "npm":
        expected_stem = f"{package.removeprefix('@').replace('/', '-')}-{version}"
        for item in files:
            if not item.filename.endswith(".tgz") or item.filename[:-4] != expected_stem:
                output.fatal(f"{item.filename} does not match npm package {package} {version}.")
        return
    artifact_id = package.rsplit(":", maxsplit=1)[-1]
    expected_prefix = f"{artifact_id}-{version}"
    for item in files:
        remainder = item.filename.removeprefix(expected_prefix)
        if remainder == item.filename or remainder[:1] not in {"-", "."}:
            output.fatal(f"{item.filename} does not match Maven artifact {package} {version}.")


def _parse_evidence(values: list[str]) -> list[tuple[str, FileDigest]]:
    result: list[tuple[str, FileDigest]] = []
    for value in values:
        evidence_type, separator, raw_path = value.partition("=")
        if separator != "=" or evidence_type not in _EVIDENCE_TYPES or not raw_path:
            output.fatal(
                "Evidence must be TYPE=PATH where TYPE is cyclonedx, pypi_lock, "
                "npm_lock, or maven_dependency_graph."
            )
        result.append((evidence_type, _hash_file(pathlib.Path(raw_path), max_bytes=50 * 1024**2)))
    if not result:
        output.fatal("At least one --evidence TYPE=PATH is required.")
    return result


def _effective_account(profile: str | None, account: str | None) -> tuple[str, str]:
    profile_name = profile or cfg_mod.current_profile_name()
    account_ref = (
        str(resolve_account(account, profile_name)["ref"]) if account is not None else None
    )
    profile_name, selected = ensure_active_account(profile_name, account_ref)
    return profile_name, selected.account_ref


def _repository_target(
    *, target: str, format: str | None, profile: str | None, account: str | None
) -> tuple[ApiClient, str]:
    """Resolve a friendly or ID-based target to its immutable repository reference."""
    if target.startswith("ar_"):
        target = f"in/{target}"
    profile_name, account_ref = _effective_account(profile, account)
    _, _, selected = resolve_target(
        target,
        profile=profile_name,
        account_ref=account_ref,
        kind=format,
    )
    if selected.target_type != "repository" or selected.repository_ref is None:
        output.fatal("Package evidence requires a writable private repository target.")
    return ApiClient.from_profile(profile_name), selected.repository_ref


def _evidence_path(path: str) -> str:
    return artifacts_path(f"package-evidence/{path}")


def _intent_path(intent_ref: object, suffix: str = "") -> str:
    return _evidence_path(f"intents/{segment(str(intent_ref))}{suffix}")


def _intent_payload(
    *,
    repository_ref: str,
    format: str,
    package: str,
    version: str,
    scope: str,
    analysis_context: str,
    artifacts: Sequence[FileDigest | dict[str, object]],
    evidence: list[tuple[str, FileDigest]],
    idempotency_key: str | None,
) -> dict[str, object]:
    manifest = [
        (
            {
                "sha256_digest": item.sha256,
                "size_bytes": item.size,
                "filename": item.filename,
            }
            if isinstance(item, FileDigest)
            else item
        )
        for item in artifacts
    ]
    if scope == "artifact" and len(manifest) != 1:
        output.fatal("Artifact scope requires exactly one distribution artifact.")
    return {
        "repository_ref": repository_ref,
        "format": format,
        "package_name": package,
        "version": version,
        "scope": scope,
        "analysis_context": analysis_context,
        "artifact_manifest": manifest,
        "evidence": [
            {
                "evidence_type": evidence_type,
                "filename": item.filename,
                "size_bytes": item.size,
                "sha256_digest": item.sha256,
            }
            for evidence_type, item in evidence
        ],
        "deadline_at": (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=24)
        ).isoformat(),
        "idempotency_key": idempotency_key or f"rvs-evidence-{uuid.uuid4()}",
    }


def _upload_failure(exc: Exception) -> str:
    """Describe an upload failure without echoing the presigned request URL."""
    if isinstance(exc, httpx2.HTTPStatusError):
        return f"the storage service answered HTTP {exc.response.status_code}"
    if isinstance(exc, httpx2.HTTPError):
        # Transport errors can quote the request, whose query string is a credential.
        return f"{type(exc).__name__} while sending the file to the storage service"
    if isinstance(exc, (KeyError, TypeError)):
        return "Ravenstash returned an invalid upload request"
    return str(exc)


def _upload_evidence(
    client: ApiClient,
    intent: dict[str, object],
    evidence: list[tuple[str, FileDigest]],
) -> dict[str, object]:
    uploads = intent.get("evidence")
    if not isinstance(uploads, list) or len(uploads) != len(evidence):
        output.fatal("Ravenstash returned an invalid evidence manifest.")
    prepared = client.post(
        _intent_path(intent["ref"], "/uploads"),
        json={
            "uploads": [
                {
                    "upload_ref": upload["ref"],
                    "content_md5": local.content_md5,
                }
                for upload, (_evidence_type, local) in zip(uploads, evidence, strict=True)
            ]
        },
    ).json()
    requests = prepared.get("uploads")
    if not isinstance(requests, list) or len(requests) != len(evidence):
        output.fatal("Ravenstash returned invalid upload requests.")
    by_ref = {str(item["ref"]): local for item, (_, local) in zip(uploads, evidence, strict=True)}
    for prepared_upload in requests:
        request = prepared_upload.get("request")
        upload_ref = str(prepared_upload.get("ref"))
        local = by_ref.get(upload_ref)
        if not isinstance(request, dict) or local is None:
            output.fatal("Ravenstash returned an invalid upload request.")
        _require_unchanged(local)
        try:
            with (
                local.path.open("rb") as stream,
                httpx2.Client(timeout=httpx2.Timeout(60.0), follow_redirects=False) as http,
            ):
                response = http.request(
                    str(request.get("method") or "PUT"),
                    str(request["url"]),
                    headers=cast("dict[str, str]", request.get("headers") or {}),
                    content=stream,
                )
                response.raise_for_status()
        except (OSError, httpx2.HTTPError, KeyError, TypeError) as exc:
            output.fatal(f"Evidence upload failed for {local.filename}: {_upload_failure(exc)}")
        intent = client.post(
            _intent_path(intent["ref"], f"/uploads/{segment(upload_ref)}/complete"),
            json={"expected_size_bytes": local.size, "sha256_digest": local.sha256},
        ).json()
    return intent


def _show_intent(intent: dict[str, object]) -> None:
    if output.is_json():
        output.console.print(
            json.dumps(intent, sort_keys=True, default=str),
            markup=False,
            highlight=False,
        )
    else:
        output.kv(
            {
                "Evidence intent": str(intent.get("ref")),
                "State": str(intent.get("state")),
                "Repository": f"in/{intent.get('repository_ref')}",
                "Format": str(intent.get("format")),
                "Package": f"{intent.get('package_name')} {intent.get('version')}",
                "Scope": str(intent.get("scope")),
                "Analysis": str(intent.get("analysis_state")),
            }
        )


def _wait(client: ApiClient, intent: dict[str, object], timeout: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while str(intent.get("state")) in _PENDING_STATES:
        if time.monotonic() >= deadline:
            output.fatal("Timed out waiting for the durable evidence intent; check it with status.")
        time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
        intent = client.get(_intent_path(intent["ref"])).json()
    return intent


@app.command("stage")
def stage(
    dist_files: Annotated[list[pathlib.Path], typer.Argument(help="Distribution files")],
    target: Annotated[str, typer.Option("--target")],
    package: Annotated[str, typer.Option("--package")],
    version: Annotated[str, typer.Option("--version")],
    format: Annotated[Literal["pypi", "npm", "maven"], typer.Option("--format")],
    scope: Annotated[Literal["artifact", "release"], typer.Option("--scope")] = "release",
    evidence: Annotated[list[str] | None, typer.Option("--evidence")] = None,
    analysis_context: Annotated[str, typer.Option("--analysis-context")] = "",
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    wait: Annotated[bool, typer.Option("--wait")] = False,
    wait_timeout: Annotated[float, typer.Option("--wait-timeout", min=1, max=3600)] = 300,
    account: Annotated[str | None, typer.Option("--account")] = None,
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
) -> None:
    """Stage evidence before publishing distribution files through the native route."""
    if not analysis_context:
        output.fatal("--analysis-context is required.")
    client, repository_ref = _repository_target(
        target=target, format=format, profile=profile, account=account
    )
    artifact_files = [_hash_file(path, max_bytes=10 * 1024**3) for path in dist_files]
    _validate_distribution_coordinates(
        artifact_files,
        format=format,
        package=package,
        version=version,
    )
    evidence_files = _parse_evidence(evidence or [])
    payload = _intent_payload(
        repository_ref=repository_ref,
        format=format,
        package=package,
        version=version,
        scope=scope,
        analysis_context=analysis_context,
        artifacts=artifact_files,
        evidence=evidence_files,
        idempotency_key=idempotency_key,
    )
    try:
        intent = client.post(_evidence_path("intents"), json=payload).json()
        intent = _upload_evidence(client, intent, evidence_files)
        if wait:
            intent = _wait(client, intent, wait_timeout)
    except (ApiError, httpx2.HTTPError, KeyError, TypeError, ValueError) as exc:
        output.fatal(str(exc))
    _show_intent(intent)
    if not output.is_json():
        output.info("Evidence is staged; publish the package with its normal native command.")


@app.command("upload")
def upload(
    evidence_file: Annotated[pathlib.Path, typer.Argument()],
    target: Annotated[str, typer.Option("--target")],
    format: Annotated[Literal["pypi", "npm", "maven"], typer.Option("--format")],
    package: Annotated[str, typer.Option("--package")],
    version: Annotated[str, typer.Option("--version")],
    artifact_sha256: Annotated[list[str], typer.Option("--artifact-sha256")],
    evidence_type: Annotated[
        Literal["cyclonedx", "pypi_lock", "npm_lock", "maven_dependency_graph"],
        typer.Option("--type"),
    ],
    analysis_context: Annotated[str, typer.Option("--analysis-context")],
    scope: Annotated[Literal["artifact", "release"], typer.Option("--scope")] = "artifact",
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    wait: Annotated[bool, typer.Option("--wait")] = False,
    wait_timeout: Annotated[float, typer.Option("--wait-timeout", min=1, max=3600)] = 300,
    account: Annotated[str | None, typer.Option("--account")] = None,
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
) -> None:
    """Attach evidence to explicitly selected artifacts of an existing release."""
    client, repository_ref = _repository_target(
        target=target, format=format, profile=profile, account=account
    )
    evidence_digest = _hash_file(evidence_file, max_bytes=50 * 1024**2)
    try:
        candidates = collection_all(
            client,
            _evidence_path("files"),
            {
                "repository_ref": repository_ref,
                "format": format,
                "package_name": package,
                "version": version,
            },
        )
        by_digest = {item["digests"]["sha256"]: item for item in candidates}
        selected = [by_digest[digest] for digest in artifact_sha256]
        if not selected:
            output.fatal("At least one --artifact-sha256 selector is required.")
        if len(set(artifact_sha256)) != len(artifact_sha256):
            output.fatal("Artifact digest selectors must be unique.")
        if any(not item.get("targetable") for item in selected):
            output.fatal("One selected artifact is a non-targetable sidecar.")
        payload = _intent_payload(
            repository_ref=repository_ref,
            format=format,
            package=package,
            version=version,
            scope=scope,
            analysis_context=analysis_context,
            artifacts=[
                {
                    "sha256_digest": item["digests"]["sha256"],
                    "size_bytes": item["size_bytes"],
                    "filename": item["filename"],
                }
                for item in selected
            ],
            evidence=[(evidence_type, evidence_digest)],
            idempotency_key=idempotency_key,
        )
        intent = client.post(_evidence_path("intents"), json=payload).json()
        intent = _upload_evidence(client, intent, [(evidence_type, evidence_digest)])
        if wait:
            intent = _wait(client, intent, wait_timeout)
    except KeyError:
        output.fatal("An artifact digest was not found in the selected package release.")
    except (ApiError, httpx2.HTTPError, TypeError, ValueError) as exc:
        output.fatal(str(exc))
    _show_intent(intent)


@app.command("status")
def status(
    evidence_intent_ref: Annotated[str, typer.Argument()],
    wait: Annotated[bool, typer.Option("--wait")] = False,
    wait_timeout: Annotated[float, typer.Option("--wait-timeout", min=1, max=3600)] = 300,
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
) -> None:
    """Show the durable binding and analysis state for one evidence intent."""
    client = ApiClient.from_profile(profile)
    try:
        intent = client.get(_intent_path(evidence_intent_ref)).json()
        if wait:
            intent = _wait(client, intent, wait_timeout)
    except (ApiError, httpx2.HTTPError, TypeError, ValueError) as exc:
        output.fatal(str(exc))
    _show_intent(intent)


@app.command("list")
def list_intents(
    target: Annotated[str, typer.Option("--target")],
    format: Annotated[
        Literal["pypi", "npm", "maven"] | None,
        typer.Option("--format", help="Require this enabled format on the target."),
    ] = None,
    account: Annotated[str | None, typer.Option("--account")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 50,
    cursor: Annotated[str | None, typer.Option("--cursor")] = None,
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
) -> None:
    """List one cursor-paginated page of evidence intents for a repository.

    Intents of every package format in the repository are listed.
    """
    client, repository_ref = _repository_target(
        target=target, format=format, profile=profile, account=account
    )
    try:
        payload = client.get(
            _evidence_path("intents"),
            params={
                key: value
                for key, value in {
                    "repository_ref": repository_ref,
                    "limit": limit,
                    "cursor": cursor,
                }.items()
                if value is not None
            },
        ).json()
        items, next_cursor = collection_page(payload)
    except (ApiError, httpx2.HTTPError, TypeError, ValueError) as exc:
        output.fatal(str(exc))
    if output.is_json():
        click.echo(json.dumps(payload))
        return
    output.table(
        ["Intent", "Format", "Package", "Version", "Scope", "State", "Analysis"],
        [
            [
                str(item.get("ref", "")),
                str(item.get("format", "")),
                str(item.get("package_name", "")),
                str(item.get("version", "")),
                str(item.get("scope", "")),
                str(item.get("state", "")),
                str(item.get("analysis_state", "")),
            ]
            for item in items
        ],
        title="Package evidence",
        json_keys=[
            "ref",
            "format",
            "package_name",
            "version",
            "scope",
            "state",
            "analysis_state",
        ],
    )
    if next_cursor:
        click.echo(f"Next page: --cursor {next_cursor}", err=True)


@app.command("retire")
def retire(
    evidence_intent_ref: Annotated[str, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes", help="Confirm evidence retirement.")] = False,
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
) -> None:
    """Retire an intent's active evidence without deleting package artifacts."""
    if not yes:
        if output.is_json():
            output.fatal("Evidence retirement requires confirmation. Pass --yes.")
        typer.confirm(
            f"Retire evidence intent {evidence_intent_ref}? Package artifacts remain unchanged.",
            abort=True,
            err=True,
        )
    try:
        intent = (
            ApiClient.from_profile(profile)
            .post(_intent_path(evidence_intent_ref, "/retire"))
            .json()
        )
    except (ApiError, httpx2.HTTPError, TypeError, ValueError) as exc:
        output.fatal(str(exc))
    _show_intent(intent)


def _download_document(
    *,
    artifact_ref: str,
    kind: str,
    destination: pathlib.Path | None,
    profile: str | None,
    analysis_context: str | None = None,
) -> None:
    client = ApiClient.from_profile(profile)
    params = {"analysis_context": analysis_context} if analysis_context else None
    try:
        response = client.get(
            _evidence_path(f"files/{segment(artifact_ref)}/{kind}"), params=params
        )
    except (ApiError, httpx2.HTTPError) as exc:
        output.fatal(str(exc))
    expected = response.headers.get("etag", "").strip('"')
    digest = hashlib.sha256(response.content).hexdigest()
    if kind == "sbom" and expected:
        try:
            document = response.json()
            properties = document["metadata"]["properties"]
            snapshot_digest = next(
                item["value"]
                for item in properties
                if item.get("name") == "ravenstash:sbom:snapshot-digest"
            )
        except KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError:
            output.fatal("Ravenstash returned an SBOM without its snapshot digest.")
        if expected != f"sha256:{snapshot_digest}":
            output.fatal("Ravenstash SBOM snapshot digest verification failed.")
    elif expected and expected != f"sha256:{digest}":
        output.fatal("Ravenstash document digest verification failed.")
    if destination is None:
        if output.is_json():
            output.fatal("Document bytes cannot be mixed with --json; pass --output.")
        import sys

        sys.stdout.buffer.write(response.content)
        return
    try:
        destination.write_bytes(response.content)
    except OSError as exc:
        output.fatal(f"Cannot write {destination}: {exc}")
    if output.is_json():
        output.console.print(
            json.dumps({"output": str(destination), "sha256_digest": digest}),
            markup=False,
            highlight=False,
        )
    else:
        output.success(f"Wrote {destination} (sha256:{digest}).")


@app.command("sbom")
def sbom(
    artifact_ref: Annotated[str, typer.Argument()],
    destination: Annotated[pathlib.Path | None, typer.Option("--output", "-o")] = None,
    analysis_context: Annotated[str, typer.Option("--analysis-context")] = "observed",
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
) -> None:
    """Download and verify the CycloneDX document for one exact artifact."""
    _download_document(
        artifact_ref=artifact_ref,
        kind="sbom",
        destination=destination,
        profile=profile,
        analysis_context=analysis_context,
    )


@app.command("report")
def report(
    artifact_ref: Annotated[str, typer.Argument()],
    destination: Annotated[pathlib.Path | None, typer.Option("--output", "-o")] = None,
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
) -> None:
    """Download and verify the latest sanitized security report for an artifact."""
    _download_document(
        artifact_ref=artifact_ref,
        kind="report",
        destination=destination,
        profile=profile,
    )
