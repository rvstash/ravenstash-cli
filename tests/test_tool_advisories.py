from rvs import tool_advisories


def test_old_client_warns_but_is_not_rejected(monkeypatch, capsys) -> None:
    tool_advisories._advisory_message.cache_clear()
    monkeypatch.setattr(tool_advisories, "_read_version", lambda command: "9.0.0")

    assert tool_advisories.warn_if_old("npm", ["/usr/bin/npm"]) is None

    captured = capsys.readouterr()
    assert "Detected npm 9.0.0" in captured.err
    assert "The command will" in captured.err
    assert "continue for compatibility" in captured.err


def test_client_at_advisory_floor_does_not_warn(monkeypatch, capsys) -> None:
    tool_advisories._advisory_message.cache_clear()
    monkeypatch.setattr(
        tool_advisories,
        "_read_version",
        lambda command: "Apache Maven 3.9.0",
    )

    tool_advisories.warn_if_old("mvn", ["mvn"])

    assert capsys.readouterr().err == ""


def test_unrecognized_or_unavailable_version_never_blocks(monkeypatch, capsys) -> None:
    tool_advisories._advisory_message.cache_clear()
    monkeypatch.setattr(
        tool_advisories,
        "_read_version",
        lambda command: "vendor development build",
    )
    tool_advisories.warn_if_old("docker", ["docker"])
    assert capsys.readouterr().err == ""

    tool_advisories._advisory_message.cache_clear()

    monkeypatch.setattr(tool_advisories, "_read_version", lambda command: None)
    tool_advisories.warn_if_old("docker", ["docker"])
    assert capsys.readouterr().err == ""
