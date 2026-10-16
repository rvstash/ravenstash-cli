import io
import json
import tarfile
import zipfile
from typing import TYPE_CHECKING

import typer
from rvs import config, output
from rvs.native.adapters import ADAPTERS
from rvs.publishing import (
    confirm_publish,
    npm_artifact,
    oci_artifacts,
    pypi_artifacts,
)
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pathlib import Path


def wheel(path: Path, name: str = "acme_sdk", version: str = "1.4.0") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA", f"Name: {name}\nVersion: {version}\n"
        )
    return path


def tar(path: Path, member: str, data: bytes) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return path


def test_pypi_groups_selected_files_by_identity_and_does_not_require_other_wheels(tmp_path):
    linux = wheel(tmp_path / "acme_sdk-1.4.0-cp312-cp312-manylinux_2_17_x86_64.whl")
    windows = wheel(tmp_path / "acme_sdk-1.4.0-cp312-cp312-win_amd64.whl")
    source = tar(
        tmp_path / "acme_sdk-1.4.0.tar.gz",
        "acme_sdk-1.4.0/PKG-INFO",
        b"Name: acme-sdk\nVersion: 1.4.0\n",
    )
    other = wheel(tmp_path / "other.whl", "other", "2.0.0")
    items = pypi_artifacts([linux, source, other])
    assert [item.identity for item in items] == ["PyPI acme-sdk==1.4.0", "PyPI other==2.0.0"]
    assert items[0].details == (linux.name, source.name)
    assert items[0].file_count == 2
    later = pypi_artifacts([windows])
    assert later[0].identity == items[0].identity
    assert later[0].details == (windows.name,)
    assert later[0].file_count == 1


def test_preview_renders_grouped_files_and_defaults_to_no(tmp_path):
    file = wheel(tmp_path / "demo.whl")
    application = typer.Typer()

    @application.command()
    def publish():
        output.set_json(False)
        confirm_publish(
            "platform/backend",
            config.AccountContext("ac_23456789", "organization", account_handle="YYYY"),
            pypi_artifacts([file]),
        )

    result = CliRunner().invoke(application, [], input="\n")
    assert result.exit_code != 0
    assert (
        "Publish to platform/backend (org:YYYY)\n\nPyPI acme-sdk==1.4.0\n  demo.whl"
        in result.output
    )
    assert "Publish this 1 file? [y/N]" in result.output


def test_npm_scoped_identity_tarball_and_tag(tmp_path):
    package = {"name": "@acme/sdk", "version": "1.4.0"}
    (tmp_path / "package.json").write_text(json.dumps(package))
    item = npm_artifact(tmp_path, tag="next")
    assert item.identity == "npm @acme/sdk@1.4.0"
    assert "acme-sdk-1.4.0.tgz (to be packed" in item.details[0]
    assert item.details[1] == "npm tag: next"
    archive = tar(tmp_path / "renamed.tgz", "package/package.json", json.dumps(package).encode())
    assert npm_artifact(archive).identity == item.identity
    assert npm_artifact(archive).details[0] == str(archive)


def test_maven_coordinates_and_classified_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pom.xml").write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><parent><groupId>com.acme</groupId><version>1.4.0</version></parent><artifactId>sdk</artifactId></project>'
    )
    project = ADAPTERS["mvn"].publish_items(["deploy"])[0]
    assert project.identity == "Maven com.acme:sdk:1.4.0"
    assert project.file_count is None
    assert "resolved by Maven" in project.details[-1]


def test_helm_reference_uses_chart_metadata_and_encodes_build_metadata(tmp_path):
    chart = tar(
        tmp_path / "renamed.tgz",
        "api-chart/Chart.yaml",
        b"apiVersion: v2\nname: api-chart\nversion: '1.4.0+build.7'\n",
    )
    item = oci_artifacts(
        "helm", ["push", str(chart), "oci://oci.rvsta.sh/platform/backend"], "oci.rvsta.sh"
    )[0]
    assert item.identity == "Helm oci://oci.rvsta.sh/platform/backend/api-chart:1.4.0_build.7"
    assert item.details == (str(chart),)


def test_container_reference_digest_platform_and_build_tag():
    reference = "oci.rvsta.sh/platform/backend/api@sha256:" + "a" * 64
    item = oci_artifacts("docker", ["push", reference, "--platform=linux/arm64"], "oci.rvsta.sh")[0]
    assert item.identity == "Container " + reference
    assert item.details == ("Platforms: linux/arm64",)
    build = oci_artifacts(
        "docker",
        ["buildx", "build", "--push", "--tag=oci.rvsta.sh/platform/backend/api:1.4.0", "."],
        "oci.rvsta.sh",
    )
    assert build[0].identity == "Container oci.rvsta.sh/platform/backend/api:1.4.0"


def test_native_pypi_glob_and_default_selection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel(dist / "demo.whl")
    (dist / "unrelated.txt").write_text("unrelated")
    assert ADAPTERS["uv"].publish_items(["publish"])[0].details == ("demo.whl",)
    assert ADAPTERS["twine"].publish_items(
        ["upload", "--repository-url", "https://example.test", "dist/*.whl"]
    )[0].details == ("demo.whl",)
    # Unknown selection options must not produce a confidently incorrect inventory.
    assert (
        ADAPTERS["uv"].publish_items(["publish", "--directory", "another-project"])[0].file_count
        is None
    )
    assert (
        ADAPTERS["uv"].publish_items(["--directory", "another-project", "publish"])[0].file_count
        is None
    )
