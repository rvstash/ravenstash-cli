"""Identify ORAS verbs independently of operand and option values."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Invocation:
    command: tuple[str, ...]

    @property
    def publishing(self) -> bool:
        return self.command in {
            ("push",),
            ("cp",),
            ("copy",),
            ("attach",),
            ("tag",),
            ("manifest", "push"),
            ("blob", "push"),
            ("restore",),
            ("manifest", "index", "create"),
            ("manifest", "index", "update"),
        }

    @property
    def deleting(self) -> bool:
        return self.command in {("manifest", "delete"), ("blob", "delete")}


def parse(argv: list[str]) -> Invocation:
    # ORAS root flags are switches, not positional arguments. Unknown options
    # are left to ORAS, with no inferred write grant from their values.
    index = 0
    while index < len(argv) and argv[index].split("=", 1)[0] in {
        "--debug",
        "--verbose",
        "--no-tty",
        "--help",
        "-h",
        "--version",
    }:
        index += 1
    if index == len(argv) or argv[index].startswith("-"):
        return Invocation(())
    command = (argv[index],)
    if command[0] in {"manifest", "blob", "repo"} and index + 1 < len(argv):
        command += (argv[index + 1],)
    if command == ("manifest", "index") and index + 2 < len(argv):
        command += (argv[index + 2],)
    return Invocation(command)


def registry_configs(argv: list[str]) -> tuple[list[str], dict[str, str | None]]:
    """Remove native config overrides so each selected file receives a temporary overlay."""
    invocation = parse(argv)
    flags = {"--registry-config", "--from-registry-config", "--to-registry-config"}
    sources: dict[str, str | None] = {}
    clean: list[str] = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--":
            clean.extend(argv[index:])
            break
        flag, equals, path = value.partition("=")
        if flag in flags:
            if not equals:
                index += 1
                if index == len(argv):
                    raise ValueError(f"{flag} requires a config path.")
                path = argv[index]
            if not path:
                raise ValueError(f"{flag} requires a config path.")
            sources[flag] = path
        else:
            clean.append(value)
        index += 1
    if invocation.command in {("cp",), ("copy",)}:
        base = sources.pop("--registry-config", None)
        sources.setdefault("--from-registry-config", base)
        sources.setdefault("--to-registry-config", base)
    elif invocation.command and invocation.command[0] in {
        "push",
        "pull",
        "attach",
        "tag",
        "discover",
        "resolve",
        "manifest",
        "blob",
        "repo",
        "login",
        "logout",
        "backup",
        "restore",
    }:
        sources.setdefault("--registry-config", None)
    return clean, sources


def local_only(argv: list[str]) -> bool:
    """Recognize native help/version and explicit local OCI-layout operations."""
    options = argv[: argv.index("--")] if "--" in argv else argv
    invocation = parse(argv)
    if any(value in {"--help", "-h", "--version"} for value in options):
        return True
    if invocation.command in {(), ("version",), ("help",), ("completion",)}:
        return True
    flags = {value.split("=", 1)[0] for value in options if not value.endswith("=false")}
    if invocation.command in {("cp",), ("copy",)}:
        return {"--from-oci-layout", "--to-oci-layout"} <= flags
    return "--oci-layout" in flags and invocation.command[0] not in {"login", "logout"}
