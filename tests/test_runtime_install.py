import hashlib
import io
import os
import subprocess
import tarfile
import zipfile
from typing import TYPE_CHECKING, Any

import pytest
from rvs.runtime import _install
from rvs.runtime import java as java_rt
from rvs.runtime import node as node_rt
from rvs.runtime import python as python_rt
from rvs.runtime._install import RuntimePlatform


if TYPE_CHECKING:
    from pathlib import Path


LINUX = RuntimePlatform("linux", "x64", "glibc")
DIGEST = "ab" * 32


@pytest.fixture
def rvs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point every managed runtime, shim, and env path at a temporary rvs home."""
    rvs_dir = tmp_path / ".rvs"
    monkeypatch.setattr(_install, "RVS_DIR", rvs_dir)
    monkeypatch.setattr(_install, "SHIMS_DIR", rvs_dir / "shims")
    monkeypatch.setattr(_install, "ENV_FILE", rvs_dir / "env")
    for module in (python_rt, node_rt, java_rt):
        monkeypatch.setattr(module, "RUNTIMES_DIR", rvs_dir / "runtimes")
    return rvs_dir


def _write_archive(path: Path, files: dict[str, bytes]) -> None:
    """Write a small runtime archive whose format follows the file name."""
    if path.name.endswith(".zip"):
        with zipfile.ZipFile(path, "w") as bundle:
            for name, content in files.items():
                bundle.writestr(name, content)
        return
    mode = "w:xz" if path.name.endswith(".tar.xz") else "w:gz"
    with tarfile.open(path, mode) as bundle:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.mode = 0o755
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))


def _fake_download(files: dict[str, bytes], calls: list[tuple[str, str]]) -> Any:
    def download(url: str, dest: Path, *, expected_sha256: str) -> None:
        calls.append((url, expected_sha256))
        _write_archive(dest, files)

    return download


def _refuse_download(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("an installed runtime was downloaded again")


def _shim(rvs_dir: Path, name: str) -> Path:
    return rvs_dir / "shims" / (f"{name}.cmd" if os.name == "nt" else name)


def _error(capsys: pytest.CaptureFixture[str]) -> str:
    return " ".join(capsys.readouterr().err.split())


# ── Platform detection ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("system", "machine", "libc", "expected"),
    [
        ("Linux", "x86_64", ("glibc", "2.39"), RuntimePlatform("linux", "x64", "glibc")),
        ("Linux", "aarch64", ("musl", "1.2"), RuntimePlatform("linux", "aarch64", "musl")),
        ("Darwin", "arm64", ("", ""), RuntimePlatform("macos", "aarch64", None)),
        ("Windows", "AMD64", ("", ""), RuntimePlatform("windows", "x64", None)),
    ],
)
def test_runtime_platform_normalizes_supported_targets(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    libc: tuple[str, str],
    expected: RuntimePlatform,
) -> None:
    monkeypatch.setattr(_install.platform, "system", lambda: system)
    monkeypatch.setattr(_install.platform, "machine", lambda: machine)
    monkeypatch.setattr(_install.platform, "libc_ver", lambda: libc)

    assert _install.runtime_platform() == expected


@pytest.mark.parametrize(
    ("system", "machine", "message"),
    [
        ("FreeBSD", "amd64", "rvs runtime management is unsupported on freebsd."),
        ("Linux", "riscv64", "Unsupported CPU architecture: riscv64."),
    ],
)
def test_runtime_platform_rejects_unsupported_targets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    system: str,
    machine: str,
    message: str,
) -> None:
    monkeypatch.setattr(_install.platform, "system", lambda: system)
    monkeypatch.setattr(_install.platform, "machine", lambda: machine)

    with pytest.raises(SystemExit) as exited:
        _install.runtime_platform()

    assert exited.value.code == 1
    assert _error(capsys) == f"Error: {message}"


@pytest.mark.parametrize(
    ("ldd", "expected"),
    [
        (subprocess.CompletedProcess(["ldd"], 1, "", "musl libc (x86_64)\n"), "musl"),
        (subprocess.CompletedProcess(["ldd"], 0, "ldd (GNU libc) 2.39\n", ""), "glibc"),
        (OSError("ldd is not installed"), "glibc"),
    ],
)
def test_linux_libc_falls_back_to_ldd_when_python_cannot_tell(
    monkeypatch: pytest.MonkeyPatch, ldd: Any, expected: str
) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        if isinstance(ldd, Exception):
            raise ldd
        return ldd

    monkeypatch.setattr(_install.platform, "libc_ver", lambda: ("", ""))
    monkeypatch.setattr(_install.subprocess, "run", fake_run)

    assert _install._linux_libc() == expected


# ── Download ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("digest", ["", "sha256:" + "0" * 63, "z" * 64])
def test_download_requires_a_valid_digest_before_any_request(
    httpx2_mock: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path, digest: str
) -> None:
    destination = tmp_path / "runtime.tar.gz"

    with pytest.raises(SystemExit):
        _install.download(
            "https://releases.example.test/runtime.tar.gz", destination, expected_sha256=digest
        )

    assert _error(capsys) == "Error: Runtime archive is missing a valid SHA-256 digest."
    assert not destination.exists()


def test_download_accepts_prefixed_uppercase_digest(httpx2_mock: Any, tmp_path: Path) -> None:
    content = b"archive"
    httpx2_mock.add_response(url="https://releases.example.test/runtime.tar.gz", content=content)
    destination = tmp_path / "runtime.tar.gz"

    _install.download(
        "https://releases.example.test/runtime.tar.gz",
        destination,
        expected_sha256="sha256:" + hashlib.sha256(content).hexdigest().upper(),
    )

    assert destination.read_bytes() == content


def test_download_rejects_non_https_response(
    httpx2_mock: Any, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    httpx2_mock.add_response(url="http://releases.example.test/runtime.tar.gz", content=b"x")
    destination = tmp_path / "runtime.tar.gz"

    with pytest.raises(SystemExit):
        _install.download(
            "http://releases.example.test/runtime.tar.gz", destination, expected_sha256=DIGEST
        )

    assert _error(capsys) == "Error: Runtime archive redirected to a non-HTTPS URL."
    assert not destination.exists()


def test_download_rejects_archive_larger_than_the_limit(
    httpx2_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(_install, "_MAX_DOWNLOAD_BYTES", 4)
    httpx2_mock.add_response(url="https://releases.example.test/runtime.tar.gz", content=b"archive")
    destination = tmp_path / "runtime.tar.gz"

    with pytest.raises(SystemExit):
        _install.download(
            "https://releases.example.test/runtime.tar.gz", destination, expected_sha256=DIGEST
        )

    assert _error(capsys) == "Error: Runtime archive exceeds 4 bytes."
    assert not destination.exists()


# ── Extract ───────────────────────────────────────────────────────────────────


def test_extract_strips_the_single_root_of_a_zip_archive(tmp_path: Path) -> None:
    archive = tmp_path / "node.zip"
    _write_archive(archive, {"node-v22.11.0-win-x64/node.exe": b"node"})
    destination = tmp_path / "installed" / "22.11.0"

    _install.extract(archive, destination, required_paths=("node.exe",))

    assert (destination / "node.exe").read_bytes() == b"node"


def test_extract_rejects_zip_member_outside_the_destination(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    archive = tmp_path / "unsafe.zip"
    _write_archive(archive, {"../outside": b"owned"})

    with pytest.raises(SystemExit):
        _install.extract(archive, tmp_path / "installed" / "runtime")

    assert _error(capsys).endswith("Error: Runtime archive contains an unsafe path.")
    assert not (tmp_path / "installed" / "outside").exists()
    assert not (tmp_path / "installed" / "runtime").exists()


def test_extract_rejects_unreadable_archive(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    archive = tmp_path / "runtime.tar.gz"
    archive.write_bytes(b"this is not an archive")

    with pytest.raises(SystemExit):
        _install.extract(archive, tmp_path / "installed" / "runtime")

    assert "Error: Runtime archive extraction was rejected:" in _error(capsys)
    assert not (tmp_path / "installed" / "runtime").exists()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (
            {"required_paths": ("bin/node",)},
            "Runtime archive is missing required file: bin/node",
        ),
        (
            {"nested_root": "Contents/Home"},
            "Runtime archive is missing required directory: Contents/Home",
        ),
    ],
)
def test_extract_rejects_archive_without_expected_layout(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, options: dict[str, Any], message: str
) -> None:
    archive = tmp_path / "runtime.tar.gz"
    _write_archive(archive, {"runtime/bin/other": b"other"})
    destination = tmp_path / "installed" / "runtime"

    with pytest.raises(SystemExit):
        _install.extract(archive, destination, **options)

    assert _error(capsys).endswith(f"Error: {message}")
    assert not destination.exists()


def test_extract_never_replaces_an_existing_destination(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    archive = tmp_path / "runtime.tar.gz"
    _write_archive(archive, {"runtime/bin/node": b"new"})
    destination = tmp_path / "installed" / "runtime"
    destination.mkdir(parents=True)
    (destination / "marker").write_text("existing", encoding="utf-8")

    with pytest.raises(SystemExit):
        _install.extract(archive, destination)

    assert f"Error: Runtime destination appeared during installation: {destination}" in _error(
        capsys
    )
    assert [path.name for path in destination.iterdir()] == ["marker"]


# ── Python ────────────────────────────────────────────────────────────────────


def test_python_asset_lookup_uses_first_immutable_release_with_digest(httpx2_mock: Any) -> None:
    suffix = "x86_64-unknown-linux-gnu-install_only.tar.gz"

    def asset(version: str, digest: str | None = None) -> dict[str, str]:
        name = f"cpython-{version}+20250101-{suffix}"
        entry = {"name": name, "browser_download_url": f"https://downloads.example.test/{name}"}
        if digest:
            entry["digest"] = digest
        return entry

    httpx2_mock.add_response(
        url=f"{python_rt._GH_RELEASES}?per_page=10&page=1",
        json=[
            {"immutable": False, "assets": [asset("3.12.9", f"sha256:{DIGEST}")]},
            {
                "immutable": True,
                "assets": [
                    asset("3.13.1", f"sha256:{DIGEST}"),
                    asset("3.12.8"),
                    asset("3.12.7", f"sha256:{DIGEST}"),
                ],
            },
        ],
    )

    assert python_rt._find_asset("3.12", "x86_64-unknown-linux-gnu") == (
        "3.12.7",
        f"https://downloads.example.test/cpython-3.12.7+20250101-{suffix}",
        f"sha256:{DIGEST}",
    )


def test_python_asset_lookup_reports_missing_build(
    httpx2_mock: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    httpx2_mock.add_response(url=f"{python_rt._GH_RELEASES}?per_page=10&page=1", json=[])

    with pytest.raises(SystemExit):
        python_rt._find_asset("3.99", "x86_64-unknown-linux-gnu")

    assert _error(capsys) == (
        "Error: No Python 3.99 build found for x86_64-unknown-linux-gnu. "
        "Check: https://github.com/astral-sh/python-build-standalone/releases"
    )


@pytest.mark.parametrize(
    ("target", "triple"),
    [
        (RuntimePlatform("linux", "x64", "glibc"), "x86_64-unknown-linux-gnu"),
        (RuntimePlatform("linux", "aarch64", "musl"), "aarch64-unknown-linux-musl"),
        (RuntimePlatform("macos", "aarch64"), "aarch64-apple-darwin"),
        (RuntimePlatform("windows", "x64"), "x86_64-pc-windows-msvc"),
    ],
)
def test_python_install_selects_build_for_platform_and_writes_shims(
    monkeypatch: pytest.MonkeyPatch, rvs_dir: Path, target: RuntimePlatform, triple: str
) -> None:
    url = f"https://downloads.example.test/cpython-3.12.7+20250101-{triple}-install_only.tar.gz"
    requested: list[tuple[str, str]] = []
    downloads: list[tuple[str, str]] = []
    executable = "python.exe" if target.windows else "bin/python3"
    monkeypatch.setattr(python_rt, "runtime_platform", lambda: target)
    monkeypatch.setattr(
        python_rt,
        "_find_asset",
        lambda version, asset_target: (
            requested.append((version, asset_target)) or ("3.12.7", url, DIGEST)
        ),
    )
    monkeypatch.setattr(
        python_rt, "download", _fake_download({f"python/{executable}": b"python"}, downloads)
    )

    installed = python_rt.install("3.12")

    assert installed == rvs_dir / "runtimes" / "python" / "3.12.7"
    assert requested == [("3.12", triple)]
    assert downloads == [(url, DIGEST)]
    assert (installed / executable).read_bytes() == b"python"
    assert 'rvs runtime which "python"' in _shim(rvs_dir, "python3").read_text(encoding="utf-8")
    assert 'rvs runtime which "python" "3.12"' in _shim(rvs_dir, "python3.12").read_text(
        encoding="utf-8"
    )
    assert (rvs_dir / "env").is_file()


def test_python_install_reuses_existing_runtime(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], rvs_dir: Path
) -> None:
    existing = rvs_dir / "runtimes" / "python" / "3.12.7"
    existing.mkdir(parents=True)
    monkeypatch.setattr(python_rt, "runtime_platform", lambda: LINUX)
    monkeypatch.setattr(
        python_rt, "_find_asset", lambda version, triple: ("3.12.7", "https://x.test/a", DIGEST)
    )
    monkeypatch.setattr(python_rt, "download", _refuse_download)

    assert python_rt.install("3.12") == existing
    assert f"Python 3.12.7 already installed at {existing}" in " ".join(
        capsys.readouterr().out.split()
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX Python builds keep executables in bin/")
def test_python_bin_prefers_the_minor_versioned_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtimes_dir = tmp_path / "runtimes"
    bin_dir = runtimes_dir / "python" / "3.12.7" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python3").touch()
    (bin_dir / "python3.12").touch()
    monkeypatch.setattr(python_rt, "RUNTIMES_DIR", runtimes_dir)

    assert python_rt.python_bin("3.12") == bin_dir / "python3.12"
    assert python_rt.python_bin("3.11") is None


# ── Node.js ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("latest", "24.1.0"),
        ("LATEST", "24.1.0"),
        ("22", "22.11.0"),
        ("22.10", "22.10.0"),
        ("v24", "24.1.0"),
        ("24.1.0", "24.1.0"),
    ],
)
def test_node_resolves_requested_version_from_release_index(
    httpx2_mock: Any, requested: str, expected: str
) -> None:
    # The newest matching release wins, whatever the index order.
    httpx2_mock.add_response(
        url=node_rt._INDEX_URL,
        json=[{"version": "v22.10.0"}, {"version": "v24.1.0"}, {"version": "v22.11.0"}],
    )

    assert node_rt._resolve_full_version(requested) == expected


@pytest.mark.parametrize(
    ("requested", "message"),
    [
        ("99", "No Node.js release found for '99'."),
        ("lts", "No Node.js release found for 'lts'."),
        # A missing minor never falls back to another release of the major.
        ("24.99", "No Node.js release found for '24.99'."),
        ("24.1.1", "No Node.js release found for '24.1.1'."),
    ],
)
def test_node_reports_unresolvable_version(
    httpx2_mock: Any, capsys: pytest.CaptureFixture[str], requested: str, message: str
) -> None:
    httpx2_mock.add_response(url=node_rt._INDEX_URL, json=[{"version": "v24.1.0"}])

    with pytest.raises(SystemExit):
        node_rt._resolve_full_version(requested)

    assert _error(capsys) == f"Error: {message}"


def _serve_node_checksums(httpx2_mock: Any, checksums: bytes) -> None:
    root = "https://nodejs.org/dist/v22.11.0"
    httpx2_mock.add_response(url=f"{root}/SHASUMS256.txt", content=checksums)
    httpx2_mock.add_response(url=f"{root}/SHASUMS256.txt.sig", content=b"signature")


def test_node_archive_digest_comes_from_signature_verified_manifest(
    httpx2_mock: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = "node-v22.11.0-linux-x64.tar.xz"
    _serve_node_checksums(httpx2_mock, f"{'cd' * 32}  other.tar.xz\n{DIGEST}  {archive}\n".encode())
    keyring_downloads: list[tuple[str, str]] = []
    verifications: list[list[str]] = []
    monkeypatch.setattr(node_rt.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        node_rt,
        "download",
        lambda url, dest, *, expected_sha256: keyring_downloads.append((url, expected_sha256)),
    )
    monkeypatch.setattr(
        node_rt.subprocess,
        "run",
        lambda command, **_kwargs: (
            verifications.append(command) or subprocess.CompletedProcess(command, 0, "", "")
        ),
    )

    assert node_rt._verified_archive_digest("22.11.0", archive, tmp_path) == DIGEST
    assert keyring_downloads == [(node_rt._RELEASE_KEYRING_URL, node_rt._RELEASE_KEYRING_SHA256)]
    assert verifications == [
        [
            "/usr/bin/gpgv",
            f"--keyring={tmp_path / 'nodejs-release-keyring.kbx'}",
            str(tmp_path / "SHASUMS256.txt.sig"),
            str(tmp_path / "SHASUMS256.txt"),
        ]
    ]


@pytest.mark.parametrize(
    ("gpgv_exit", "checksums", "message"),
    [
        (
            1,
            f"{DIGEST}  node-v22.11.0-linux-x64.tar.xz\n",
            "Node.js release signature verification failed.",
        ),
        (
            0,
            f"{DIGEST}  node-v22.11.0-darwin-arm64.tar.gz\n",
            "Node.js signed manifest does not contain node-v22.11.0-linux-x64.tar.xz.",
        ),
    ],
)
def test_node_archive_digest_rejects_unverified_or_incomplete_manifest(
    httpx2_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    gpgv_exit: int,
    checksums: str,
    message: str,
) -> None:
    _serve_node_checksums(httpx2_mock, checksums.encode())
    monkeypatch.setattr(node_rt.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(node_rt, "download", lambda url, dest, *, expected_sha256: None)
    monkeypatch.setattr(
        node_rt.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, gpgv_exit, "", "BAD"),
    )

    with pytest.raises(SystemExit):
        node_rt._verified_archive_digest("22.11.0", "node-v22.11.0-linux-x64.tar.xz", tmp_path)

    assert _error(capsys) == f"Error: {message}"


def test_node_archive_digest_requires_gpgv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(node_rt.shutil, "which", lambda name: None)

    with pytest.raises(SystemExit):
        node_rt._verified_archive_digest("22.11.0", "node-v22.11.0-linux-x64.tar.xz", tmp_path)

    assert _error(capsys) == (
        "Error: Node.js installation requires gpgv to verify the signed release manifest."
    )


@pytest.mark.parametrize(
    ("target", "archive", "files"),
    [
        (LINUX, "node-v22.11.0-linux-x64.tar.xz", ("bin/node", "bin/npm")),
        (
            RuntimePlatform("macos", "aarch64"),
            "node-v22.11.0-darwin-arm64.tar.gz",
            ("bin/node", "bin/npm"),
        ),
        (RuntimePlatform("windows", "x64"), "node-v22.11.0-win-x64.zip", ("node.exe", "npm.cmd")),
    ],
)
def test_node_install_downloads_verified_archive_for_platform(
    monkeypatch: pytest.MonkeyPatch,
    rvs_dir: Path,
    target: RuntimePlatform,
    archive: str,
    files: tuple[str, ...],
) -> None:
    verified: list[str] = []
    downloads: list[tuple[str, str]] = []
    root = archive.removesuffix(".tar.xz").removesuffix(".tar.gz").removesuffix(".zip")
    monkeypatch.setattr(node_rt, "runtime_platform", lambda: target)
    monkeypatch.setattr(node_rt, "_resolve_full_version", lambda version: "22.11.0")
    monkeypatch.setattr(
        node_rt,
        "_verified_archive_digest",
        lambda version, archive_name, temp_dir: verified.append(archive_name) or DIGEST,
    )
    monkeypatch.setattr(
        node_rt,
        "download",
        _fake_download({f"{root}/{name}": name.encode() for name in files}, downloads),
    )

    installed = node_rt.install("22")

    assert installed == rvs_dir / "runtimes" / "node" / "22.11.0"
    assert verified == [archive]
    assert downloads == [(f"https://nodejs.org/dist/v22.11.0/{archive}", DIGEST)]
    for name in files:
        assert (installed / name).read_bytes() == name.encode()
    assert 'rvs runtime which "node" --executable "node"' in _shim(rvs_dir, "node").read_text(
        encoding="utf-8"
    )
    assert _shim(rvs_dir, "npm").is_file()
    assert not _shim(rvs_dir, "corepack").exists()


def test_node_install_reuses_existing_runtime(
    monkeypatch: pytest.MonkeyPatch, rvs_dir: Path
) -> None:
    existing = rvs_dir / "runtimes" / "node" / "22.11.0"
    existing.mkdir(parents=True)
    monkeypatch.setattr(node_rt, "runtime_platform", lambda: LINUX)
    monkeypatch.setattr(node_rt, "_resolve_full_version", lambda version: "22.11.0")
    monkeypatch.setattr(node_rt, "_verified_archive_digest", _refuse_download)
    monkeypatch.setattr(node_rt, "download", _refuse_download)

    assert node_rt.install("22") == existing


# ── Java ──────────────────────────────────────────────────────────────────────


def _temurin_release(link: str, checksum: str | None = DIGEST) -> dict[str, Any]:
    return {
        "binary": {"package": {"link": link, "checksum": checksum}},
        "version": {"semver": "21.0.5+11"},
    }


_TEMURIN_ROOT = "https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.5"


@pytest.mark.parametrize(
    ("target", "os_name", "archive", "files"),
    [
        (LINUX, "linux", "jdk-linux.tar.gz", {"jdk-21.0.5+11/bin/java": b"java"}),
        (
            RuntimePlatform("linux", "aarch64", "musl"),
            "alpine-linux",
            "jdk-alpine.tar.gz",
            {"jdk-21.0.5+11/bin/java": b"java"},
        ),
        (
            RuntimePlatform("macos", "aarch64"),
            "mac",
            "jdk-mac.tar.gz",
            {"jdk-21.0.5+11/Contents/Home/bin/java": b"java"},
        ),
        (
            RuntimePlatform("windows", "x64"),
            "windows",
            "jdk-windows.zip",
            {"jdk-21.0.5+11/bin/java.exe": b"java"},
        ),
    ],
)
def test_java_install_selects_temurin_build_for_platform(
    monkeypatch: pytest.MonkeyPatch,
    rvs_dir: Path,
    target: RuntimePlatform,
    os_name: str,
    archive: str,
    files: dict[str, bytes],
) -> None:
    queries: list[tuple[int, str, str]] = []
    downloads: list[tuple[str, str]] = []
    link = f"{_TEMURIN_ROOT}/{archive}"
    monkeypatch.setattr(java_rt, "runtime_platform", lambda: target)
    monkeypatch.setattr(
        java_rt,
        "_latest_release",
        lambda major, arch, os_name: (
            queries.append((major, arch, os_name)) or _temurin_release(link)
        ),
    )
    monkeypatch.setattr(java_rt, "download", _fake_download(files, downloads))

    installed = java_rt.install("21.0.5+11")

    java = "bin/java.exe" if target.windows else "bin/java"
    assert installed == rvs_dir / "runtimes" / "java" / "21.0.5+11"
    assert queries == [(21, target.arch, os_name)]
    assert downloads == [(link, DIGEST)]
    assert (installed / java).read_bytes() == b"java"
    assert 'rvs runtime which "java"' in _shim(rvs_dir, "java").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "link",
    [
        f"http://github.com/{_TEMURIN_ROOT.removeprefix('https://github.com/')}/jdk.tar.gz",
        "https://downloads.example.test/jdk.tar.gz",
        "https://user:secret@github.com/adoptium/jdk.tar.gz",
        "",
    ],
)
def test_java_install_rejects_untrusted_download_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], rvs_dir: Path, link: str
) -> None:
    monkeypatch.setattr(java_rt, "runtime_platform", lambda: LINUX)
    monkeypatch.setattr(java_rt, "_latest_release", lambda *_args: _temurin_release(link))
    monkeypatch.setattr(java_rt, "download", _refuse_download)

    with pytest.raises(SystemExit):
        java_rt.install("21")

    assert _error(capsys) == "Error: Temurin release metadata returned an untrusted download URL."
    assert not (rvs_dir / "runtimes").exists()


def test_java_install_requires_a_published_checksum(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], rvs_dir: Path
) -> None:
    monkeypatch.setattr(java_rt, "runtime_platform", lambda: LINUX)
    monkeypatch.setattr(
        java_rt,
        "_latest_release",
        lambda *_args: _temurin_release(f"{_TEMURIN_ROOT}/jdk.tar.gz", checksum=None),
    )
    monkeypatch.setattr(java_rt, "download", _refuse_download)

    with pytest.raises(SystemExit):
        java_rt.install("21")

    assert _error(capsys) == ("Error: Temurin release metadata did not provide a SHA-256 digest.")


def test_java_install_rejects_non_numeric_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(java_rt, "runtime_platform", lambda: LINUX)
    monkeypatch.setattr(java_rt, "_latest_release", _refuse_download)

    with pytest.raises(SystemExit):
        java_rt.install("latest")

    assert _error(capsys) == "Error: Cannot parse Java version 'latest'."


def test_java_install_reuses_existing_runtime(
    monkeypatch: pytest.MonkeyPatch, rvs_dir: Path
) -> None:
    existing = rvs_dir / "runtimes" / "java" / "21.0.5+11"
    existing.mkdir(parents=True)
    monkeypatch.setattr(java_rt, "runtime_platform", lambda: LINUX)
    monkeypatch.setattr(
        java_rt, "_latest_release", lambda *_args: _temurin_release(f"{_TEMURIN_ROOT}/jdk.tar.gz")
    )
    monkeypatch.setattr(java_rt, "download", _refuse_download)

    assert java_rt.install("21") == existing


def test_java_metadata_reports_missing_release(
    httpx2_mock: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    httpx2_mock.add_response(
        url=(
            "https://api.adoptium.net/v3/assets/latest/99/hotspot?architecture=x64"
            "&image_type=jdk&jvm_impl=hotspot&os=linux&vendor=eclipse"
        ),
        json=[],
    )

    with pytest.raises(SystemExit):
        java_rt._latest_release(99, "x64", "linux")

    assert _error(capsys) == "Error: No Temurin JDK 99 release found."


def test_java_bin_points_into_the_selected_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtimes_dir = tmp_path / "runtimes"
    (runtimes_dir / "java" / "17.0.13+11").mkdir(parents=True)
    (runtimes_dir / "java" / "21.0.5+11").mkdir()
    monkeypatch.setattr(java_rt, "RUNTIMES_DIR", runtimes_dir)

    java = "java.exe" if os.name == "nt" else "java"
    assert java_rt.java_bin("17") == runtimes_dir / "java" / "17.0.13+11" / "bin" / java
    assert java_rt.java_bin("11") is None
