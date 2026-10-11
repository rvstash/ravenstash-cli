import hashlib
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rvs import portable_update as portable_update_mod
from rvs import update as update_mod
from rvs.cli import app
from rvs.installations import Installation
from typer.testing import CliRunner


runner = CliRunner()

# The autouse fixture below stubs the signed APT channel lookup; keep the real
# implementation for the tests that exercise its validation.
_verified_apt_channel_manifest = update_mod._channel_manifest


def _completed(returncode: int = 0, stdout: str = "") -> Any:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _newer(installed: str, candidate: str) -> bool:
    """Portable stand-in for `dpkg --compare-versions candidate gt installed`."""
    return tuple(map(int, candidate.split("."))) > tuple(map(int, installed.split(".")))


def _manifest(latest: str) -> dict[str, Any]:
    minor = latest.rpartition(".")[0]
    return {
        "schema": 1,
        "recommended": "v0",
        "channels": {
            "v0": {"latest": latest, "minor_targets": {minor: latest}, "status": "supported"}
        },
    }


@pytest.fixture(autouse=True)
def _isolated_apt(monkeypatch: Any, tmp_path: Path) -> None:
    # Never read the host's APT source or fetch the real signed channel manifest.
    monkeypatch.setattr(update_mod, "_APT_SOURCE", tmp_path / "missing-ravenstash-cli.list")
    monkeypatch.setattr(update_mod, "_channel_manifest", lambda: None)


def test_update_reports_new_apt_candidate(monkeypatch: Any) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.3", "0.16.4"))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: True)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    assert "0.16.4 is available" in result.output
    assert "rvs update --apply" in result.output


def test_update_reports_installed_and_latest_versions_when_current(monkeypatch: Any) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.3", "0.16.3"))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: False)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(update_mod, "_announce_new_channel", lambda channel, manifest: None)

    result = runner.invoke(app, ["update"])
    output = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "Installed: rvs 0.16.3" in output
    assert "Latest in release channel v0: rvs 0.16.3" in output
    assert "You are up to date" in output


def test_update_does_not_self_replace_unmanaged_install(monkeypatch: Any) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: (None, None))
    monkeypatch.setattr(update_mod, "_cli_version", lambda: "0.16.4")

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 0
    assert "not a recognized managed installation" in result.output
    assert (
        "Install a managed release with: curl -fsSL https://ravenstash.com/install.sh | bash"
        in _one_line(result.output)
    )


def test_nix_detection_excludes_source_test_environment() -> None:
    assert not update_mod._is_nix_application_executable(
        "/nix/store/abc123-ravenstash-cli-test-env/bin/python3"
    )


def test_nix_detection_recognizes_packaged_application_environment() -> None:
    assert update_mod._is_nix_application_executable(
        "/nix/store/abc123-ravenstash-cli-env/bin/python3"
    )


def _portable(tmp_path: Path) -> Installation:
    return Installation(
        1,
        "portable",
        "user",
        "0.16.3",
        "v0",
        "linux-musl-amd64",
        str(tmp_path / "root"),
        str(tmp_path / "bin"),
    )


def test_portable_update_previews_signed_channel_release(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: (None, None))
    monkeypatch.setattr(update_mod, "_portable_installation", lambda: _portable(tmp_path))
    monkeypatch.setattr(
        update_mod,
        "fetch_channel_manifest",
        lambda: {
            "schema": 1,
            "recommended": "v0",
            "channels": {
                "v0": {
                    "latest": "0.16.4",
                    "minor_targets": {"0.16": "0.16.4"},
                    "status": "supported",
                }
            },
        },
    )

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 0, result.output
    assert "0.16.4 is available for linux-musl-amd64" in result.output
    assert "rvs update --apply" in result.output


def test_portable_update_applies_exact_candidate(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[tuple[Installation, str, bool]] = []
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: (None, None))
    monkeypatch.setattr(update_mod, "_portable_installation", lambda: _portable(tmp_path))
    monkeypatch.setattr(
        update_mod,
        "apply_portable_update",
        lambda installation, version, *, candidate: (
            calls.append((installation, version, candidate)) or False
        ),
    )

    result = runner.invoke(app, ["update", "--candidate", "0.16.4rc1", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls == [(_portable(tmp_path), "0.16.4rc1", True)]
    assert "Updated rvs to 0.16.4rc1" in result.output


def test_winget_update_uses_exact_series_identifier(monkeypatch: Any) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: (None, None))
    monkeypatch.setattr(update_mod, "_portable_installation", lambda: None)
    monkeypatch.setattr(update_mod, "_managed_method", lambda: "winget")
    monkeypatch.setattr(update_mod, "_cli_version", lambda: "0.16.3")
    monkeypatch.setattr(update_mod.shutil, "which", lambda command: command)
    monkeypatch.setattr(
        update_mod,
        "fetch_channel_manifest",
        lambda: {
            "schema": 1,
            "recommended": "v0",
            "channels": {
                "v0": {
                    "latest": "0.16.4",
                    "minor_targets": {"0.16": "0.16.4"},
                    "status": "supported",
                }
            },
        },
    )
    monkeypatch.setattr(
        update_mod.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or _completed(),
    )

    result = runner.invoke(app, ["update", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls == [["winget", "upgrade", "--exact", "--id", "Ravenstash.RavenstashCLI.v0"]]


def test_nix_update_replaces_pinned_tag_and_rolls_back_on_failure(monkeypatch: Any) -> None:
    calls: list[list[str]] = []
    return_codes = iter((0, 1, 0))
    monkeypatch.setattr(update_mod.shutil, "which", lambda command: command)
    monkeypatch.setattr(
        update_mod.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or _completed(next(return_codes)),
    )

    try:
        update_mod._replace_nix_profile("0.16.3", "0.16.4")
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("failed Nix replacement did not exit")

    repository = "github:rvstash/ravenstash-cli"
    assert calls == [
        ["nix", "profile", "remove", "ravenstash-cli"],
        ["nix", "profile", "install", f"{repository}/v0.16.4"],
        ["nix", "profile", "install", f"{repository}/v0.16.3"],
    ]


def test_homebrew_exact_minor_selector_fails_without_mutating_package_state(
    monkeypatch: Any,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: (None, None))
    monkeypatch.setattr(update_mod, "_portable_installation", lambda: None)
    monkeypatch.setattr(update_mod, "_managed_method", lambda: "homebrew")
    monkeypatch.setattr(update_mod, "_cli_version", lambda: "0.16.3")
    monkeypatch.setattr(update_mod.shutil, "which", lambda command: command)
    monkeypatch.setattr(
        update_mod,
        "fetch_channel_manifest",
        lambda: {
            "schema": 1,
            "recommended": "v0",
            "channels": {
                "v0": {
                    "latest": "0.18.0",
                    "minor_targets": {"0.17": "0.17.2", "0.18": "0.18.0"},
                    "status": "supported",
                }
            },
        },
    )
    monkeypatch.setattr(
        update_mod.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or _completed(),
    )

    result = runner.invoke(app, ["update", "--to", "0.17", "--apply", "--yes"])

    assert result.exit_code == 1
    assert "signed portable installer" in result.output
    assert calls == []


def test_candidate_version_maps_to_debian_prerelease() -> None:
    assert update_mod._candidate_debian_version("0.16.4rc2") == "0.16.4~rc2"


def test_candidate_preview_verifies_without_installing(monkeypatch: Any, tmp_path: Path) -> None:
    (tmp_path / "dpkg-deb").touch()
    monkeypatch.setattr(update_mod, "_DPKG_DEB", tmp_path / "dpkg-deb")
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.3", "0.16.3"))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: True)

    def fake_download(candidate: str, destination: Path) -> Path:
        package = destination / f"ravenstash-cli_{candidate}_amd64.deb"
        package.touch()
        return package

    monkeypatch.setattr(update_mod, "_download_candidate", fake_download)
    monkeypatch.setattr(
        update_mod,
        "_run_visible",
        lambda command: (_ for _ in ()).throw(AssertionError("preview installed a package")),
    )

    result = runner.invoke(app, ["update", "--candidate", "0.16.4rc1"])

    assert result.exit_code == 0, result.output
    assert "Verified signed release candidate rvs 0.16.4rc1" in result.output
    assert "--candidate 0.16.4rc1 --apply" in result.output


def test_candidate_apply_installs_verified_local_package(monkeypatch: Any, tmp_path: Path) -> None:
    apt_get = tmp_path / "apt-get"
    dpkg_deb = tmp_path / "dpkg-deb"
    for path in (apt_get, dpkg_deb):
        path.touch()
    monkeypatch.setattr(update_mod, "_APT_GET", apt_get)
    monkeypatch.setattr(update_mod, "_DPKG_DEB", dpkg_deb)
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.3", "0.16.3"))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: True)

    def fake_download(candidate: str, destination: Path) -> Path:
        package = destination / f"ravenstash-cli_{candidate}_amd64.deb"
        package.touch()
        return package

    monkeypatch.setattr(update_mod, "_download_candidate", fake_download)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        update_mod,
        "_run_visible",
        lambda command: calls.append(command) or _completed(),
    )

    result = runner.invoke(app, ["update", "--candidate", "0.16.4rc1", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][:3] == [str(apt_get), "install", "--yes"]
    assert Path(calls[0][3]).name == "ravenstash-cli_0.16.4rc1_amd64.deb"


def test_candidate_rejects_stable_version_and_series_option(monkeypatch: Any) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: (None, None))
    invalid = runner.invoke(app, ["update", "--candidate", "0.16.3"])
    combined = runner.invoke(app, ["update", "--candidate", "0.16.4rc1", "--to", "0.16"])

    assert invalid.exit_code == 1
    assert "must look like 0.15.1rc1" in invalid.output
    assert combined.exit_code == 1
    assert "cannot be used together" in combined.output


def test_candidate_download_authenticates_release_assets(monkeypatch: Any, tmp_path: Path) -> None:
    candidate = "0.16.4rc1"
    package_name = f"ravenstash-cli_{candidate}_amd64.deb"
    package = b"signed candidate package"
    checksum_name = f"ravenstash-cli-v{candidate}-checksums.txt"
    checksum = f"{hashlib.sha256(package).hexdigest()}  {package_name}\n".encode()
    signature_name = f"{checksum_name}.asc"
    tag = f"v{candidate}"
    root = f"{portable_update_mod.RELEASE_DOWNLOAD_ROOT}/{tag}"
    payloads = {
        f"{root}/{package_name}": package,
        f"{root}/{checksum_name}": checksum,
        f"{root}/{signature_name}": b"signature",
    }
    release = {
        "tag_name": tag,
        "draft": False,
        "prerelease": True,
        "assets": [
            {"name": name, "browser_download_url": f"{root}/{name}"}
            for name in (package_name, checksum_name, signature_name)
        ],
    }

    class Response:
        def __init__(self, content: bytes = b"", json_data: Any = None) -> None:
            self.content = content
            self._json = json_data

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return self._json

    class Client:
        def __enter__(self) -> Client:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def get(self, url: str, **_kwargs: Any) -> Response:
            if url == f"{portable_update_mod.RELEASE_API}/releases/tags/{tag}":
                return Response(json_data=release)
            return Response(content=payloads[url])

    monkeypatch.setattr(portable_update_mod.httpx, "Client", lambda **_kwargs: Client())
    monkeypatch.setattr(portable_update_mod, "verify_detached", lambda *_args: None)
    monkeypatch.setattr(update_mod.platform, "machine", lambda: "x86_64")

    metadata_commands: list[list[str]] = []

    def fake_run(command: list[str]) -> Any:
        metadata_commands.append(command)
        return _completed(stdout="ravenstash-cli\n0.16.4~rc1\namd64\n")

    monkeypatch.setattr(update_mod, "_run", fake_run)

    result = update_mod._download_candidate(candidate, tmp_path)

    assert result == tmp_path / package_name
    assert result.read_bytes() == package
    assert metadata_commands == [
        [
            str(update_mod._DPKG_DEB),
            "--show",
            "--showformat=${Package}\\n${Version}\\n${Architecture}\\n",
            str(result),
        ]
    ]


def test_apt_source_recognizes_rolling_v0_on_arm64() -> None:
    source = (
        "deb [arch=arm64 signed-by=/etc/apt/keyrings/ravenstash-cli.gpg] "
        "https://releases.ravenstash.com/ravenstash/apt v0 main"
    )

    assert update_mod._SOURCE_PATTERN.fullmatch(source)


def test_update_apply_uses_fixed_apt_paths(monkeypatch: Any, tmp_path: Path) -> None:
    assert update_mod._APT_GET == Path("/usr/bin/apt-get")
    apt_get = tmp_path / "apt-get"
    apt_get.touch()
    monkeypatch.setattr(update_mod, "_APT_GET", apt_get)
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.3", "0.16.4"))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(update_mod.os, "geteuid", lambda: 0, raising=False)
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> Any:
        calls.append(command)
        return _completed()

    monkeypatch.setattr(update_mod, "_run_visible", fake_run)

    result = runner.invoke(app, ["update", "--apply", "--yes"])

    assert result.exit_code == 0
    assert calls == [
        [str(apt_get), "update"],
        [str(apt_get), "install", "--only-upgrade", "--yes", "ravenstash-cli=0.16.4"],
    ]


def test_update_rejects_candidate_outside_configured_channel(monkeypatch: Any) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.3", "1.0.0"))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: True)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 1
    assert "does not belong to configured release channel v0" in result.output


def test_exact_minor_upgrade_uses_authenticated_manifest_without_changing_source(
    monkeypatch: Any,
) -> None:
    versions = iter((("0.16.3", "0.18.0"), ("0.16.3", "0.18.0")))
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: next(versions))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: True)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(
        update_mod,
        "_channel_manifest",
        lambda: {
            "schema": 1,
            "recommended": "v0",
            "channels": {
                "v0": {
                    "latest": "0.18.0",
                    "minor_targets": {"0.16": "0.16.3", "0.17": "0.17.2"},
                    "status": "supported",
                },
            },
        },
    )
    calls: list[list[str]] = []

    def fake_visible(command: list[str]) -> Any:
        calls.append(command)
        return _completed()

    monkeypatch.setattr(update_mod, "_run_visible", fake_visible)

    result = runner.invoke(app, ["update", "--apply", "--to", "0.17", "--yes"])

    assert result.exit_code == 0
    assert calls == [
        [str(update_mod._APT_GET), "update"],
        [str(update_mod._APT_GET), "install", "--only-upgrade", "--yes", "ravenstash-cli=0.17.2"],
    ]
    assert "minor release 0.17" in result.output


def test_exact_minor_rejects_unpublished_target_without_apt_changes(monkeypatch: Any) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.3", "0.18.0"))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: True)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(
        update_mod,
        "_channel_manifest",
        lambda: {
            "schema": 1,
            "recommended": "v0",
            "channels": {
                "v0": {
                    "latest": "0.18.0",
                    "minor_targets": {"0.18": "0.18.0"},
                    "status": "supported",
                }
            },
        },
    )
    monkeypatch.setattr(
        update_mod,
        "_run_visible",
        lambda command: (_ for _ in ()).throw(AssertionError("APT was changed")),
    )

    result = runner.invoke(app, ["update", "--apply", "--to", "0.17", "--yes"])

    assert result.exit_code == 1
    assert "minor release 0.17 is not available" in result.output


def test_update_to_only_previews_and_never_changes_apt_source(monkeypatch):
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.3", "0.16.3"))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: True)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(
        update_mod,
        "_channel_manifest",
        lambda: {
            "schema": 1,
            "recommended": "v0",
            "channels": {
                "v0": {
                    "latest": "0.18.0",
                    "minor_targets": {"0.17": "0.17.2", "0.18": "0.18.0"},
                    "status": "supported",
                }
            },
        },
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("preview attempted an APT change")

    monkeypatch.setattr(update_mod, "_run_visible", unexpected)
    monkeypatch.setattr(update_mod.typer, "confirm", unexpected)
    result = runner.invoke(app, ["update", "--to", "0.17"])
    assert result.exit_code == 0, result.output
    assert "0.17.2" in result.stdout
    assert "--apply" in result.stdout


def test_update_yes_requires_explicit_apply(monkeypatch):
    monkeypatch.setattr(
        update_mod,
        "_apt_versions",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected APT read")),
    )
    result = runner.invoke(app, ["update", "--to", "0.17", "--yes"])
    assert result.exit_code == 1
    assert "--yes requires --apply" in result.stderr


def test_update_to_current_series_still_applies_available_update(monkeypatch, tmp_path):
    apt_get = tmp_path / "apt-get"
    apt_get.touch()
    monkeypatch.setattr(update_mod, "_APT_GET", apt_get)
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.17.0", "0.17.1"))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: True)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(
        update_mod,
        "_channel_manifest",
        lambda: {
            "channels": {
                "v0": {
                    "latest": "0.18.0",
                    "minor_targets": {"0.17": "0.17.1", "0.18": "0.18.0"},
                    "status": "supported",
                }
            }
        },
    )
    calls = []
    monkeypatch.setattr(
        update_mod, "_run_visible", lambda command: calls.append(command) or _completed()
    )
    result = runner.invoke(app, ["update", "--to", "0.17", "--apply", "--yes"])
    assert result.exit_code == 0, result.output
    assert calls[-1] == [
        str(apt_get),
        "install",
        "--only-upgrade",
        "--yes",
        "ravenstash-cli=0.17.1",
    ]


def test_update_to_rejects_package_downgrade_before_source_change(monkeypatch):
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.18.0", "0.16.3"))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: False)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(
        update_mod,
        "_channel_manifest",
        lambda: {
            "channels": {
                "v0": {
                    "latest": "0.18.0",
                    "minor_targets": {"0.17": "0.17.0"},
                    "status": "supported",
                }
            }
        },
    )
    result = runner.invoke(app, ["update", "--to", "0.17", "--apply", "--yes"])
    assert result.exit_code == 1
    assert "not newer" in result.stderr


def test_update_to_stops_if_installed_package_changes(monkeypatch):
    versions = iter((("0.16.3", "0.16.3"), ("0.18.0", "0.17.0")))
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: next(versions))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: True)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(
        update_mod,
        "_channel_manifest",
        lambda: {
            "channels": {
                "v0": {
                    "latest": "0.18.0",
                    "minor_targets": {"0.17": "0.17.0"},
                    "status": "supported",
                }
            }
        },
    )
    calls = []
    monkeypatch.setattr(
        update_mod, "_run_visible", lambda command: calls.append(command) or _completed()
    )
    result = runner.invoke(app, ["update", "--to", "0.17", "--apply", "--yes"])
    assert result.exit_code == 1
    assert calls == [[str(update_mod._APT_GET), "update"]]


_MINOR_LINES = {
    "schema": 1,
    "recommended": "v0",
    "channels": {
        "v0": {
            "latest": "0.18.0",
            "minor_targets": {"0.17": "0.17.2", "0.18": "0.18.0"},
            "status": "supported",
        }
    },
}


@pytest.mark.parametrize(
    ("after_failure", "restore_exit", "restored", "message"),
    [
        (
            # dpkg no longer reports a cleanly installed package.
            (None, "0.18.0"),
            0,
            True,
            "APT could not install rvs 0.17.2; rvs 0.16.3 was restored.",
        ),
        (
            ("0.17.2", "0.18.0"),
            0,
            True,
            "APT could not install rvs 0.17.2; rvs 0.16.3 was restored.",
        ),
        (
            (None, "0.18.0"),
            100,
            True,
            "APT could not install rvs 0.17.2, and restoring rvs 0.16.3 also failed. "
            "Restore the package with: sudo apt-get install --allow-downgrades ravenstash-cli=0.16.3",
        ),
        (
            # APT stopped before it changed the package, so there is nothing to restore.
            ("0.16.3", "0.18.0"),
            0,
            False,
            "APT could not install rvs 0.17.2; rvs 0.16.3 is still installed.",
        ),
    ],
)
def test_update_to_restores_the_installed_version_when_apt_fails(
    monkeypatch: Any,
    after_failure: tuple[str | None, str],
    restore_exit: int,
    restored: bool,
    message: str,
) -> None:
    versions = iter((("0.16.3", "0.18.0"), ("0.16.3", "0.18.0"), after_failure))
    exits = iter((0, 100, restore_exit))
    calls: list[list[str]] = []
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: next(versions))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(update_mod, "_channel_manifest", lambda: _MINOR_LINES)
    monkeypatch.setattr(
        update_mod, "_run_visible", lambda command: calls.append(command) or _completed(next(exits))
    )

    result = runner.invoke(app, ["update", "--to", "0.17", "--apply", "--yes"])

    apt_get = str(update_mod._APT_GET)
    expected = [
        [apt_get, "update"],
        [apt_get, "install", "--only-upgrade", "--yes", "ravenstash-cli=0.17.2"],
    ]
    if restored:
        expected.append(
            [apt_get, "install", "--yes", "--allow-downgrades", "ravenstash-cli=0.16.3"]
        )
    assert result.exit_code == 1
    assert calls == expected
    # The original failure is always the reported error, whatever the restore did.
    assert _one_line(result.stderr).endswith(f"Error: {message}")
    assert "Updated rvs" not in result.output


@pytest.mark.parametrize(
    ("after_failure", "restore_exit", "restored", "message"),
    [
        (
            # dpkg no longer reports a cleanly installed package.
            (None, "0.16.8"),
            0,
            True,
            "APT could not install rvs 0.16.8; rvs 0.16.7 was restored.",
        ),
        (
            ("0.16.8", "0.16.8"),
            0,
            True,
            "APT could not install rvs 0.16.8; rvs 0.16.7 was restored.",
        ),
        (
            (None, "0.16.8"),
            100,
            True,
            "APT could not install rvs 0.16.8, and restoring rvs 0.16.7 also failed. "
            "Restore the package with: sudo apt-get install --allow-downgrades ravenstash-cli=0.16.7",
        ),
        (
            # APT stopped before it changed the package, so there is nothing to restore.
            ("0.16.7", "0.16.8"),
            0,
            False,
            "APT could not install rvs 0.16.8; rvs 0.16.7 is still installed.",
        ),
    ],
)
def test_update_apply_restores_the_installed_version_when_apt_fails(
    monkeypatch: Any,
    tmp_path: Path,
    after_failure: tuple[str | None, str],
    restore_exit: int,
    restored: bool,
    message: str,
) -> None:
    apt_get = tmp_path / "apt-get"
    apt_get.touch()
    versions = iter((("0.16.7", "0.16.7"), ("0.16.7", "0.16.8"), after_failure))
    exits = iter((0, 100, restore_exit))
    calls: list[list[str]] = []
    monkeypatch.setattr(update_mod, "_APT_GET", apt_get)
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: next(versions))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(update_mod, "_channel_manifest", lambda: _manifest("0.16.8"))
    monkeypatch.setattr(
        update_mod, "_run_visible", lambda command: calls.append(command) or _completed(next(exits))
    )

    result = runner.invoke(app, ["update", "--apply", "--yes"])

    expected = [
        [str(apt_get), "update"],
        [str(apt_get), "install", "--only-upgrade", "--yes", "ravenstash-cli=0.16.8"],
    ]
    if restored:
        expected.append(
            [str(apt_get), "install", "--yes", "--allow-downgrades", "ravenstash-cli=0.16.7"]
        )
    assert result.exit_code == 1
    assert calls == expected
    # The original failure is always the reported error, whatever the restore did.
    assert _one_line(result.stderr).endswith(f"Error: {message}")
    assert "Updated rvs" not in result.output


@pytest.mark.parametrize(
    ("installed", "after_failure", "restore_exit", "restored", "message"),
    [
        (
            # The candidate was unpacked but never became cleanly installed.
            "0.16.3",
            None,
            0,
            True,
            "APT could not install rvs 0.16.4rc1; rvs 0.16.3 was restored.",
        ),
        (
            "0.16.3",
            "0.16.4~rc1",
            0,
            True,
            "APT could not install rvs 0.16.4rc1; rvs 0.16.3 was restored.",
        ),
        (
            "0.16.3",
            None,
            100,
            True,
            "APT could not install rvs 0.16.4rc1, and restoring rvs 0.16.3 also failed. "
            "Restore the package with: sudo apt-get install --allow-downgrades ravenstash-cli=0.16.3",
        ),
        (
            # APT stopped before it changed the package, so there is nothing to restore.
            "0.16.3",
            "0.16.3",
            0,
            False,
            "APT could not install rvs 0.16.4rc1; rvs 0.16.3 is still installed.",
        ),
        (
            # An earlier candidate came from GitHub; APT has no copy to restore.
            "0.16.3~rc2",
            None,
            0,
            False,
            "APT could not install rvs 0.16.4rc1; rvs 0.16.3~rc2 is not published to APT "
            "and was not restored.",
        ),
    ],
)
def test_candidate_apply_restores_the_installed_version_when_apt_fails(
    monkeypatch: Any,
    tmp_path: Path,
    installed: str,
    after_failure: str | None,
    restore_exit: int,
    restored: bool,
    message: str,
) -> None:
    apt_get = tmp_path / "apt-get"
    dpkg_deb = tmp_path / "dpkg-deb"
    for path in (apt_get, dpkg_deb):
        path.touch()
    versions = iter(((installed, installed), (after_failure, installed)))
    exits = iter((100, restore_exit))
    calls: list[list[str]] = []
    monkeypatch.setattr(update_mod, "_APT_GET", apt_get)
    monkeypatch.setattr(update_mod, "_DPKG_DEB", dpkg_deb)
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: next(versions))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda installed, candidate: True)

    def fake_download(candidate: str, destination: Path) -> Path:
        package = destination / f"ravenstash-cli_{candidate}_amd64.deb"
        package.touch()
        return package

    monkeypatch.setattr(update_mod, "_download_candidate", fake_download)
    monkeypatch.setattr(
        update_mod, "_run_visible", lambda command: calls.append(command) or _completed(next(exits))
    )

    result = runner.invoke(app, ["update", "--candidate", "0.16.4rc1", "--apply", "--yes"])

    assert result.exit_code == 1
    assert calls[0][:3] == [str(apt_get), "install", "--yes"]
    assert Path(calls[0][3]).name == "ravenstash-cli_0.16.4rc1_amd64.deb"
    assert calls[1:] == (
        [[str(apt_get), "install", "--yes", "--allow-downgrades", f"ravenstash-cli={installed}"]]
        if restored
        else []
    )
    # The original failure is always the reported error, whatever the restore did.
    assert _one_line(result.stderr).endswith(f"Error: {message}")
    assert "Updated rvs" not in result.output


@pytest.mark.parametrize("installed", ["0.16.4~rc1", "0.16.4~dev123+gabcdef12"])
def test_apt_restore_leaves_a_version_that_apt_never_published(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str], installed: str
) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: (None, "0.16.4"))
    monkeypatch.setattr(
        update_mod,
        "_run_visible",
        lambda command: (_ for _ in ()).throw(AssertionError("APT cannot restore this version")),
    )

    with pytest.raises(SystemExit) as exc_info:
        update_mod._restore_apt_version(installed, "0.16.4")

    assert exc_info.value.code == 1
    assert _one_line(capsys.readouterr().err) == (
        f"Error: APT could not install rvs 0.16.4; rvs {installed} is not published to APT "
        "and was not restored."
    )


def test_update_to_minor_leaves_later_plain_updates_on_the_newest_release(
    monkeypatch: Any, tmp_path: Path
) -> None:
    apt_get = tmp_path / "apt-get"
    apt_get.touch()
    installed = ["0.16.3"]
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> Any:
        calls.append(command)
        if command[1] == "install":
            installed[0] = command[-1].removeprefix("ravenstash-cli=")
        return _completed()

    monkeypatch.setattr(update_mod, "_APT_GET", apt_get)
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: (installed[0], "0.18.0"))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(update_mod, "_channel_manifest", lambda: _MINOR_LINES)
    monkeypatch.setattr(update_mod, "_run_visible", fake_run)

    selected = runner.invoke(app, ["update", "--to", "0.17", "--apply", "--yes"])

    assert selected.exit_code == 0, selected.output
    assert installed == ["0.17.2"]
    # One refresh and one exact install: the selector leaves no pin or hold behind.
    assert calls == [
        [str(apt_get), "update"],
        [str(apt_get), "install", "--only-upgrade", "--yes", "ravenstash-cli=0.17.2"],
    ]

    preview = runner.invoke(app, ["update"])
    applied = runner.invoke(app, ["update", "--apply", "--yes"])

    assert preview.exit_code == 0, preview.output
    assert "rvs 0.18.0 is available (installed: 0.17.2)" in _one_line(preview.output)
    assert applied.exit_code == 0, applied.output
    assert calls[-1] == [
        str(apt_get),
        "install",
        "--only-upgrade",
        "--yes",
        "ravenstash-cli=0.18.0",
    ]
    assert installed == ["0.18.0"]


def test_update_reports_a_release_the_local_apt_list_does_not_know_yet(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.7", "0.16.7"))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(update_mod, "_channel_manifest", lambda: _manifest("0.16.8"))
    monkeypatch.setattr(update_mod, "_run_visible", lambda command: _completed(1))

    result = runner.invoke(app, ["update"])
    output = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "rvs 0.16.8 is available (installed: 0.16.7)" in output
    assert "rvs update --apply" in output
    assert "up to date" not in output


def test_update_apply_refreshes_only_the_ravenstash_source_then_installs(
    monkeypatch: Any, tmp_path: Path
) -> None:
    apt_get = tmp_path / "apt-get"
    apt_get.touch()
    source = tmp_path / "ravenstash-cli.list"
    source.write_text("deb [arch=amd64] https://releases.ravenstash.com/ravenstash/apt v0 main\n")
    versions = iter((("0.16.7", "0.16.7"), ("0.16.7", "0.16.8")))
    monkeypatch.setattr(update_mod, "_APT_GET", apt_get)
    monkeypatch.setattr(update_mod, "_APT_SOURCE", source)
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: next(versions))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(update_mod, "_channel_manifest", lambda: _manifest("0.16.8"))
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> Any:
        calls.append(command)
        return _completed()

    monkeypatch.setattr(update_mod, "_run_visible", fake_run)

    result = runner.invoke(app, ["update", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls == [
        [
            str(apt_get),
            "update",
            "-o",
            f"Dir::Etc::sourcelist={source}",
            "-o",
            "Dir::Etc::sourceparts=-",
            "-o",
            "APT::Get::List-Cleanup=0",
        ],
        [str(apt_get), "install", "--only-upgrade", "--yes", "ravenstash-cli=0.16.8"],
    ]
    assert "Updated rvs to 0.16.8" in result.output


def test_update_apply_stops_when_apt_still_lacks_the_signed_release(
    monkeypatch: Any, tmp_path: Path
) -> None:
    apt_get = tmp_path / "apt-get"
    apt_get.touch()
    monkeypatch.setattr(update_mod, "_APT_GET", apt_get)
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.7", "0.16.7"))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(update_mod, "_channel_manifest", lambda: _manifest("0.16.8"))
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> Any:
        calls.append(command)
        return _completed()

    monkeypatch.setattr(update_mod, "_run_visible", fake_run)

    result = runner.invoke(app, ["update", "--apply", "--yes"])
    output = " ".join(result.output.split())

    assert result.exit_code == 1
    assert "APT does not offer rvs 0.16.8" in output
    assert [command[1] for command in calls] == ["update"]


def test_update_apply_reports_a_failed_ravenstash_refresh(monkeypatch: Any, tmp_path: Path) -> None:
    apt_get = tmp_path / "apt-get"
    apt_get.touch()
    monkeypatch.setattr(update_mod, "_APT_GET", apt_get)
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.7", "0.16.7"))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(update_mod, "_channel_manifest", lambda: _manifest("0.16.8"))
    monkeypatch.setattr(update_mod, "_run_visible", lambda command: _completed(100))

    result = runner.invoke(app, ["update", "--apply", "--yes"])

    assert result.exit_code == 1
    assert "refresh for the Ravenstash source failed" in " ".join(result.output.split())


def test_update_without_signed_channel_says_the_check_used_local_apt(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.7", "0.16.7"))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")

    result = runner.invoke(app, ["update"])
    output = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "You are up to date" in output
    assert "sudo apt-get update" in output


def test_update_with_signed_channel_confirms_up_to_date_without_apt_hint(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.8", "0.16.7"))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(update_mod, "_channel_manifest", lambda: _manifest("0.16.8"))

    result = runner.invoke(app, ["update"])
    output = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "Latest in release channel v0: rvs 0.16.8. You are up to date" in output
    assert "apt-get update" not in output


def _channels(recommended: str = "v0", **channels: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "recommended": recommended,
        "generated_at": "2026-09-27T05:00:00Z",
        "expires": "2999-01-01T00:00:00Z",
        "channels": channels,
    }


def _supported(latest: str, **minor_targets: str) -> dict[str, Any]:
    targets = minor_targets or {latest.rpartition(".")[0]: latest}
    return {"latest": latest, "minor_targets": targets, "status": "supported"}


def _one_line(text: str) -> str:
    return " ".join(text.split())


# ── APT inspection ────────────────────────────────────────────────────────────


def test_apt_versions_reads_installed_package_and_policy_candidate(
    monkeypatch: Any, tmp_path: Path
) -> None:
    dpkg_query = tmp_path / "dpkg-query"
    apt_cache = tmp_path / "apt-cache"
    dpkg_query.touch()
    apt_cache.touch()
    monkeypatch.setattr(update_mod, "_DPKG_QUERY", dpkg_query)
    monkeypatch.setattr(update_mod, "_APT_CACHE", apt_cache)
    outputs = {
        str(dpkg_query): _completed(stdout="ii 0.16.3\n"),
        str(apt_cache): _completed(
            stdout="rvs:\n  Installed: 0.16.3\n  Candidate: 0.16.4\n  Version table:\n"
        ),
    }
    monkeypatch.setattr(update_mod, "_run", lambda command: outputs[command[0]])

    assert update_mod._apt_versions() == ("0.16.3", "0.16.4")


def test_apt_versions_ignores_removed_package_and_missing_candidate(
    monkeypatch: Any, tmp_path: Path
) -> None:
    dpkg_query = tmp_path / "dpkg-query"
    apt_cache = tmp_path / "apt-cache"
    dpkg_query.touch()
    apt_cache.touch()
    monkeypatch.setattr(update_mod, "_DPKG_QUERY", dpkg_query)
    monkeypatch.setattr(update_mod, "_APT_CACHE", apt_cache)
    outputs = {
        str(dpkg_query): _completed(stdout="rc 0.16.3\n"),
        str(apt_cache): _completed(stdout="rvs:\n  Candidate: (none)\n"),
    }
    monkeypatch.setattr(update_mod, "_run", lambda command: outputs[command[0]])

    assert update_mod._apt_versions() == (None, None)


def test_apt_versions_is_empty_without_dpkg(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(update_mod, "_DPKG_QUERY", tmp_path / "missing-dpkg-query")
    monkeypatch.setattr(
        update_mod, "_run", lambda command: (_ for _ in ()).throw(AssertionError("ran APT"))
    )

    assert update_mod._apt_versions() == (None, None)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "deb [arch=amd64 signed-by=/etc/apt/keyrings/ravenstash-cli.gpg] "
            "https://releases.ravenstash.com/ravenstash/apt v1 main\n",
            "v1",
        ),
        ("deb https://mirror.example.test/ravenstash/apt v1 main\n", None),
        (None, None),
    ],
)
def test_current_channel_trusts_only_the_signed_ravenstash_source(
    monkeypatch: Any, tmp_path: Path, source: str | None, expected: str | None
) -> None:
    source_file = tmp_path / "ravenstash-cli.list"
    if source is not None:
        source_file.write_text(source, encoding="utf-8")
    monkeypatch.setattr(update_mod, "_APT_SOURCE", source_file)

    assert update_mod._current_channel() == expected


def test_root_command_uses_sudo_when_not_root(monkeypatch: Any, tmp_path: Path) -> None:
    sudo = tmp_path / "sudo"
    sudo.touch()
    monkeypatch.setattr(update_mod, "_SUDO", sudo)
    monkeypatch.setattr(update_mod.os, "geteuid", lambda: 1000, raising=False)

    assert update_mod._root_command(["apt-get", "update"]) == [str(sudo), "apt-get", "update"]


def test_root_command_requires_sudo_when_not_root(
    monkeypatch: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(update_mod, "_SUDO", tmp_path / "missing-sudo")
    monkeypatch.setattr(update_mod.os, "geteuid", lambda: 1000, raising=False)

    with pytest.raises(SystemExit):
        update_mod._root_command(["apt-get", "update"])

    assert _one_line(capsys.readouterr().err) == (
        "Error: Updating requires root privileges and /usr/bin/sudo is unavailable."
    )


# ── Signed APT channel manifest ───────────────────────────────────────────────


def _serve_apt_channels(
    httpx2_mock: Any, monkeypatch: Any, tmp_path: Path, manifest: dict[str, Any], gpgv_exit: int = 0
) -> list[list[str]]:
    keyring = tmp_path / "ravenstash-cli.gpg"
    gpgv = tmp_path / "gpgv"
    keyring.touch()
    gpgv.touch()
    monkeypatch.setattr(update_mod, "_APT_KEYRING", keyring)
    monkeypatch.setattr(update_mod, "_GPGV", gpgv)
    monkeypatch.setenv("RVS_HOME", str(tmp_path / "rvs-home"))
    httpx2_mock.add_response(url=update_mod._CHANNELS_URL, json=manifest)
    httpx2_mock.add_response(url=update_mod._CHANNELS_SIGNATURE_URL, content=b"signature")
    verifications: list[list[str]] = []
    monkeypatch.setattr(
        update_mod.subprocess,
        "run",
        lambda command, **_kwargs: verifications.append(command) or _completed(gpgv_exit),
    )
    return verifications


def test_apt_channel_manifest_accepts_gpgv_verified_manifest(
    httpx2_mock: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    manifest = _channels(v0=_supported("0.17.1", **{"0.16": "0.16.3", "0.17": "0.17.1"}))
    verifications = _serve_apt_channels(httpx2_mock, monkeypatch, tmp_path, manifest)

    assert _verified_apt_channel_manifest() == manifest
    assert len(verifications) == 1
    assert verifications[0][:2] == [
        str(tmp_path / "gpgv"),
        f"--keyring={tmp_path / 'ravenstash-cli.gpg'}",
    ]


@pytest.mark.parametrize(
    ("manifest", "gpgv_exit"),
    [
        (_channels(v0=_supported("0.17.1")), 1),
        ({**_channels(v0=_supported("0.17.1")), "schema": 2}, 0),
        (_channels(recommended="v1", v0=_supported("0.17.1")), 0),
        (_channels(v0=_supported("1.0.0")), 0),
        (_channels(v0=_supported("0.17.1", **{"0.17": "0.16.9"})), 0),
        (_channels(v0={"latest": "0.17.1", "minor_targets": {}, "status": "supported"}), 0),
    ],
    ids=[
        "bad-signature",
        "unknown-schema",
        "unknown-recommendation",
        "latest-outside-channel",
        "minor-target-mismatch",
        "no-minor-targets",
    ],
)
def test_apt_channel_manifest_rejects_unverified_or_inconsistent_manifest(
    httpx2_mock: Any, monkeypatch: Any, tmp_path: Path, manifest: dict[str, Any], gpgv_exit: int
) -> None:
    _serve_apt_channels(httpx2_mock, monkeypatch, tmp_path, manifest, gpgv_exit)

    assert _verified_apt_channel_manifest() is None


def test_apt_channel_manifest_ignores_a_manifest_without_a_validity_period(
    httpx2_mock: Any, monkeypatch: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _channels(v0=_supported("0.17.1"))
    del manifest["generated_at"], manifest["expires"]
    _serve_apt_channels(httpx2_mock, monkeypatch, tmp_path, manifest)

    assert _verified_apt_channel_manifest() is None
    assert "Ignoring the release-channel manifest" in _one_line(capsys.readouterr().err)


def test_apt_channel_manifest_requires_installed_keyring(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(update_mod, "_APT_KEYRING", tmp_path / "missing.gpg")

    assert _verified_apt_channel_manifest() is None


# ── Target selection ──────────────────────────────────────────────────────────


def test_requested_target_resolves_channels_and_minor_lines() -> None:
    manifest = _channels(
        recommended="v1",
        v0=_supported("0.18.0", **{"0.17": "0.17.2", "0.18": "0.18.0"}),
        v1=_supported("1.2.0"),
    )

    assert update_mod._requested_target(manifest, "v0", None) == (
        "v0",
        "0.18.0",
        "release channel v0",
    )
    assert update_mod._requested_target(manifest, "v0", "1") == (
        "v1",
        "1.2.0",
        "release channel v1",
    )
    assert update_mod._requested_target(manifest, "v0", "0.17") == (
        "v0",
        "0.17.2",
        "minor release 0.17",
    )


@pytest.mark.parametrize(
    ("requested", "message"),
    [
        ("0", "release-channel downgrades are not supported automatically"),
        ("0.18", "release-channel downgrades are not supported automatically"),
        ("latest", "update target must look like 0, v0, or 0.15"),
    ],
)
def test_requested_target_rejects_downgrades_and_malformed_targets(
    requested: str, message: str
) -> None:
    manifest = _channels(recommended="v1", v0=_supported("0.18.0"), v1=_supported("1.2.0"))

    with pytest.raises(ValueError) as raised:
        update_mod._requested_target(manifest, "v1", requested)

    assert str(raised.value) == message


def test_channel_latest_ignores_unsupported_channel() -> None:
    manifest = _channels(v0={**_supported("0.18.0"), "status": "retired"})

    assert update_mod._channel_latest(manifest, "v0") is None
    assert update_mod._channel_latest(None, "v0") is None


def test_update_announces_newer_recommended_channel(monkeypatch: Any) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.18.0", "0.18.0"))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(
        update_mod,
        "_channel_manifest",
        lambda: _channels(recommended="v1", v0=_supported("0.18.0"), v1=_supported("1.0.0")),
    )

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 0, result.output
    assert (
        "rvs 1.0.0 is available in release channel v1. "
        "Review the migration notes, then run: rvs update --to 1"
    ) in _one_line(result.output)


def test_update_reports_missing_apt_candidate(monkeypatch: Any) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.3", None))

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 1
    assert _one_line(result.stderr) == (
        "Error: APT has no rvs candidate. Check that the Ravenstash source is configured, "
        "then run `sudo apt-get update`."
    )


@pytest.mark.parametrize(
    ("refreshed", "message"),
    [
        (("0.16.7", "0.16.9"), "APT now offers rvs 0.16.9 instead of 0.16.8"),
        (("0.16.6", "0.16.8"), "The installed package changed during refresh"),
    ],
)
def test_update_apply_stops_when_apt_state_disagrees(
    monkeypatch: Any,
    tmp_path: Path,
    refreshed: tuple[str, str],
    message: str,
) -> None:
    apt_get = tmp_path / "apt-get"
    apt_get.touch()
    versions = iter((("0.16.7", "0.16.7"), refreshed))
    calls: list[list[str]] = []
    monkeypatch.setattr(update_mod, "_APT_GET", apt_get)
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: next(versions))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(update_mod, "_channel_manifest", lambda: _manifest("0.16.8"))
    monkeypatch.setattr(
        update_mod, "_run_visible", lambda command: calls.append(command) or _completed()
    )

    result = runner.invoke(app, ["update", "--apply", "--yes"])

    assert result.exit_code == 1
    assert message in _one_line(result.stderr)
    assert calls == [[str(apt_get), "update"]]


def test_update_apply_requires_apt_get(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(update_mod, "_APT_GET", tmp_path / "missing-apt-get")
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.7", "0.16.8"))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")

    result = runner.invoke(app, ["update", "--apply", "--yes"])

    assert result.exit_code == 1
    assert "Cannot update because /usr/bin/apt-get is unavailable." in result.stderr


@pytest.mark.parametrize(
    ("channel", "manifest", "message"),
    [
        (None, _manifest("0.17.2"), "rvs is not managed by a recognized Ravenstash APT source."),
        ("v0", None, "Cannot authenticate the Ravenstash release-channel manifest."),
    ],
)
def test_update_to_requires_recognized_source_and_signed_manifest(
    monkeypatch: Any, channel: str | None, manifest: dict[str, Any] | None, message: str
) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.3", "0.16.3"))
    monkeypatch.setattr(update_mod, "_current_channel", lambda: channel)
    monkeypatch.setattr(update_mod, "_channel_manifest", lambda: manifest)

    result = runner.invoke(app, ["update", "--to", "0.17"])

    assert result.exit_code == 1
    assert _one_line(result.stderr) == f"Error: {message}"


def test_update_to_preview_names_target_and_apply_command(monkeypatch: Any) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: ("0.16.3", "0.16.3"))
    monkeypatch.setattr(update_mod, "_upgrade_available", _newer)
    monkeypatch.setattr(update_mod, "_current_channel", lambda: "v0")
    monkeypatch.setattr(
        update_mod,
        "_channel_manifest",
        lambda: _channels(v0=_supported("0.18.0", **{"0.17": "0.17.2", "0.18": "0.18.0"})),
    )

    result = runner.invoke(app, ["update", "--to", "0.17"])

    assert result.exit_code == 0, result.output
    output = _one_line(result.output)
    assert "Target: minor release 0.17, rvs 0.17.2" in output
    assert "Install it with: rvs update --to 0.17 --apply" in output


# ── Release candidates on APT installations ───────────────────────────────────


@pytest.mark.parametrize(
    ("installed", "has_dpkg_deb", "newer_candidate", "message"),
    [
        (None, True, True, "Release candidates can only update an existing rvs APT installation."),
        ("0.16.3", False, True, "Candidate verification requires /usr/bin/dpkg-deb."),
        (
            "0.16.5",
            True,
            False,
            "Release candidate 0.16.4rc1 is not newer than installed rvs 0.16.5.",
        ),
    ],
)
def test_candidate_update_refuses_unsafe_starting_points(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    installed: str | None,
    has_dpkg_deb: bool,
    newer_candidate: bool,
    message: str,
) -> None:
    dpkg_deb = tmp_path / "dpkg-deb"
    if has_dpkg_deb:
        dpkg_deb.touch()
    monkeypatch.setattr(update_mod, "_DPKG_DEB", dpkg_deb)
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: (installed, None))
    monkeypatch.setattr(update_mod, "_upgrade_available", lambda *_args: newer_candidate)
    monkeypatch.setattr(
        update_mod,
        "_download_candidate",
        lambda *_args: (_ for _ in ()).throw(AssertionError("downloaded a candidate")),
    )

    with pytest.raises(SystemExit):
        update_mod._update_candidate("0.16.4rc1", apply=False, yes=False)

    assert _one_line(capsys.readouterr().err) == f"Error: {message}"


def test_candidate_download_rejects_mismatched_package_metadata(
    monkeypatch: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "ravenstash-cli_0.16.4rc1_arm64.deb"
    monkeypatch.setattr(update_mod.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(
        update_mod,
        "download_verified_assets",
        lambda version, names, destination, *, candidate: {next(iter(names)): package},
    )
    monkeypatch.setattr(
        update_mod, "_run", lambda command: _completed(stdout="ravenstash-cli\n0.16.4\narm64\n")
    )

    with pytest.raises(SystemExit):
        update_mod._download_candidate("0.16.4rc1", tmp_path)

    assert _one_line(capsys.readouterr().err) == (
        "Error: The candidate package metadata does not match the requested release."
    )


def test_candidate_download_reports_authentication_failure(
    monkeypatch: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def rejected(*_args: Any, **_kwargs: Any) -> dict[str, Path]:
        raise portable_update_mod.UpdateError("the release checksum signature is invalid")

    monkeypatch.setattr(update_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(update_mod, "download_verified_assets", rejected)

    with pytest.raises(SystemExit):
        update_mod._download_candidate("0.16.4rc1", tmp_path)

    assert _one_line(capsys.readouterr().err) == "Error: the release checksum signature is invalid"


def test_candidate_architecture_rejects_unsupported_cpu(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(update_mod.platform, "machine", lambda: "riscv64")

    with pytest.raises(SystemExit):
        update_mod._candidate_architecture()

    assert _one_line(capsys.readouterr().err) == (
        "Error: Candidate packages are available only for Linux amd64 and arm64."
    )


# ── Portable installations ────────────────────────────────────────────────────


def _use_portable(monkeypatch: Any, installation: Installation, manifest: Any) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: (None, None))
    monkeypatch.setattr(update_mod, "_portable_installation", lambda: installation)
    monkeypatch.setattr(update_mod, "fetch_channel_manifest", manifest)


def test_portable_update_reports_up_to_date_and_newer_channel(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _use_portable(
        monkeypatch,
        _portable(tmp_path),
        lambda: _channels(recommended="v1", v0=_supported("0.16.3"), v1=_supported("1.0.0")),
    )

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 0, result.output
    output = _one_line(result.output)
    assert (
        "Installed: rvs 0.16.3. Latest in release channel v0: rvs 0.16.3. You are up to date."
    ) in output
    assert "rvs 1.0.0 is available in release channel v1." in output


def test_portable_update_refuses_older_target(monkeypatch: Any, tmp_path: Path) -> None:
    _use_portable(
        monkeypatch,
        _portable(tmp_path),
        lambda: _channels(v0=_supported("0.16.3", **{"0.15": "0.15.9", "0.16": "0.16.3"})),
    )

    result = runner.invoke(app, ["update", "--to", "0.15"])

    assert result.exit_code == 1
    assert _one_line(result.stderr) == (
        "Error: The requested release is not newer than the installed portable version."
    )


def test_portable_update_reports_unauthenticated_manifest(monkeypatch: Any, tmp_path: Path) -> None:
    def rejected() -> dict[str, Any]:
        raise portable_update_mod.UpdateError(
            "could not authenticate the Ravenstash release-channel manifest"
        )

    _use_portable(monkeypatch, _portable(tmp_path), rejected)

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 1
    assert _one_line(result.stderr) == (
        "Error: could not authenticate the Ravenstash release-channel manifest"
    )


@pytest.mark.skipif(os.name == "nt", reason="system-scope root check applies to POSIX installs")
def test_portable_system_update_requires_root(monkeypatch: Any, tmp_path: Path) -> None:
    installation = replace(_portable(tmp_path), scope="system")
    _use_portable(monkeypatch, installation, lambda: _manifest("0.16.4"))
    monkeypatch.setattr(update_mod.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        update_mod,
        "apply_portable_update",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("applied update")),
    )

    result = runner.invoke(app, ["update", "--apply", "--yes"])

    assert result.exit_code == 1
    assert _one_line(result.stderr) == (
        "Error: System-wide portable updates require root privileges. Run: sudo rvs update --apply"
    )


@pytest.mark.parametrize(
    ("outcome", "exit_code", "message"),
    [
        (True, 0, "Verified and staged rvs 0.16.4. It will activate after this command exits."),
        (False, 0, "Updated rvs to 0.16.4."),
        (
            portable_update_mod.UpdateError("another rvs update is already running"),
            1,
            "another rvs update is already running",
        ),
    ],
)
def test_portable_update_apply_reports_activation_outcome(
    monkeypatch: Any, tmp_path: Path, outcome: Any, exit_code: int, message: str
) -> None:
    _use_portable(monkeypatch, _portable(tmp_path), lambda: _manifest("0.16.4"))

    def apply(installation: Installation, version: str, *, candidate: bool) -> bool:
        assert (version, candidate) == ("0.16.4", False)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(update_mod, "apply_portable_update", apply)

    result = runner.invoke(app, ["update", "--apply", "--yes"])

    assert result.exit_code == exit_code
    assert message in _one_line(result.output)


def test_portable_installation_reports_invalid_receipt(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    def invalid(**_kwargs: Any) -> Installation:
        raise ValueError("invalid target in rvs installation receipt")

    monkeypatch.setattr(update_mod, "detect_portable_installation", invalid)

    with pytest.raises(SystemExit):
        update_mod._portable_installation()

    assert _one_line(capsys.readouterr().err) == (
        "Error: invalid target in rvs installation receipt"
    )


# ── Package-manager installations ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("executable", "frozen", "expected"),
    [
        ("/nix/store/abc123-ravenstash-cli-env/bin/python3", False, "nix"),
        ("/opt/homebrew/Cellar/ravenstash-cli@0/0.16.3/libexec/rvs", True, "homebrew"),
        ("/usr/local/lib/rvs/rvs", True, None),
        ("/home/developer/.venv/bin/python3", False, None),
    ],
)
def test_managed_method_detects_package_manager_from_executable(
    monkeypatch: Any, executable: str, frozen: bool, expected: str | None
) -> None:
    monkeypatch.setattr(update_mod.sys, "executable", executable)
    monkeypatch.setattr(update_mod.sys, "frozen", frozen, raising=False)

    assert update_mod._managed_method() == expected


@pytest.mark.parametrize(
    ("executable", "expected"),
    [
        (
            r"C:\Users\dev\AppData\Local\Microsoft\WinGet\Packages\Ravenstash.RavenstashCLI.v0_x\rvs.exe",
            "winget",
        ),
        (r"C:\Program Files\rvs\rvs.exe", None),
    ],
)
def test_managed_method_detects_winget_installations_on_windows(
    monkeypatch: Any, executable: str, expected: str | None
) -> None:
    monkeypatch.setattr(update_mod.sys, "executable", executable)
    monkeypatch.setattr(update_mod.sys, "frozen", True, raising=False)
    # Only the platform check is Windows-specific; path handling is portable.
    monkeypatch.setattr(update_mod, "os", SimpleNamespace(name="nt"))

    assert update_mod._managed_method() == expected


def _use_managed(monkeypatch: Any, method: str, installed: str, manifest: dict[str, Any]) -> None:
    monkeypatch.setattr(update_mod, "_apt_versions", lambda: (None, None))
    monkeypatch.setattr(update_mod, "_portable_installation", lambda: None)
    monkeypatch.setattr(update_mod, "_managed_method", lambda: method)
    monkeypatch.setattr(update_mod, "_cli_version", lambda: installed)
    monkeypatch.setattr(update_mod, "fetch_channel_manifest", lambda: manifest)


@pytest.mark.parametrize(
    ("method", "arguments", "expected"),
    [
        ("homebrew", ["update"], "Install it with: brew upgrade ravenstash-cli@0"),
        (
            "homebrew",
            ["update", "--to", "1"],
            "Install it with: rvs update --to 1 --apply (replaces the v0 homebrew package)",
        ),
        (
            "nix",
            ["update"],
            "Install it with: rvs update --apply "
            "(replaces the pinned Nix profile with tag v0.16.4)",
        ),
    ],
)
def test_managed_update_preview_names_the_package_manager_command(
    monkeypatch: Any, method: str, arguments: list[str], expected: str
) -> None:
    _use_managed(
        monkeypatch,
        method,
        "0.16.3",
        _channels(v0=_supported("0.16.4"), v1=_supported("1.0.0")),
    )

    result = runner.invoke(app, arguments)

    assert result.exit_code == 0, result.output
    assert expected in _one_line(result.output)


def test_managed_update_reports_up_to_date(monkeypatch: Any) -> None:
    _use_managed(monkeypatch, "homebrew", "0.16.4", _manifest("0.16.4"))

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 0, result.output
    assert "Installed: rvs 0.16.4. Latest in release channel v0: rvs 0.16.4." in _one_line(
        result.output
    )


def test_managed_update_refuses_older_target(monkeypatch: Any) -> None:
    _use_managed(monkeypatch, "nix", "0.16.4", _manifest("0.16.3"))

    result = runner.invoke(app, ["update"])

    assert result.exit_code == 1
    assert _one_line(result.stderr) == (
        "Error: The requested release is not newer than the installed managed version."
    )


def test_managed_update_does_not_install_candidates(monkeypatch: Any) -> None:
    _use_managed(monkeypatch, "homebrew", "0.16.3", _manifest("0.16.4"))

    result = runner.invoke(app, ["update", "--candidate", "0.16.4rc1"])

    assert result.exit_code == 1
    assert (
        _one_line(result.stderr) == "Error: Release candidates are not installed through homebrew."
    )


@pytest.mark.parametrize(
    ("which", "exit_code", "message"),
    [
        (None, 1, "Cannot update because brew is unavailable."),
        ("brew", 1, "homebrew could not install the rvs update."),
    ],
)
def test_managed_update_apply_reports_package_manager_failure(
    monkeypatch: Any, which: str | None, exit_code: int, message: str
) -> None:
    _use_managed(monkeypatch, "homebrew", "0.16.3", _manifest("0.16.4"))
    monkeypatch.setattr(update_mod.shutil, "which", lambda command: which)
    monkeypatch.setattr(update_mod.subprocess, "run", lambda command, **_kwargs: _completed(1))

    result = runner.invoke(app, ["update", "--apply", "--yes"])

    assert result.exit_code == exit_code
    assert _one_line(result.stderr) == f"Error: {message}"


def test_managed_update_apply_across_channels_replaces_series(monkeypatch: Any) -> None:
    moved: list[tuple[str, str, str]] = []
    _use_managed(
        monkeypatch,
        "homebrew",
        "0.16.3",
        _channels(v0=_supported("0.16.4"), v1=_supported("1.0.0")),
    )
    monkeypatch.setattr(
        update_mod,
        "_replace_managed_series",
        lambda method, current, target: moved.append((method, current, target)),
    )

    result = runner.invoke(app, ["update", "--to", "1", "--apply", "--yes"])

    assert result.exit_code == 0, result.output
    assert moved == [("homebrew", "v0", "v1")]


@pytest.mark.parametrize(
    ("method", "return_codes", "commands", "message"),
    [
        (
            "homebrew",
            (0, 0),
            [["brew", "uninstall", "ravenstash-cli@0"], ["brew", "install", "ravenstash-cli@1"]],
            "homebrew moved rvs from v0 to v1; open a new shell and run rvs --version.",
        ),
        (
            "winget",
            (0, 1, 0),
            [
                ["winget", "uninstall", "--exact", "--id", "Ravenstash.RavenstashCLI.v0"],
                ["winget", "install", "--exact", "--id", "Ravenstash.RavenstashCLI.v1"],
                ["winget", "install", "--exact", "--id", "Ravenstash.RavenstashCLI.v0"],
            ],
            "winget could not move rvs to v1; the v0 package was restored.",
        ),
        (
            "homebrew",
            (0, 1, 1),
            [
                ["brew", "uninstall", "ravenstash-cli@0"],
                ["brew", "install", "ravenstash-cli@1"],
                ["brew", "install", "ravenstash-cli@0"],
            ],
            "homebrew series migration and rollback both failed. "
            "Restore the package with: brew install ravenstash-cli@0",
        ),
        (
            "homebrew",
            (1,),
            [["brew", "uninstall", "ravenstash-cli@0"]],
            "homebrew could not remove the existing ravenstash-cli@0 package.",
        ),
    ],
)
def test_managed_series_replacement_restores_previous_package_on_failure(
    monkeypatch: Any,
    capsys: pytest.CaptureFixture[str],
    method: str,
    return_codes: tuple[int, ...],
    commands: list[list[str]],
    message: str,
) -> None:
    calls: list[list[str]] = []
    codes = iter(return_codes)
    monkeypatch.setattr(update_mod.shutil, "which", lambda command: command)
    monkeypatch.setattr(
        update_mod.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or _completed(next(codes)),
    )

    try:
        update_mod._replace_managed_series(method, "v0", "v1")
    except SystemExit as exc:
        assert exc.code == 1

    captured = capsys.readouterr()
    assert calls == commands
    assert message in _one_line(captured.out + captured.err)


def test_nix_update_replaces_profile_with_target_tag(
    monkeypatch: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(update_mod.shutil, "which", lambda command: command)
    monkeypatch.setattr(
        update_mod.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or _completed(),
    )

    update_mod._replace_nix_profile("0.16.3", "0.16.4")

    assert calls[-1] == ["nix", "profile", "install", "github:rvstash/ravenstash-cli/v0.16.4"]
    assert "Nix updated the rvs profile to 0.16.4" in _one_line(capsys.readouterr().out)
