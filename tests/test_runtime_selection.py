import os
from typing import TYPE_CHECKING

import pytest
from rvs.runtime import java as java_rt
from rvs.runtime import node as node_rt
from rvs.runtime import python as python_rt
from rvs.runtime import tools
from rvs.runtime.selection import selected_version


if TYPE_CHECKING:
    from pathlib import Path


def test_python_find_without_version_returns_latest_install(monkeypatch, tmp_path: Path) -> None:
    runtimes_dir = tmp_path / "runtimes"
    monkeypatch.setattr(python_rt, "RUNTIMES_DIR", runtimes_dir)
    (runtimes_dir / "python" / "3.11.9").mkdir(parents=True)
    (runtimes_dir / "python" / "3.12.3").mkdir()

    assert python_rt.find("") == runtimes_dir / "python" / "3.12.3"


def test_runtime_versions_use_semantic_order_and_latest_prefix_match(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtimes_dir = tmp_path / "runtimes"
    base = runtimes_dir / "python"
    for version in ("3.9.20", "3.12.9", "3.12.10"):
        (base / version).mkdir(parents=True)
    monkeypatch.setattr(python_rt, "RUNTIMES_DIR", runtimes_dir)

    assert [version for version, _path in python_rt.list_installed()] == [
        "3.9.20",
        "3.12.9",
        "3.12.10",
    ]
    assert python_rt.find("") == base / "3.12.10"
    assert python_rt.find("3.12") == base / "3.12.10"


def test_node_find_without_version_returns_latest_install(monkeypatch, tmp_path: Path) -> None:
    runtimes_dir = tmp_path / "runtimes"
    monkeypatch.setattr(node_rt, "RUNTIMES_DIR", runtimes_dir)
    (runtimes_dir / "node" / "22.11.0").mkdir(parents=True)
    (runtimes_dir / "node" / "24.14.1").mkdir()

    assert node_rt.find("") == runtimes_dir / "node" / "24.14.1"


def test_java_find_matches_major_and_without_version_returns_latest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtimes_dir = tmp_path / "runtimes"
    monkeypatch.setattr(java_rt, "RUNTIMES_DIR", runtimes_dir)
    (runtimes_dir / "java" / "17.0.10+7").mkdir(parents=True)
    (runtimes_dir / "java" / "21.0.3+9").mkdir()

    assert java_rt.find("21") == runtimes_dir / "java" / "21.0.3+9"
    assert java_rt.find("") == runtimes_dir / "java" / "21.0.3+9"


def test_tools_resolve_prefers_project_pinned_python(monkeypatch, tmp_path: Path) -> None:
    runtimes_dir = tmp_path / "runtimes"
    py311_root = runtimes_dir / "python" / "3.11.9"
    py312_root = runtimes_dir / "python" / "3.12.3"
    py311_bin = py311_root if os.name == "nt" else py311_root / "bin"
    py312_bin = py312_root if os.name == "nt" else py312_root / "bin"
    py311_bin.mkdir(parents=True)
    py312_bin.mkdir(parents=True)
    pip_relative = "Scripts/pip.exe" if os.name == "nt" else "pip"
    (py311_bin / pip_relative).parent.mkdir(parents=True, exist_ok=True)
    (py312_bin / pip_relative).parent.mkdir(parents=True, exist_ok=True)
    (py311_bin / pip_relative).write_text("", encoding="utf-8")
    (py312_bin / pip_relative).write_text("", encoding="utf-8")
    (tmp_path / ".python-version").write_text("3.11\n", encoding="utf-8")

    monkeypatch.setattr(python_rt, "RUNTIMES_DIR", runtimes_dir)
    monkeypatch.chdir(tmp_path)

    assert selected_version("python") == "3.11"
    assert tools.resolve("pip") == str(py311_bin / pip_relative)


def test_selected_version_uses_nearest_marker_and_skips_blank_lines(tmp_path: Path) -> None:
    project = tmp_path / "project"
    service = project / "service"
    service.mkdir(parents=True)
    (project / ".node-version").write_text("22\n", encoding="utf-8")
    (service / ".node-version").write_text("\n   \n24.14\n", encoding="utf-8")

    assert selected_version("node", service) == "24.14"
    assert selected_version("node", project) == "22"
    assert selected_version("ruby", service) is None


def test_tools_resolve_prefers_managed_node_over_path(monkeypatch, tmp_path: Path) -> None:
    runtimes_dir = tmp_path / "runtimes"
    node_root = runtimes_dir / "node" / "22.11.0"
    npm = node_root / "npm.cmd" if os.name == "nt" else node_root / "bin" / "npm"
    npm.parent.mkdir(parents=True)
    npm.touch()
    monkeypatch.setattr(node_rt, "RUNTIMES_DIR", runtimes_dir)
    monkeypatch.setattr(tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.chdir(tmp_path)

    assert tools.resolve("npm") == str(npm)
    assert tools.npm() == str(npm)


def test_tools_resolve_falls_back_to_path_without_managed_runtime(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(python_rt, "RUNTIMES_DIR", tmp_path / "runtimes")
    monkeypatch.setattr(node_rt, "RUNTIMES_DIR", tmp_path / "runtimes")
    monkeypatch.setattr(tools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.chdir(tmp_path)

    assert tools.resolve("python3") == "/usr/bin/python3"
    assert tools.resolve("node") == "/usr/bin/node"
    assert tools.pip_cmd() == ["/usr/bin/pip"]


@pytest.mark.parametrize(
    ("name", "install_kind", "hint"),
    [
        ("npm", "node", "Install it via rvs: rvs runtime install node <version>"),
        ("helm", "helm", "Install it system-wide and ensure it is on your PATH."),
    ],
)
def test_tools_require_explains_how_to_install_missing_tool(
    monkeypatch, tmp_path: Path, capsys, name: str, install_kind: str, hint: str
) -> None:
    monkeypatch.setattr(node_rt, "RUNTIMES_DIR", tmp_path / "runtimes")
    monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exited:
        tools.require(name, install_kind=install_kind)

    assert exited.value.code == 1
    assert " ".join(capsys.readouterr().err.split()) == (
        f"Error: '{name}' is not installed and was not found on PATH. {hint}"
    )


def test_tools_pip_cmd_exits_when_no_pip_is_available(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(python_rt, "RUNTIMES_DIR", tmp_path / "runtimes")
    monkeypatch.setattr(tools.shutil, "which", lambda _name: None)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exited:
        tools.pip_cmd()

    assert exited.value.code == 1
