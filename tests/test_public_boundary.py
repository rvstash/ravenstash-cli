"""Keep the tracked tree free of credentials, generated artifacts, and stray links.

This is a defense-in-depth policy check over every tracked file. It is not a
substitute for reviewing a change, nor for the dedicated secret scanner that
runs beside it in CI.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent

# Private keys and service credentials never belong in the tree.
CREDENTIAL = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b"
    r"|\bgh[pousr]_[A-Za-z0-9]{36}\b|hooks\.slack\.com/services/|\bxox[abprs]-[A-Za-z0-9-]+"
)

# Generated output, caches, and local artifacts are never tracked.
FORBIDDEN_PATHS = re.compile(
    r"(^|/)(__pycache__|\.venv|node_modules|dist|build|\.pytest_cache|\.ruff_cache)/"
    r"|\.egg-info(/|$)|\.py[co]$|(^|/)\.coverage$"
    r"|\.(whl|deb|rpm|dmg|msi|exe|dll|so|dylib|tgz|zip)$|\.tar\.(gz|xz|bz2)$"
)

# The contributor guide is self-contained: it points at nothing outside the repository.
AGENTS_FORBIDDEN = re.compile(r"\.\./|(?<![\w.])/(?:home|Users)/")

# Markdown links must stay inside the repository or use an absolute URL.
MARKDOWN_LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^)\s]+)\)")


def _tracked_files() -> list[str]:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("the public-boundary check runs in a Git checkout")
    listed = subprocess.run(
        [git, "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    return [path for path in listed.decode("utf-8").split("\0") if path]


def _text(path: str) -> str | None:
    try:
        return (ROOT / path).read_text(encoding="utf-8")
    except UnicodeDecodeError, FileNotFoundError, IsADirectoryError:
        return None


@pytest.fixture(scope="module")
def tracked() -> dict[str, str | None]:
    return {path: _text(path) for path in _tracked_files()}


def test_no_generated_or_local_artifacts_are_tracked(tracked: dict[str, str | None]) -> None:
    assert [path for path in tracked if FORBIDDEN_PATHS.search(path)] == []


def test_a_fresh_add_keeps_every_tracked_file(tracked: dict[str, str | None]) -> None:
    # A tracked file that an ignore rule matches survives only until the tree is
    # added to a new repository, where it is silently dropped.
    git = shutil.which("git")
    assert git is not None
    present = [path for path in tracked if (ROOT / path).is_file()]
    ignored = subprocess.run(
        [git, "check-ignore", "--no-index", "-z", "--stdin"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        input="\0".join(present).encode("utf-8"),
    )

    assert ignored.returncode in (0, 1), ignored.stderr
    assert [path for path in ignored.stdout.decode("utf-8").split("\0") if path] == []


def test_tracked_text_contains_no_keys_or_credentials(tracked: dict[str, str | None]) -> None:
    findings = [
        f"{path}:{number}"
        for path, text in tracked.items()
        if text is not None
        for number, line in enumerate(text.splitlines(), start=1)
        if CREDENTIAL.search(line)
    ]

    assert findings == []


def test_contributor_guide_stands_alone(tracked: dict[str, str | None]) -> None:
    guide = tracked.get("AGENTS.md")

    assert guide is not None
    assert AGENTS_FORBIDDEN.findall(guide) == []


def test_markdown_links_stay_inside_the_repository(tracked: dict[str, str | None]) -> None:
    broken = []
    for path, text in tracked.items():
        if text is None or not path.endswith(".md"):
            continue
        base = (ROOT / path).parent
        for target in MARKDOWN_LINK.findall(text):
            resolved = (base / target.split("#", 1)[0]).resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                broken.append(f"{path}: {target}")

    assert broken == []
