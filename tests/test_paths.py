from rvs import paths


def test_rvs_home_defaults_to_home_rvs(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("RVS_HOME", raising=False)
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path)

    assert paths.rvs_home() == tmp_path / ".rvs"


def test_rvs_home_uses_environment_override(monkeypatch, tmp_path) -> None:
    configured = tmp_path / "custom-rvs"
    monkeypatch.setenv("RVS_HOME", str(configured))

    assert paths.rvs_home() == configured
    assert paths.rvs_path("runtimes", "python") == configured / "runtimes" / "python"
