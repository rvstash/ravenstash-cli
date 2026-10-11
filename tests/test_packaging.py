import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import pytest
import yaml
from rvs.portable_update import extract_release


POSTINSTALL = Path(__file__).parents[1] / "packaging" / "scripts" / "postinstall.sh"
INSTALLER = Path(__file__).parents[1] / "packaging" / "install.sh"
WINDOWS_INSTALLER = Path(__file__).parents[1] / "packaging" / "install.ps1"
ROOT = Path(__file__).parents[1]


def _run_postinstall(rocm_root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"RAVENSTASH_ROCM_ROOT": str(rocm_root)}
    return subprocess.run(
        [str(POSTINSTALL)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.skipif(os.name == "nt", reason="Debian postinstall is POSIX-only")
def test_postinstall_is_silent_without_rocm_rvs(tmp_path: Path) -> None:
    result = _run_postinstall(tmp_path)

    assert result.stdout == ""


@pytest.mark.parametrize(
    "relative_path",
    ("rocm/bin/rvs", "rocm/extras-7/bin/rvs", "rocm-7.2.0/bin/rvs"),
)
@pytest.mark.skipif(os.name == "nt", reason="Debian postinstall is POSIX-only")
def test_postinstall_reports_rocm_rvs_and_ravenstash_alias(
    tmp_path: Path, relative_path: str
) -> None:
    rocm_rvs = tmp_path / relative_path
    rocm_rvs.parent.mkdir(parents=True)
    rocm_rvs.write_text("#!/bin/sh\n", encoding="utf-8")
    rocm_rvs.chmod(0o755)

    result = _run_postinstall(tmp_path)

    assert f"AMD ROCm Validation Suite was detected at {rocm_rvs}." in result.stdout
    assert "Both tools provide the 'rvs' shortcut" in result.stdout
    assert "'ravenstash' command" in result.stdout


def test_release_metadata_uses_apache_license() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    notice_text = (ROOT / "NOTICE").read_text(encoding="utf-8")
    project_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "Apache License\n                           Version 2.0" in license_text
    assert "Copyright 2026 Ravenstash" in notice_text
    assert 'license = "Apache-2.0"' in project_text


def test_release_artifacts_include_license_and_notice() -> None:
    shell_builds = [
        (ROOT / "packaging/scripts/build-tarball.sh").read_text(encoding="utf-8"),
        (ROOT / "packaging/scripts/build-deb.sh").read_text(encoding="utf-8"),
    ]
    portable_build = (ROOT / "packaging/scripts/build_portable.py").read_text(encoding="utf-8")

    for build in shell_builds:
        assert "cp LICENSE" in build
        assert "cp NOTICE" in build
    assert 'ROOT / "LICENSE"' in portable_build
    assert 'ROOT / "NOTICE"' in portable_build


def test_portable_bundle_includes_runtime_dependency_license_metadata() -> None:
    spec = (ROOT / "packaging/pyinstaller/rvs.spec").read_text(encoding="utf-8")

    assert '"cryptography"' in spec
    assert '"rvs/resources"' in spec
    assert '"httpcore2"' in spec
    assert '"httpx2"' in spec
    assert '"idna"' in spec
    assert '"truststore"' in spec
    assert '"httpcore"' not in spec
    assert '"httpx"' not in spec
    assert "datas += _metadata(distribution)" in spec


def test_bundled_update_key_matches_release_signing_identity() -> None:
    bundled = (ROOT / "rvs/resources/ravenstash-cli.asc").read_bytes()
    release = (ROOT / "packaging/repository/keys/ravenstash-cli.asc").read_bytes()
    fingerprint = (ROOT / "packaging/repository/keys/fingerprint.txt").read_text(encoding="ascii")

    assert bundled == release
    assert fingerprint.strip() == "3B7C20FC370D1A7C813DF3A2E9679F951AD8BAA0"


def test_runtime_sbom_does_not_emit_an_unknown_project_requirement() -> None:
    build = (ROOT / "packaging/scripts/build-release-artifacts.sh").read_text(encoding="utf-8")

    assert "--no-emit-project" in build


def test_apt_repository_script_exports_installable_public_key() -> None:
    script = (ROOT / "packaging" / "scripts" / "update-apt-repo.sh").read_text(encoding="utf-8")

    assert '--export "$RAVENSTASH_CLI_APT_GPG_KEY_ID"' in script
    assert '"${APT_REPO_DIR}/${RAVENSTASH_CLI_KEYRING}"' in script


def test_parallel_public_apt_gates_materialize_their_local_keyring() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    apt_gate = workflow.split("  verify-apt:\n", 1)[1].split("\n  publish-github-release:", 1)[0]

    prepare = "run: packaging/repository/prepare_keyring.sh"
    keyring_mount = (
        "packaging/repository/keys/${RAVENSTASH_CLI_KEYRING}"
        ":/usr/share/keyrings/${RAVENSTASH_CLI_KEYRING}:ro"
    )
    assert "runner: ubuntu-24.04-arm" in apt_gate
    assert prepare in apt_gate
    assert keyring_mount in apt_gate
    assert apt_gate.index(prepare) < apt_gate.index(keyring_mount)


def test_apt_publisher_batches_architectures_without_weakening_activation_order() -> None:
    publisher = (ROOT / "packaging/repository/publish_apt.sh").read_text(encoding="utf-8")

    assert '--include "*/by-hash/SHA256/*"' in publisher
    assert '--include "*/Packages"' in publisher
    assert '--include "*/InRelease"' in publisher
    assert publisher.index('--include "*/by-hash/SHA256/*"') < publisher.index(
        '--include "*/Packages"'
    )
    assert publisher.index('--include "*/Packages"') < publisher.index('--include "*/InRelease"')
    assert publisher.index('--include "*/InRelease"') < publisher.index(
        '"$repository/channels.json"'
    )
    assert 'readonly destination="s3://${bucket}/${RAVENSTASH_CLI_NAMESPACE}/apt"' in publisher
    assert "update_destination" not in publisher


@pytest.mark.skipif(os.name == "nt", reason="APT publication tooling is POSIX-only")
def test_apt_publisher_uses_constant_number_of_storage_calls(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    for channel in ("v0", "v1", "v2"):
        (repository / "dists" / channel).mkdir(parents=True)
    (repository / "pool").mkdir()
    calls = tmp_path / "aws-calls"
    binary = tmp_path / "bin"
    binary.mkdir()
    aws = binary / "aws"
    aws.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$AWS_CALLS"\n', encoding="utf-8")
    aws.chmod(0o755)

    subprocess.run(
        [
            str(ROOT / "packaging/repository/publish_apt.sh"),
            str(repository),
            "https://r2.example.test",
            "test-bucket",
        ],
        check=True,
        env=os.environ
        | {
            "AWS_CALLS": str(calls),
            "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}",
        },
    )

    assert len(calls.read_text(encoding="utf-8").splitlines()) == 8


def test_portable_update_policy_is_published_after_github_release() -> None:
    release_text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    release = yaml.safe_load(release_text)

    release_jobs = release["jobs"]
    assert release_jobs["publish-update-manifest"]["needs"] == "publish-github-release"
    assert release_jobs["sign-channel-manifest"]["needs"] == "publish-github-release"
    assert set(release_jobs["deploy-installer"]["needs"]) == {
        "publish-update-manifest",
        "sign-channel-manifest",
    }
    policy = release_text[
        release_text.index("  publish-update-manifest:") : release_text.index(
            "  sign-channel-manifest:"
        )
    ]
    assert "signed/channels.json.gpg" in policy
    assert 's3://${RAVENSTASH_CLI_STORAGE_BUCKET}/${RAVENSTASH_CLI_NAMESPACE}"' in policy


def test_scheduled_apt_refresh_waits_for_the_first_publication() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/refresh-apt.yml").read_text("utf-8"))
    jobs = workflow["jobs"]

    # PyYAML reads the ``on`` key as a boolean.
    assert set(workflow[True]) == {"schedule", "workflow_dispatch"}
    assert workflow[True]["workflow_dispatch"] is None
    # An unpublished repository has no signed state and no secrets: the schedule
    # must stay idle there, while a manual dispatch always runs.
    assert jobs["sign"]["if"] == (
        "github.event_name != 'schedule' || vars.RAVENSTASH_CLI_PUBLISHING == 'enabled'"
    )
    assert set(jobs) == {"sign", "publish"}
    assert jobs["publish"]["needs"] == "sign"
    assert "if" not in jobs["publish"]
    for document in ("RELEASING.md", "packaging/README.md"):
        assert "`RAVENSTASH_CLI_PUBLISHING`" in (ROOT / document).read_text(encoding="utf-8")


def test_first_stable_release_bootstraps_an_empty_apt_namespace() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text("utf-8"))["jobs"]
    restore = next(
        step["run"]
        for step in jobs["restore-apt"]["steps"]
        if step.get("name") == "Restore and authenticate canonical APT state"
    )
    authenticate = next(
        step["run"]
        for step in jobs["sign"]["steps"]
        if step.get("name", "").startswith("Authenticate release inventory and prior state")
    )
    append = (ROOT / "packaging/repository/append_and_sign_apt.sh").read_text(encoding="utf-8")

    # The only source of prior state is the release namespace itself.
    assert restore.count("aws s3 ") == 1
    assert (
        'aws s3 sync \\\n  "s3://${RAVENSTASH_CLI_STORAGE_BUCKET}/${RAVENSTASH_CLI_NAMESPACE}/apt/"'
        " prior/"
    ) in restore
    # An empty namespace becomes a marker; anything else must authenticate.
    assert (
        'if [[ -z "$(find prior -mindepth 1 -print -quit)" ]]; then\n'
        "  touch prior/BOOTSTRAP\n"
        "else\n"
        "  python3 packaging/repository/recover_missing_apt_indexes.py"
    ) in restore
    assert "python3 packaging/repository/verify_apt.py" in restore.split("else\n", 1)[1]
    # The signer accepts the marker only when it is the whole prior state.
    assert (
        "if [[ -f prior/BOOTSTRAP ]]; then\n"
        "    test \"$(find prior -mindepth 1 -maxdepth 1 -printf '%f\\n')\" = BOOTSTRAP\n"
        "  else\n"
        "    python3 packaging/repository/verify_apt.py"
    ) in authenticate
    assert (
        'if compgen -G "$prior_repository/dists/*/InRelease" >/dev/null; then\n'
        '  python3 "$script_dir/verify_apt.py" "$prior_repository" "$trusted_keyring"'
    ) in append
    assert 'else\n  test -f "$prior_repository/BOOTSTRAP"\n  mkdir -p "$output_repository"\nfi' in (
        append
    )
    # Without a prior manifest, the release channel is the recommendation.
    assert (
        'existing-recommended \\\n    "$output_repository" "$RAVENSTASH_CLI_APT_CHANNEL"' in append
    )


def test_release_keeps_target_handoffs_isolated() -> None:
    release_text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    jobs = yaml.safe_load(release_text)["jobs"]
    build_jobs = {
        name: job
        for name, job in jobs.items()
        if job.get("uses") == "./.github/workflows/_build-release-target.yml"
    }
    artifact_names = [
        Path(path).name
        for job in build_jobs.values()
        for path in job["with"]["artifact_paths"].splitlines()
    ]

    assert "merge-multiple: true" not in release_text
    assert "Download isolated target handoffs" in release_text
    assert 'test "${#matches[@]}" = 1' in release_text
    assert len(build_jobs) == 8
    assert {job["needs"] for job in build_jobs.values()} == {"validate"}
    assert len(artifact_names) == len(set(artifact_names)) == 10
    assert set(jobs["assemble"]["needs"]) == set(build_jobs)


def test_top_level_workflow_run_names_are_distinct_and_purpose_first() -> None:
    expected_prefixes = {
        "ci.yml": "${{ github.event_name == 'pull_request'",
        "platform-certification.yml": "${{ github.event_name == 'schedule'",
        "refresh-apt.yml": "${{ github.event_name == 'schedule'",
        "release.yml": "${{ inputs.kind == 'candidate'",
        "test-build.yml": "Test build ·",
    }

    for filename, prefix in expected_prefixes.items():
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
        )
        assert workflow["run-name"].startswith(prefix)
        assert "github.sha" not in workflow["run-name"]
        assert "inputs.source_sha" not in workflow["run-name"]


def test_protected_branch_has_stable_aggregate_ci_gates() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
    gate = jobs["gate"]
    assert gate["name"] == "CI gate"
    assert set(gate["needs"]) == {
        "changes",
        "quality",
        "platform-tests",
        "package-smoke",
        "release-policy",
        "nix",
        "secret-scan",
    }
    assert jobs["quality"]["needs"] == "changes"
    assert jobs["platform-tests"]["needs"] == "changes"
    assert jobs["package-smoke"]["needs"] == "changes"
    assert jobs["release-policy"]["needs"] == "changes"
    assert jobs["nix"]["needs"] == "changes"


def test_builder_images_name_their_release_and_digest() -> None:
    dockerfile = (ROOT / "packaging/Dockerfile.ubuntu20").read_text(encoding="utf-8")
    images = re.findall(r"^FROM (\S+)", dockerfile, re.MULTILINE)
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    uv_versions = set(re.findall(r'^ +version: "(\d+\.\d+\.\d+)"$', workflow, re.MULTILINE))

    # A digest alone follows whatever image is newest; the tag names the release it pins.
    assert len(images) == 2
    assert all(re.fullmatch(r"[\w./-]+:[\w.-]+@sha256:[0-9a-f]{64}", image) for image in images)
    assert images[1].startswith("ubuntu:20.04@")
    assert len(uv_versions) == 1
    assert images[0].startswith(f"ghcr.io/astral-sh/uv:{uv_versions.pop()}@")


def test_dependency_updates_keep_the_build_baseline() -> None:
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    updates = {entry["package-ecosystem"]: entry for entry in config["updates"]}

    assert set(updates) == {"uv", "npm", "github-actions", "docker"}
    # One grouped pull request per ecosystem instead of one per dependency.
    for ecosystem in ("uv", "npm", "github-actions"):
        assert [group["patterns"] for group in updates[ecosystem]["groups"].values()] == [["*"]]
    ignored = {
        rule["dependency-name"]: rule.get("update-types") for rule in updates["docker"]["ignore"]
    }
    assert ignored == {
        "ubuntu": ["version-update:semver-major", "version-update:semver-minor"],
        "astral-sh/uv": None,
    }


def test_every_change_runs_the_test_suite_and_its_policy_checks() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
    selector = jobs["changes"]["steps"][1]["run"]
    gate = jobs["gate"]["steps"][0]["run"]

    # No changed path, not even documentation, may skip the tracked-tree policy tests.
    assert "quality=false" not in selector
    assert selector.count("quality=true") == 1
    assert selector.index("quality=true") < selector.index("while IFS= read -r path")
    assert "pytest" in jobs["quality"]["steps"][-1]["run"]
    assert 'test "$QUALITY_RESULT" = success' in gate


def test_publication_graph_parallelizes_safe_jobs_and_serializes_mutations() -> None:
    publication = yaml.safe_load(
        (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    )
    jobs = publication["jobs"]

    assert "needs" not in jobs["validate"]
    assert jobs["restore-apt"]["needs"] == "validate"
    assert set(jobs["sign"]["needs"]) == {"assemble", "restore-apt"}
    assert jobs["publish-apt"]["needs"] == "sign"
    assert jobs["verify-apt"]["needs"] == "publish-apt"
    assert jobs["verify-apt"]["strategy"]["fail-fast"] is False
    assert len(jobs["verify-apt"]["strategy"]["matrix"]["include"]) == 2
    assert jobs["prepare-github-release"]["needs"] == "sign"
    prepare_release = jobs["prepare-github-release"]["steps"][-1]["run"]
    assert "gh release create" in prepare_release
    assert "--draft" in prepare_release
    assert "expected-release-assets.tsv" in prepare_release
    assert set(jobs["publish-github-release"]["needs"]) == {
        "prepare-github-release",
        "verify-apt",
    }
    publish_release = jobs["publish-github-release"]["steps"][0]["run"]
    assert "gh release create" not in publish_release
    assert "cmp expected-release-assets.tsv actual-release-assets.tsv" in publish_release
    assert 'gh api --method PATCH "$RELEASE_API_URL" -F draft=false' in publish_release
    assert jobs["publish-update-manifest"]["needs"] == "publish-github-release"
    assert jobs["sign-channel-manifest"]["needs"] == "publish-github-release"
    assert set(jobs["deploy-installer"]["needs"]) == {
        "publish-update-manifest",
        "sign-channel-manifest",
    }
    assert set(jobs["publish-channel-manifest"]["needs"]) == {
        "sign-channel-manifest",
        "deploy-installer",
    }
    assert set(jobs["cleanup-artifacts"]["needs"]) == {
        "publish-github-release",
        "publish-channel-manifest",
    }
    assert jobs["restore-apt"]["if"] == "inputs.kind == 'stable'"
    assert jobs["publish-apt"]["if"] == "inputs.kind == 'stable'"
    assert jobs["verify-apt"]["if"] == "inputs.kind == 'stable'"


def test_release_builds_once_and_appends_architectures_once() -> None:
    publication = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    append = "packaging/repository/append_and_sign_apt.sh"

    assert "candidate_run_id" not in publication
    assert "release-candidate.yml" not in publication
    assert "refs/remotes/origin/${SOURCE_BRANCH}" in publication
    assert "inputs.kind == 'candidate'" in publication
    assert "--prerelease" in publication
    for gate in ("ci.yml", "platform-certification.yml"):
        assert gate in publication
    assert "platform-ci.yml" not in publication
    assert "release-policy-ci.yml" not in publication
    assert "${{ env.RAVENSTASH_CLI_PACKAGE }}-release-${{ inputs.source_sha }}" in publication
    assert publication.count(append) == 1
    assert '"build/${package}_${VERSION}_amd64.deb"' in publication
    assert '"build/${package}_${VERSION}_arm64.deb"' in publication
    assert "APT_PROMOTE_CHANNEL" not in publication


def test_test_build_is_expiring_unsigned_and_never_publishes() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/test-build.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    build_jobs = {name: job for name, job in jobs.items() if "uses" in job}

    assert len(build_jobs) == 8
    assert {job["needs"] for job in build_jobs.values()} == {"prepare"}
    assert all(
        job["with"]["artifact_prefix"] == "ravenstash-cli-test-target"
        for job in build_jobs.values()
    )
    assert all("version_override" in job["with"] for job in build_jobs.values())
    assert jobs["assemble"]["steps"][-2]["with"]["retention-days"] == 7
    workflow_text = (ROOT / ".github/workflows/test-build.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in workflow_text
    assert "contents: write" not in workflow_text
    assert "gh release" not in workflow_text
    assert "publish_apt" not in workflow_text
    assert "cosign" not in workflow_text


def test_successful_publication_is_not_failed_by_best_effort_cleanup() -> None:
    publication = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "Delete current-run handoffs\n        continue-on-error: true" in publication


def test_publication_refuses_to_replace_an_existing_tag_or_release() -> None:
    publication = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    release_slot = "packaging/scripts/assert-github-release-slot-empty.sh"
    assert publication.count(release_slot) == 2
    assert "gh release upload" not in publication
    assert "--clobber" not in publication


@pytest.mark.parametrize(
    ("mode", "expected_returncode"),
    (("empty", 0), ("tag", 1), ("release", 1), ("api-error", 1)),
)
@pytest.mark.skipif(os.name == "nt", reason="release-slot tooling is POSIX-only")
def test_release_slot_check_fails_closed(
    tmp_path: Path, mode: str, expected_returncode: int
) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    gh = binary / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$GH_MODE:$*" in\n'
        "  api-error:*) exit 1 ;;\n"
        "  tag:*matching-refs*) printf '%s\\n' refs/tags/v0.15.4 ;;\n"
        "  release:*releases*) printf '%s\\n' 1234 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)

    result = subprocess.run(
        [
            str(ROOT / "packaging/scripts/assert-github-release-slot-empty.sh"),
            "rvstash/ravenstash-cli",
            "v0.15.4",
        ],
        check=False,
        env=os.environ
        | {
            "GH_MODE": mode,
            "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}",
        },
    )

    assert result.returncode == expected_returncode


@pytest.mark.skipif(os.name == "nt", reason="release-slot tooling is POSIX-only")
def test_release_slot_check_accepts_candidate_tag(tmp_path: Path) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    gh = binary / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gh.chmod(0o755)

    result = subprocess.run(
        [
            str(ROOT / "packaging/scripts/assert-github-release-slot-empty.sh"),
            "rvstash/ravenstash-cli",
            "v0.15.4rc1",
        ],
        check=False,
        env=os.environ | {"PATH": f"{binary}{os.pathsep}{os.environ['PATH']}"},
    )

    assert result.returncode == 0


def test_ci_only_tracks_main() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert workflow.count("branches: [main]") == 2
    assert "release/v" not in workflow


def test_reusable_builder_uses_exact_bundles_and_supplies_musl_bash() -> None:
    builder = (ROOT / ".github/workflows/_build-release-target.yml").read_text(encoding="utf-8")

    assert "apk add --no-cache bash binutils build-base libffi-dev" in builder
    assert 'bundle="build/release/${RAVENSTASH_CLI_ASSET_STEM}-v${VERSION}-${TARGET}"' in builder
    assert "find build/release" not in builder
    assert builder.count('test -d "$bundle"') == 2
    assert "prepare_test_build.py apply" in builder


def test_channel_recommendation_activates_after_installer_verification() -> None:
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    jobs = release["jobs"]

    assert jobs["publish-update-manifest"]["needs"] == "publish-github-release"
    assert jobs["sign-channel-manifest"]["needs"] == "publish-github-release"
    assert jobs["publish-update-manifest"]["environment"] == "apt-storage"
    assert jobs["sign-channel-manifest"]["environment"] == "apt-signing"
    assert set(jobs["deploy-installer"]["needs"]) == {
        "publish-update-manifest",
        "sign-channel-manifest",
    }
    assert jobs["deploy-installer"]["environment"] == "installer-delivery"
    assert set(jobs["publish-channel-manifest"]["needs"]) == {
        "sign-channel-manifest",
        "deploy-installer",
    }
    assert jobs["publish-channel-manifest"]["environment"] == "apt-storage"
    assert not (ROOT / ".github/workflows/promote-installer.yml").exists()


def test_installer_worker_binds_both_public_routes() -> None:
    configuration = (ROOT / "packaging/installer-worker/wrangler.toml").read_text(encoding="utf-8")

    assert 'pattern = "https://ravenstash.com/install.sh*"' in configuration
    assert 'pattern = "https://ravenstash.com/install.ps1*"' in configuration
    # Routes name their zone; the tree carries no zone or account identifier.
    assert configuration.count('zone_name = "ravenstash.com"') == 2
    assert "zone_id" not in configuration
    assert "account_id" not in configuration


def test_installer_worker_embeds_both_installers_before_every_build() -> None:
    worker = ROOT / "packaging/installer-worker"
    scripts = json.loads((worker / "package.json").read_text(encoding="utf-8"))["scripts"]
    embed = (worker / "scripts/embed-installer.mjs").read_text(encoding="utf-8")
    entry = (worker / "src/index.js").read_text(encoding="utf-8")
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    # The embedded copies are build output: regenerated before use, never tracked.
    assert scripts["check"].startswith("npm run embed && ")
    assert scripts["deploy"].startswith("npm run embed && ")
    for module in ("install.generated.js", "install-ps1.generated.js"):
        assert f'"../src/{module}"' in embed
        assert f'"./{module}"' in entry
    assert "packaging/installer-worker/src/*.generated.js" in ignored


def test_debian_package_installs_node_signature_verifier() -> None:
    manifest = (ROOT / "packaging" / "scripts" / "build-deb.sh").read_text(encoding="utf-8")

    assert "Depends: ca-certificates, gpgv" in manifest
    assert '"$STAGING/usr/bin/docker-credential-rvs"' in manifest


@pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg is unavailable")
def test_release_candidate_uses_debian_prerelease_ordering() -> None:
    helper = ROOT / "packaging/scripts/common.sh"
    converted = subprocess.run(
        ["bash", "-c", f'source "{helper}"; rvs_debian_version 0.15.4rc2'],
        check=True,
        capture_output=True,
        text=True,
    )
    ordering = subprocess.run(
        ["dpkg", "--compare-versions", converted.stdout.strip(), "lt", "0.15.4"],
        check=False,
    )

    assert converted.stdout == "0.15.4~rc2\n"
    assert ordering.returncode == 0


def test_frozen_bundle_dispatches_docker_credential_helper() -> None:
    entrypoint = (ROOT / "packaging" / "pyinstaller" / "entrypoint.py").read_text(encoding="utf-8")
    shared_entrypoint = (ROOT / "rvs" / "entrypoint.py").read_text(encoding="utf-8")
    build = (ROOT / "packaging" / "scripts" / "build-pyinstaller.sh").read_text(encoding="utf-8")

    assert "from rvs.entrypoint import main" in entrypoint
    assert 'Path(sys.argv[0]).stem == "docker-credential-rvs"' in shared_entrypoint
    assert "dist/pyinstaller/rvs/docker-credential-rvs" in build


def _gnu_tar_is_available() -> bool:
    tar = shutil.which("tar")
    if os.name == "nt" or tar is None or shutil.which("bash") is None:
        return False
    described = subprocess.run([tar, "--version"], check=False, capture_output=True, text=True)
    return "GNU tar" in described.stdout


def _glibc_tarball_source(tmp_path: Path) -> Path:
    """Place the tarball builder next to a frozen bundle shaped like the real one."""
    source = tmp_path / "source"
    scripts = source / "packaging" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("common.sh", "build-tarball.sh"):
        shutil.copy2(ROOT / "packaging" / "scripts" / name, scripts / name)
    shutil.copy2(ROOT / "packaging" / "identity.env", source / "packaging" / "identity.env")
    for document in ("README.md", "LICENSE", "NOTICE"):
        shutil.copy2(ROOT / document, source / document)
    frozen = source / "dist" / "pyinstaller" / "rvs"
    (frozen / "_internal").mkdir(parents=True)
    (frozen / "_internal" / "base_library.zip").write_bytes(b"library")
    launcher = frozen / "rvs"
    launcher.write_text("#!/bin/sh\nprintf 'Ravenstash CLI 0.15.4\\n'\n", encoding="utf-8")
    launcher.chmod(0o755)
    # The frozen build links its aliases, which the Debian package keeps as links.
    for alias in ("ravenstash", "docker-credential-rvs"):
        (frozen / alias).symlink_to("rvs")
    return source


def _build_glibc_tarball(source: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "packaging/scripts/build-tarball.sh"],
        check=False,
        capture_output=True,
        text=True,
        cwd=source,
        env=os.environ
        | {
            "RAVENSTASH_CLI_VERSION": "0.15.4",
            "RAVENSTASH_CLI_ARCH": "amd64",
            "SOURCE_DATE_EPOCH": "0",
        },
    )


@pytest.mark.skipif(not _gnu_tar_is_available(), reason="the glibc tarball build needs GNU tar")
def test_glibc_tarball_ships_command_aliases_the_updater_accepts(tmp_path: Path) -> None:
    source = _glibc_tarball_source(tmp_path)

    result = _build_glibc_tarball(source)

    assert result.returncode == 0, result.stderr
    archive = source / "dist" / "release" / "ravenstash-cli-v0.15.4-linux-amd64.tar.gz"
    with tarfile.open(archive, "r:gz") as bundle:
        assert [m.name for m in bundle.getmembers() if not (m.isreg() or m.isdir())] == []
    extracted = extract_release(archive, tmp_path / "extracted", "0.15.4", "linux-amd64")
    launcher = (extracted / "rvs").read_bytes()
    for alias in ("ravenstash", "docker-credential-rvs"):
        assert not (extracted / alias).is_symlink()
        assert (extracted / alias).read_bytes() == launcher
        assert os.access(extracted / alias, os.X_OK)
    # The shared frozen bundle is left as it was for the Debian package.
    assert (source / "dist" / "pyinstaller" / "rvs" / "ravenstash").is_symlink()


@pytest.mark.skipif(not _gnu_tar_is_available(), reason="the glibc tarball build needs GNU tar")
def test_glibc_tarball_build_fails_when_a_link_member_remains(tmp_path: Path) -> None:
    source = _glibc_tarball_source(tmp_path)
    internal = source / "dist" / "pyinstaller" / "rvs" / "_internal"
    (internal / "libexample.so").symlink_to("base_library.zip")

    result = _build_glibc_tarball(source)

    assert result.returncode == 1
    assert "not regular files or directories" in result.stderr
    assert "_internal/libexample.so" in result.stderr
    assert not (source / "dist" / "release" / "ravenstash-cli-v0.15.4-linux-amd64.tar.gz").exists()


def test_portable_archive_builds_copy_aliases_and_run_the_updater_extraction() -> None:
    scripts = ROOT / "packaging" / "scripts"
    portable = (scripts / "build_portable.py").read_text(encoding="utf-8")
    tarball = (scripts / "build-tarball.sh").read_text(encoding="utf-8")
    artifacts = (scripts / "build-release-artifacts.sh").read_text(encoding="utf-8")
    smoke = (scripts / "smoke_portable.py").read_text(encoding="utf-8")
    builder = (ROOT / ".github/workflows/_build-release-target.yml").read_text(encoding="utf-8")

    # Installed clients reject link members, so no archive builder may create one.
    for link_call in ("symlink", "hardlink", "os.link(", "ln -s"):
        assert link_call not in portable
        assert link_call not in tarball
    assert "_copy_alias(executable, bundle" in portable
    assert 'install -m 0755 "$STAGING/rvs" "$STAGING/$alias"' in tarball
    assert "contains members that are not regular files or directories" in tarball
    # Every target unpacks its own archive with the updater before it is handed off.
    assert "extracted = extract_release(archive," in smoke
    assert "packaging/scripts/smoke_portable.py" in artifacts
    assert artifacts.index("build-tarball.sh") < artifacts.index("smoke_portable.py")
    assert builder.count("packaging/scripts/smoke_portable.py") == 2
    assert "packaging/scripts/build-in-ubuntu20.sh" in builder


def test_installer_is_owned_by_cli_packaging_and_pins_release_identity() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    major, _minor, _patch = version.split(".", 2)
    channel = f"v{major}"

    if os.name != "nt":
        assert INSTALLER.stat().st_mode & 0o111
    assert "https://releases.ravenstash.com/ravenstash/apt" in source
    assert f'readonly release_version="{version}"' in source
    assert f'readonly compatibility_channel="{channel}"' in source
    assert "3B7C20FC370D1A7C813DF3A2E9679F951AD8BAA0" in source
    assert "--proto '=https' --proto-redir '=https' --tlsv1.2" in source

    windows_source = WINDOWS_INSTALLER.read_text(encoding="utf-8")
    assert f'$ReleaseVersion = "{version}"' in windows_source
    assert f'$CompatibilityChannel = "{channel}"' in windows_source
    assert "Get-FileHash -Algorithm SHA256" in windows_source
    assert "Get-AuthenticodeSignature" not in windows_source
    assert "RVS_GITHUB_TOKEN" in windows_source


def test_installer_accepts_musl_ldd_diagnostic_exit_status() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert source.count('ldd_output="$(ldd --version 2>&1 || true)"') == 2
    assert "ldd --version 2>&1 | grep -qi musl" not in source


def test_installer_prints_ravenstash_banner_after_success() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert "RVS_INSTALL_NO_BANNER" in source
    assert "RVS_INSTALL_NO_HINTS" in source
    assert "█████████████████████████▄▄▄" in source
    assert "CLI installed successfully" in source
    assert "print_success_banner" in source
    assert 'say "next: rvs auth login"' in source


def test_installer_supports_explicit_portable_ci_installation() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert 'readonly portable_installer_api="1"' in source
    assert 'readonly install_method="${RVS_INSTALL_METHOD:-auto}"' in source
    assert "auto | portable)" in source
    assert '[[ "$install_method" == "auto" ]]' in source
    assert "RVS_INSTALL_VERSION requires RVS_INSTALL_METHOD=portable" in source
    assert "RVS_INSTALL_VERSION must be a stable or release-candidate version" in source
    assert "(rc[1-9][0-9]*)?" in source
    assert "${asset_stem}-v${selected_release_version}" in source
    assert "portable_compatibility_channel" not in source
    assert '--channel "$compatibility_channel"' in source


def test_portable_installers_record_atomic_update_metadata() -> None:
    posix = INSTALLER.read_text(encoding="utf-8")
    windows = WINDOWS_INSTALLER.read_text(encoding="utf-8")

    assert "_record-install" in posix
    assert 'ln -sfn "$selected_release_version" "${install_root}/current"' in posix
    assert '"${install_root}/current/rvs"' in posix
    assert "_record-install" in windows
    assert "Join-Path $source $ReceiptName" in windows


def test_package_manager_manifests_cover_both_desktop_architectures(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    for name in (
        "ravenstash-cli-v0.15.3-macos-amd64.tar.gz",
        "ravenstash-cli-v0.15.3-macos-arm64.tar.gz",
        "ravenstash-cli-v0.15.3-windows-amd64.zip",
        "ravenstash-cli-v0.15.3-windows-arm64.zip",
    ):
        (release / name).write_bytes(name.encode())

    subprocess.run(
        [
            sys.executable,
            str(ROOT / "packaging/scripts/generate_package_manifests.py"),
            str(release),
            "0.15.3",
            "rvstash/ravenstash-cli",
        ],
        check=True,
    )

    archive = release / "ravenstash-cli-v0.15.3-package-manifests.tar.gz"
    winget = "package-manifests/winget/Ravenstash.RavenstashCLI.v0"
    with tarfile.open(archive) as package:
        names = package.getnames()
        installer = package.extractfile(f"{winget}.installer.yaml")
        assert installer is not None
        installer_text = installer.read().decode()
        locale = package.extractfile(f"{winget}.locale.en-US.yaml")
        assert locale is not None
        locale_text = locale.read().decode()
        for name in names:
            if name.endswith(".yaml"):
                manifest = package.extractfile(name)
                assert manifest is not None
                assert isinstance(yaml.safe_load(manifest), dict)
        cask = package.extractfile("package-manifests/homebrew/ravenstash-cli@0.rb")
        assert cask is not None
        cask_text = cask.read().decode()
    assert "package-manifests/homebrew/ravenstash-cli@0.rb" in names
    assert "Architecture: x64" in installer_text
    assert "Architecture: arm64" in installer_text
    assert "License: Apache-2.0" in locale_text
    assert "rvstash/ravenstash-cli/releases/download/v0.15.3/ravenstash-cli-v" in installer_text
    assert 'arch arm: "arm64", intel: "amd64"' in cask_text


def test_package_manager_manifests_use_rolling_major_channel_for_v1(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    for name in (
        "ravenstash-cli-v1.1.0-macos-amd64.tar.gz",
        "ravenstash-cli-v1.1.0-macos-arm64.tar.gz",
        "ravenstash-cli-v1.1.0-windows-amd64.zip",
        "ravenstash-cli-v1.1.0-windows-arm64.zip",
    ):
        (release / name).write_bytes(name.encode())

    subprocess.run(
        [
            sys.executable,
            str(ROOT / "packaging/scripts/generate_package_manifests.py"),
            str(release),
            "1.1.0",
            "rvstash/ravenstash-cli",
        ],
        check=True,
    )

    with tarfile.open(release / "ravenstash-cli-v1.1.0-package-manifests.tar.gz") as package:
        names = package.getnames()
    assert "package-manifests/homebrew/ravenstash-cli@1.rb" in names
    assert any("Ravenstash.RavenstashCLI.v1" in name for name in names)
