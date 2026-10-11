"""A synthetic release key and signed inventory for the update tests.

Nothing here is, or is derived from, real release material. The key is an
insecure but reproducible RSA key built from two Mersenne primes, so its private
half is public knowledge and no secret is committed. The inventory names a
version that is never released and lists made-up digests. PKCS #1 v1.5
signatures are deterministic, so every byte is the same on every run and every
platform, and no signing tool is needed.

The packets have the shape GnuPG writes: a v4 RSA public key, and a v4
binary-document signature whose hashed area carries the creation time and the
issuer fingerprint and whose unhashed area carries the issuer key ID.
"""

import base64
import hashlib
import textwrap
from typing import TYPE_CHECKING

from rvs import identity, update_trust


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


SIGNATURE_TAG = 2
PUBLIC_KEY_TAG = 6
RSA = 1
SHA256 = 8
SHA384 = 9
SHA512 = 10
DIGESTS = {SHA256: hashlib.sha256, SHA384: hashlib.sha384, SHA512: hashlib.sha512}
# DER prefixes of the PKCS #1 v1.5 DigestInfo for each hash (RFC 8017, section 9.2).
_DIGEST_INFO = {
    SHA256: bytes.fromhex("3031300d060960864801650304020105000420"),
    SHA384: bytes.fromhex("3041300d060960864801650304020205000430"),
    SHA512: bytes.fromhex("3051300d060960864801650304020305000440"),
}

# The Mersenne primes M607 and M521, and a second pair for an unrelated key.
TOY_P = 2**607 - 1
TOY_Q = 2**521 - 1
OTHER_P = 2**1279 - 1
OTHER_Q = 2**127 - 1
TOY_EXPONENT = 65537
TOY_PUBLIC_KEY = update_trust._RSAKey(TOY_EXPONENT, TOY_P * TOY_Q)
# 2026-01-01T00:00:00Z, fixed so that key and signature bytes never change.
_CREATED = (1767225600).to_bytes(4, "big")

# A version that is never released, in the rolling v0 channel.
SYNTHETIC_VERSION = "0.999.0"
RELEASE_TARGETS = (
    "linux-amd64",
    "linux-arm64",
    "linux-musl-amd64",
    "linux-musl-arm64",
    "macos-amd64",
    "macos-arm64",
    "windows-amd64",
    "windows-arm64",
)


def mpi(value: int) -> bytes:
    bits = value.bit_length()
    return bits.to_bytes(2, "big") + value.to_bytes((bits + 7) // 8, "big")


def old_format_packet(tag: int, body: bytes) -> bytes:
    """Frame a packet with the old-format two-octet length GnuPG uses."""
    return bytes([0x80 | tag << 2 | 0x01]) + len(body).to_bytes(2, "big") + body


def armor(
    binary: bytes, *, block: str, headers: tuple[str, ...] = (), newline: str = "\n"
) -> bytes:
    lines = [
        f"-----BEGIN PGP {block}-----",
        *headers,
        "",
        *textwrap.wrap(base64.b64encode(binary).decode("ascii"), 64),
        f"-----END PGP {block}-----",
        "",
    ]
    return newline.join(lines).encode("ascii")


def rsa_sign(message: bytes, hash_algorithm: int, *, p: int = TOY_P, q: int = TOY_Q) -> int:
    """Return the PKCS #1 v1.5 signature of ``message`` under the key ``p * q``."""
    modulus = p * q
    size = (modulus.bit_length() + 7) // 8
    digest_info = _DIGEST_INFO[hash_algorithm] + DIGESTS[hash_algorithm](message).digest()
    padding = b"\xff" * (size - len(digest_info) - 3)
    encoded = int.from_bytes(b"\x00\x01" + padding + b"\x00" + digest_info)
    return pow(encoded, pow(TOY_EXPONENT, -1, (p - 1) * (q - 1)), modulus)


def public_key_body(modulus: int = TOY_P * TOY_Q) -> bytes:
    return bytes([4]) + _CREATED + bytes([RSA]) + mpi(modulus) + mpi(TOY_EXPONENT)


def fingerprint(key_body: bytes) -> str:
    """The v4 fingerprint of a public-key packet body (RFC 4880, section 12.2)."""
    framed = b"\x99" + len(key_body).to_bytes(2, "big") + key_body
    return hashlib.sha1(framed, usedforsecurity=False).hexdigest().upper()


TEST_KEY_BODY = public_key_body()
TEST_KEY_FINGERPRINT = fingerprint(TEST_KEY_BODY)
TEST_KEY_ARMOR = armor(old_format_packet(PUBLIC_KEY_TAG, TEST_KEY_BODY), block="PUBLIC KEY BLOCK")


def signature_header(hash_algorithm: int = SHA256, *, issuer: str = TEST_KEY_FINGERPRINT) -> bytes:
    """The hashed part of a v4 binary-document signature, as GnuPG writes it."""
    issuer_fingerprint = bytes([4]) + bytes.fromhex(issuer)
    hashed = (
        bytes([len(issuer_fingerprint) + 1, 33]) + issuer_fingerprint + bytes([5, 2]) + _CREATED
    )
    return bytes([4, 0x00, RSA, hash_algorithm]) + len(hashed).to_bytes(2, "big") + hashed


def signed_message(subject: bytes, header: bytes) -> bytes:
    """The exact byte stream an OpenPGP v4 signature hashes (RFC 4880, section 5.2.4)."""
    return subject + header + b"\x04\xff" + len(header).to_bytes(4, "big")


def detached_signature(
    subject: bytes, *, hash_algorithm: int = SHA256, p: int = TOY_P, q: int = TOY_Q
) -> bytes:
    """Sign ``subject`` and return the binary detached-signature packet."""
    header = signature_header(hash_algorithm, issuer=fingerprint(public_key_body(p * q)))
    message = signed_message(subject, header)
    issuer_key_id = bytes([9, 16]) + bytes.fromhex(fingerprint(public_key_body(p * q)))[-8:]
    body = (
        header
        + len(issuer_key_id).to_bytes(2, "big")
        + issuer_key_id
        + DIGESTS[hash_algorithm](message).digest()[:2]
        + mpi(rsa_sign(message, hash_algorithm, p=p, q=q))
    )
    return old_format_packet(SIGNATURE_TAG, body)


def release_assets(version: str = SYNTHETIC_VERSION) -> list[str]:
    """Every asset a release inventory lists, under its published name."""
    stem = identity.ASSET_STEM
    return [
        f"{stem}-install.ps1",
        f"{stem}-install.sh",
        f"{stem}-source-identity.txt",
        *(identity.archive_name(version, target) for target in RELEASE_TARGETS),
        f"{stem}-v{version}-package-manifests.tar.gz",
        f"{stem}-v{version}-sbom.cdx.json",
        identity.debian_package_name(version, "amd64"),
        identity.debian_package_name(version, "arm64"),
    ]


def asset_content(name: str) -> bytes:
    """The made-up bytes of a synthetic release asset."""
    return f"synthetic release asset: {name}\n".encode()


def inventory(version: str = SYNTHETIC_VERSION) -> bytes:
    """A checksum inventory in the ``sha256sum`` format the release workflow signs."""
    lines = [
        f"{hashlib.sha256(asset_content(name)).hexdigest()}  {name}\n"
        for name in sorted(release_assets(version))
    ]
    return "".join(lines).encode("ascii")


def trust_test_key(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    """Make the synthetic key the pinned release key for one test.

    The real loader still runs: it reads the key file, parses its packet, and
    checks the fingerprint pin before any signature is accepted.
    """
    (directory / identity.SIGNING_KEY_NAME).write_bytes(TEST_KEY_ARMOR)
    monkeypatch.setattr(update_trust, "files", lambda _package: directory)
    monkeypatch.setattr(update_trust, "SIGNING_KEY_FINGERPRINT", TEST_KEY_FINGERPRINT)
