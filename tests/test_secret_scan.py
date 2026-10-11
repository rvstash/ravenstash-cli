"""The CI gate scans every commit for secrets with a pinned, independent scanner."""

import re
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent


def _ci_jobs() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]


def test_ci_gate_requires_the_secret_scan_for_every_change() -> None:
    jobs = _ci_jobs()
    scan = jobs["secret-scan"]
    gate = jobs["gate"]

    # The scan depends on no path filter: it runs for every push and ready pull request.
    assert "needs" not in scan
    assert scan["steps"][0]["with"] == {"fetch-depth": 0, "persist-credentials": False}
    assert "secret-scan" in gate["needs"]
    assert 'test "$SECRET_SCAN_RESULT" = success' in gate["steps"][0]["run"]


def test_secret_scan_runs_a_digest_pinned_scanner_over_the_history() -> None:
    command = _ci_jobs()["secret-scan"]["steps"][1]["run"]

    assert re.search(r"ghcr\.io/gitleaks/gitleaks@sha256:[0-9a-f]{64}\s", command)
    assert "git /repo --config /repo/.gitleaks.toml" in command
    assert "--redact" in command
    assert ":/repo:ro" in command


def test_secret_scan_allows_only_named_public_trust_anchors() -> None:
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))

    assert config["extend"] == {"useDefault": True}
    allowlist = config["allowlist"]
    assert allowlist["regexTarget"] == "line"
    # Each exemption names one assignment; none exempts a path, commit, or rule.
    assert set(allowlist) == {"description", "regexTarget", "regexes"}
    for pattern in allowlist["regexes"]:
        assert re.fullmatch(r'\w+ ?= ?"\[[0-9A-Fa-f\-]+\]\{40\}"', pattern), pattern
