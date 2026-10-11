import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import pytest
import release_signing
from rvs import portable_update as portable_update_mod
from rvs.identity import SIGNING_KEY_NAME, archive_name, checksums_name
from rvs.installations import (
    RECEIPT_NAME,
    Installation,
    compatibility_channel,
    detect_portable_installation,
    read_receipt,
    write_receipt,
)
from rvs.portable_update import (
    UpdateError,
    activate_posix,
    extract_release,
    latest_for_minor,
    newer,
)
from rvs.update_trust import VerificationError, _release_key, verify_detached


COMMANDS = ("rvs", "ravenstash", "docker-credential-rvs")


@pytest.fixture(autouse=True)
def _isolated_rvs_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the accepted-manifest state out of the developer's real ~/.rvs."""
    monkeypatch.setenv("RVS_HOME", str(tmp_path / "rvs-home"))


def _installation(tmp_path: Path, version: str = "0.15.3") -> Installation:
    return Installation(
        schema=1,
        method="portable",
        scope="user",
        version=version,
        channel=compatibility_channel(version),
        target="linux-musl-amd64",
        install_root=str(tmp_path / "root"),
        bin_directory=str(tmp_path / "bin"),
    )


def _launcher(path: Path, version: str) -> None:
    path.write_text(f"#!/bin/sh\nprintf 'Ravenstash CLI {version}\\n'\n", encoding="utf-8")
    path.chmod(0o755)


def test_release_key_is_the_pinned_4096_bit_rsa_key() -> None:
    assert _release_key().key_size == 4096


def test_detached_verification_rejects_non_signature_data() -> None:
    with pytest.raises(VerificationError, match=r"^invalid OpenPGP packet header$"):
        verify_detached(b"manifest", b"not a signature")


def test_detached_verification_accepts_signed_inventory_and_rejects_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release_signing.trust_test_key(monkeypatch, tmp_path)
    inventory = release_signing.inventory()
    signature = release_signing.armor(
        release_signing.detached_signature(inventory), block="SIGNATURE"
    )

    verify_detached(inventory, signature)
    with pytest.raises(
        VerificationError, match=r"^OpenPGP signature digest prefix does not match$"
    ):
        verify_detached(inventory + b"changed\n", signature)


def test_channel_discovery_authenticates_the_public_manifest(
    httpx2_mock: Any, monkeypatch: Any
) -> None:
    manifest = {
        "schema": 1,
        "recommended": "v0",
        "generated_at": "2026-09-27T05:00:00Z",
        "expires": "2999-01-01T00:00:00Z",
        "channels": {
            "v0": {
                "latest": "0.16.1",
                "minor_targets": {"0.15": "0.15.3", "0.16": "0.16.1"},
                "status": "supported",
            }
        },
    }
    httpx2_mock.add_response(url=portable_update_mod.CHANNELS_URL, json=manifest)
    httpx2_mock.add_response(url=f"{portable_update_mod.CHANNELS_URL}.gpg", content=b"signature")
    verified: list[tuple[bytes, bytes]] = []
    monkeypatch.setattr(
        portable_update_mod,
        "verify_detached",
        lambda subject, signature: verified.append((subject, signature)),
    )

    assert portable_update_mod.fetch_channel_manifest() == manifest
    assert verified and verified[0][1] == b"signature"
    assert latest_for_minor(manifest, "0.15") == "0.15.3"


@pytest.mark.parametrize(
    ("installed", "candidate", "expected"),
    [
        ("0.15.4rc1", "0.15.4rc2", True),
        ("0.15.4rc2", "0.15.4", True),
        ("0.15.3", "0.15.4", True),
        ("0.15.4", "0.15.3", False),
    ],
)
def test_portable_version_order(installed: str, candidate: str, expected: bool) -> None:
    assert newer(candidate, installed) is expected


def test_receipt_round_trip_and_running_executable_detection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    installation = _installation(tmp_path)
    executable = installation.root / installation.version / "rvs"
    executable.parent.mkdir(parents=True)
    executable.touch()
    receipt = executable.parent / RECEIPT_NAME
    write_receipt(receipt, installation)
    monkeypatch.setattr("rvs.installations.platform_target", lambda: installation.target)

    assert read_receipt(receipt) == installation
    assert (
        detect_portable_installation(executable=executable, installed_version=installation.version)
        == installation
    )


def test_receipt_rejects_root_install_path(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    invalid = Installation(**{**installation.__dict__, "install_root": "/"})

    with pytest.raises(ValueError, match="invalid install root"):
        write_receipt(tmp_path / "receipt.json", invalid)


def test_extract_release_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "ravenstash-cli-v0.15.4-linux-musl-amd64.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        content = b"bad"
        member = tarfile.TarInfo("ravenstash-cli-v0.15.4-linux-musl-amd64/../outside")
        member.size = len(content)
        bundle.addfile(member, io.BytesIO(content))

    with pytest.raises(UpdateError, match="unsafe path"):
        extract_release(archive, tmp_path / "extracted", "0.15.4", "linux-musl-amd64")


@pytest.mark.skipif(os.name == "nt", reason="POSIX activation uses executable symlinks")
def test_posix_activation_switches_all_commands_and_keeps_previous_version(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path)
    old = installation.root / installation.version
    old.mkdir(parents=True)
    for command in COMMANDS:
        _launcher(old / command, installation.version)
    installation.bin.mkdir(parents=True)
    for command in COMMANDS:
        (installation.bin / command).symlink_to(old / command)

    extracted = tmp_path / "new"
    extracted.mkdir()
    for command in COMMANDS:
        _launcher(extracted / command, "0.15.4")

    activate_posix(extracted, installation, "0.15.4")

    assert (installation.root / "current").readlink() == Path("0.15.4")
    assert old.is_dir()
    assert read_receipt(installation.root / "0.15.4" / RECEIPT_NAME).version == "0.15.4"
    for command in COMMANDS:
        assert (installation.bin / command).resolve() == installation.root / "0.15.4" / command


@pytest.mark.skipif(os.name == "nt", reason="POSIX activation uses executable symlinks")
def test_posix_activation_rolls_back_before_replacing_an_unowned_command(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path)
    old = installation.root / installation.version
    old.mkdir(parents=True)
    for command in COMMANDS:
        _launcher(old / command, installation.version)
    installation.bin.mkdir(parents=True)
    (installation.bin / "rvs").write_text("not installer owned\n", encoding="utf-8")
    (installation.bin / "ravenstash").symlink_to(old / "ravenstash")
    (installation.bin / "docker-credential-rvs").symlink_to(old / "docker-credential-rvs")

    extracted = tmp_path / "new"
    extracted.mkdir()
    for command in COMMANDS:
        _launcher(extracted / command, "0.15.4")

    with pytest.raises(UpdateError, match="non-symlink command"):
        activate_posix(extracted, installation, "0.15.4")

    assert (installation.bin / "rvs").read_text(encoding="utf-8") == "not installer owned\n"
    for alias in ("ravenstash", "docker-credential-rvs"):
        assert (installation.bin / alias).readlink() == old / alias
    assert not (installation.root / "current").exists()
    assert not (installation.root / "0.15.4").exists()


def test_receipt_is_non_secret_json(tmp_path: Path) -> None:
    receipt = tmp_path / RECEIPT_NAME
    write_receipt(receipt, _installation(tmp_path))

    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert set(payload) == {
        "bin_directory",
        "channel",
        "install_root",
        "method",
        "schema",
        "scope",
        "target",
        "version",
    }


def _timed_manifest(generated_at: str, expires: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "recommended": "v0",
        "generated_at": generated_at,
        "expires": expires,
        "channels": {
            "v0": {"latest": "0.16.1", "minor_targets": {"0.16": "0.16.1"}, "status": "supported"}
        },
    }


def test_channel_freshness_rejects_expired_and_regressing_manifests() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    check = portable_update_mod.check_manifest_freshness
    newest = _timed_manifest("2026-09-27T05:00:00Z", "2026-10-04T05:00:00Z")

    check(newest, now=now)
    # The same manifest is still acceptable, an older signed one is a replay.
    check(newest, now=now)
    with pytest.raises(UpdateError, match="older than one rvs already verified"):
        check(_timed_manifest("2026-09-26T05:00:00Z", "2026-10-03T05:00:00Z"), now=now)
    with pytest.raises(UpdateError, match="expired at"):
        check(
            _timed_manifest("2026-09-28T05:00:00Z", "2026-09-29T05:00:00Z"),
            now=datetime(2026, 9, 30, tzinfo=UTC),
        )
    check(_timed_manifest("2026-09-28T05:00:00Z", "2026-10-05T05:00:00Z"), now=now)
    with pytest.raises(UpdateError, match="older than one rvs already verified"):
        check(newest, now=now)


@pytest.mark.parametrize(
    "fields",
    [
        {"generated_at": "2026-09-27T05:00:00Z"},
        {"generated_at": "2026-09-27T05:00:00", "expires": "2026-10-04T05:00:00Z"},
        {"generated_at": "2026-09-27T05:00:00Z", "expires": "2026-09-27T05:00:00Z"},
        {"generated_at": 7, "expires": "2026-10-04T05:00:00Z"},
    ],
)
def test_channel_freshness_rejects_malformed_timestamps(fields: dict[str, Any]) -> None:
    from datetime import UTC, datetime

    manifest = {"schema": 1, "channels": {}, "recommended": "v0", **fields}

    with pytest.raises(UpdateError):
        portable_update_mod.check_manifest_freshness(
            manifest, now=datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
        )


def test_channel_freshness_requires_the_signed_validity_period() -> None:
    # Nothing was accepted before, so only the missing fields can be the reason.
    with pytest.raises(UpdateError, match=r"^the signed release-channel manifest has an invalid"):
        portable_update_mod.check_manifest_freshness(
            {"schema": 1, "channels": {}, "recommended": "v0"}
        )


def test_channel_discovery_rejects_a_manifest_without_a_validity_period(
    httpx2_mock: Any, monkeypatch: Any
) -> None:
    manifest = _signed_manifest()
    del manifest["generated_at"], manifest["expires"]
    httpx2_mock.add_response(url=portable_update_mod.CHANNELS_URL, json=manifest)
    httpx2_mock.add_response(url=f"{portable_update_mod.CHANNELS_URL}.gpg", content=b"signature")
    monkeypatch.setattr(portable_update_mod, "verify_detached", lambda *_args: None)

    with pytest.raises(UpdateError) as raised:
        portable_update_mod.fetch_channel_manifest()

    assert str(raised.value) == "the signed release-channel manifest has an invalid generated_at"


def test_channel_discovery_rejects_an_expired_signed_manifest(
    httpx2_mock: Any, monkeypatch: Any
) -> None:
    manifest = _timed_manifest("2020-01-01T00:00:00Z", "2020-01-08T00:00:00Z")
    httpx2_mock.add_response(url=portable_update_mod.CHANNELS_URL, json=manifest)
    httpx2_mock.add_response(url=f"{portable_update_mod.CHANNELS_URL}.gpg", content=b"signature")
    monkeypatch.setattr(portable_update_mod, "verify_detached", lambda *_args: None)

    with pytest.raises(UpdateError, match="expired at"):
        portable_update_mod.fetch_channel_manifest()


def _signed_manifest(**overrides: Any) -> dict[str, Any]:
    return {
        "schema": 1,
        "recommended": "v0",
        "generated_at": "2026-09-27T05:00:00Z",
        "expires": "2999-01-01T00:00:00Z",
        "channels": {
            "v0": {"latest": "0.16.1", "minor_targets": {"0.16": "0.16.1"}, "status": "supported"}
        },
        **overrides,
    }


def test_version_key_rejects_non_release_versions() -> None:
    with pytest.raises(UpdateError, match=r"^unsupported rvs version: 0\.16$"):
        newer("0.16", "0.15.3")


def test_github_requests_use_optional_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RVS_GITHUB_TOKEN", raising=False)
    assert portable_update_mod._headers() == {"Accept": "application/vnd.github+json"}

    monkeypatch.setenv("RVS_GITHUB_TOKEN", "github-token")
    assert portable_update_mod._headers()["Authorization"] == "Bearer github-token"


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (_signed_manifest(schema=2), "has an unsupported schema"),
        (_signed_manifest(recommended="v1"), "has no valid recommendation"),
        (
            _signed_manifest(channels={"stable": {}}, recommended="stable"),
            "contains an invalid channel",
        ),
        (
            _signed_manifest(
                channels={"v0": {"latest": "1.0.0", "minor_targets": {"1.0": "1.0.0"}}}
            ),
            "contains an invalid version",
        ),
        (
            _signed_manifest(channels={"v0": {"latest": "0.16.1", "minor_targets": {}}}),
            "has no minor targets",
        ),
        (
            _signed_manifest(
                channels={"v0": {"latest": "0.16.1", "minor_targets": {"0.16": "0.15.3"}}}
            ),
            "contains an invalid minor target",
        ),
    ],
)
def test_channel_discovery_rejects_inconsistent_signed_manifest(
    httpx2_mock: Any, monkeypatch: pytest.MonkeyPatch, manifest: dict[str, Any], message: str
) -> None:
    httpx2_mock.add_response(url=portable_update_mod.CHANNELS_URL, json=manifest)
    httpx2_mock.add_response(url=f"{portable_update_mod.CHANNELS_URL}.gpg", content=b"signature")
    monkeypatch.setattr(portable_update_mod, "verify_detached", lambda *_args: None)

    with pytest.raises(UpdateError) as raised:
        portable_update_mod.fetch_channel_manifest()

    assert str(raised.value) == f"the signed release-channel manifest {message}"


def test_channel_discovery_rejects_bad_signature(
    httpx2_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    httpx2_mock.add_response(url=portable_update_mod.CHANNELS_URL, json=_signed_manifest())
    httpx2_mock.add_response(url=f"{portable_update_mod.CHANNELS_URL}.gpg", content=b"forged")

    def reject(*_args: Any) -> None:
        raise VerificationError("OpenPGP signature digest prefix does not match")

    monkeypatch.setattr(portable_update_mod, "verify_detached", reject)

    with pytest.raises(UpdateError) as raised:
        portable_update_mod.fetch_channel_manifest()

    assert str(raised.value) == "could not authenticate the Ravenstash release-channel manifest"


def test_channel_discovery_reports_unreachable_manifest(httpx2_mock: Any) -> None:
    httpx2_mock.add_response(url=portable_update_mod.CHANNELS_URL, status_code=503)
    httpx2_mock.add_response(url=f"{portable_update_mod.CHANNELS_URL}.gpg", content=b"signature")

    with pytest.raises(UpdateError) as raised:
        portable_update_mod.fetch_channel_manifest()

    assert str(raised.value) == "could not authenticate the Ravenstash release-channel manifest"


def test_channel_and_minor_lookups_require_supported_targets() -> None:
    manifest = {
        "channels": {
            "v0": {"latest": "0.16.1", "minor_targets": {"0.16": "0.16.1"}, "status": "supported"},
            "v1": {"latest": "1.0.0", "minor_targets": {"1.0": "1.0.0"}, "status": "retired"},
        }
    }

    assert portable_update_mod.latest_for_channel(manifest, "v0") == "0.16.1"
    with pytest.raises(UpdateError, match=r"^release channel v1 is not available for upgrade$"):
        portable_update_mod.latest_for_channel(manifest, "v1")
    with pytest.raises(UpdateError, match=r"^release channel v2 is not available for upgrade$"):
        portable_update_mod.latest_for_minor(manifest, "2.0")
    with pytest.raises(UpdateError, match=r"^minor release 0\.15 is not available for upgrade$"):
        latest_for_minor(manifest, "0.15")


@pytest.mark.parametrize(
    ("target", "archive"),
    [
        ("linux-musl-amd64", "ravenstash-cli-v0.15.4-linux-musl-amd64.tar.gz"),
        ("macos-arm64", "ravenstash-cli-v0.15.4-macos-arm64.tar.gz"),
        ("windows-amd64", "ravenstash-cli-v0.15.4-windows-amd64.zip"),
    ],
)
def test_release_archive_name_follows_target_platform(target: str, archive: str) -> None:
    assert archive_name("0.15.4", target) == archive


# ── Authenticated release downloads ───────────────────────────────────────────

_ARCHIVE = "ravenstash-cli-v0.15.4-linux-musl-amd64.tar.gz"
_CHECKSUMS = "ravenstash-cli-v0.15.4-checksums.txt"
_SIGNATURE = f"{_CHECKSUMS}.asc"
_DOWNLOADS = f"{portable_update_mod.RELEASE_DOWNLOAD_ROOT}/v0.15.4"
_RELEASE_URL = f"{portable_update_mod.RELEASE_API}/releases/tags/v0.15.4"


def _release(**overrides: Any) -> dict[str, Any]:
    return {
        "tag_name": "v0.15.4",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": name, "browser_download_url": f"{_DOWNLOADS}/{name}"}
            for name in (_ARCHIVE, _CHECKSUMS, _SIGNATURE)
        ],
        **overrides,
    }


def _serve_release(
    httpx2_mock: Any, monkeypatch: pytest.MonkeyPatch, checksums: str, archive: bytes = b"archive"
) -> None:
    httpx2_mock.add_response(url=_RELEASE_URL, json=_release())
    httpx2_mock.add_response(url=f"{_DOWNLOADS}/{_ARCHIVE}", content=archive)
    httpx2_mock.add_response(url=f"{_DOWNLOADS}/{_CHECKSUMS}", content=checksums.encode())
    httpx2_mock.add_response(url=f"{_DOWNLOADS}/{_SIGNATURE}", content=b"signature")
    monkeypatch.setattr(portable_update_mod, "verify_detached", lambda *_args: None)


def test_release_download_returns_checksum_verified_archive(
    httpx2_mock: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    digest = hashlib.sha256(b"archive").hexdigest()
    _serve_release(httpx2_mock, monkeypatch, f"{'0' * 64}  other.zip\n{digest}  {_ARCHIVE}\n")

    archive = portable_update_mod.download_release(
        "0.15.4", "linux-musl-amd64", tmp_path, candidate=False
    )

    assert archive == tmp_path / _ARCHIVE
    assert archive.read_bytes() == b"archive"


@pytest.mark.parametrize(
    ("checksums", "message"),
    [
        (f"{'0' * 64}  {_ARCHIVE}\n", f"the release asset checksum does not match: {_ARCHIVE}"),
        (f"{'0' * 64}  other.zip\n", f"the signed checksum inventory does not contain: {_ARCHIVE}"),
        (f"not-a-digest  {_ARCHIVE}\n", f"the signed checksum for {_ARCHIVE} is invalid"),
        (
            f"{'0' * 64}  {_ARCHIVE}\n{'1' * 64}  {_ARCHIVE}\n",
            "the signed checksum inventory contains duplicate entries",
        ),
    ],
)
def test_release_download_rejects_archive_not_covered_by_signed_checksums(
    httpx2_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    checksums: str,
    message: str,
) -> None:
    _serve_release(httpx2_mock, monkeypatch, checksums)

    with pytest.raises(UpdateError) as raised:
        portable_update_mod.download_release(
            "0.15.4", "linux-musl-amd64", tmp_path, candidate=False
        )

    assert str(raised.value) == message


def test_release_download_rejects_invalid_checksum_signature(
    httpx2_mock: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _serve_release(httpx2_mock, monkeypatch, f"{'0' * 64}  {_ARCHIVE}\n")

    def reject(*_args: Any) -> None:
        raise VerificationError("OpenPGP signature digest prefix does not match")

    monkeypatch.setattr(portable_update_mod, "verify_detached", reject)

    with pytest.raises(UpdateError) as raised:
        portable_update_mod.download_release(
            "0.15.4", "linux-musl-amd64", tmp_path, candidate=False
        )

    assert str(raised.value) == "the release checksum signature is invalid"


@pytest.mark.parametrize(
    ("release", "candidate", "message"),
    [
        (
            _release(prerelease=True),
            False,
            "GitHub release v0.15.4 is not a published stable release",
        ),
        (_release(), True, "GitHub release v0.15.4 is not a published release candidate"),
        (_release(draft=True), False, "GitHub release v0.15.4 is not a published stable release"),
        (_release(assets=None), False, "GitHub release v0.15.4 has invalid asset metadata"),
        (
            _release(
                assets=[{"name": _ARCHIVE, "browser_download_url": f"{_DOWNLOADS}/{_ARCHIVE}"}]
            ),
            False,
            f"GitHub release v0.15.4 is missing signed assets: {_CHECKSUMS}, {_SIGNATURE}",
        ),
        (
            _release(
                assets=[
                    {"name": name, "browser_download_url": f"https://mirror.example.test/{name}"}
                    for name in (_ARCHIVE, _CHECKSUMS, _SIGNATURE)
                ]
            ),
            False,
            "GitHub release v0.15.4 contains an unexpected asset URL",
        ),
    ],
)
def test_release_download_rejects_unexpected_release_metadata(
    httpx2_mock: Any,
    tmp_path: Path,
    release: dict[str, Any],
    candidate: bool,
    message: str,
) -> None:
    httpx2_mock.add_response(url=_RELEASE_URL, json=release)

    with pytest.raises(UpdateError) as raised:
        portable_update_mod.download_release(
            "0.15.4", "linux-musl-amd64", tmp_path, candidate=candidate
        )

    assert str(raised.value) == message
    assert list(tmp_path.iterdir()) == []


def test_release_download_reports_unreachable_github(httpx2_mock: Any, tmp_path: Path) -> None:
    httpx2_mock.add_response(url=_RELEASE_URL, status_code=404)

    with pytest.raises(UpdateError) as raised:
        portable_update_mod.download_release(
            "0.15.4", "linux-musl-amd64", tmp_path, candidate=False
        )

    assert str(raised.value) == "could not download v0.15.4 from GitHub"


# ── Signed release inventory ──────────────────────────────────────────────────


def _serve_signed_release(
    httpx2_mock: Any,
    target: str,
    *,
    inventory: bytes | None = None,
    signature: bytes | None = None,
    archive: bytes | None = None,
) -> str:
    """Serve one synthetic release whose inventory carries a real signature."""
    version = release_signing.SYNTHETIC_VERSION
    name = archive_name(version, target)
    signed = release_signing.inventory()
    contents = {
        name: release_signing.asset_content(name) if archive is None else archive,
        checksums_name(version): signed if inventory is None else inventory,
        f"{checksums_name(version)}.asc": release_signing.armor(
            release_signing.detached_signature(signed), block="SIGNATURE"
        )
        if signature is None
        else signature,
    }
    downloads = f"{portable_update_mod.RELEASE_DOWNLOAD_ROOT}/v{version}"
    httpx2_mock.add_response(
        url=f"{portable_update_mod.RELEASE_API}/releases/tags/v{version}",
        json={
            "tag_name": f"v{version}",
            "draft": False,
            "prerelease": False,
            "assets": [
                {"name": asset, "browser_download_url": f"{downloads}/{asset}"}
                for asset in contents
            ],
        },
    )
    for asset, content in contents.items():
        httpx2_mock.add_response(url=f"{downloads}/{asset}", content=content)
    return name


def _download_signed_release(target: str, destination: Path) -> Path:
    return portable_update_mod.download_release(
        release_signing.SYNTHETIC_VERSION, target, destination, candidate=False
    )


@pytest.fixture
def trusted_test_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key_directory = tmp_path / "release-key"
    key_directory.mkdir()
    release_signing.trust_test_key(monkeypatch, key_directory)


@pytest.mark.parametrize("target", release_signing.RELEASE_TARGETS)
@pytest.mark.usefixtures("trusted_test_key")
def test_signed_inventory_authenticates_the_archive_of_each_target(
    httpx2_mock: Any, tmp_path: Path, target: str
) -> None:
    name = _serve_signed_release(httpx2_mock, target)
    destination = tmp_path / "download"
    destination.mkdir()

    archive = _download_signed_release(target, destination)

    assert archive == destination / name
    assert name.startswith(f"ravenstash-cli-v{release_signing.SYNTHETIC_VERSION}-{target}.")
    assert archive.read_bytes() == release_signing.asset_content(name)


@pytest.mark.usefixtures("trusted_test_key")
def test_signed_inventory_rejects_the_archive_of_another_target(
    httpx2_mock: Any, tmp_path: Path
) -> None:
    other = archive_name(release_signing.SYNTHETIC_VERSION, "linux-arm64")
    name = _serve_signed_release(
        httpx2_mock, "linux-amd64", archive=release_signing.asset_content(other)
    )

    with pytest.raises(UpdateError) as raised:
        _download_signed_release("linux-amd64", tmp_path)

    assert str(raised.value) == f"the release asset checksum does not match: {name}"


@pytest.mark.usefixtures("trusted_test_key")
def test_tampered_signed_inventory_is_rejected(httpx2_mock: Any, tmp_path: Path) -> None:
    # The attacker rewrites the digest of the archive they substituted.
    name = archive_name(release_signing.SYNTHETIC_VERSION, "linux-amd64")
    substituted = b"substituted archive"
    genuine = hashlib.sha256(release_signing.asset_content(name)).hexdigest()
    tampered = release_signing.inventory().replace(
        genuine.encode(), hashlib.sha256(substituted).hexdigest().encode()
    )
    assert tampered != release_signing.inventory()
    _serve_signed_release(httpx2_mock, "linux-amd64", inventory=tampered, archive=substituted)

    with pytest.raises(UpdateError) as raised:
        _download_signed_release("linux-amd64", tmp_path)

    assert str(raised.value) == "the release checksum signature is invalid"


@pytest.mark.usefixtures("trusted_test_key")
def test_inventory_signed_by_another_key_is_rejected(httpx2_mock: Any, tmp_path: Path) -> None:
    forged = release_signing.armor(
        release_signing.detached_signature(
            release_signing.inventory(), p=release_signing.OTHER_P, q=release_signing.OTHER_Q
        ),
        block="SIGNATURE",
    )
    _serve_signed_release(httpx2_mock, "linux-amd64", signature=forged)

    with pytest.raises(UpdateError) as raised:
        _download_signed_release("linux-amd64", tmp_path)

    assert str(raised.value) == "the release checksum signature is invalid"


def test_pinned_release_key_rejects_an_inventory_signed_by_the_test_key(
    httpx2_mock: Any, tmp_path: Path
) -> None:
    # Nothing is substituted: the bundled production key is the only trust root.
    _serve_signed_release(httpx2_mock, "linux-amd64")

    with pytest.raises(UpdateError) as raised:
        _download_signed_release("linux-amd64", tmp_path)

    assert str(raised.value) == "the release checksum signature is invalid"


def test_replaced_key_file_cannot_authenticate_an_inventory(
    httpx2_mock: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The key file is swapped for the signer's own key, but the fingerprint pin is not.
    from rvs import update_trust

    (tmp_path / SIGNING_KEY_NAME).write_bytes(release_signing.TEST_KEY_ARMOR)
    monkeypatch.setattr(update_trust, "files", lambda _package: tmp_path)
    destination = tmp_path / "download"
    destination.mkdir()
    _serve_signed_release(httpx2_mock, "linux-amd64")

    with pytest.raises(UpdateError) as raised:
        _download_signed_release("linux-amd64", destination)

    assert str(raised.value) == "the release checksum signature is invalid"


# ── Archive extraction ────────────────────────────────────────────────────────


def _tar_release(path: Path, members: dict[str, bytes], *, mode: int = 0o755) -> None:
    with tarfile.open(path, "w:gz") as bundle:
        for name, content in members.items():
            member = tarfile.TarInfo(name)
            member.mode = mode
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))


def test_extract_release_returns_directory_with_launcher(tmp_path: Path) -> None:
    archive = tmp_path / _ARCHIVE
    root = "ravenstash-cli-v0.15.4-linux-musl-amd64"
    _tar_release(
        archive,
        {f"{root}/{command}": b"launcher" for command in COMMANDS} | {f"{root}/lib/data": b"data"},
    )

    extracted = extract_release(archive, tmp_path / "extracted", "0.15.4", "linux-musl-amd64")

    assert extracted == tmp_path / "extracted" / root
    for command in COMMANDS:
        assert (extracted / command).read_bytes() == b"launcher"


@pytest.mark.skipif(
    os.name != "nt", reason="zip extraction keeps no POSIX execute bit; Windows-only archive"
)
def test_extract_release_supports_windows_zip(tmp_path: Path) -> None:
    archive = tmp_path / "ravenstash-cli-v0.15.4-windows-amd64.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for command in COMMANDS:
            bundle.writestr(f"ravenstash-cli-v0.15.4-windows-amd64/{command}.exe", b"launcher")

    extracted = extract_release(archive, tmp_path / "extracted", "0.15.4", "windows-amd64")

    for command in COMMANDS:
        assert (extracted / f"{command}.exe").read_bytes() == b"launcher"


def test_extract_release_rejects_zip_symlink_members(tmp_path: Path) -> None:
    archive = tmp_path / "ravenstash-cli-v0.15.4-windows-amd64.zip"
    link = zipfile.ZipInfo("ravenstash-cli-v0.15.4-windows-amd64/rvs.exe")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(link, "C:/Windows/System32/cmd.exe")

    with pytest.raises(
        UpdateError, match=r"^the portable release archive contains an unsafe path$"
    ):
        extract_release(archive, tmp_path / "extracted", "0.15.4", "windows-amd64")

    assert not (tmp_path / "extracted").exists()


def test_extract_release_rejects_symlink_members(tmp_path: Path) -> None:
    archive = tmp_path / _ARCHIVE
    with tarfile.open(archive, "w:gz") as bundle:
        link = tarfile.TarInfo("ravenstash-cli-v0.15.4-linux-musl-amd64/rvs")
        link.type = tarfile.SYMTYPE
        link.linkname = "/usr/bin/sh"
        bundle.addfile(link)

    with pytest.raises(
        UpdateError, match=r"^the portable release archive contains an unsafe path$"
    ):
        extract_release(archive, tmp_path / "extracted", "0.15.4", "linux-musl-amd64")


def test_extract_release_rejects_hard_link_members(tmp_path: Path) -> None:
    archive = tmp_path / _ARCHIVE
    root = "ravenstash-cli-v0.15.4-linux-musl-amd64"
    with tarfile.open(archive, "w:gz") as bundle:
        launcher = tarfile.TarInfo(f"{root}/rvs")
        launcher.mode = 0o755
        launcher.size = len(b"launcher")
        bundle.addfile(launcher, io.BytesIO(b"launcher"))
        link = tarfile.TarInfo(f"{root}/ravenstash")
        link.type = tarfile.LNKTYPE
        link.linkname = f"{root}/rvs"
        bundle.addfile(link)

    with pytest.raises(
        UpdateError, match=r"^the portable release archive contains an unsafe path$"
    ):
        extract_release(archive, tmp_path / "extracted", "0.15.4", "linux-musl-amd64")


_RELEASE_TARGETS = (
    "linux-amd64",
    "linux-arm64",
    "linux-musl-amd64",
    "linux-musl-arm64",
    "macos-amd64",
    "macos-arm64",
    "windows-amd64",
    "windows-arm64",
)


def _staged_release(parent: Path, version: str, target: str) -> Path:
    """Lay out one release bundle as the build scripts stage it for archiving."""
    suffix = ".exe" if target.startswith("windows-") else ""
    staging = parent / f"ravenstash-cli-v{version}-{target}"
    (staging / "_internal" / "rvs" / "resources").mkdir(parents=True)
    (staging / "_internal" / "base_library.zip").write_bytes(b"library")
    (staging / "_internal" / "rvs" / "resources" / SIGNING_KEY_NAME).write_bytes(b"key")
    for command in COMMANDS:
        launcher = staging / f"{command}{suffix}"
        launcher.write_bytes(b"launcher")
        launcher.chmod(0o755)
    for document in ("README.md", "LICENSE", "NOTICE"):
        (staging / document).write_bytes(b"document")
    return staging


@pytest.mark.parametrize("target", _RELEASE_TARGETS)
def test_extract_release_accepts_every_published_archive_layout(
    tmp_path: Path, target: str
) -> None:
    if target.startswith("windows-") and os.name != "nt":
        pytest.skip("zip extraction keeps no POSIX execute bit; Windows-only archive")
    version = "0.15.4"
    staging = _staged_release(tmp_path / "staging", version, target)
    suffix = ".exe" if target.startswith("windows-") else ""
    if suffix:
        archive = tmp_path / f"{staging.name}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    bundle.write(path, Path(staging.name) / path.relative_to(staging))
    else:
        archive = tmp_path / f"{staging.name}.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(staging, arcname=staging.name)

    extracted = extract_release(archive, tmp_path / "extracted", version, target)

    for command in COMMANDS:
        launcher = extracted / f"{command}{suffix}"
        assert launcher.is_file() and not launcher.is_symlink()
        assert launcher.read_bytes() == b"launcher"
        assert os.name == "nt" or os.access(launcher, os.X_OK)
    assert (extracted / "_internal" / "rvs" / "resources" / SIGNING_KEY_NAME).is_file()


@pytest.mark.parametrize("alias", ["ravenstash", "docker-credential-rvs"])
def test_extract_release_rejects_a_bundle_whose_alias_is_a_link(tmp_path: Path, alias: str) -> None:
    # Installed clients refuse link members, so a release must never ship one.
    version, target = "0.15.4", "linux-amd64"
    staging = _staged_release(tmp_path / "staging", version, target)
    archive = tmp_path / f"{staging.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(
            staging,
            arcname=staging.name,
            filter=lambda m: None if m.name.endswith(f"/{alias}") else m,
        )
        link = tarfile.TarInfo(f"{staging.name}/{alias}")
        link.type = tarfile.SYMTYPE
        link.linkname = "rvs"
        bundle.addfile(link)

    with pytest.raises(
        UpdateError, match=r"^the portable release archive contains an unsafe path$"
    ):
        extract_release(archive, tmp_path / "extracted", version, target)


def test_extract_release_rejects_archive_for_another_target(tmp_path: Path) -> None:
    archive = tmp_path / _ARCHIVE
    _tar_release(archive, {"ravenstash-cli-v0.15.4-macos-arm64/rvs": b"launcher"})

    with pytest.raises(
        UpdateError, match=r"^the portable release archive contains an unsafe path$"
    ):
        extract_release(archive, tmp_path / "extracted", "0.15.4", "linux-musl-amd64")


def test_extract_release_requires_launcher(tmp_path: Path) -> None:
    archive = tmp_path / _ARCHIVE
    _tar_release(archive, {"ravenstash-cli-v0.15.4-linux-musl-amd64/README": b"readme"})

    with pytest.raises(
        UpdateError, match=r"^the portable release archive is missing its rvs launcher$"
    ):
        extract_release(archive, tmp_path / "extracted", "0.15.4", "linux-musl-amd64")


@pytest.mark.skipif(os.name == "nt", reason="executable permission bits are POSIX-only")
def test_extract_release_requires_executable_launcher(tmp_path: Path) -> None:
    archive = tmp_path / _ARCHIVE
    root = "ravenstash-cli-v0.15.4-linux-musl-amd64"
    _tar_release(archive, {f"{root}/{command}": b"launcher" for command in COMMANDS}, mode=0o644)

    with pytest.raises(UpdateError, match=r"contains a non-executable rvs launcher$"):
        extract_release(archive, tmp_path / "extracted", "0.15.4", "linux-musl-amd64")


def _archive_release(staging: Path, archive: Path, alter: Any = None) -> Path:
    """Archive a staged bundle the way its target ships it, optionally altering members."""
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(staging.rglob("*")):
                if path.is_file() and (alter is None or alter(path.name) is not None):
                    bundle.write(path, Path(staging.name) / path.relative_to(staging))
        return archive

    def member(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if alter is None or not info.isfile():
            return info
        mode = alter(Path(info.name).name)
        if mode is None:
            return None
        info.mode = mode
        return info

    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(staging, arcname=staging.name, filter=member)
    return archive


@pytest.mark.parametrize("target", ["linux-amd64", "macos-arm64", "windows-amd64"])
@pytest.mark.parametrize("command", COMMANDS)
def test_extract_release_requires_every_command(tmp_path: Path, target: str, command: str) -> None:
    version = "0.15.4"
    windows = target.startswith("windows-")
    staging = _staged_release(tmp_path / "staging", version, target)
    missing = f"{command}.exe" if windows else command
    archive = _archive_release(
        staging,
        tmp_path / archive_name(version, target),
        lambda name: None if name == missing else 0o755,
    )

    with pytest.raises(UpdateError) as raised:
        extract_release(archive, tmp_path / "extracted", version, target)

    assert str(raised.value) == f"the portable release archive is missing its {command} launcher"


@pytest.mark.parametrize("command", COMMANDS)
def test_extract_release_requires_the_windows_suffix_on_every_command(
    tmp_path: Path, command: str
) -> None:
    # A launcher without its suffix is not a command Windows can run.
    version, target = "0.15.4", "windows-amd64"
    staging = _staged_release(tmp_path / "staging", version, target)
    (staging / f"{command}.exe").rename(staging / command)
    archive = _archive_release(staging, tmp_path / archive_name(version, target))

    with pytest.raises(UpdateError) as raised:
        extract_release(archive, tmp_path / "extracted", version, target)

    assert str(raised.value) == f"the portable release archive is missing its {command} launcher"


@pytest.mark.skipif(os.name == "nt", reason="executable permission bits are POSIX-only")
@pytest.mark.parametrize("command", COMMANDS)
def test_extract_release_requires_every_command_to_be_executable(
    tmp_path: Path, command: str
) -> None:
    version, target = "0.15.4", "linux-amd64"
    staging = _staged_release(tmp_path / "staging", version, target)
    archive = _archive_release(
        staging,
        tmp_path / archive_name(version, target),
        lambda name: 0o644 if name == command else 0o755,
    )

    with pytest.raises(UpdateError) as raised:
        extract_release(archive, tmp_path / "extracted", version, target)

    assert str(raised.value) == (
        f"the portable release archive contains a non-executable {command} launcher"
    )


@pytest.mark.parametrize("command", COMMANDS)
def test_extract_release_requires_every_command_to_be_a_file(tmp_path: Path, command: str) -> None:
    version, target = "0.15.4", "linux-amd64"
    staging = _staged_release(tmp_path / "staging", version, target)
    (staging / command).unlink()
    (staging / command).mkdir()
    (staging / command / "launcher").write_bytes(b"launcher")
    archive = _archive_release(staging, tmp_path / archive_name(version, target))

    with pytest.raises(UpdateError) as raised:
        extract_release(archive, tmp_path / "extracted", version, target)

    assert str(raised.value) == f"the portable release archive is missing its {command} launcher"


# ── Activation ────────────────────────────────────────────────────────────────


def test_update_lock_refuses_concurrent_update(tmp_path: Path) -> None:
    # Both POSIX flock and Windows byte-range locks conflict between two open
    # handles, even within one process, and the lock is released on exit.
    with portable_update_mod.update_lock(tmp_path):
        with pytest.raises(UpdateError, match=r"^another rvs update is already running$"):
            with portable_update_mod.update_lock(tmp_path):
                pass

    with portable_update_mod.update_lock(tmp_path):
        pass


def test_update_lock_refuses_while_windows_update_is_pending(tmp_path: Path) -> None:
    (tmp_path / ".update-pending").write_text("0.15.4\n", encoding="ascii")

    with pytest.raises(UpdateError, match=r"^a staged Windows rvs update is still pending$"):
        with portable_update_mod.update_lock(tmp_path):
            pass


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(0, "Ravenstash CLI 0.15.3\n"), (1, "Ravenstash CLI 0.15.4\n")],
)
def test_smoke_check_requires_the_expected_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, stdout: str
) -> None:
    monkeypatch.setattr(
        portable_update_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, returncode, stdout, ""),
    )

    with pytest.raises(UpdateError, match=r"failed its version health check$"):
        portable_update_mod._smoke(tmp_path / "rvs", "0.15.4")


def test_posix_activation_refuses_existing_target_version(tmp_path: Path) -> None:
    installation = _installation(tmp_path)
    (installation.root / "0.15.4").mkdir(parents=True)
    extracted = tmp_path / "new"
    extracted.mkdir()

    with pytest.raises(UpdateError) as raised:
        activate_posix(extracted, installation, "0.15.4")

    assert str(raised.value) == (
        f"the target installation already exists: {installation.root / '0.15.4'}"
    )
    assert extracted.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX activation uses executable symlinks")
def test_posix_activation_restores_previous_version_when_health_check_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installation = _installation(tmp_path)
    old = installation.root / installation.version
    old.mkdir(parents=True)
    installation.bin.mkdir(parents=True)
    (installation.root / "current").symlink_to(installation.version)
    for command in COMMANDS:
        _launcher(old / command, installation.version)
        (installation.bin / command).symlink_to(installation.root / "current" / command)
    extracted = tmp_path / "new"
    extracted.mkdir()

    def smoke(executable: Path, version: str) -> None:
        if executable.parent == installation.bin:
            raise UpdateError("the staged rvs executable failed its version health check")

    monkeypatch.setattr(portable_update_mod, "_smoke", smoke)

    with pytest.raises(UpdateError, match="health check"):
        activate_posix(extracted, installation, "0.15.4")

    assert (installation.root / "current").readlink() == Path(installation.version)
    for command in COMMANDS:
        assert (installation.bin / command).readlink() == installation.root / "current" / command
        assert (installation.bin / command).resolve() == old / command
    assert not (installation.root / "0.15.4").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX activation uses executable symlinks")
def test_posix_activation_restores_every_command_when_one_cannot_be_switched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installation = _installation(tmp_path)
    old = installation.root / installation.version
    old.mkdir(parents=True)
    installation.bin.mkdir(parents=True)
    for command in COMMANDS:
        _launcher(old / command, installation.version)
        (installation.bin / command).symlink_to(old / command)
    extracted = tmp_path / "new"
    extracted.mkdir()
    for command in COMMANDS:
        _launcher(extracted / command, "0.15.4")
    replace_symlink = portable_update_mod._replace_symlink
    switched: list[str] = []

    def fail_on_the_last_command(path: Path, target: Path) -> None:
        if len(switched) == len(COMMANDS) - 1:
            # The other commands already run the new version; the rollback may proceed.
            for moved in switched:
                assert (installation.bin / moved).resolve() == (
                    installation.root / "0.15.4" / moved
                )
            switched.append(path.name)
            raise OSError("no space left on device")
        switched.append(path.name)
        replace_symlink(path, target)

    monkeypatch.setattr(portable_update_mod, "_replace_symlink", fail_on_the_last_command)

    with pytest.raises(OSError, match="no space left on device"):
        activate_posix(extracted, installation, "0.15.4")

    assert sorted(switched[: len(COMMANDS)]) == sorted(COMMANDS)
    for command in COMMANDS:
        assert (installation.bin / command).readlink() == old / command
    assert not (installation.root / "current").exists()
    assert not (installation.root / "0.15.4").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX activation uses executable symlinks")
def test_posix_activation_removes_every_command_it_created_when_health_check_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installation = _installation(tmp_path)
    extracted = tmp_path / "new"
    extracted.mkdir()
    for command in COMMANDS:
        _launcher(extracted / command, "0.15.4")

    def smoke(executable: Path, version: str) -> None:
        if executable.parent == installation.bin:
            for command in COMMANDS:
                assert (installation.bin / command).resolve() == (
                    installation.root / "0.15.4" / command
                )
            raise UpdateError("the staged rvs executable failed its version health check")

    monkeypatch.setattr(portable_update_mod, "_smoke", smoke)

    with pytest.raises(UpdateError, match="health check"):
        activate_posix(extracted, installation, "0.15.4")

    assert list(installation.bin.iterdir()) == []
    assert list(installation.root.iterdir()) == []


def test_windows_staging_records_pending_update_and_starts_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installation = Installation(**{**_installation(tmp_path).__dict__, "target": "windows-amd64"})
    installation.root.mkdir()
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    for command in COMMANDS:
        (extracted / f"{command}.exe").write_bytes(b"launcher")
    helper_dir = tmp_path / "temp"
    helper_dir.mkdir()
    launched: list[list[str]] = []
    monkeypatch.setattr(tempfile, "tempdir", str(helper_dir))
    monkeypatch.setattr(portable_update_mod, "_smoke", lambda executable, version: None)
    monkeypatch.setattr(
        portable_update_mod.subprocess,
        "Popen",
        lambda command, **_kwargs: launched.append(command),
    )

    portable_update_mod.stage_windows(extracted, installation, "0.15.4")

    staged = next(installation.root.glob(".staged-0.15.4-*"))
    assert sorted(path.name for path in staged.glob("*.exe")) == sorted(
        f"{command}.exe" for command in COMMANDS
    )
    assert not extracted.exists()
    assert (installation.root / ".update-pending").read_text(encoding="ascii") == "0.15.4\n"
    assert read_receipt(staged / RECEIPT_NAME).bin_directory == str(installation.root / "bin")
    assert launched[0][:2] == ["powershell.exe", "-NoLogo"]
    assert launched[0][-6:] == [
        "-InstallRoot",
        str(installation.root),
        "-Staged",
        str(staged),
        "-Version",
        "0.15.4",
    ]
    assert [path.suffix for path in helper_dir.iterdir()] == [".ps1"]
    assert next(helper_dir.iterdir()).read_text(encoding="utf-8") == (
        portable_update_mod._WINDOWS_HELPER
    )


def test_windows_helper_replaces_and_restores_the_command_directory_as_one() -> None:
    helper = portable_update_mod._WINDOWS_HELPER
    activation, rollback = helper.split("catch {", 1)
    rollback = rollback.split("finally {", 1)[0]

    # The staged directory holds every launcher and replaces the installed one whole.
    assert '$bin = Join-Path $InstallRoot "bin"' in helper
    assert activation.index("Move-Item -LiteralPath $bin -Destination $backup") < (
        activation.index("Move-Item -LiteralPath $Staged -Destination $bin")
    )
    assert activation.index("Move-Item -LiteralPath $Staged -Destination $bin") < (
        activation.index('& (Join-Path $bin "rvs.exe") --version')
    )
    assert rollback.index("Remove-Item -Recurse -Force -LiteralPath $bin") < (
        rollback.index("Move-Item -LiteralPath $backup -Destination $bin")
    )
    # No launcher is moved or restored by name, so the commands cannot diverge.
    assert re.findall(r"[\w-]+\.exe", helper) == ["rvs.exe"]
    assert [line for line in helper.splitlines() if "Move-Item" in line] == [
        "  if (Test-Path -LiteralPath $bin) { Move-Item -LiteralPath $bin -Destination $backup }",
        "  Move-Item -LiteralPath $Staged -Destination $bin",
        "    Move-Item -LiteralPath $backup -Destination $bin",
    ]


def test_windows_staging_cleans_up_when_helper_cannot_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    installation = Installation(**{**_installation(tmp_path).__dict__, "target": "windows-amd64"})
    installation.root.mkdir()
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(portable_update_mod, "_smoke", lambda executable, version: None)

    def no_powershell(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("powershell.exe")

    monkeypatch.setattr(portable_update_mod.subprocess, "Popen", no_powershell)

    with pytest.raises(FileNotFoundError):
        portable_update_mod.stage_windows(extracted, installation, "0.15.4")

    assert not (installation.root / ".update-pending").exists()
    assert list(installation.root.glob(".staged-*")) == []


@pytest.mark.parametrize(
    ("target", "asynchronous", "activation"),
    [("windows-amd64", True, "stage_windows"), ("linux-musl-amd64", False, "activate_posix")],
)
def test_apply_portable_update_activates_for_target_platform(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
    asynchronous: bool,
    activation: str,
) -> None:
    installation = Installation(**{**_installation(tmp_path).__dict__, "target": target})
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        portable_update_mod,
        "download_release",
        lambda version, release_target, work, *, candidate: (
            calls.append(("download", (version, release_target, candidate))) or work / "archive"
        ),
    )
    monkeypatch.setattr(
        portable_update_mod,
        "extract_release",
        lambda archive, destination, version, release_target: destination / "rvs",
    )
    for name in ("stage_windows", "activate_posix"):
        monkeypatch.setattr(
            portable_update_mod,
            name,
            lambda extracted, _installation, version, name=name: calls.append((name, version)),
        )

    assert portable_update_mod.apply_portable_update(installation, "0.15.4", candidate=True) is (
        asynchronous
    )
    assert calls == [("download", ("0.15.4", target, True)), (activation, "0.15.4")]


@pytest.mark.parametrize("command", COMMANDS)
def test_apply_portable_update_refuses_an_incomplete_archive_before_activation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str
) -> None:
    installation = _installation(tmp_path)
    staging = _staged_release(tmp_path / "staging", "0.15.4", installation.target)
    (staging / command).unlink()
    archive = _archive_release(staging, tmp_path / archive_name("0.15.4", installation.target))
    monkeypatch.setattr(portable_update_mod, "download_release", lambda *_args, **_kwargs: archive)

    def activated(*_args: Any) -> None:
        raise AssertionError("an incomplete archive must not be activated")

    monkeypatch.setattr(portable_update_mod, "activate_posix", activated)
    monkeypatch.setattr(portable_update_mod, "stage_windows", activated)

    with pytest.raises(UpdateError) as raised:
        portable_update_mod.apply_portable_update(installation, "0.15.4", candidate=False)

    assert str(raised.value) == f"the portable release archive is missing its {command} launcher"
    assert [path.name for path in installation.root.iterdir()] == [".update.lock"]
    assert not installation.bin.exists()
