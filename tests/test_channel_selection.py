import pytest

from vigi_vision.channel_selection import Channel, select_channel


def test_select_channel_returns_configured_online_channel() -> None:
    # Given
    channels = (
        Channel(channel_id=1, name="Front", alias="Front", online=True),
        Channel(channel_id=2, name="Back", alias="Back", online=True),
    )

    # When
    selected = select_channel(channels, configured_channel_id=2)

    # Then
    assert selected.channel_id == 2


def test_select_channel_returns_only_online_channel() -> None:
    # Given
    channels = (
        Channel(channel_id=1, name="Front", alias="Front", online=False),
        Channel(channel_id=2, name="Back", alias="Back", online=True),
    )

    # When
    selected = select_channel(channels, configured_channel_id=None)

    # Then
    assert selected.channel_id == 2


def test_select_channel_rejects_ambiguous_online_channels() -> None:
    # Given
    channels = (
        Channel(channel_id=1, name="Front", alias="Front", online=True),
        Channel(channel_id=2, name="Back", alias="Back", online=True),
    )

    # When / Then
    with pytest.raises(ValueError, match="VIGI_CHANNEL_ID"):
        _ = select_channel(channels, configured_channel_id=None)


@pytest.mark.parametrize(
    "configured_channel_id",
    [1, 2],
)
def test_select_channel_rejects_configured_missing_or_offline_channel(
    configured_channel_id: int,
) -> None:
    # Given
    channels = (Channel(channel_id=1, name="Front", alias="Front", online=False),)

    # When / Then
    with pytest.raises(ValueError, match="configured"):
        _ = select_channel(channels, configured_channel_id=configured_channel_id)


def test_select_channel_rejects_when_no_online_channel_exists() -> None:
    # Given
    channels = (Channel(channel_id=1, name="Front", alias="Front", online=False),)

    # When / Then
    with pytest.raises(ValueError, match="online"):
        _ = select_channel(channels, configured_channel_id=None)
