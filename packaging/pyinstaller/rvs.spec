# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


ROOT = Path.cwd()
IS_WINDOWS = os.name == "nt"


def _metadata(package: str):
    try:
        return copy_metadata(package)
    except Exception:
        return []


def _submodules(package: str) -> list[str]:
    try:
        return collect_submodules(package)
    except Exception:
        return []


RUNTIME_DISTRIBUTIONS = (
    "annotated-doc",
    "anyio",
    "cffi",
    "click",
    "colorama",
    "cryptography",
    "h11",
    "httpcore2",
    "httpx2",
    "idna",
    "jaraco.classes",
    "jaraco.context",
    "jaraco.functools",
    "jeepney",
    "keyring",
    "markdown-it-py",
    "mdurl",
    "more-itertools",
    "pycparser",
    "Pygments",
    "pywin32-ctypes",
    "PyYAML",
    "ravenstash-cli",
    "rich",
    "SecretStorage",
    "shellingham",
    "tomli-w",
    "truststore",
    "typer",
)

datas = [(str(ROOT / "rvs" / "resources" / "ravenstash-cli.asc"), "rvs/resources")]
for distribution in RUNTIME_DISTRIBUTIONS:
    datas += _metadata(distribution)

hiddenimports = []
hiddenimports.append("msvcrt" if os.name == "nt" else "fcntl")
hiddenimports += _submodules("keyring.backends")
hiddenimports += [
    "secretstorage",
    "secretstorage.collection",
    "secretstorage.item",
    "jeepney",
    "jeepney.auth",
    "jeepney.bus",
    "jeepney.io.blocking",
    "jeepney.wrappers",
]

a = Analysis(
    [str(ROOT / "packaging" / "pyinstaller" / "entrypoint.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The encrypted vault is unavailable on Windows, where credentials use the
    # native Credential Manager. Release verification uses the pinned RSA key
    # directly and does not need a system or bundled cryptography library.
    excludes=["cryptography"] if IS_WINDOWS else [],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rvs",
    debug=False,
    bootloader_ignore_signals=False,
    strip=not IS_WINDOWS,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=os.environ.get("RAVENSTASH_CLI_CODESIGN_IDENTITY"),
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=not IS_WINDOWS,
    upx=True,
    upx_exclude=[],
    name="rvs",
)
