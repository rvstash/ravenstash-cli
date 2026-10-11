"""Persist a retry correlation ID, never a refresh secret, under the profile lock."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4


def pending_refresh_operation(directory: Path, profile: str, token: str) -> tuple[str, Path]:
    """Keep the ID after an ambiguous response so the next invocation can recover."""
    profile_digest = hashlib.sha256(profile.encode()).hexdigest()[:24]
    token_digest = hashlib.sha256(token.encode()).hexdigest()
    path = directory / f".refresh-{profile_digest}.pending.json"
    try:
        value = json.loads(path.read_text())
        if isinstance(value, dict) and value.get("refresh_sha256") == token_digest:
            return str(UUID(value["operation_id"])), path
    except OSError, ValueError, KeyError, TypeError, AttributeError:
        pass
    operation_id = str(uuid4())
    descriptor, temporary_name = tempfile.mkstemp(prefix=".refresh-operation-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump({"operation_id": operation_id, "refresh_sha256": token_digest}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return operation_id, path
