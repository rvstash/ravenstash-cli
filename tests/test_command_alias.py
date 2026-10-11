"""Keep ``rvs`` and ``ravenstash`` one command, shipped together everywhere.

``ravenstash`` is the full-name alias of ``rvs``: both run the same entry point,
and every installation carries them with ``docker-credential-rvs``. These tests
fail when the two names behave differently or when a packaging target, an
installer, or the portable updater stops treating the three commands as one set.
"""

import ast
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest
import rvs
import yaml
from click import unstyle
from rvs import entrypoint, identity, portable_update
from rvs.installations import platform_target
from rvs.oci import credential_helper


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "packaging" / "scripts"

# The one expectation every target below is held to, in the order they are listed.
COMMANDS = ("rvs", "ravenstash", "docker-credential-rvs")
ENTRY_POINTS = {
    "rvs": "rvs.entrypoint:main",
    "ravenstash": "rvs.entrypoint:main",
    "docker-credential-rvs": "rvs.oci.credential_helper:main",
}
USAGE = "Usage: {program} [OPTIONS] COMMAND [ARGS]..."


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# ── One entry point ───────────────────────────────────────────────────────────


def test_both_names_resolve_to_one_entry_point() -> None:
    scripts = tomllib.loads(_read("pyproject.toml"))["project"]["scripts"]

    assert tuple(scripts) == COMMANDS
    assert scripts == ENTRY_POINTS
    loaded = {
        name: importlib.metadata.EntryPoint(name, value, "console_scripts").load()
        for name, value in scripts.items()
    }
    assert loaded["rvs"] is entrypoint.main
    assert loaded["ravenstash"] is loaded["rvs"]
    assert loaded["docker-credential-rvs"] is credential_helper.main


def test_installed_distribution_registers_the_same_entry_points() -> None:
    try:
        distribution = importlib.metadata.distribution(identity.PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("the project is not installed in this environment")
    installed = {
        entry.name: entry for entry in distribution.entry_points if entry.group == "console_scripts"
    }

    assert {name: entry.value for name, entry in installed.items()} == ENTRY_POINTS
    assert installed["ravenstash"].load() is installed["rvs"].load() is entrypoint.main


# ── One behaviour ─────────────────────────────────────────────────────────────


def _launcher(directory: Path, command: str) -> Path:
    """Write the console script an installer generates for one entry point."""
    module, _, function = ENTRY_POINTS[command].partition(":")
    script = directory / command
    script.write_text(
        f"import sys\nfrom {module} import {function}\nsys.exit({function}())\n", encoding="utf-8"
    )
    return script


def _invoke(tmp_path: Path, command: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in {"FORCE_COLOR", "GITHUB_ACTIONS", "PY_COLORS", "RVS_PROFILE", "RVS_TOKEN"}
    }
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(_launcher(tmp_path, command)), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment
        | {
            "COLUMNS": "100",
            "NO_COLOR": "1",
            "PYTHONIOENCODING": "utf-8",
            # Run the package under test, wherever this interpreter found it.
            "PYTHONPATH": str(Path(rvs.__file__).resolve().parents[1]),
            "RVS_HOME": str(home / ".rvs"),
        },
    )


def _lines(text: str) -> list[str]:
    return [unstyle(line).rstrip() for line in text.splitlines()]


def _differing_lines(first: str, second: str) -> list[tuple[str, str]]:
    return [
        (ours.strip(), theirs.strip())
        for ours, theirs in zip(_lines(first), _lines(second), strict=True)
        if ours != theirs
    ]


def test_version_is_the_same_under_both_names(tmp_path: Path) -> None:
    short = _invoke(tmp_path, "rvs", "--version")
    full = _invoke(tmp_path, "ravenstash", "--version")

    assert short.returncode == 0, short.stderr
    assert short.stdout.startswith(f"{identity.PRODUCT_NAME} ")
    assert (full.returncode, full.stdout, full.stderr) == (0, short.stdout, short.stderr)


@pytest.mark.parametrize(
    "arguments", [("--help",), ("-h",), ()], ids=["--help", "-h", "no arguments"]
)
def test_root_help_differs_only_in_the_usage_line(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    short = _invoke(tmp_path, "rvs", *arguments)
    full = _invoke(tmp_path, "ravenstash", *arguments)

    assert (short.returncode, short.stderr) == (0, "")
    assert (full.returncode, full.stderr) == (0, "")
    # The usage line names the program as it was invoked; nothing else follows the name.
    assert _differing_lines(short.stdout, full.stdout) == [
        (USAGE.format(program="rvs"), USAGE.format(program="ravenstash"))
    ]
    # The help text itself keeps calling the command `rvs` under either name.
    assert "Print output from rvs as JSON." in " ".join(_lines(full.stdout))


@pytest.mark.parametrize(
    ("arguments", "command", "error"),
    [
        (("--no-such-option",), "", "No such option: --no-such-option"),
        (("update", "--no-such-option"), " update", "No such option: --no-such-option"),
        (("no-such-command",), "", "No such command 'no-such-command'."),
    ],
    ids=["root usage error", "command usage error", "unknown command"],
)
def test_usage_errors_exit_the_same_under_both_names(
    tmp_path: Path, arguments: tuple[str, ...], command: str, error: str
) -> None:
    short = _invoke(tmp_path, "rvs", *arguments)
    full = _invoke(tmp_path, "ravenstash", *arguments)

    assert (short.returncode, short.stdout) == (2, "")
    assert (full.returncode, full.stdout) == (2, "")
    assert error in " ".join(_lines(short.stderr))
    # Only the usage line and the help pointer under it name the invoked program.
    usage, pointer = _differing_lines(short.stderr, full.stderr)
    assert usage[0].startswith(f"Usage: rvs{command} [OPTIONS]")
    assert usage[1] == usage[0].replace("Usage: rvs", "Usage: ravenstash", 1)
    assert pointer == (
        f"Try 'rvs{command} -h' for help.",
        f"Try 'ravenstash{command} -h' for help.",
    )


def test_a_failing_command_exits_the_same_under_both_names(tmp_path: Path) -> None:
    short = _invoke(tmp_path, "rvs", "update", "--yes")
    full = _invoke(tmp_path, "ravenstash", "update", "--yes")

    assert short.returncode == 1
    assert "--yes requires --apply." in short.stderr
    assert (full.returncode, full.stdout, full.stderr) == (1, short.stdout, short.stderr)


# ── One set in the updater ────────────────────────────────────────────────────


def test_portable_updater_installs_the_same_set() -> None:
    assert portable_update.COMMANDS == COMMANDS


# ── One set in every packaging target ─────────────────────────────────────────


def _shell_loop(script: str, variable: str) -> tuple[str, ...]:
    (words,) = re.findall(rf"^\s*for {variable} in ([\w -]+); do$", script, re.MULTILINE)
    return tuple(words.split())


def _python_loop(source: str, variable: str) -> tuple[str, ...]:
    (loop,) = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == variable
    ]
    return ast.literal_eval(loop.iter)


def test_frozen_bundle_links_both_aliases_to_the_launcher() -> None:
    build = _read("packaging/scripts/build-pyinstaller.sh")

    aliases = re.findall(r"^ln -sfn rvs dist/pyinstaller/rvs/([\w-]+)$", build, re.MULTILINE)
    assert ("rvs", *aliases) == COMMANDS
    assert "dist/pyinstaller/rvs/rvs --version\n" in build
    assert "dist/pyinstaller/rvs/ravenstash --version\n" in build
    assert "test -x dist/pyinstaller/rvs/docker-credential-rvs\n" in build


def test_debian_build_script_links_every_command() -> None:
    build = _read("packaging/scripts/build-deb.sh")

    links = re.findall(
        r'^ln -s "\.\./lib/\$PACKAGE/([\w-]+)" "\$STAGING/usr/bin/([\w-]+)"$', build, re.MULTILINE
    )
    assert tuple(command for _target, command in links) == COMMANDS
    # `ravenstash` is the launcher itself; the credential helper keeps its own name.
    assert dict((command, target) for target, command in links) == {
        "rvs": "rvs",
        "ravenstash": "rvs",
        "docker-credential-rvs": "docker-credential-rvs",
    }
    assert build.count('"$STAGING/usr/bin/') == len(COMMANDS)
    assert 'cp -a dist/pyinstaller/rvs/. "$STAGING/usr/lib/$PACKAGE/"' in build


def test_glibc_tarball_build_copies_every_alias() -> None:
    build = _read("packaging/scripts/build-tarball.sh")

    assert ("rvs", *_shell_loop(build, "alias")) == COMMANDS
    assert 'install -m 0755 "$STAGING/rvs" "$STAGING/$alias"' in build


def test_portable_build_copies_every_alias() -> None:
    build = _read("packaging/scripts/build_portable.py")

    assert ("rvs", *_python_loop(build, "alias")) == COMMANDS
    assert 'executable = bundle / f"rvs{suffix}"' in build
    assert '_copy_alias(executable, bundle / f"{alias}{suffix}")' in build


def test_portable_smoke_test_requires_every_command() -> None:
    smoke = _read("packaging/scripts/smoke_portable.py")
    tree = ast.parse(smoke)

    (assigned,) = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and [getattr(target, "id", None) for target in node.targets] == ["COMMANDS"]
    ]
    assert ast.literal_eval(assigned) == COMMANDS
    # Once for the built bundle, once for the archive the updater unpacked.
    loops = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For) and getattr(node.iter, "id", None) == "COMMANDS"
    ]
    assert len(loops) == 2
    assert '[str(bundle / f"ravenstash{suffix}"), "auth", "status"]' in smoke


_FAKE_PYINSTALLER = """\
import sys
from pathlib import Path

bundle = Path(sys.argv[sys.argv.index("--distpath") + 1]) / "rvs"
(bundle / "_internal" / "cryptography").mkdir(parents=True)
launcher = bundle / "rvs"
launcher.write_text(
    "#!/bin/sh\\n"
    'case "$1" in\\n'
    "  --version) printf 'Ravenstash CLI 0.15.4\\\\n' ;;\\n"
    "  auth) exit 1 ;;\\n"
    "esac\\n",
    encoding="utf-8",
)
launcher.chmod(0o755)
"""


def _portable_source(tmp_path: Path) -> Path:
    """Place the portable builder in a tree whose frozen bundle is a stand-in."""
    source = tmp_path / "source"
    scripts = source / "packaging" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("build_portable.py", "smoke_portable.py", "release_identity.py"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    shutil.copy2(ROOT / "packaging" / "identity.env", source / "packaging" / "identity.env")
    (source / "packaging" / "pyinstaller").mkdir()
    (source / "pyproject.toml").write_text('[project]\nversion = "0.15.4"\n', encoding="utf-8")
    for document in ("README.md", "LICENSE", "NOTICE"):
        (source / document).write_text("document\n", encoding="utf-8")
    freezer = tmp_path / "freezer" / "PyInstaller"
    freezer.mkdir(parents=True)
    # A regular package, so it also shadows an installed PyInstaller.
    (freezer / "__init__.py").write_text("", encoding="utf-8")
    (freezer / "__main__.py").write_text(_FAKE_PYINSTALLER, encoding="utf-8")
    return source


def _run_packaging_script(
    tmp_path: Path, script: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    """Run a copied packaging script with the stand-in freezer and the package under test."""
    search_path = [str(tmp_path / "freezer"), str(Path(rvs.__file__).resolve().parents[1])]
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"PYTHONPATH": os.pathsep.join(search_path), "SOURCE_DATE_EPOCH": "0"},
    )


@pytest.mark.skipif(os.name == "nt", reason="the stand-in launcher is a POSIX shell script")
def test_portable_build_ships_every_command_and_its_smoke_test_demands_them(
    tmp_path: Path,
) -> None:
    source = _portable_source(tmp_path)
    scripts = source / "packaging" / "scripts"
    target = platform_target()
    root = identity.archive_root("0.15.4", target)

    built = _run_packaging_script(tmp_path, scripts / "build_portable.py")

    assert built.returncode == 0, built.stderr
    archive = source / "dist" / "release" / identity.archive_name("0.15.4", target)
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
    assert [member.name for member in members if not (member.isreg() or member.isdir())] == []
    assert sorted(
        Path(member.name).name
        for member in members
        if member.isreg() and member.mode & 0o111 and Path(member.name).parent == Path(root)
    ) == sorted(COMMANDS)
    extracted = portable_update.extract_release(archive, tmp_path / "extracted", "0.15.4", target)
    launcher = (extracted / "rvs").read_bytes()
    for command in COMMANDS:
        assert (extracted / command).read_bytes() == launcher

    staged = (source / "build" / "release" / root).resolve()
    smoked = _run_packaging_script(tmp_path, scripts / "smoke_portable.py", str(staged))
    assert smoked.returncode == 0, smoked.stderr
    assert f"rvs update accepts {archive.name}" in smoked.stdout

    for command in COMMANDS:
        launcher_path = staged / command
        content = launcher_path.read_bytes()
        launcher_path.unlink()
        refused = _run_packaging_script(tmp_path, scripts / "smoke_portable.py", str(staged))
        assert refused.returncode == 1
        assert refused.stderr.strip() == f"missing executable: {launcher_path}"
        launcher_path.write_bytes(content)
        launcher_path.chmod(0o755)


def _function_body(script: str, name: str) -> str:
    return script.split(f"\n{name}() {{\n", 1)[1].split("\n}\n", 1)[0]


def test_shell_installer_links_and_replaces_every_command() -> None:
    portable = _function_body(_read("packaging/install.sh"), "install_portable_archive")

    # The unpacked archive must hold every command as a regular executable.
    assert _shell_loop(portable, "launcher") == COMMANDS
    assert 'fail "the portable archive is missing its ${launcher} launcher"' in portable
    # An existing command is replaced only when the installer owns it.
    assert _shell_loop(portable, "command_name") == COMMANDS
    assert 'fail "${command_path} exists and is not an installer-owned symlink"' in portable
    links = re.findall(
        r'^ +(as_root )?ln -sfn "\$\{install_root\}/current/([\w-]+)" '
        r'"\$\{bin_directory\}/([\w-]+)"$',
        portable,
        re.MULTILINE,
    )
    # A system and a user installation each link the whole set through `current`.
    for scope in ("as_root ", ""):
        assert [(source, link) for prefix, source, link in links if prefix == scope] == [
            (command, command) for command in COMMANDS
        ]
    assert len(links) == 2 * len(COMMANDS)
    # A repair replaces the version directory whole; no command is removed by name.
    assert re.findall(r"\brm -rf -- (\S+)", portable) == ['"$install_directory"'] * 2
    assert (
        re.findall(r"\bmv (\S+) (\S+)", portable)
        == [('"$archive_root"', '"$install_directory"')] * 2
    )


def test_windows_installer_requires_and_replaces_every_command() -> None:
    installer = _read("packaging/install.ps1")

    (required,) = re.findall(r"foreach \(\$launcherName in @\(([^)]*)\)\) \{", installer)
    assert tuple(re.findall(r'"([\w-]+)\.exe"', required)) == COMMANDS
    assert 'throw "Archive is missing $launcherName"' in installer
    assert set(re.findall(r'"([\w-]+)\.exe"', installer)) == set(COMMANDS)
    # The command directory is staged and swapped whole, on installation and repair.
    assert "Copy-Item -Recurse -LiteralPath $source -Destination $staged" in installer
    assert "if (Test-Path $bin) { Remove-Item -Recurse -Force -LiteralPath $bin }" in installer
    assert "Move-Item -LiteralPath $staged -Destination $bin" in installer
    assert re.findall(r"Remove-Item [^\n]*-LiteralPath (\$\w+)", installer) == [
        "$bin",
        "$temporary",
    ]


def test_package_manager_manifests_expose_every_command(tmp_path: Path) -> None:
    version = "0.15.0"
    release = tmp_path / "release"
    release.mkdir()
    for target in ("macos-amd64", "macos-arm64", "windows-amd64", "windows-arm64"):
        (release / identity.archive_name(version, target)).write_bytes(target.encode())

    subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "generate_package_manifests.py"),
            str(release),
            version,
            identity.REPOSITORY,
        ],
        check=True,
    )

    cask = identity.homebrew_cask("v0")
    winget = identity.winget_package("v0")
    with tarfile.open(release / f"{identity.ASSET_STEM}-v{version}-package-manifests.tar.gz") as (
        package
    ):
        manifests = {
            member.name: package.extractfile(member).read().decode()  # type: ignore[union-attr]
            for member in package.getmembers()
            if member.isfile()
        }
    binaries = re.findall(
        r'^  binary "([^"]+)/([\w-]+)"$',
        manifests[f"package-manifests/homebrew/{cask}.rb"],
        re.MULTILINE,
    )
    assert binaries == [
        (f"{identity.ASSET_STEM}-v#{{version}}-macos-#{{arch}}", command) for command in COMMANDS
    ]
    installers = yaml.safe_load(manifests[f"package-manifests/winget/{winget}.installer.yaml"])[
        "Installers"
    ]
    assert [installer["Architecture"] for installer in installers] == ["x64", "arm64"]
    for installer, target in zip(installers, ("windows-amd64", "windows-arm64"), strict=True):
        assert installer["NestedInstallerType"] == "portable"
        assert installer["NestedInstallerFiles"] == [
            {
                "RelativeFilePath": f"{identity.archive_root(version, target)}\\{command}.exe",
                "PortableCommandAlias": command,
            }
            for command in COMMANDS
        ]


def test_nix_flake_wraps_every_command() -> None:
    flake = _read("flake.nix")

    assert _shell_loop(flake, "command") == COMMANDS
    assert 'wrapProgram "$out/bin/$command" \\\n' in flake
