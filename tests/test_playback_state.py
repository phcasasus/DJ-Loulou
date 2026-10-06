"""Testes unitários dos modelos de playback e do estado inicial do player."""

import asyncio
from types import SimpleNamespace

import pytest

from cogs.music import MusicPlayer, PlaybackState, Track, TrackOrigin


class FakeCog:
    """Colaboração mínima do MusicPlayer, sem persistência ou Discord reais."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.bot = SimpleNamespace(loop=loop)
        self.save_calls = 0

    def save_states(self) -> None:
        self.save_calls += 1


@pytest.mark.asyncio
async def test_new_player_uses_autoplay_enabled_default_and_empty_playback_state(
    fake_guild, fake_text_channel
):
    """Validates: Requirements 1.1"""
    player = MusicPlayer(FakeCog(asyncio.get_running_loop()), fake_guild, fake_text_channel)

    try:
        assert player.state == PlaybackState()
        assert player.state.autoplay_enabled is True
        assert player.state.recommendation_source is None
        assert player.state.automatic_history == set()
        assert player.current is None
        assert list(player.explicit_queue) == []
    finally:
        player._task.cancel()
        await asyncio.gather(player._task, return_exceptions=True)


@pytest.mark.asyncio
async def test_explicit_queue_rejects_automatic_tracks(fake_guild, fake_text_channel):
    """Validates: Requirements 2.1"""
    player = MusicPlayer(FakeCog(asyncio.get_running_loop()), fake_guild, fake_text_channel)
    automatic_track = Track(
        url="https://www.youtube.com/watch?v=automatico",
        title="Recomendação",
        duration=180,
        origin=TrackOrigin.AUTOMATIC,
        provider="youtube",
        video_id="automatico",
    )

    try:
        with pytest.raises(ValueError, match="explicit_queue"):
            player.enqueue([automatic_track])

        assert list(player.explicit_queue) == []
    finally:
        player._task.cancel()
        await asyncio.gather(player._task, return_exceptions=True)


def test_track_snapshot_preserves_origin_and_youtube_metadata():
    """Validates: Requirements 7.1"""
    track = Track(
        url="https://www.youtube.com/watch?v=video123",
        title="Faixa solicitada",
        duration=245,
        requested_by_id=42,
        requested_by_name="Membro",
        origin=TrackOrigin.EXPLICIT,
        provider="youtube",
        video_id="video123",
        is_live=False,
    )

    snapshot = track.to_snapshot()

    assert snapshot == {
        "url": "https://www.youtube.com/watch?v=video123",
        "title": "Faixa solicitada",
        "duration": 245,
        "requested_by_id": 42,
        "requested_by_name": "Membro",
        "origin": "explicit",
        "provider": "youtube",
        "video_id": "video123",
        "is_live": False,
    }
    assert Track.from_snapshot(snapshot) == track
