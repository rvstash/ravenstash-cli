from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from refresh_handoff import apply_handoff, create_handoff


def repository(root: Path, marker: str) -> None:
    (root / "dists/v0/main/binary-amd64").mkdir(parents=True, exist_ok=True)
    (root / "pool/main/r/ravenstash-cli").mkdir(parents=True, exist_ok=True)
    (root / "channels.json").write_text(f"channels {marker}")
    (root / "channels.json.gpg").write_text(f"manifest signature {marker}")
    (root / "ravenstash-cli.gpg").write_text("public key")
    (root / "pool/main/r/ravenstash-cli/ravenstash-cli.deb").write_text("package")
    (root / "dists/v0/main/binary-amd64/Packages").write_text("index")
    for name in ("InRelease", "Release", "Release.gpg"):
        (root / "dists/v0" / name).write_text(f"{name} {marker}")


class RefreshHandoffTests(unittest.TestCase):
    def test_round_trip_changes_only_signed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = root / "prior"
            signed = root / "signed"
            restored = root / "restored"
            repository(prior, "old")
            shutil.copytree(prior, restored)
            shutil.copytree(prior, signed)
            repository(signed, "new")
            (signed / "apt-ftparchive-release-v0.conf").write_text("temporary")

            bundle = root / "handoff.tar.gz"
            create_handoff(prior, signed, bundle)
            apply_handoff(restored, bundle)

            self.assertEqual((restored / "channels.json").read_text(), "channels new")
            self.assertEqual(
                (restored / "dists/v0/InRelease").read_text(),
                "InRelease new",
            )
            self.assertEqual(
                (restored / "pool/main/r/ravenstash-cli/ravenstash-cli.deb").read_text(), "package"
            )

    def test_create_rejects_immutable_package_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = root / "prior"
            signed = root / "signed"
            repository(prior, "old")
            shutil.copytree(prior, signed)
            (signed / "pool/main/r/ravenstash-cli/ravenstash-cli.deb").write_text("replacement")

            with self.assertRaises(SystemExit):
                create_handoff(prior, signed, root / "handoff.tar.gz")


if __name__ == "__main__":
    unittest.main()
