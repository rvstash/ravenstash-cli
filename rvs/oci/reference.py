"""OCI path, tag and SHA-256 reference grammar supported by Ravenstash."""

import re

from .docker import _NAME, _TAG


def qualify_reference(root: str, operand: str | None = None) -> str:
    if operand is None:
        return root
    name_tag, at, digest = operand.partition("@")
    name, colon, tag = name_tag.partition(":")
    if (
        not _NAME.fullmatch(name)
        or (colon and not _TAG.fullmatch(tag))
        or (at and (colon or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)))
    ):
        raise ValueError("Use an OCI path, path:tag, or path@sha256:<64 lowercase hex digits>.")
    return f"{root}/{operand}"
