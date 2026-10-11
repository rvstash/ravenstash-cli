from rvs.oci.runner import _operations_for


def test_oras_delete_requests_delete_without_publish_authority():
    assert _operations_for("oras", ["manifest", "delete", "images/app@sha256:abc"]) == (
        "read",
        "delete",
    )
    assert _operations_for("oras", ["push", "images/app:latest", "file.txt"]) == (
        "read",
        "publish",
    )
