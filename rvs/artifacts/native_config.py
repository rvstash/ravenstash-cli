"""Tool-specific native setup recipes; rendering has no side effects."""

import shlex
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from xml.sax.saxutils import escape


if TYPE_CHECKING:
    from .discovery import Discovery
from .routing import npm_auth_token_key


TOOLS = {
    "pip": "pypi",
    "uv": "pypi",
    "twine": "pypi",
    "npm": "npm",
    "mvn": "maven",
    "docker": "oci",
    "helm": "oci",
}


def render(found: Discovery, tool: str, kind: str | None, access: str | None) -> dict:
    tool = "mvn" if tool == "maven" else tool
    if tool not in {*TOOLS, "oras"}:
        raise ValueError(
            f"Unknown native tool '{tool}'. Choose pip, uv, twine, npm, mvn, docker, helm, or oras."
        )
    if tool in {"pip", "twine"} and access is not None:
        raise ValueError(
            f"{tool} has fixed {'read' if tool == 'pip' else 'publish'} intent; --access is not supported."
        )
    mirror = found.target.target_type != "repository"
    effective = (
        "read"
        if tool == "pip"
        else "publish"
        if tool == "twine"
        else access or ("read" if mirror else "publish")
    )
    if effective not in {"read", "publish"}:
        raise ValueError("Native config access must be read or publish.")
    if mirror and effective != "read":
        raise ValueError("Private mirrors are read-only; publication is not available.")
    if tool == "oras":
        formats = (
            [found.select_format(kind, ("oci",))]
            if kind
            else [item for item in found.formats if item in {"oci"}]
        )
        if not formats:
            raise ValueError("ORAS requires an enabled OCI format.")
    else:
        expected = TOOLS[tool]
        if kind is not None and kind != expected:
            raise ValueError(f"{tool} requires --format {expected}.")
        formats = [found.select_format(expected)]
    snippets: list[dict[str, str]] = []

    def add(category: str, text: str, filename: str | None = None) -> None:
        item = {"kind": category, "text": text}
        if filename:
            item["filename"] = filename
        else:
            item["shell"] = "sh"
        snippets.append(item)

    flags = shlex.join(
        [
            "--target",
            found.target.stable_selector,
            "--account",
            found.target.account_ref,
            "--profile",
            found.profile,
        ]
    )
    mint = f"rvs art token mint {flags} --format {','.join(formats)} --access {effective}"
    add(
        "environment",
        f"RVS_NATIVE_TOKEN=$({mint})\nexport RVS_NATIVE_TOKEN\n# Expires after at most four hours; mint again when needed. Do not commit secrets.",
    )
    first = formats[0]
    read = found.endpoint(first)
    push = found.endpoint(first, "publish") if effective == "publish" else None
    add("command", f"# Discover this address again:\nrvs art endpoint {flags} --format {first}")
    if tool == "pip":
        host = urlsplit(read).hostname
        add("config", f"[global]\nindex-url = {read}\n", "pip.conf")
        add(
            "environment",
            f"""# pip reads NETRC; this creates a private temporary credential file.
RVS_NETRC=$(mktemp)
chmod 600 "$RVS_NETRC"
printf 'machine {host} login rvs password %s\\n' "$RVS_NATIVE_TOKEN" > "$RVS_NETRC"
export NETRC="$RVS_NETRC"
export PIP_INDEX_URL={shlex.quote(read)}""",
        )
        add(
            "command",
            'pip install PACKAGE\n# Remove the temporary credential file after use:\nrm -- "$RVS_NETRC"\nunset NETRC RVS_NATIVE_TOKEN',
        )
    elif tool == "uv":
        config = f'[[tool.uv.index]]\nname = "ravenstash"\nurl = "{read}"\ndefault = true\nauthenticate = "always"\n'
        if push:
            config += f'publish-url = "{push}"\n'
        add("config", config, "pyproject.toml")
        add(
            "environment",
            'export UV_INDEX_RAVENSTASH_USERNAME=rvs\nexport UV_INDEX_RAVENSTASH_PASSWORD="$RVS_NATIVE_TOKEN"',
        )
        add(
            "command",
            "uv sync\nuv pip install PACKAGE"
            + ("\nuv build\nuv publish --index ravenstash" if push else ""),
        )
    elif tool == "twine":
        add(
            "environment",
            f'export TWINE_USERNAME=rvs\nexport TWINE_PASSWORD="$RVS_NATIVE_TOKEN"\nexport TWINE_REPOSITORY_URL={shlex.quote(str(push))}\nexport TWINE_NON_INTERACTIVE=1',
        )
        add("command", "twine upload dist/*")
    elif tool == "npm":
        config = f"registry={read}\n{npm_auth_token_key(read)}=${{RVS_NATIVE_TOKEN}}\n"
        if push and push != read:
            config += f"{npm_auth_token_key(push)}=${{RVS_NATIVE_TOKEN}}\n"
        add("config", config, ".npmrc")
        add(
            "command",
            "npm install PACKAGE"
            + (f"\nnpm publish --registry {shlex.quote(push)}" if push else ""),
        )
    elif tool == "mvn":
        servers = "<server><id>ravenstash-read</id><username>rvs</username><password>${env.RVS_NATIVE_TOKEN}</password></server>"
        if push:
            servers += "\n    <server><id>ravenstash-publish</id><username>rvs</username><password>${env.RVS_NATIVE_TOKEN}</password></server>"
        add(
            "config",
            f"""<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">
  <servers>{servers}</servers>
  <mirrors><mirror><id>ravenstash-read</id><mirrorOf>*</mirrorOf><url>{escape(read)}</url></mirror></mirrors>
  <profiles><profile><id>ravenstash</id><repositories><repository>
    <id>ravenstash-read</id><url>{escape(read)}</url>
  </repository></repositories></profile></profiles>
  <activeProfiles><activeProfile>ravenstash</activeProfile></activeProfiles>
</settings>""",
            "settings.xml",
        )
        add(
            "command",
            "mvn --settings settings.xml dependency:resolve"
            + (
                f"\nmvn --settings settings.xml deploy:deploy-file -DrepositoryId=ravenstash-publish -Durl={shlex.quote(push)} -Dfile=artifact.jar -DgroupId=com.example -DartifactId=example -Dversion=1.0.0"
                if push
                else ""
            ),
        )
    else:
        # Explicit per-tool files isolate these examples from a user's shared
        # Docker credential helpers and from logouts in another native client.
        add(
            "environment",
            """# Login below writes credentials to this temporary directory.
RVS_OCI_CONFIG=$(mktemp -d)
chmod 700 "$RVS_OCI_CONFIG"
# Default native login stores may be shared: logout can affect other clients.
# These explicit files isolate this setup. One host entry covers one repository token.""",
        )
        if tool == "docker":
            command = 'docker --config "$RVS_OCI_CONFIG"'
        elif tool == "helm":
            command = 'helm --registry-config "$RVS_OCI_CONFIG/helm.json"'
        else:
            command = "oras"
        login = (
            "registry login"
            if tool == "helm"
            else 'login --registry-config "$RVS_OCI_CONFIG/oras.json"'
            if tool == "oras"
            else "login"
        )
        transport = " --plain-http" if found.oci_plain_http and tool != "docker" else ""
        if found.oci_plain_http and tool == "docker":
            add(
                "command",
                f"# This profile uses HTTP. Docker's daemon must allow {read} as an insecure registry.",
            )
        add(
            "command",
            f'printf %s "$RVS_NATIVE_TOKEN" | {command} {login} {shlex.quote(read)} --username rvs --password-stdin{transport}',
        )
        for fmt in formats:
            root = found.native_reference(fmt)
            add("command", f"# {fmt.title()} reference:\nrvs art reference {flags} --format {fmt}")
            if tool == "docker":
                ref = found.native_reference(fmt, "backend:latest")
                text = f"{command} pull {ref}"
                if push:
                    text += f"\n{command} tag LOCAL_IMAGE {ref}\n{command} push {ref}"
            elif tool == "helm":
                text = f"{command} pull oci://{root}/charts/api --version 1.2.0{transport}"
                if push:
                    text += f"\n{command} push api-1.2.0.tgz oci://{root}/charts{transport}"
            else:
                for label, path in (
                    ("Container OCI", "images/api:latest"),
                    ("Helm OCI", "charts/api:1.2.0"),
                ):
                    ref = found.native_reference(fmt, path)
                    text = f'# {label}: copy an existing supported graph.\n{command} manifest fetch --registry-config "$RVS_OCI_CONFIG/oras.json" {ref}{transport}'
                    if push:
                        text += f'\n{command} cp --from-registry-config "$RVS_OCI_CONFIG/oras.json" --to-registry-config "$RVS_OCI_CONFIG/oras.json" SOURCE_REGISTRY/{path} {ref}'
                        if found.oci_plain_http:
                            text += " --to-plain-http"
                    add("command", text)
                continue
            add("command", text)
        add(
            "command",
            '# After finishing all commands, delete the temporary login files:\nrm -r -- "$RVS_OCI_CONFIG"\nunset RVS_NATIVE_TOKEN',
        )
    return {
        "tool": tool,
        "target": found.target.display_selector,
        "formats": formats,
        "access": effective,
        "snippets": snippets,
    }
