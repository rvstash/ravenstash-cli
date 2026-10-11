"""Minimal verification for Ravenstash's pinned OpenPGP release key.

The release key is an RSA v4 OpenPGP key and release metadata uses detached
binary-document signatures.  Keeping this verifier deliberately narrow gives
portable and Windows builds the same trust root without requiring a system
``gpg`` installation.  Unsupported packet, key, signature, or hash algorithms
fail closed.
"""

import base64
import hashlib
import hmac
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING

from .identity import SIGNING_KEY_NAME


if TYPE_CHECKING:
    from collections.abc import Iterator


SIGNING_KEY_FINGERPRINT = "3B7C20FC370D1A7C813DF3A2E9679F951AD8BAA0"


class VerificationError(ValueError):
    """Authenticated release material is malformed or has an invalid signature."""


@dataclass(frozen=True)
class _RSAKey:
    exponent: int
    modulus: int

    @property
    def key_size(self) -> int:
        return self.modulus.bit_length()


def _dearmor(value: bytes) -> bytes:
    if not value.startswith(b"-----BEGIN PGP "):
        return value
    try:
        lines = value.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise VerificationError("OpenPGP armor is invalid") from exc
    payload: list[str] = []
    in_payload = False
    for line in lines[1:]:
        if line.startswith("-----END PGP "):
            break
        if not in_payload:
            if not line:
                in_payload = True
            continue
        if line.startswith("="):
            continue
        if line:
            payload.append(line)
    if not payload:
        raise VerificationError("OpenPGP armor contains no payload")
    try:
        return base64.b64decode("".join(payload), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise VerificationError("OpenPGP armor is invalid") from exc


def _packets(value: bytes) -> Iterator[tuple[int, bytes]]:
    offset = 0
    while offset < len(value):
        header = value[offset]
        offset += 1
        if not header & 0x80:
            raise VerificationError("invalid OpenPGP packet header")
        if header & 0x40:
            tag = header & 0x3F
            if offset >= len(value):
                raise VerificationError("truncated OpenPGP packet")
            first = value[offset]
            offset += 1
            if first < 192:
                length = first
            elif first < 224:
                if offset >= len(value):
                    raise VerificationError("truncated OpenPGP packet length")
                length = ((first - 192) << 8) + value[offset] + 192
                offset += 1
            elif first == 255:
                if offset + 4 > len(value):
                    raise VerificationError("truncated OpenPGP packet length")
                length = int.from_bytes(value[offset : offset + 4])
                offset += 4
            else:
                raise VerificationError("partial OpenPGP packet lengths are unsupported")
        else:
            tag = (header >> 2) & 0x0F
            length_type = header & 0x03
            size = (1, 2, 4, 0)[length_type]
            if size == 0 or offset + size > len(value):
                raise VerificationError("indeterminate or truncated OpenPGP packet")
            length = int.from_bytes(value[offset : offset + size])
            offset += size
        end = offset + length
        if end > len(value):
            raise VerificationError("truncated OpenPGP packet body")
        yield tag, value[offset:end]
        offset = end


def _mpi(value: bytes, offset: int) -> tuple[int, int]:
    if offset + 2 > len(value):
        raise VerificationError("truncated OpenPGP MPI")
    bits = int.from_bytes(value[offset : offset + 2])
    size = (bits + 7) // 8
    start = offset + 2
    end = start + size
    if end > len(value):
        raise VerificationError("truncated OpenPGP MPI")
    integer = int.from_bytes(value[start:end])
    if integer.bit_length() != bits:
        raise VerificationError("non-canonical OpenPGP MPI")
    return integer, end


def _release_key() -> _RSAKey:
    armored = files("rvs.resources").joinpath(SIGNING_KEY_NAME).read_bytes()
    packets = list(_packets(_dearmor(armored)))
    try:
        body = next(body for tag, body in packets if tag == 6)
    except StopIteration as exc:
        raise VerificationError("release key has no public-key packet") from exc
    if len(body) < 6 or body[0] != 4 or body[5] not in {1, 3}:
        raise VerificationError("release key is not a supported RSA v4 key")
    fingerprint = (
        hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body, usedforsecurity=False)
        .hexdigest()
        .upper()
    )
    if fingerprint != SIGNING_KEY_FINGERPRINT:
        raise VerificationError("release key fingerprint does not match the pinned identity")
    modulus, offset = _mpi(body, 6)
    exponent, _offset = _mpi(body, offset)
    return _RSAKey(exponent, modulus)


def verify_detached(subject: bytes, detached_signature: bytes) -> None:
    """Verify one detached binary-document signature from the release key."""

    packets = list(_packets(_dearmor(detached_signature)))
    if len(packets) != 1 or packets[0][0] != 2:
        raise VerificationError("expected one OpenPGP signature packet")
    body = packets[0][1]
    if len(body) < 12 or body[0] != 4 or body[1] != 0 or body[2] not in {1, 3}:
        raise VerificationError("unsupported OpenPGP release signature")
    hash_algorithm = {
        8: (hashlib.sha256, bytes.fromhex("3031300d060960864801650304020105000420")),
        9: (hashlib.sha384, bytes.fromhex("3041300d060960864801650304020205000430")),
        10: (hashlib.sha512, bytes.fromhex("3051300d060960864801650304020305000440")),
    }.get(body[3])
    if hash_algorithm is None:
        raise VerificationError("unsupported OpenPGP signature hash")
    hashed_length = int.from_bytes(body[4:6])
    hashed_end = 6 + hashed_length
    if hashed_end + 4 > len(body):
        raise VerificationError("truncated OpenPGP signature metadata")
    unhashed_length = int.from_bytes(body[hashed_end : hashed_end + 2])
    digest_prefix_offset = hashed_end + 2 + unhashed_length
    signature_offset = digest_prefix_offset + 2
    if signature_offset > len(body):
        raise VerificationError("truncated OpenPGP signature")
    signed_header = body[:hashed_end]
    trailer = b"\x04\xff" + len(signed_header).to_bytes(4, "big")
    hash_constructor, digest_info_prefix = hash_algorithm
    digest = hash_constructor(subject + signed_header + trailer).digest()
    if digest[:2] != body[digest_prefix_offset:signature_offset]:
        raise VerificationError("OpenPGP signature digest prefix does not match")
    signature_integer, end = _mpi(body, signature_offset)
    if end != len(body):
        raise VerificationError("unexpected data after OpenPGP signature")
    key = _release_key()
    size = (key.key_size + 7) // 8
    if signature_integer >= key.modulus:
        raise VerificationError("OpenPGP release signature is invalid")
    encoded = pow(signature_integer, key.exponent, key.modulus).to_bytes(size, "big")
    digest_info = digest_info_prefix + digest
    padding_size = size - len(digest_info) - 3
    if padding_size < 8:
        raise VerificationError("OpenPGP release key is too small for its signature hash")
    expected = b"\x00\x01" + (b"\xff" * padding_size) + b"\x00" + digest_info
    if not hmac.compare_digest(encoded, expected):
        raise VerificationError("OpenPGP release signature is invalid")
