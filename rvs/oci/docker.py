"""Bounded Docker argument parsing and private image shorthand."""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import typer

from .. import output


_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_NAME = re.compile(rf"{_COMPONENT}(?:/{_COMPONENT})*")
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_GLOBAL_VALUES = {
    "--config",
    "--context",
    "-c",
    "--host",
    "-H",
    "--log-level",
    "-l",
    "--tlscacert",
    "--tlscert",
    "--tlskey",
}
_GLOBAL_FLAGS = {"--debug", "-D", "--tls", "--tlsverify", "--help", "-h", "--version", "-v"}


@dataclass
class Invocation:
    argv: list[str]
    globals: list[str]
    command: tuple[str, ...]
    operands: list[int]
    config_dir: Path | None = None
    all_tags: bool = False

    @property
    def publishing(self) -> bool:
        if self.command in {
            ("push",),
            ("image", "push"),
            ("manifest", "push"),
            ("buildx", "imagetools", "create"),
        }:
            return True
        return self.command in {("build",), ("image", "build"), ("buildx", "build")} and any(
            arg == "--push" or arg == "--push=true" for arg in self.argv
        )


def parse(argv: list[str]) -> Invocation:
    """Remove --config so it cannot bypass the temporary credential overlay."""
    prefix: list[str] = []
    config_dir = None
    index = 0
    while index < len(argv) and argv[index].startswith("-"):
        argument = argv[index]
        option, separator, value = argument.partition("=")
        attached = False
        if not separator and argument[:2] in {"-c", "-H", "-l"} and len(argument) > 2:
            option, value, attached = argument[:2], argument[2:], True
        if option in _GLOBAL_VALUES:
            consumed = 1
            if not separator and not attached:
                if index + 1 >= len(argv):
                    output.fatal(f"Docker option {option} requires a value.")
                value = argv[index + 1]
                consumed = 2
            if option == "--config":
                config_dir = Path(value).expanduser().absolute()
            else:
                prefix.extend(argv[index : index + consumed])
            index += consumed
        elif option in _GLOBAL_FLAGS:
            prefix.append(argument)
            index += 1
        else:
            output.fatal(f"Unsupported Docker global option {argument}.")
    remaining = argv[index:]
    command = tuple(remaining[:1])
    if remaining and remaining[0] in {"image", "buildx", "manifest"}:
        command = tuple(remaining[:2])
        if command == ("buildx", "imagetools"):
            command = tuple(remaining[:3])
    result = Invocation([*prefix, *remaining], prefix, command, [], config_dir)
    if command not in {
        ("push",),
        ("pull",),
        ("tag",),
        ("image", "push"),
        ("image", "pull"),
        ("image", "tag"),
    }:
        return result
    index = len(prefix) + len(command)
    positional_only = False
    while index < len(result.argv):
        arg = result.argv[index]
        option, separator, value = arg.partition("=")
        if arg == "--" and not positional_only:
            positional_only = True
        elif arg.startswith("-") and not positional_only:
            if option in {"--help", "-h"}:
                result.operands = []
                return result
            if command[-1] != "tag" and option in {"--all-tags", "-a", "--quiet", "-q"}:
                if separator and value not in {"true", "false"}:
                    output.fatal(f"Invalid Docker boolean option {arg}.")
                if option in {"--all-tags", "-a"}:
                    result.all_tags = not separator or value == "true"
            elif command[-1] != "tag" and option == "--platform":
                if not separator:
                    index += 1
                    if index >= len(result.argv):
                        output.fatal("Docker --platform requires a value.")
            else:
                output.fatal(f"Unsupported Docker {command[-1]} option {arg}.")
        else:
            result.operands.append(index)
        index += 1
    expected = 2 if command[-1] == "tag" else 1
    if len(result.operands) != expected:
        output.fatal(f"Docker {command[-1]} requires {expected} image reference(s).")
    return result


def expand(invocation: Invocation, root: str) -> tuple[str, str] | None:
    """Rewrite only the remote operand. Return source/destination for local tagging."""
    if not invocation.operands:
        return None
    index = invocation.operands[-1]
    value = invocation.argv[index]
    first, slash, _ = value.partition("/")
    if slash and (first == "localhost" or "." in first or ":" in first or first.lower() != first):
        return None
    name_tag, at, digest = value.partition("@")
    name, colon, tag = name_tag.partition(":")
    if re.fullmatch(r"[0-9a-f]{64}", value) or value.startswith("sha256:"):
        output.fatal("An image ID needs a destination name. Use `rvs docker tag <id> <name:tag>`.")
    if not _NAME.fullmatch(name) or (colon and not _TAG.fullmatch(tag)):
        output.fatal(f"Invalid private Docker image reference: {value}")
    if at and (
        invocation.command[-1] != "pull" or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    ):
        output.fatal("Digest shorthand is supported only for pulls using name@sha256:<digest>.")
    if invocation.all_tags and invocation.command[-1] == "push":
        output.fatal("Docker push --all-tags requires a fully qualified image reference.")
    if invocation.all_tags and (colon or at):
        output.fatal("Docker --all-tags requires an image name without a tag or digest.")
    suffix = value if colon or at or invocation.all_tags else f"{value}:latest"
    destination = f"{root}/{suffix}"
    if len(f"{root}/{name}") > 255:
        output.fatal("Resolved Docker image name exceeds 255 characters.")
    invocation.argv[index] = destination
    output_message = f"Docker {invocation.command[-1]}: {destination}"
    # Keep native stdout available for --quiet output and scripts.
    typer.echo(output_message, err=True)
    if invocation.command[-1] == "push":
        return suffix, destination
    if invocation.command[-1] == "tag":
        return invocation.argv[invocation.operands[0]], destination
    return None


def inspect_image(
    executable: str,
    globals: list[str],
    env: dict[str, str],
    reference: str,
    *,
    missing_ok: bool = False,
) -> str | None:
    result = subprocess.run(
        [executable, *globals, "image", "inspect", "--format", "{{.Id}}", reference],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        if missing_ok and "no such image:" in result.stderr.lower():
            return None
        output.fatal(f"Cannot inspect local Docker image {reference}: {result.stderr.strip()}")
    identity = result.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
        output.fatal(f"Docker returned an invalid image identity for {reference}.")
    return identity


def check_tag(
    executable: str, invocation: Invocation, env: dict[str, str], source: str, destination: str
) -> str:
    identity = inspect_image(executable, invocation.globals, env, source)
    assert identity is not None
    existing = inspect_image(executable, invocation.globals, env, destination, missing_ok=True)
    if existing is not None and existing != identity:
        output.fatal(
            f"Local destination {destination} points to another image. "
            "Use plain `docker tag <source> <full-destination>` to replace it explicitly."
        )
    return identity
