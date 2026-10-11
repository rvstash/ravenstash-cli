import io
import json
import sys

import pytest
from rvs.oci import credential_helper
from rvs.oci.credential_helper import BROKER_INVALID, CREDENTIALS_NOT_FOUND
from rvs.oci.registry import normalized_registry_host


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("https://OCI.example./v1/", "oci.example"),
        ("oci.example:5000", "oci.example:5000"),
        ("https://[::1]:5000/", "[::1]:5000"),
        ("[::1]", "[::1]"),
        ("", ""),
        ("https:///", ""),
    ],
)
def test_registry_authority_preserves_host_and_port(address, expected):
    assert normalized_registry_host(address) == expected


@pytest.mark.parametrize(
    ("server", "requested", "refusal"),
    [
        ("oci.example", "https://OCI.example./v1/", None),
        ("[::1]:5000", "http://[::1]:5000", None),
        ("[::1]:5000", "http://[::1]:5001", CREDENTIALS_NOT_FOUND),
        ("oci.example", "another.example", CREDENTIALS_NOT_FOUND),
        ("https:///", "https:///", BROKER_INVALID),
    ],
)
def test_credential_helper_checks_canonical_authority(
    monkeypatch, tmp_path, capsys, server, requested, refusal
):
    broker = tmp_path / "broker.json"
    broker.write_text(
        json.dumps({"server": server, "username": "__token__", "secret": "test-secret"})
    )
    broker.chmod(0o600)
    monkeypatch.setenv("RVS_OCI_CREDENTIAL_FILE", str(broker))
    monkeypatch.setattr(sys, "argv", ["docker-credential-rvs", "get"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(requested))
    if refusal is None:
        credential_helper.main()
        assert json.loads(capsys.readouterr().out)["Secret"] == "test-secret"
    else:
        with pytest.raises(SystemExit, match="1"):
            credential_helper.main()
        # Docker reads the refusal from stdout; the secret never appears.
        assert capsys.readouterr() == (f"{refusal}\n", "")
