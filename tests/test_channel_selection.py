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


def test_select_channel_prefers_channel_one_as_default() -> None:
    # Given
    channels = (
        Channel(channel_id=1, name="Front", alias="Front", online=True),
        Channel(channel_id=2, name="Back", alias="Back", online=True),
    )

    # When
    selected = select_channel(channels, configured_channel_id=None)

    # Then
    assert selected.channel_id == 1


def test_select_channel_uses_smallest_online_id_when_channel_one_is_absent() -> None:
    # Given
    channels = (
        Channel(channel_id=7, name="Back", alias="Back", online=True),
        Channel(channel_id=3, name="Side", alias="Side", online=True),
        Channel(channel_id=9, name="Lobby", alias="Lobby", online=False),
    )

    # When
    selected = select_channel(channels, configured_channel_id=None)

    # Then
    assert selected.channel_id == 3


def test_select_channel_ignores_invalid_and_offline_defaults() -> None:
    # Given
    channels = (
        Channel(channel_id=0, name="Invalid", alias="Invalid", online=True),
        Channel(channel_id=-2, name="Invalid", alias="Invalid", online=True),
        Channel(channel_id=8, name="Offline", alias="Offline", online=False),
    )

    # When / Then
    with pytest.raises(ValueError, match="online"):
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


def test_select_channel_preserves_explicit_selection_across_refreshes() -> None:
    # Given
    configured_channel_id = 2
    initial_channels = (
        Channel(channel_id=1, name="Front", alias="Front", online=True),
        Channel(channel_id=2, name="Back", alias="Back", online=True),
    )
    refreshed_channels = (
        Channel(channel_id=7, name="New", alias="New", online=True),
        Channel(channel_id=2, name="Back", alias="Back", online=True),
        Channel(channel_id=1, name="Front", alias="Front", online=True),
    )

    # When
    initial = select_channel(initial_channels, configured_channel_id)
    refreshed = select_channel(refreshed_channels, configured_channel_id)

    # Then
    assert initial.channel_id == 2
    assert refreshed.channel_id == 2
