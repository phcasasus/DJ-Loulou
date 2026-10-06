"""Teste baseado em propriedade para aceitar uma recomendação qualificada."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from hypothesis import given, settings, strategies as st

from cogs.music import (
    MusicPlayer,
    RecommendationDecision,
    Track,
    TrackOrigin,
    YouTubeSource,
)


_VIDEO_ID = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-",
    min_size=1,
    max_size=16,
)


class _FakeCog:
    """Colaboração mínima do player, sem Discord nem persistência real."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.bot = SimpleNamespace(loop=loop)

    def persist_states(self) -> bool:
        return True


@st.composite
def _qualified_recommendations(draw: st.DrawFn) -> tuple[YouTubeSource, set[str], Track]:
    source_id = draw(_VIDEO_ID)
    history = set(draw(st.lists(_VIDEO_ID.filter(lambda video_id: video_id != source_id), unique=True, max_size=6)))
    excluded_ids = history | {source_id}
    candidate_id = draw(_VIDEO_ID.filter(lambda video_id: video_id not in excluded_ids))
    source = YouTubeSource(
        video_id=source_id,
        webpage_url=f"https://www.youtube.com/watch?v={source_id}",
    )
    candidate = Track(
        url=f"https://www.youtube.com/watch?v={candidate_id}",
        title=f"Recomendação {candidate_id}",
        duration=180,
        origin=TrackOrigin.AUTOMATIC,
        provider="youtube",
        video_id=candidate_id,
    )
    return source, history, candidate


# Feature: youtube-autoplay-queue, Property 3: Aceitação de recomendação qualificada
@pytest.mark.asyncio
@settings(max_examples=100)
@given(recommendation=_qualified_recommendations())
async def test_qualified_recommendation_starts_once_and_updates_only_automatic_history(recommendation):
    """Validates: Requirements 3.3, 3.5, 4.2, 5.4"""
    source, history, candidate = recommendation
    guild = SimpleNamespace(
        id=30,
        voice_client=SimpleNamespace(is_connected=lambda: True),
    )
    text_channel = SimpleNamespace(id=20)
    player = MusicPlayer(_FakeCog(asyncio.get_running_loop()), guild, text_channel)
    player._task.cancel()  # O teste controla explicitamente a transição da recomendação.

    try:
        player.state.recommendation_source = source
        player.state.automatic_history = set(history)
        decision = RecommendationDecision(
            source=source,
            autoplay_enabled=True,
            explicit_queue_empty=True,
            loop_mode="off",
            playback_version=player.state.playback_version,
        )
        player._recommendation_decision = decision

        assert player.current is None
        assert candidate.video_id != source.video_id
        assert candidate.video_id not in history
        assert await player.accept_recommendation(decision, candidate) is True

        action, pending = await player.select_next_action()
        assert action == "automatic"
        accepted_decision, accepted_track = pending
        assert accepted_decision == decision
        assert accepted_track is candidate

        async with player.lock:
            await player._mark_track_started_locked(accepted_track)

        assert player.current is candidate
        assert player.state.recommendation_source == source
        assert player.state.automatic_history == history | {candidate.video_id}
        assert list(player.explicit_queue) == []
        assert (await player.select_next_action())[0] == "playing"
    finally:
        await asyncio.gather(player._task, return_exceptions=True)
