"""Canonical public names for user and organization account resources."""


def typed_handle(account_type: object, handle: object) -> str:
    """Return the unambiguous public name for an account resource."""
    if account_type == "personal":
        prefix = "user"
    elif account_type == "organization":
        prefix = "org"
    elif isinstance(account_type, str) and account_type.strip():
        # Account types are an open value set; show a new type as-is.
        prefix = account_type.strip()
    else:
        raise ValueError("account type cannot be empty")

    if not isinstance(handle, str) or not handle.strip():
        raise ValueError("account handle cannot be empty")
    return f"{prefix}:{handle.strip()}"
