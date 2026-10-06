"""Teste baseado em propriedades para ordenação da fila explícita."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from cogs.music import MusicPlayer, Track, TrackOrigin, YouTubeSource


class _FakeCog:
    """Colaboração mínima do player, sem persistência nem serviços externos."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.bot = SimpleNamespace(loop=loop)
        self.persist_calls = 0

    def persist_states(self) -> None:
        self.persist_calls += 1


def _explicit_track(track_id: str) -> Track:
    return Track(
        url=f"https://www.youtube.com/watch?v={track_id}",
        title=f"Solicitação {track_id}",
        duration=180,
        requested_by_id=1,
        requested_by_name="Membro",
        origin=TrackOrigin.EXPLICIT,
        provider="youtube",
        video_id=track_id,
    )


def _current_track(origin: TrackOrigin) -> Track:
    return Track(
        url="https://www.youtube.com/watch?v=atual",
        title="Faixa atual",
        duration=180,
        origin=origin,
        provider="youtube",
        video_id="atual",
    )


# Feature: youtube-autoplay-queue, Property 1: Ordenação e prioridade absoluta da fila explícita
# **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 6.3**
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    operations=st.lists(st.sampled_from(("play", "playnext")), min_size=1, max_size=20),
    current_origin=st.sampled_from((TrackOrigin.EXPLICIT, TrackOrigin.AUTOMATIC)),
)
@pytest.mark.asyncio
async def test_explicit_queue_orders_requests_and_wins_before_autoplay(
    operations: list[str], current_origin: TrackOrigin, fake_guild, fake_text_channel
):
    """A cabeça explícita é a próxima faixa após atual explícita ou automática."""
    player = MusicPlayer(_FakeCog(asyncio.get_running_loop()), fake_guild, fake_text_channel)
    player._task.cancel()
    await asyncio.gather(player._task, return_exceptions=True)

    try:
        player.current = _current_track(current_origin)
        player.state.recommendation_source = YouTubeSource(
            video_id="fonte", webpage_url="https://www.youtube.com/watch?v=fonte"
        )
        expected_ids: list[str] = []

        for position, operation in enumerate(operations):
            track_id = f"solicitacao-{position}"
            priority = operation == "playnext"
            await player.enqueue_explicit([_explicit_track(track_id)], priority=priority)
            if priority:
                expected_ids.insert(0, track_id)
            else:
                expected_ids.append(track_id)
            assert [track.video_id for track in player.explicit_queue] == expected_ids

        await player._finish_track(player.current, "fim")
        action, selected = await player.select_next_action()

        assert action == "explicit"
        assert isinstance(selected, Track)
        assert selected.video_id == expected_ids[0]
        assert selected.origin is TrackOrigin.EXPLICIT
    finally:
        player._task.cancel()
        await asyncio.gather(player._task, return_exceptions=True)
