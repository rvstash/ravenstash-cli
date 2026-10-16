"""Maven adapter: a temporary settings.xml that merges the user's settings."""

import os
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

from .. import output
from ..publishing import maven_publish_items
from ..runtime import tools
from .adapter import NativeAdapter
from .common import arg_value, nearest_named_files


if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..publishing import PublishItem
    from .common import PackageOperation, RegistryRoute


class Maven(NativeAdapter):
    name = "mvn"
    kind = "maven"
    env_prefixes = ("MAVEN_",)
    project_files = ("pom.xml",)

    def command_prefix(self) -> list[str]:
        return [tools.require("mvn", install_kind="system")]

    def operations(self, argv: list[str]) -> tuple[PackageOperation, ...]:
        return ("read", "publish") if self.is_publishing(argv) else ("read",)

    def is_publishing(self, argv: list[str]) -> bool:
        return maven_is_upload(argv)

    def publish_items(self, argv: list[str]) -> list[PublishItem]:
        return maven_publish_items(argv)

    def inject(
        self,
        cmd: list[str],
        native_arg_start: int,
        env: dict[str, str],
        route: RegistryRoute,
        temp_dir: Path,
        *,
        isolate: bool,
    ) -> None:
        del env
        maven_args = cmd[native_arg_start:]
        urls = [route.maven_repo_url]
        if maven_is_upload(maven_args):
            urls.append(route.maven_upload_url)
        settings_path = write_maven_settings(
            urls=urls,
            token=route.package_token,
            temp_dir=temp_dir,
            argv=maven_args,
            isolate=isolate,
            route=route,
        )
        _replace_maven_settings_arg(cmd, settings_path, native_arg_start)
        server_id = route.maven_server_id
        if maven_has_goal(maven_args, "deploy"):
            cmd.append(f"-DaltDeploymentRepository={server_id}::default::{route.maven_upload_url}")
        if maven_has_goal(maven_args, "deploy-file"):
            cmd.extend(
                [
                    f"-DrepositoryId={server_id}",
                    f"-Durl={route.maven_upload_url}",
                ]
            )

    def config_files(self, argv: list[str], env: Mapping[str, str]) -> list[Path]:
        del env
        candidates: list[Path] = []
        settings_file = _maven_settings_arg(argv)
        if settings_file:
            candidates.append(Path(settings_file).expanduser())
        candidates.append(Path.home() / ".m2/settings.xml")
        candidates.extend(nearest_named_files(("pom.xml",)))
        return candidates

    def extra_source_files(self, argv: list[str], base: Path) -> list[Path]:
        files: list[Path] = []
        pom = arg_value(argv, "--file", "-f")
        if pom:
            path = base / pom
            files.append(path / "pom.xml" if path.is_dir() else path)
        global_settings = arg_value(argv, "--global-settings", "-gs")
        if global_settings:
            files.append(base / global_settings)
        return files


def maven_has_goal(argv: list[str], goal: str) -> bool:
    return any(arg == goal or arg.endswith(f":{goal}") for arg in argv if not arg.startswith("-"))


def maven_is_upload(argv: list[str]) -> bool:
    return maven_has_goal(argv, "deploy") or maven_has_goal(argv, "deploy-file")


def write_maven_settings(
    *,
    urls: list[str],
    token: str,
    temp_dir: Path,
    argv: list[str],
    isolate: bool,
    route: RegistryRoute | None,
) -> Path:
    base_settings = None if isolate else _maven_settings_arg(argv)
    base_path = (
        Path(base_settings).expanduser() if base_settings else Path.home() / ".m2/settings.xml"
    )
    root = _load_maven_settings(base_path) if not isolate else ET.Element("settings")
    server_ids = set() if route is not None else _maven_server_ids_for_urls(urls, argv)
    if route is not None:
        server_ids.add(route.maven_server_id)
        _ensure_maven_profile(root, route, isolate=isolate)
    if not server_ids:
        server_ids.add(f"rvs-{secrets.token_hex(12)}")
    servers = _ensure_xml_child(root, "servers")
    if route is not None:
        for element in root.iter():
            if _local_name(element.tag) in {"repository", "pluginRepository", "snapshotRepository"}:
                if (
                    _child_text(element, "id") == route.maven_server_id
                    and _child_text(element, "url") not in urls
                ):
                    output.fatal(
                        "Native Maven settings reuse this run's Ravenstash repository ID "
                        "for another URL."
                    )
    for server_id in sorted(server_ids):
        _upsert_maven_server(servers, server_id, token)

    settings_path = temp_dir / "settings.xml"
    ET.ElementTree(root).write(settings_path, encoding="utf-8", xml_declaration=True)
    settings_path.chmod(0o600)
    return settings_path


def _load_maven_settings(path: Path) -> ET.Element:
    if path.exists() and path.is_file():
        try:
            return ET.parse(path).getroot()
        except ET.ParseError:
            pass
    return ET.Element("settings")


def _maven_server_ids_for_urls(urls: list[str], argv: list[str]) -> set[str]:
    ids: set[str] = set()
    for path in Maven().existing_config_files(argv, os.environ):
        if path.suffix.lower() != ".xml":
            continue
        try:
            root = ET.parse(path).getroot()
        except OSError, ET.ParseError:
            continue
        for element in root.iter():
            if _local_name(element.tag) not in {
                "repository",
                "pluginRepository",
                "snapshotRepository",
            }:
                continue
            repo_url = _child_text(element, "url")
            repo_id = _child_text(element, "id")
            if repo_id and repo_url and repo_url in urls:
                ids.add(repo_id)
    return ids


def _ensure_maven_profile(root: ET.Element, route: RegistryRoute, *, isolate: bool = False) -> None:
    server_id = route.maven_server_id
    mirrors = _ensure_xml_child(root, "mirrors")
    for existing in mirrors:
        if _child_text(existing, "id") == server_id:
            if _child_text(existing, "url") != route.maven_repo_url:
                output.fatal(
                    "Native Maven settings reuse this run's Ravenstash mirror ID for another URL."
                )
            continue
        pattern = _child_text(existing, "mirrorOf") or ""
        if pattern:
            output.warn(
                "Existing Maven mirrors are preserved for additional repositories; "
                "Central and the selected Ravenstash repository use the selected target."
            )
            # Exclude our repository and Central even from an existing exact match.
            remaining = [
                part.strip()
                for part in pattern.split(",")
                if part.strip() not in {"central", server_id}
            ]
            _ensure_xml_child(existing, "mirrorOf").text = ",".join(
                [*remaining, "!central", f"!{server_id}"]
            )
    mirror = _find_child_with_text(mirrors, "mirror", "id", server_id)
    if mirror is None:
        mirror = ET.SubElement(mirrors, "mirror")
        ET.SubElement(mirror, "id").text = server_id
    _ensure_xml_child(mirror, "url").text = route.maven_repo_url
    _ensure_xml_child(mirror, "mirrorOf").text = "*" if isolate else "central"

    profiles = _ensure_xml_child(root, "profiles")
    profile = _find_child_with_text(profiles, "profile", "id", "rvs")
    if profile is None:
        profile = ET.SubElement(profiles, "profile")
        ET.SubElement(profile, "id").text = "rvs"
    repositories = _ensure_xml_child(profile, "repositories")
    plugin_repositories = _ensure_xml_child(profile, "pluginRepositories")
    _upsert_maven_repository(repositories, "repository", route.maven_repo_url, server_id)
    _upsert_maven_repository(
        plugin_repositories, "pluginRepository", route.maven_repo_url, server_id
    )
    active_profiles = _ensure_xml_child(root, "activeProfiles")
    if _find_child_text(active_profiles, "activeProfile", "rvs") is None:
        ET.SubElement(active_profiles, "activeProfile").text = "rvs"


def _upsert_maven_repository(parent: ET.Element, tag: str, url: str, server_id: str) -> None:
    repo = _find_child_with_text(parent, tag, "id", server_id)
    if repo is None:
        repo = ET.SubElement(parent, tag)
        ET.SubElement(repo, "id").text = server_id
    url_element = _ensure_xml_child(repo, "url")
    url_element.text = url


def _upsert_maven_server(servers: ET.Element, server_id: str, token: str) -> None:
    server = _find_child_with_text(servers, "server", "id", server_id)
    if server is None:
        server = ET.SubElement(servers, "server")
        ET.SubElement(server, "id").text = server_id
    username = _ensure_xml_child(server, "username")
    password = _ensure_xml_child(server, "password")
    username.text = "__token__"
    password.text = token


def _replace_maven_settings_arg(
    cmd: list[str],
    settings_path: Path,
    native_arg_start: int,
) -> None:
    cleaned: list[str] = cmd[:native_arg_start]
    skip_next = False
    for arg in cmd[native_arg_start:]:
        if skip_next:
            skip_next = False
            continue
        if arg in {"-s", "--settings"}:
            skip_next = True
            continue
        if arg.startswith("--settings="):
            continue
        cleaned.append(arg)
    cleaned[native_arg_start:native_arg_start] = ["--settings", str(settings_path)]
    cmd[:] = cleaned


def _maven_settings_arg(argv: list[str]) -> str | None:
    return arg_value(argv, "--settings", "-s")


def _ensure_xml_child(parent: ET.Element, tag: str) -> ET.Element:
    for child in parent:
        if _local_name(child.tag) == tag:
            return child
    return ET.SubElement(parent, tag)


def _find_child_with_text(
    parent: ET.Element,
    child_tag: str,
    grandchild_tag: str,
    value: str,
) -> ET.Element | None:
    for child in parent:
        if _local_name(child.tag) != child_tag:
            continue
        if _child_text(child, grandchild_tag) == value:
            return child
    return None


def _find_child_text(parent: ET.Element, tag: str, value: str) -> str | None:
    for child in parent:
        if _local_name(child.tag) == tag and (child.text or "").strip() == value:
            return child.text
    return None


def _child_text(parent: ET.Element, tag: str) -> str | None:
    for child in parent:
        if _local_name(child.tag) == tag:
            text = (child.text or "").strip()
            return text or None
    return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
