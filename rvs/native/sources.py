"""Advisory native source discovery. Never edit inputs or fetch remote includes."""

import json
import os
import shlex
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .. import output


if TYPE_CHECKING:
    from collections.abc import Iterator

    from .runner import NativeTool, RegistryRoute


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
                for include in _includes(shlex.split(line, comments=True)):
                    if "://" not in include:
                        yield from _file_sources(path.parent / include, seen)
                if path.name.endswith("npmrc"):
                    if "registry" in line.split("=", 1)[0].lower():
                        yield line
                else:
                    yield line
    except FileNotFoundError:
        return
    except OSError, UnicodeError, ValueError, ET.ParseError, TypeError, AttributeError:
        output.warn("Could not inspect a native source file; the native tool will validate it.")


def _includes(argv: list[str]) -> Iterator[str]:
    for index, arg in enumerate(argv):
        if arg in {"-r", "--requirement", "-c", "--constraint", "--build-constraint"}:
            if index + 1 < len(argv):
                yield argv[index + 1]
        elif arg.startswith(("--requirement=", "--constraint=", "--build-constraint=")):
            yield arg.split("=", 1)[1]
        elif arg.startswith(("-r", "-c")) and len(arg) > 2 and not arg.startswith("--"):
            yield arg[2:]


def warn_additional_sources(
    tool: NativeTool,
    original_argv: list[str],
    argv: list[str],
    env: dict[str, str],
    route: RegistryRoute,
) -> None:
    from .runner import (
        _arg_value,
        _candidate_config_files,
        _extract_urls,
        _relevant_env_values,
        _same_origin,
    )

    texts = [" ".join(argv), *_relevant_env_values(tool, env)]
    files = _candidate_config_files(tool, original_argv, env)
    directory = _arg_value(original_argv, "--directory", "-C") if tool == "uv" else None
    project = _arg_value(original_argv, "--project") if tool == "uv" else None
    if tool == "npm":
        directory = _arg_value(original_argv, "--prefix")
    base = Path(directory or os.getcwd()).resolve()
    if project:
        base = (base / project).resolve()
    names = {
        "pip": (),
        "uv": ("uv.lock", "uv.toml", "pyproject.toml"),
        "npm": ("package-lock.json", "npm-shrinkwrap.json", "package.json", ".npmrc"),
        "mvn": ("pom.xml",),
        "twine": (),
    }[tool]
    for parent in (base, *base.parents):
        files.extend(parent / name for name in names)
    if tool in {"pip", "uv"}:
        files.extend(base / name for name in _includes(original_argv) if "://" not in name)
    if tool == "uv":
        config = _arg_value(original_argv, "--config-file")
        if config:
            files.append(base / config)
    if tool == "mvn":
        pom = _arg_value(original_argv, "--file", "-f")
        if pom:
            path = base / pom
            files.append(path / "pom.xml" if path.is_dir() else path)
        global_settings = _arg_value(original_argv, "--global-settings", "-gs")
        if global_settings:
            files.append(base / global_settings)
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
        if tool == "pip":
            output.warn(
                "pip combines candidates across indexes; extra indexes have no lower priority."
            )
