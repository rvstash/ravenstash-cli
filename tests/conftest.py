"""Shared test isolation."""

import pytest
from rvs import output
from rvs.account import commands as account_cmd


@pytest.fixture(autouse=True)
def _forget_verified_identities():
    """The CLI verifies an identity once per process; each test is its own process."""
    account_cmd._identities.clear()
    yield
    account_cmd._identities.clear()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    """Render at one fixed width so assertions do not depend on the runner.

    The consoles are created at import time and would otherwise size tables
    and wrap messages to the host terminal (narrower on Windows runners).
    """
    for console in (output.console, output.err_console):
        monkeypatch.setattr(console, "width", 200)
