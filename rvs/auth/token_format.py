"""Public credential encoding checks; only the server decides authority."""

import base64
import re


# Static native clients cannot reread rotated process environments, so each
# invocation issues one bounded four-hour token.
STATIC_NATIVE_DURATION_SECONDS = 4 * 60 * 60


# Personal-account PAT, organization-account PAT, and organization automation token.
# Each is bound to exactly one account; a sign-in session token is not.
ACCOUNT_TOKEN_MARKERS = ("rvs_ust", "rvs_uot", "rvs_oat")


def validate_public_token(value: str, *, native: bool = False) -> str:
    markers = "rvs_slt" if native else "|".join(ACCOUNT_TOKEN_MARKERS)
    match = re.fullmatch(rf"({markers})([A-Za-z0-9_-]{{43}})", value)
    if match is None:
        raise ValueError("Invalid public credential format")
    suffix = match[2]
    decoded = base64.b64decode(suffix + "=", altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != suffix:
        raise ValueError("Invalid public credential encoding")
    return value
