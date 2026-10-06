"""Testes baseados em propriedades para o contexto manual de recomendações."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from cogs.music import MusicPlayer, Track, TrackOrigin, YouTubeSource


_VIDEO_ID = st.from_regex(r"[A-Za-z0-9_-]{1,16}", fullmatch=True)


@dataclass
class _VoiceClient:
    """Dublê mínimo que mantém o loop do player conectado durante o teste."""

    connected: bool = True

    def is_connected(self) -> bool:
        return self.connected


class _Cog:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.bot = SimpleNamespace(loop=loop)


@st.composite
def _track_contexts(draw: st.DrawFn) -> tuple[str, Track, YouTubeSource, set[str], int]:
    """Gera inícios válidos para cada ramo da transição de contexto."""

    kind = draw(st.sampled_from(("explicit_youtube", "explicit_without_youtube", "automatic")))
    source_id = draw(_VIDEO_ID)
    source = YouTubeSource(
        video_id=source_id,
        webpage_url=f"https://www.youtube.com/watch?v={source_id}",
    )
    history = set(draw(st.lists(_VIDEO_ID, unique=True, max_size=6)))
    version = draw(st.integers(min_value=0, max_value=1_000_000))
    track_id = draw(_VIDEO_ID)

    if kind == "explicit_youtube":
        track = Track(
            url=f"https://www.youtube.com/watch?v={track_id}",
            title=f"Manual {track_id}",
            duration=180,
            origin=TrackOrigin.EXPLICIT,
            provider="youtube",
            video_id=track_id,
        )
    elif kind == "explicit_without_youtube":
        track = Track(
            url=f"https://soundcloud.example.test/tracks/{track_id}",
            title=f"Manual sem YouTube {track_id}",
            duration=180,
            origin=TrackOrigin.EXPLICIT,
            provider="soundcloud",
            video_id=None,
        )
    else:
        track = Track(
            url=f"https://www.youtube.com/watch?v={track_id}",
            title=f"Automática {track_id}",
            duration=180,
            origin=TrackOrigin.AUTOMATIC,
            provider="youtube",
            video_id=track_id,
        )

    return kind, track, source, history, version


# Feature: youtube-autoplay-queue, Property 4: Contexto manual de recomendação
@settings(max_examples=100)
@given(context=_track_contexts())
@pytest.mark.asyncio
async def test_track_start_maintains_manual_recommendation_context(context):
    """Validates: Requirements 4.1, 4.2, 4.3, 4.4"""
    kind, track, previous_source, previous_history, previous_version = context
    guild = SimpleNamespace(id=1, voice_client=_VoiceClient())
    player = MusicPlayer(_Cog(asyncio.get_running_loop()), guild, SimpleNamespace())

    try:
        player.state.recommendation_source = previous_source
        player.state.automatic_history = set(previous_history)
        player.state.playback_version = previous_version

        async with player.lock:
            await player._mark_track_started_locked(track)

        assert player.current is track
        if kind == "explicit_youtube":
            assert player.state.recommendation_source == track.youtube_source
            assert player.state.automatic_history == set()
            assert player.state.playback_version == previous_version + 1
        elif kind == "explicit_without_youtube":
            assert player.state.recommendation_source == previous_source
            assert player.state.automatic_history == previous_history
            assert player.state.playback_version == previous_version
        else:
            assert player.state.recommendation_source == previous_source
            assert player.state.automatic_history == previous_history | {track.video_id}
            assert player.state.playback_version == previous_version
    finally:
        player._task.cancel()
        await asyncio.gather(player._task, return_exceptions=True)
