"""Resolve Helm chart operands without changing local paths or explicit URLs."""

import re
from dataclasses import dataclass
from pathlib import Path

import typer

from .. import output


# Helm 3 native flag arity. Unknown bare flags fail closed: guessing whether the
# next token is a value could rewrite a release name, value, or output path.
_VALUES = set(
    """
burst-limit kube-apiserver kube-as-group kube-as-user kube-ca-file kube-context
kube-tls-server-name kube-token kubeconfig namespace qps registry-config
repository-cache repository-config ca-file cert-file key-file keyring password
username version repo destination untardir description labels name-template
output post-renderer post-renderer-args set set-file set-json set-literal
set-string timeout values history-max api-versions kube-version output-dir
show-only jsonpath
""".split()
)
_FLAGS = set(
    """
debug kube-insecure-skip-tls-verify help atomic create-namespace dependency-update
devel disable-openapi-validation enable-dns force generate-name hide-notes
hide-secret insecure-skip-tls-verify no-hooks pass-credentials plain-http
render-subchart-notes replace skip-crds skip-schema-validation take-ownership
verify wait wait-for-jobs prov untar cleanup-on-fail install reset-then-reuse-values
reset-values reuse-values include-crds is-upgrade release-name skip-tests validate
""".split()
)
_SHORT = {
    "n": "namespace",
    "f": "values",
    "l": "labels",
    "o": "output",
    "d": "destination",
    "a": "api-versions",
    "s": "show-only",
    "g": "generate-name",
    "h": "help",
    "i": "install",
}
_COMMANDS = {"push", "pull", "install", "upgrade", "show", "template"}
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_PATH = re.compile(rf"{_COMPONENT}(?:/{_COMPONENT})*")


@dataclass
class Invocation:
    argv: list[str]
    command: str = ""
    operands: list[int] | None = None
    config: Path | None = None
    explicit_repo: bool = False
    help: bool = False

    @property
    def publishing(self) -> bool:
        return self.command == "push" and not self.help


def parse(arguments: list[str]) -> Invocation:
    result = Invocation([], operands=[])
    operands: list[int] = []
    index = 0
    positional_only = False
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--" and not positional_only:
            positional_only = True
            result.argv.append(argument)
            index += 1
            continue
        if argument.startswith("-") and not positional_only:
            option, equal, value = argument.partition("=")
            name = option.removeprefix("--")
            attached = False
            if not argument.startswith("--") and len(option) >= 2:
                name = _SHORT.get(option[1], option[1:])
                if len(option) > 2 and not equal:
                    value, attached = option[2:], True
            count = 1
            if name in _VALUES:
                if not equal and not attached:
                    if index + 1 == len(arguments):
                        output.fatal(f"Helm --{name} requires a value.")
                    value = arguments[index + 1]
                    count = 2
                if name == "registry-config":
                    result.config = Path(value).expanduser().absolute()
                else:
                    result.argv.extend(arguments[index : index + count])
                if name == "repo":
                    result.explicit_repo = True
            elif name in _FLAGS or name == "dry-run":
                if attached:
                    output.fatal("Use separate Helm short flags or long options.")
                result.argv.append(argument)
                if name == "help" and value != "false":
                    result.help = True
            elif equal and argument.startswith("--"):
                result.argv.append(argument)
            else:
                output.fatal(
                    f"Unsupported Helm option {option}; use --option=value for new value options."
                )
            index += count
            continue
        if not result.command:
            result.command = argument
            if argument not in _COMMANDS:
                # Commands without chart shorthand retain their native arguments.
                result.argv.extend(arguments[index:])
                return result
        else:
            operands.append(len(result.argv))
        result.argv.append(argument)
        index += 1
    result.operands = operands
    return result


def expand(invocation: Invocation, root: str) -> list[str]:
    argv = invocation.argv.copy()
    if invocation.help or invocation.command not in _COMMANDS:
        return argv
    operands = invocation.operands or []
    command = invocation.command
    if command == "show":
        if not operands or argv[operands[0]] not in {"all", "chart", "crds", "readme", "values"}:
            return argv
        operands = operands[1:]
    if command == "push":
        if len(operands) == 1:
            argv.append(f"oci://{root}")
            typer.echo(f"Helm push: oci://{root}", err=True)
            return argv
        if len(operands) != 2:
            output.fatal(
                "Helm push requires a chart archive and an optional destination directory."
            )
        targets = operands[-1:]
    elif command == "pull":
        targets = operands
    elif command in {"install", "upgrade", "template"}:
        # Helm validates release-name/generate-name combinations itself.
        if len(operands) not in {1, 2} or (command == "upgrade" and len(operands) != 2):
            output.fatal(f"Invalid Helm {command} chart arguments.")
        targets = operands[-1:]
    else:
        if len(operands) != 1:
            output.fatal("Helm show requires one chart reference.")
        targets = operands
    if not targets:
        output.fatal(f"Helm {command} requires a chart reference.")
    for index in targets:
        value = argv[index]
        if "://" in value:
            continue
        if command != "push" and (
            invocation.explicit_repo
            or value.startswith(("./", "../", "/", "~/"))
            or value in {".", ".."}
            or value.endswith(".tgz")
        ):
            continue
        name, at, digest = value.partition("@")
        if not _PATH.fullmatch(name) or (
            at and (command == "push" or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest))
        ):
            output.fatal(
                "Invalid private Helm path. Use --version for chart versions and oci:// for full references."
            )
        resolved = f"oci://{root}/{value}"
        if len(f"{root}/{name}") > 255:
            output.fatal("Resolved Helm chart path exceeds 255 characters.")
        argv[index] = resolved
        typer.echo(f"Helm {command}: {resolved}", err=True)
    return argv
