"""Wording shared by the commands that delete package content.

Deleting a package version or an OCI manifest moves it to the repository trash.
It stops being installable, stays restorable in the web app, and is permanently
deleted when the trash retention period ends. Until then the same version, file,
or manifest digest cannot be published again. OCI tag deletion is immediate and
is not trashed.
"""

NATIVE_MANIFEST_DELETE_NOTICE = (
    "Deleting a manifest moves it to the repository trash, with its referrers. "
    "Restore it in the web app before it is permanently deleted."
)
NATIVE_UNPUBLISH_NOTICE = (
    "Unpublishing moves the version to the repository trash. "
    "Restore it in the web app before it is permanently deleted."
)
WHOLE_PACKAGE_UNPUBLISH_HINT = (
    "If npm reported E405: unpublishing a whole package works only while it has one "
    "version left. Use `rvs art package delete-version NAME VERSION` for one version, "
    "or delete the package in the web app."
)


def moved_to_trash(subject: str, location: str) -> str:
    """Return the success line for a delete that moved `subject` to the trash."""
    return (
        f"Moved {subject} in {location} to trash. "
        "Restore it in the web app before it is permanently deleted."
    )
