import importlib.util
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "scripts" / "prepare_test_build.py"
SPEC = importlib.util.spec_from_file_location("prepare_test_build", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
prepare_test_build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_test_build)


def test_derive_version_is_traceable_and_pep_440_ordered() -> None:
    version = prepare_test_build.derive_version(
        "0.15.4", "0123456789abcdef0123456789abcdef01234567", "12345"
    )
    assert version == "0.15.4.dev12345+g01234567"


@pytest.mark.parametrize(
    ("base_version", "source_sha", "run_id"),
    [
        ("0.15", "0" * 40, "1"),
        ("0.15.4", "ABCDEF" * 6 + "ABCD", "1"),
        ("0.15.4", "0" * 40, "0"),
    ],
)
def test_derive_version_rejects_ambiguous_identity(
    base_version: str, source_sha: str, run_id: str
) -> None:
    with pytest.raises(ValueError):
        prepare_test_build.derive_version(base_version, source_sha, run_id)


def test_apply_version_updates_every_build_identity(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    lock = tmp_path / "uv.lock"
    packaging = tmp_path / "packaging"
    packaging.mkdir()
    installer = packaging / "install.sh"
    windows_installer = packaging / "install.ps1"
    project.write_text(
        '[build-system]\nrequires = ["setuptools>=75"]\n\n'
        '[project]\nname = "ravenstash-cli"\nversion = "0.15.3"\n',
        encoding="utf-8",
    )
    installer.write_text('readonly release_version="0.15.3"\n', encoding="utf-8")
    windows_installer.write_text('$ReleaseVersion = "0.15.3"\n', encoding="utf-8")
    lock.write_text(
        'version = 1\n\n[[package]]\nname = "other"\nversion = "9.0"\n\n'
        '[[package]]\nname = "ravenstash-cli"\nversion = "0.15.3"\nsource = { editable = "." }\n',
        encoding="utf-8",
    )

    version = "0.15.4.dev12345+g01234567"
    prepare_test_build.apply_version(tmp_path, version)

    with project.open("rb") as source:
        assert tomllib.load(source)["project"]["version"] == version
    assert 'name = "other"\nversion = "9.0"' in lock.read_text(encoding="utf-8")
    assert f'name = "ravenstash-cli"\nversion = "{version}"' in lock.read_text(encoding="utf-8")
    assert installer.read_text(encoding="utf-8") == f'readonly release_version="{version}"\n'
    assert windows_installer.read_text(encoding="utf-8") == f'$ReleaseVersion = "{version}"\n'


@pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg is unavailable")
def test_debian_test_version_sorts_before_candidate_and_stable() -> None:
    common = ROOT / "packaging" / "scripts" / "common.sh"
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{common}"; rvs_debian_version "0.15.4.dev12345+g01234567"',
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "0.15.4~dev12345+g01234567"
    assert (
        subprocess.run(
            [
                "dpkg",
                "--compare-versions",
                result.stdout.strip(),
                "lt",
                "0.15.4~rc1",
            ],
            check=False,
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            ["dpkg", "--compare-versions", "0.15.4~rc1", "lt", "0.15.4"],
            check=False,
        ).returncode
        == 0
    )
