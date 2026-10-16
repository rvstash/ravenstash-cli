"""Advisory native source discovery. Never edit inputs or fetch remote includes."""

import json
import re
import shlex
import tomllib
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import yaml

from .. import output
from .pypi import requirement_includes


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from .adapter import NativeAdapter
    from .common import RegistryRoute


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
# pnpm settings files: only these keys name registries.
_PNPM_SETTING_KEYS = ("registry", "registries")


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _locked_urls(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"url", "registry", "resolved"} and isinstance(item, str):
                yield item
            elif isinstance(item, (dict, list)):
                yield from _locked_urls(item)
    elif isinstance(value, list):
        for item in value:
            yield from _locked_urls(item)


def _file_sources(path: Path, seen: set[Path]) -> Iterator[str]:
    path = path.resolve()
    if path in seen:
        return
    seen.add(path)
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            data = json.loads(text)
            if path.name == "package.json":
                for key in ("dependencies", "devDependencies", "optionalDependencies", "overrides"):
                    yield from _strings(data.get(key, {}))
            else:
                yield from _locked_urls(data)
        elif path.suffix == ".toml" or path.name == "uv.lock":
            data = tomllib.loads(text)
            if path.name == "pyproject.toml":
                yield from _strings(data.get("tool", {}).get("uv", {}))
                project = data.get("project", {})
                yield from _strings(project.get("dependencies", []))
                yield from _strings(project.get("optional-dependencies", {}))
                yield from _strings(data.get("dependency-groups", {}))
                yield from _strings(data.get("build-system", {}).get("requires", []))
            else:
                yield from _locked_urls(data)
                for key in (
                    "index-url",
                    "extra-index-url",
                    "find-links",
                    "index",
                    "sources",
                    "pip",
                ):
                    yield from _strings(data.get(key, {}))
        elif path.name == "pnpm-lock.yaml":
            # Every URL in a pnpm lockfile (tarball, git repository, or specifier)
            # is a source. Lockfiles can be large, so this one is scanned as text.
            yield text
        elif path.suffix == ".yaml":
            data = yaml.safe_load(text)
            if isinstance(data, dict):
                for key in _PNPM_SETTING_KEYS:
                    yield from _strings(data.get(key, {}))
        elif path.suffix == ".xml":
            root = ET.fromstring(text)
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] in {
                    "repository",
                    "pluginRepository",
                    "snapshotRepository",
                    "mirror",
                }:
                    for child in element:
                        if child.tag.rsplit("}", 1)[-1] == "url" and child.text:
                            yield child.text
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                # Requirements may recursively include other files. No network reads.
                for include in requirement_includes(shlex.split(line, comments=True)):
                    if "://" not in include:
                        yield from _file_sources(path.parent / include, seen)
                if path.name.endswith("npmrc"):
                    if "registry" in line.split("=", 1)[0].lower():
                        yield line
                else:
                    yield line
    except FileNotFoundError:
        return
    except (
        OSError,
        UnicodeError,
        ValueError,
        ET.ParseError,
        TypeError,
        AttributeError,
        yaml.YAMLError,
    ):
        output.warn("Could not inspect a native source file; the native tool will validate it.")


def warn_additional_sources(
    adapter: NativeAdapter,
    original_argv: list[str],
    argv: list[str],
    env: dict[str, str],
    route: RegistryRoute,
) -> None:
    texts = [" ".join(argv), *adapter.env_values(env)]
    files = adapter.existing_config_files(original_argv, env)
    base = adapter.project_dir(original_argv)
    for parent in (base, *base.parents):
        files.extend(parent / name for name in adapter.project_files)
    files.extend(adapter.extra_source_files(original_argv, base))
    seen: set[Path] = set()
    for path in files:
        texts.extend(_file_sources(path, seen))

    # Compare repository paths as well as hosts. Never print credentials, query
    # strings, or token-bearing paths (including third-party entitlement URLs).
    from ..artifacts.routing import native_root_url

    roots = [
        native_root_url(url, route.native_path)
        for url in (route.read_base_url, route.push_base_url)
        if url is not None
    ]
    outside = set()
    for text in texts:
        for url in _extract_urls(text):
            try:
                parsed = urlsplit(url)
                if not any(
                    _same_origin(url, root)
                    and (parsed.path.rstrip("/") + "/").startswith(urlsplit(root).path)
                    for root in roots
                ):
                    outside.add(parsed.hostname or "unknown host")
            except ValueError:
                continue
    if outside:
        # Host strings are untrusted too; avoid interpolating Rich markup.
        output.warn(
            "Additional native sources were detected outside the selected Ravenstash target. "
            "Native configuration and lockfile rules still apply; direct downloads bypass "
            "this target's policy and accounting. Lockfiles are unchanged by rvs."
        )
        for note in adapter.source_notes():
            output.warn(note)


def _extract_urls(text: str) -> list[str]:
    return [match.group(0).rstrip(".,);]") for match in _URL_RE.finditer(text)]


def _same_origin(left: str, right: str) -> bool:
    first = urlsplit(left)
    second = urlsplit(right)
    try:
        return (
            first.scheme.lower(),
            first.hostname.rstrip(".").lower() if first.hostname else None,
            first.port or (443 if first.scheme.lower() == "https" else 80),
        ) == (
            second.scheme.lower(),
            second.hostname.rstrip(".").lower() if second.hostname else None,
            second.port or (443 if second.scheme.lower() == "https" else 80),
        )
    except ValueError:
        return False
