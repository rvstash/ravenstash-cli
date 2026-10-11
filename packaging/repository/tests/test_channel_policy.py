from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channel_policy import (
    channel_for_version,
    compatibility_channel,
    existing_recommended,
    filter_packages,
    manifest,
    normalize_channel,
    policy,
    validate_freshness,
    version_matches_channel,
)


UTC = dt.timezone.utc  # noqa: UP017


class ChannelPolicyTests(unittest.TestCase):
    def test_pre_one_releases_share_a_rolling_major_channel(self) -> None:
        self.assertEqual(channel_for_version("0.15.3"), "v0")
        self.assertEqual(channel_for_version("0.16.0~rc.1"), "v0")
        self.assertTrue(version_matches_channel("0.16.0", "v0"))

    def test_major_versions_are_channel_boundaries(self) -> None:
        self.assertEqual(channel_for_version("1.0.9"), "v1")
        self.assertEqual(channel_for_version("1.1.0"), "v1")
        self.assertEqual(channel_for_version("2.0.0"), "v2")

    def test_distribution_is_its_compatibility_channel(self) -> None:
        self.assertEqual(compatibility_channel("v0"), "v0")

    def test_filter_packages_excludes_other_channels(self) -> None:
        packages = (
            "Package: ravenstash-cli\nVersion: 0.15.3\nArchitecture: amd64\n\n"
            "Package: ravenstash-cli\nVersion: 0.16.0\nArchitecture: amd64\n"
        )
        selected = filter_packages(packages, "v0")
        self.assertIn("Version: 0.15.3", selected)
        self.assertIn("Version: 0.16.0", selected)

    def test_filter_packages_excludes_other_architectures(self) -> None:
        packages = (
            "Package: ravenstash-cli\nVersion: 0.15.3\nArchitecture: amd64\n\n"
            "Package: ravenstash-cli\nVersion: 0.15.3\nArchitecture: arm64\n"
        )
        selected = filter_packages(packages, "v0", "arm64")
        self.assertNotIn("Architecture: amd64", selected)
        self.assertIn("Architecture: arm64", selected)

    def test_invalid_channel_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            normalize_channel("stable")

    def test_manifest_maps_each_minor_to_its_latest_stable_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            packages = repository / "dists/v0/main/binary-amd64/Packages"
            packages.parent.mkdir(parents=True)
            packages.write_text(
                "Package: ravenstash-cli\nVersion: 0.15.3\n\n"
                "Package: ravenstash-cli\nVersion: 0.15.6\n\n"
                "Package: ravenstash-cli\nVersion: 0.16.1\n",
                encoding="utf-8",
            )

            payload = manifest(repository, "v0")

        self.assertEqual(payload["channels"]["v0"]["latest"], "0.16.1")
        self.assertEqual(
            payload["channels"]["v0"]["minor_targets"],
            {"0.15": "0.15.6", "0.16": "0.16.1"},
        )

    def test_first_publication_recommends_the_release_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            # A new repository has no manifest yet; a later one keeps its choice.
            self.assertEqual(existing_recommended(repository, "0"), "v0")
            (repository / "channels.json").write_text('{"recommended": "v1"}', encoding="utf-8")
            self.assertEqual(existing_recommended(repository, "v0"), "v1")

    def test_manifest_signs_a_bounded_validity_period(self) -> None:
        now = dt.datetime(2026, 9, 27, 5, 41, 12, 345, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            packages = repository / "dists/v0/main/binary-amd64/Packages"
            packages.parent.mkdir(parents=True)
            packages.write_text("Package: ravenstash-cli\nVersion: 0.15.3\n", encoding="utf-8")

            payload = manifest(repository, "v0", now=now)
            later = manifest(repository, "v0", now=now + dt.timedelta(days=1))

        self.assertEqual(payload["generated_at"], "2026-09-27T05:41:12Z")
        self.assertEqual(payload["expires"], "2026-10-04T05:41:12Z")
        self.assertEqual(policy(payload), policy(later))
        self.assertNotIn("generated_at", policy(payload))
        validate_freshness(payload, now=now + dt.timedelta(days=6))
        with self.assertRaisesRegex(ValueError, "expired"):
            validate_freshness(payload, now=now + dt.timedelta(days=7))

    def test_freshness_rejects_missing_partial_or_long_periods(self) -> None:
        now = dt.datetime(2026, 9, 27, tzinfo=UTC)
        for fields in (
            {"schema": 1},
            {"generated_at": "2026-09-27T00:00:00Z"},
            {"generated_at": "2026-09-27T00:00:00Z", "expires": "2026-10-27T00:00:00Z"},
            {"generated_at": "2026-09-28T00:00:00Z", "expires": "2026-09-30T00:00:00Z"},
            {"generated_at": "2026-09-27T00:00:00+00:00", "expires": "2026-09-30T00:00:00Z"},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_freshness(fields, now=now)


if __name__ == "__main__":
    unittest.main()
