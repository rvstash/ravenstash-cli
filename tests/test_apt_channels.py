import pytest
from rvs.apt_channels import (
    channel_for_version,
    channel_order,
    minor_target_for_version,
    normalize_channel,
    normalize_minor_target,
    version_matches_channel,
)


@pytest.mark.parametrize(
    ("version", "channel"),
    (
        ("0.15.3", "v0"),
        ("0.15.4~rc.1", "v0"),
        ("0.16.0", "v0"),
        ("1.0.0", "v1"),
        ("1.12.3", "v1"),
        ("2.0.0", "v2"),
    ),
)
def test_channel_for_version(version: str, channel: str) -> None:
    assert channel_for_version(version) == channel
    assert version_matches_channel(version, channel)


@pytest.mark.parametrize("value", ("stable", "v0.15", "v0.15.3", "v01", "latest"))
def test_invalid_channels_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_channel(value)


def test_channel_order_crosses_pre_one_and_stable_boundaries() -> None:
    assert channel_order("v0") < channel_order("1") < channel_order("v2")


def test_minor_targets_are_distinct_from_channels() -> None:
    assert normalize_minor_target("0.16") == "0.16"
    assert minor_target_for_version("0.16.4") == "0.16"
    with pytest.raises(ValueError):
        normalize_minor_target("v0.16")
