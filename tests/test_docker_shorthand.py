import pytest
from rvs.oci.docker import expand, parse


ROOT = "registry.example:5000/in/ar_xyzabcde"


@pytest.mark.parametrize("value", ["team/api:RC1", "api:1.0", "team/backend/worker:dev"])
def test_nested_push(value):
    invocation = parse(["push", value])
    assert expand(invocation, ROOT) == (value, f"{ROOT}/{value}")
    assert invocation.argv == ["push", f"{ROOT}/{value}"]


@pytest.mark.parametrize(
    "value",
    [
        "docker.io/library/nginx:latest",
        "localhost:5000/api:1",
        "localhost/api",
        "team.v2/api:1",
        "Registry/api:1",
        "[::1]:5000/api:1",
    ],
)
def test_explicit_registry_is_never_rewritten(value):
    invocation = parse(["pull", value])
    assert expand(invocation, ROOT) is None
    assert invocation.argv == ["pull", value]


@pytest.mark.parametrize(
    "args",
    [
        ["push", "--all-tags", "api"],
        ["push", "api@sha256:" + "a" * 64],
        ["push", "sha256:" + "a" * 64],
        ["push", "a" * 64],
        ["pull", "team//api"],
        ["pull", "team/../api"],
        ["pull", "api:"],
        ["pull", "Api:1"],
        ["pull", "api@sha256:bad"],
        ["pull", "--all-tags", "api:1"],
        ["tag", "local", "api@sha256:" + "a" * 64],
    ],
)
def test_invalid_shorthand_fails(args):
    with pytest.raises(SystemExit):
        expand(parse(args), ROOT)


def test_digest_and_all_tags_pull():
    value = "team/api@sha256:" + "a" * 64
    invocation = parse(["image", "pull", "--platform", "linux/arm64", value])
    assert expand(invocation, ROOT) is None
    assert invocation.argv[-1] == f"{ROOT}/{value}"
    invocation = parse(["pull", "-a", "team/api"])
    expand(invocation, ROOT)
    assert invocation.argv[-1] == f"{ROOT}/team/api"


@pytest.mark.parametrize(
    "args",
    [
        ["push"],
        ["tag", "api"],
        ["pull", "one", "two"],
        ["pull", "--unknown", "api"],
        ["--context"],
        ["pull", "--platform"],
    ],
)
def test_invalid_operands_and_options_fail(args):
    with pytest.raises(SystemExit):
        parse(args)


@pytest.mark.parametrize(
    "args",
    [
        ["run", "push"],
        ["build", "--build-arg", "push", "."],
        ["buildx", "build", "--push=false", "."],
        ["--context", "push", "pull", "api"],
    ],
)
def test_argument_named_push_is_not_publish(args):
    assert not parse(args).publishing


def test_explicit_bulk_push_preserved():
    args = ["push", "--all-tags", f"{ROOT}/api"]
    invocation = parse(args)
    assert expand(invocation, ROOT) is None
    assert invocation.argv == args


def test_build_and_local_commands_are_not_expanded():
    for args in [
        ["build", "-t", "api:1", "."],
        ["run", "api:1"],
        ["buildx", "build", "--push", "-t", "api:1", "."],
    ]:
        invocation = parse(args)
        assert expand(invocation, ROOT) is None
        assert invocation.argv == args
