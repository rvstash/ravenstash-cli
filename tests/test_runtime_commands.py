import hashlib
import io
import os
import tarfile
from typing import TYPE_CHECKING

import pytest
from rvs.runtime import _install
from rvs.runtime import commands as runtime_cmd
from rvs.runtime import java as java_rt
from rvs.runtime import node as node_rt
from rvs.runtime import python as python_rt
from rvs.runtime._install import RuntimePlatform
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pathlib import Path


runner = CliRunner()


def _point_runtime_dirs(monkeypatch, runtimes_dir: Path) -> None:
    monkeypatch.setattr(python_rt, "RUNTIMES_DIR", runtimes_dir)
    monkeypatch.setattr(node_rt, "RUNTIMES_DIR", runtimes_dir)
    monkeypatch.setattr(java_rt, "RUNTIMES_DIR", runtimes_dir)


def test_runtime_list_displays_all_managed_runtimes(monkeypatch, tmp_path: Path) -> None:
    runtimes_dir = tmp_path / "runtimes"
    _point_runtime_dirs(monkeypatch, runtimes_dir)
    (runtimes_dir / "python" / "3.12.3").mkdir(parents=True)
    (runtimes_dir / "node" / "24.14.1").mkdir(parents=True)
    (runtimes_dir / "java" / "21.0.3+9").mkdir(parents=True)

    result = runner.invoke(runtime_cmd.app, ["list"])

    assert result.exit_code == 0
    assert "python" in result.output
    assert "3.12.3" in result.output
    assert "node" in result.output
    assert "24.14.1" in result.output
    assert "java" in result.output
    assert "21.0.3+9" in result.output


def test_runtime_which_uses_project_pinned_node_version(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtimes_dir = tmp_path / "runtimes"
    node_root = runtimes_dir / "node" / "24.14.1"
    node_bin = node_root if os.name == "nt" else node_root / "bin"
    node_bin.mkdir(parents=True)
    node_name = "node.exe" if os.name == "nt" else "node"
    (node_bin / node_name).write_text("", encoding="utf-8")
    (tmp_path / ".node-version").write_text("24\n", encoding="utf-8")
    _point_runtime_dirs(monkeypatch, runtimes_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(runtime_cmd.app, ["which", "node"])

    assert result.exit_code == 0
    assert result.output.strip() == str(node_bin / node_name)


def test_runtime_use_writes_marker_for_resolved_full_version(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtimes_dir = tmp_path / "runtimes"
    (runtimes_dir / "java" / "21.0.3+9").mkdir(parents=True)
    _point_runtime_dirs(monkeypatch, runtimes_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(runtime_cmd.app, ["use", "java", "21"])

    assert result.exit_code == 0
    assert (tmp_path / ".java-version").read_text(encoding="utf-8") == "21.0.3+9\n"
    assert "Pinned java 21.0.3+9" in result.output


def test_runtime_use_refreshes_project_aware_shim(monkeypatch, tmp_path: Path) -> None:
    runtimes_dir = tmp_path / "runtimes"
    node_root = runtimes_dir / "node" / "24.14.1"
    node_bin = node_root if os.name == "nt" else node_root / "bin"
    node_bin.mkdir(parents=True)
    (node_bin / ("node.exe" if os.name == "nt" else "node")).write_text("", encoding="utf-8")
    shims_dir = tmp_path / "shims"
    shims_dir.mkdir()
    (shims_dir / ("node.cmd" if os.name == "nt" else "node")).write_text(
        '#!/bin/sh\nexec /old/node "$@"\n', encoding="utf-8"
    )
    _point_runtime_dirs(monkeypatch, runtimes_dir)
    monkeypatch.setattr(_install, "SHIMS_DIR", shims_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(runtime_cmd.app, ["use", "node", "24"])

    assert result.exit_code == 0
    shim = (shims_dir / ("node.cmd" if os.name == "nt" else "node")).read_text(encoding="utf-8")
    assert 'rvs runtime which "node" --executable "node"' in shim


def test_runtime_uninstall_removes_matching_runtime_with_yes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtimes_dir = tmp_path / "runtimes"
    runtime_dir = runtimes_dir / "python" / "3.12.3"
    runtime_dir.mkdir(parents=True)
    _point_runtime_dirs(monkeypatch, runtimes_dir)

    result = runner.invoke(runtime_cmd.app, ["uninstall", "python", "3.12", "--yes"])

    assert result.exit_code == 0
    assert not runtime_dir.exists()
    assert "Removed python runtime '3.12'" in result.output


def test_runtime_install_rejects_unknown_kind() -> None:
    result = runner.invoke(runtime_cmd.app, ["install", "ruby", "3.3"])

    assert result.exit_code == 1
    assert "Unknown runtime 'ruby'" in result.stderr


def test_runtime_env_uses_patched_env_file(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    rendered_file = env_file.with_name("env.ps1") if os.name == "nt" else env_file
    rendered = (
        '$env:RVS_HOME = Join-Path $HOME ".rvs"\n'
        if os.name == "nt"
        else "export RVS_HOME=/tmp/rvs\n"
    )

    def write_fake_env() -> None:
        rendered_file.write_text(rendered, encoding="utf-8")

    monkeypatch.setattr(runtime_cmd, "ENV_FILE", env_file)
    monkeypatch.setattr(runtime_cmd, "write_env_file", write_fake_env)

    result = runner.invoke(runtime_cmd.app, ["env"])

    assert result.exit_code == 0
    assert result.output == rendered


def test_runtime_doctor_reports_managed_and_system_paths(monkeypatch, tmp_path: Path) -> None:
    runtimes_dir = tmp_path / "runtimes"
    python_dir = runtimes_dir / "python" / "3.12.3"
    python_dir.mkdir(parents=True)
    _point_runtime_dirs(monkeypatch, runtimes_dir)
    monkeypatch.setattr(runtime_cmd.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    captured: dict[str, list[list[str]]] = {}
    monkeypatch.setattr(
        runtime_cmd.output,
        "table",
        lambda columns, rows, title=None: captured.update({"rows": rows}),
    )

    result = runner.invoke(runtime_cmd.app, ["doctor"])

    assert result.exit_code == 0
    assert captured["rows"][0] == ["python", str(python_dir), "/usr/bin/python3"]
    assert captured["rows"][1] == ["node", "none", "/usr/bin/node"]
    assert captured["rows"][2] == ["java", "none", "/usr/bin/java"]


def test_write_shim_and_env_file_use_configured_rvs_dirs(monkeypatch, tmp_path: Path) -> None:
    rvs_dir = tmp_path / ".rvs"
    shims_dir = rvs_dir / "shims"
    env_file = rvs_dir / "env"
    monkeypatch.setattr(_install, "RVS_DIR", rvs_dir)
    monkeypatch.setattr(_install, "SHIMS_DIR", shims_dir)
    monkeypatch.setattr(_install, "ENV_FILE", env_file)

    _install.write_shim("python3", tmp_path / "python3")
    _install.write_env_file()

    shim = shims_dir / ("python3.cmd" if os.name == "nt" else "python3")
    expected = (
        f'@"{tmp_path / "python3"}" %*\n'
        if os.name == "nt"
        else f'#!/bin/sh\nexec "{tmp_path / "python3"}" "$@"\n'
    )
    assert shim.read_text(encoding="utf-8") == expected
    if os.name != "nt":
        assert shim.stat().st_mode & 0o755 == 0o755
    assert 'export RVS_HOME="$HOME/.rvs"' in env_file.read_text(encoding="utf-8")


def test_runtime_shim_resolves_project_pin_dynamically(monkeypatch, tmp_path: Path) -> None:
    shims_dir = tmp_path / "shims"
    monkeypatch.setattr(_install, "SHIMS_DIR", shims_dir)

    _install.write_shim(
        "npm",
        tmp_path / "node" / "bin" / "npm",
        runtime_kind="node",
    )

    shim = (shims_dir / ("npm.cmd" if os.name == "nt" else "npm")).read_text(encoding="utf-8")
    assert 'rvs runtime which "node" --executable "npm"' in shim
    assert ("%RVS_RUNTIME_TARGET%" if os.name == "nt" else 'exec "$target" "$@"') in shim


def test_extract_rejects_parent_path_and_leaves_no_destination(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        payload = b"owned"
        member = tarfile.TarInfo("../outside")
        member.size = len(payload)
        tf.addfile(member, io.BytesIO(payload))

    with pytest.raises(SystemExit):
        _install.extract(archive, tmp_path / "runtime")

    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "runtime").exists()


def test_extract_validates_layout_before_atomic_install(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        payload = b"binary"
        member = tarfile.TarInfo("runtime/bin/node")
        member.mode = 0o755
        member.size = len(payload)
        tf.addfile(member, io.BytesIO(payload))

    destination = tmp_path / "installed" / "24.0.0"
    _install.extract(archive, destination, required_paths=("bin/node",))

    assert (destination / "bin/node").read_bytes() == b"binary"


def test_extract_can_normalize_macos_jdk_home(tmp_path: Path) -> None:
    archive = tmp_path / "jdk.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        payload = b"java"
        member = tarfile.TarInfo("jdk/Contents/Home/bin/java")
        member.mode = 0o755
        member.size = len(payload)
        tf.addfile(member, io.BytesIO(payload))

    destination = tmp_path / "installed" / "21.0.1"
    _install.extract(
        archive,
        destination,
        nested_root="Contents/Home",
        required_paths=("bin/java",),
    )

    assert (destination / "bin/java").read_bytes() == b"java"


def test_download_rejects_checksum_mismatch(httpx2_mock, tmp_path: Path) -> None:
    httpx2_mock.add_response(
        url="https://releases.example.test/runtime.tar.gz",
        content=b"archive",
    )
    destination = tmp_path / "runtime.tar.gz"

    with pytest.raises(SystemExit):
        _install.download(
            "https://releases.example.test/runtime.tar.gz",
            destination,
            expected_sha256="0" * 64,
        )

    assert not destination.exists()


def test_download_accepts_matching_checksum(httpx2_mock, tmp_path: Path) -> None:
    content = b"archive"
    httpx2_mock.add_response(
        url="https://releases.example.test/runtime.tar.gz",
        content=content,
    )
    destination = tmp_path / "runtime.tar.gz"

    _install.download(
        "https://releases.example.test/runtime.tar.gz",
        destination,
        expected_sha256=hashlib.sha256(content).hexdigest(),
    )

    assert destination.read_bytes() == content


def test_temurin_metadata_uses_supported_latest_assets_endpoint(httpx2_mock) -> None:
    url = "https://api.adoptium.net/v3/assets/latest/21/hotspot"
    httpx2_mock.add_response(
        url=(
            f"{url}?architecture=aarch64&image_type=jdk&jvm_impl=hotspot&os=windows&vendor=eclipse"
        ),
        json=[{"version": {"semver": "21.0.9+10"}}],
    )

    release = java_rt._latest_release(21, "aarch64", "windows")

    assert release["version"]["semver"] == "21.0.9+10"


def test_python_github_api_uses_available_workflow_token(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-actions-token")

    assert python_rt._gh_headers()["Authorization"] == "Bearer github-actions-token"


def test_node_install_rejects_unpublished_musl_archive(monkeypatch) -> None:
    monkeypatch.setattr(
        node_rt,
        "runtime_platform",
        lambda: RuntimePlatform(system="linux", arch="x64", libc="musl"),
    )

    with pytest.raises(SystemExit):
        node_rt.install("22")


def test_node_converts_windows_paths_for_msys_gpgv() -> None:
    path = node_rt.Path(r"C:\Users\runneradmin\AppData\Local\Temp\SHASUMS256.txt")

    assert node_rt._msys_path(path) == "/c/Users/runneradmin/AppData/Local/Temp/SHASUMS256.txt"


def _isolate_home(monkeypatch, tmp_path: Path) -> Path:
    """Give setup-shell an empty home and rvs directory under tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    _point_runtime_dirs(monkeypatch, tmp_path / "runtimes")
    monkeypatch.setattr(_install, "RVS_DIR", home / ".rvs")
    monkeypatch.setattr(_install, "SHIMS_DIR", home / ".rvs" / "shims")
    monkeypatch.setattr(_install, "ENV_FILE", home / ".rvs" / "env")
    return home


def test_runtime_install_force_removes_existing_runtime_before_installing(
    monkeypatch, tmp_path: Path
) -> None:
    runtimes_dir = tmp_path / "runtimes"
    existing = runtimes_dir / "python" / "3.12.3"
    existing.mkdir(parents=True)
    _point_runtime_dirs(monkeypatch, runtimes_dir)
    monkeypatch.setattr(runtime_cmd, "runtime_platform", lambda: RuntimePlatform("macos", "x64"))
    installed: list[str] = []
    monkeypatch.setattr(python_rt, "install", lambda version: installed.append(version))

    result = runner.invoke(runtime_cmd.app, ["install", "python", "3.12", "--force"])

    assert result.exit_code == 0, result.output
    assert installed == ["3.12"]
    assert not existing.exists()
    assert "Removing" in result.output


def test_runtime_install_warns_on_non_debian_glibc_linux(monkeypatch) -> None:
    monkeypatch.setattr(
        runtime_cmd, "runtime_platform", lambda: RuntimePlatform("linux", "x64", "glibc")
    )
    monkeypatch.setattr(runtime_cmd, "is_debian", lambda: False)
    installed: list[str] = []
    monkeypatch.setattr(node_rt, "install", lambda version: installed.append(version))

    result = runner.invoke(runtime_cmd.app, ["install", "node", "22"])

    assert result.exit_code == 0, result.output
    assert installed == ["22"]
    assert "does not appear to be Debian/Ubuntu based" in " ".join(result.stderr.split())


def test_runtime_uninstall_reports_unknown_version(monkeypatch, tmp_path: Path) -> None:
    _point_runtime_dirs(monkeypatch, tmp_path / "runtimes")

    result = runner.invoke(runtime_cmd.app, ["uninstall", "node", "22", "--yes"])

    assert result.exit_code == 1
    assert "No installed node runtime matches '22'." in result.stderr


def test_runtime_uninstall_keeps_runtime_when_confirmation_is_declined(
    monkeypatch, tmp_path: Path
) -> None:
    runtimes_dir = tmp_path / "runtimes"
    runtime_dir = runtimes_dir / "java" / "21.0.3+9"
    runtime_dir.mkdir(parents=True)
    _point_runtime_dirs(monkeypatch, runtimes_dir)

    result = runner.invoke(runtime_cmd.app, ["uninstall", "java", "21"], input="n\n")

    assert result.exit_code == 1
    assert "Remove java runtime at" in result.output
    assert runtime_dir.is_dir()


def test_runtime_list_explains_how_to_install_when_empty(monkeypatch, tmp_path: Path) -> None:
    _point_runtime_dirs(monkeypatch, tmp_path / "runtimes")

    result = runner.invoke(runtime_cmd.app, ["list"])

    assert result.exit_code == 0
    assert "No runtimes installed. Run: rvs runtime install <runtime> <version>" in result.output


@pytest.mark.parametrize(
    ("arguments", "hint"),
    [
        (["which", "java"], "rvs runtime install java <version>"),
        (["which", "java", "21"], "rvs runtime install java 21"),
    ],
)
def test_runtime_which_explains_how_to_install_missing_runtime(
    monkeypatch, tmp_path: Path, arguments: list[str], hint: str
) -> None:
    _point_runtime_dirs(monkeypatch, tmp_path / "runtimes")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(runtime_cmd.app, arguments)

    assert result.exit_code == 1
    assert f"Install with: {hint}" in result.stderr


def test_runtime_which_resolves_named_executable_for_shims(monkeypatch, tmp_path: Path) -> None:
    runtimes_dir = tmp_path / "runtimes"
    node_root = runtimes_dir / "node" / "22.11.0"
    npm = node_root / "npm.cmd" if os.name == "nt" else node_root / "bin" / "npm"
    npm.parent.mkdir(parents=True)
    npm.touch()
    _point_runtime_dirs(monkeypatch, runtimes_dir)

    result = runner.invoke(runtime_cmd.app, ["which", "node", "22", "--executable", "npm"])

    assert result.exit_code == 0
    assert result.output.strip() == str(npm)


def test_runtime_which_prints_runtime_root_when_binary_is_missing(
    monkeypatch, tmp_path: Path
) -> None:
    runtimes_dir = tmp_path / "runtimes"
    java_root = runtimes_dir / "java" / "21.0.3+9"
    java_root.mkdir(parents=True)
    _point_runtime_dirs(monkeypatch, runtimes_dir)

    result = runner.invoke(runtime_cmd.app, ["which", "java", "21"])

    assert result.exit_code == 0
    assert result.output.strip() == str(java_root)


def test_runtime_use_rejects_version_that_is_not_installed(monkeypatch, tmp_path: Path) -> None:
    _point_runtime_dirs(monkeypatch, tmp_path / "runtimes")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(runtime_cmd.app, ["use", "python", "3.13"])

    assert result.exit_code == 1
    assert "No installed python matches '3.13'." in result.stderr
    assert not (tmp_path / ".python-version").exists()


def test_runtime_use_writes_python_shims_for_the_pinned_minor(monkeypatch, tmp_path: Path) -> None:
    runtimes_dir = tmp_path / "runtimes"
    python_root = runtimes_dir / "python" / "3.12.3"
    python = python_root / "python.exe" if os.name == "nt" else python_root / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.touch()
    shims_dir = tmp_path / "shims"
    _point_runtime_dirs(monkeypatch, runtimes_dir)
    monkeypatch.setattr(_install, "SHIMS_DIR", shims_dir)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(runtime_cmd.app, ["use", "python", "3.12"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".python-version").read_text(encoding="utf-8") == "3.12.3\n"
    suffix = ".cmd" if os.name == "nt" else ""
    minor_shim = (shims_dir / f"python3.12{suffix}").read_text(encoding="utf-8")
    assert 'rvs runtime which "python" "3.12" --executable "python3.12"' in minor_shim
    assert (shims_dir / f"python3{suffix}").is_file()


def test_runtime_setup_shell_adds_bash_source_line_once(monkeypatch, tmp_path: Path) -> None:
    home = _isolate_home(monkeypatch, tmp_path)
    source_line = '. "$HOME/.rvs/env"'

    first = runner.invoke(runtime_cmd.app, ["setup-shell", "--shell", "bash"])
    second = runner.invoke(runtime_cmd.app, ["setup-shell", "--shell", "bash"])

    assert first.exit_code == 0, first.output
    assert "Added rvs env source to:" in first.output
    assert second.exit_code == 0, second.output
    assert "Already configured; no changes made." in second.output
    for rc in (home / ".bashrc", home / ".bash_profile"):
        assert rc.read_text(encoding="utf-8").count(source_line) == 1
    assert (home / ".rvs" / "env").is_file()


def test_runtime_setup_shell_writes_fish_configuration(monkeypatch, tmp_path: Path) -> None:
    home = _isolate_home(monkeypatch, tmp_path)

    result = runner.invoke(runtime_cmd.app, ["setup-shell", "--shell", "fish"])

    assert result.exit_code == 0, result.output
    assert (home / ".config" / "fish" / "conf.d" / "rvs.fish").read_text(encoding="utf-8") == (
        '# rvs managed runtimes\nset -gx PATH "$HOME/.rvs/shims" $PATH\n'
    )


def test_runtime_setup_shell_uses_existing_profile_for_unknown_shell(
    monkeypatch, tmp_path: Path
) -> None:
    home = _isolate_home(monkeypatch, tmp_path)
    (home / ".profile").write_text("# existing\n", encoding="utf-8")

    result = runner.invoke(runtime_cmd.app, ["setup-shell", "--shell", "ksh"])

    assert result.exit_code == 0, result.output
    assert (home / ".profile").read_text(encoding="utf-8") == (
        '# existing\n\n# rvs managed runtimes\n. "$HOME/.rvs/env"\n'
    )
    assert not (home / ".bashrc").exists()
