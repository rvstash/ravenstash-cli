from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from restore_public_apt import package_paths, public_fetcher, restore_public_repository


class Response(io.BytesIO):
    def geturl(self) -> str:
        return "https://releases.ravenstash.com/ravenstash/apt/channels.json"

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class RestorePublicAptTests(unittest.TestCase):
    def test_package_paths_reject_parent_traversal(self) -> None:
        packages = "Package: ravenstash-cli\nFilename: ../outside.deb\n"
        with self.assertRaises(SystemExit):
            package_paths(packages)

    def test_package_paths_accepts_an_empty_architecture_index(self) -> None:
        self.assertEqual(package_paths(""), set())

    @patch("restore_public_apt.time.sleep")
    @patch("restore_public_apt.urllib.request.urlopen")
    def test_public_fetch_retries_a_transient_failure(self, urlopen, sleep) -> None:
        urlopen.side_effect = [urllib.error.URLError("transient"), Response(b"manifest")]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "channels.json"
            public_fetcher("https://releases.ravenstash.com/ravenstash/apt")(
                "channels.json", destination
            )
            self.assertEqual(destination.read_bytes(), b"manifest")
        sleep.assert_called_once_with(1)

    def test_restore_fails_closed_when_nothing_is_published(self) -> None:
        requested: list[str] = []

        def fetch(path: str, _destination: Path) -> None:
            requested.append(path)
            raise SystemExit(f"error: cannot download public APT path {path}: HTTP Error 404")

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "repository"
            keyring = Path(directory) / "trusted.gpg"
            keyring.write_bytes(b"trusted key")
            with self.assertRaises(SystemExit):
                restore_public_repository(destination, keyring, fetch)

            # The signed manifest is the entry point; nothing else is consulted.
            self.assertEqual(requested, ["channels.json"])
            self.assertEqual(list(destination.iterdir()), [])

    @patch("restore_public_apt.verify")
    @patch("restore_public_apt.verify_signature")
    def test_restore_fetches_authenticated_distributions_and_packages(
        self,
        verify_signature,
        verify,
    ) -> None:
        amd64_package_path = "pool/main/r/ravenstash-cli/ravenstash-cli_0.15.3_amd64_test.deb"
        arm64_package_path = "pool/main/r/ravenstash-cli/ravenstash-cli_0.15.3_arm64_test.deb"
        amd64_packages = (
            f"Package: ravenstash-cli\nVersion: 0.15.3\nArchitecture: amd64\nFilename: {amd64_package_path}\n"
        ).encode()
        arm64_packages = (
            f"Package: ravenstash-cli\nVersion: 0.15.3\nArchitecture: arm64\nFilename: {arm64_package_path}\n"
        ).encode()
        amd64_digest = hashlib.sha256(amd64_packages).hexdigest()
        arm64_digest = hashlib.sha256(arm64_packages).hexdigest()
        release = (
            "SHA256:\n"
            f" {amd64_digest} {len(amd64_packages)} main/binary-amd64/Packages\n"
            f" {arm64_digest} {len(arm64_packages)} main/binary-arm64/Packages\n"
        ).encode()
        manifest = json.dumps(
            {
                "channels": {"v0": {}},
                "recommended": "v0",
                "schema": 1,
            }
        ).encode()
        files = {
            "channels.json": manifest,
            "channels.json.gpg": b"manifest signature",
            amd64_package_path: b"amd64 deb",
            arm64_package_path: b"arm64 deb",
        }
        for distribution in ("v0",):
            prefix = f"dists/{distribution}"
            files[f"{prefix}/InRelease"] = b"inrelease"
            files[f"{prefix}/Release"] = release
            files[f"{prefix}/Release.gpg"] = b"release signature"
            files[f"{prefix}/main/binary-amd64/Packages"] = amd64_packages
            files[f"{prefix}/main/binary-amd64/by-hash/SHA256/{amd64_digest}"] = amd64_packages
            files[f"{prefix}/main/binary-arm64/Packages"] = arm64_packages
            files[f"{prefix}/main/binary-arm64/by-hash/SHA256/{arm64_digest}"] = arm64_packages

        def authenticate(_keyring, signature, content=None):
            if signature.name == "channels.json.gpg":
                return manifest
            return release if content is None else content.read_bytes()

        verify_signature.side_effect = authenticate
        requested: list[str] = []

        def fetch(path: str, destination: Path) -> None:
            requested.append(path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(files[path])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keyring = root / "trusted.gpg"
            keyring.write_bytes(b"trusted key")
            destination = root / "repository"
            restore_public_repository(destination, keyring, fetch)

            self.assertEqual((destination / amd64_package_path).read_bytes(), b"amd64 deb")
            self.assertEqual((destination / arm64_package_path).read_bytes(), b"arm64 deb")
            self.assertEqual((destination / "ravenstash-cli.gpg").read_bytes(), b"trusted key")
            self.assertEqual(requested.count(amd64_package_path), 1)
            self.assertEqual(requested.count(arm64_package_path), 1)
            verify.assert_called_once_with(destination, keyring)

            requested.clear()
            verify.reset_mock()
            metadata = root / "metadata"
            restore_public_repository(metadata, keyring, fetch, include_packages=False)
            self.assertFalse((metadata / amd64_package_path).exists())
            self.assertFalse((metadata / arm64_package_path).exists())
            self.assertNotIn(amd64_package_path, requested)
            self.assertNotIn(arm64_package_path, requested)
            verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
