"""Teste baseado em propriedades para invariantes dos controles de interrupção."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal

from hypothesis import given, settings, strategies as st

from cogs.music import MusicPlayer, RecommendationDecision, Track, TrackOrigin, YouTubeSource


_ID = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=16)
_OPERATION = st.sampled_from(("stop", "clear", "leave"))
_LOOP_MODE = st.sampled_from(("off", "musica", "fila"))
_CURRENT_ORIGIN = st.sampled_from(("none", "explicit", "automatic"))


@dataclass
class _VoiceChannel:
    id: int = 10


class _VoiceClient:
    """Dublê isolado para testar transições sem Discord ou voz reais."""

    def __init__(self) -> None:
        self.channel: _VoiceChannel | None = _VoiceChannel()
        self.connected = True
        self.stop_calls = 0
        self.disconnect_calls: list[bool] = []

    def is_connected(self) -> bool:
        return self.connected

    def stop(self) -> None:
        self.stop_calls += 1

    async def disconnect(self, *, force: bool = False) -> None:
        self.disconnect_calls.append(force)
        self.connected = False
        self.channel = None


@dataclass
class _Guild:
    voice_client: _VoiceClient
    id: int = 1


class _Cog:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.bot = SimpleNamespace(loop=loop)
        self.players: dict[int, MusicPlayer] = {}
        self.persist_calls = 0

    def persist_states(self) -> None:
        self.persist_calls += 1


@dataclass(frozen=True)
class _InterruptionCase:
    operation: Literal["stop", "clear", "leave"]
    autoplay_enabled: bool
    source_id: str | None
    history_ids: tuple[str, ...]
    queue_ids: tuple[str, ...]
    current_origin: Literal["none", "explicit", "automatic"]
    loop_mode: Literal["off", "musica", "fila"]
    playback_version: int
    pending_recommendation: bool
    suffix: str


@st.composite
def _interruption_cases(draw: st.DrawFn) -> _InterruptionCase:
    """Gera estados coerentes, incluindo uma recomendação já aceita quando aplicável."""

    pending_recommendation = draw(st.booleans())
    suffix = draw(_ID)
    source_id = f"source-{draw(_ID)}" if draw(st.booleans()) or pending_recommendation else None
    current_origin = draw(_CURRENT_ORIGIN)
    if source_id is None and current_origin == "automatic":
        current_origin = "explicit"

    queue_ids = tuple(f"queued-{item}" for item in draw(st.lists(_ID, unique=True, max_size=6)))
    loop_mode = draw(_LOOP_MODE)
    autoplay_enabled = draw(st.booleans())

    if pending_recommendation:
        # Uma recomendação reservada somente é válida sem faixa ou pedidos explícitos.
        source_id = source_id or f"source-{suffix}"
        current_origin = "none"
        queue_ids = ()
        loop_mode = "off"
        autoplay_enabled = True

    history_ids = (
        tuple(f"history-{item}" for item in draw(st.lists(_ID, unique=True, max_size=6)))
        if source_id is not None
        else ()
    )

    return _InterruptionCase(
        operation=draw(_OPERATION),
        autoplay_enabled=autoplay_enabled,
        source_id=source_id,
        history_ids=history_ids,
        queue_ids=queue_ids,
        current_origin=current_origin,
        loop_mode=loop_mode,
        playback_version=draw(st.integers(min_value=0, max_value=1_000_000)),
        pending_recommendation=pending_recommendation,
        suffix=suffix,
    )


def _explicit_track(track_id: str) -> Track:
    return Track(
        url=f"https://www.youtube.com/watch?v={track_id}",
        title=f"Solicitação {track_id}",
        duration=180,
        origin=TrackOrigin.EXPLICIT,
        provider="youtube",
        video_id=track_id,
    )


def _automatic_track(track_id: str) -> Track:
    return Track(
        url=f"https://www.youtube.com/watch?v={track_id}",
        title=f"Recomendação {track_id}",
        duration=180,
        origin=TrackOrigin.AUTOMATIC,
        provider="youtube",
        video_id=track_id,
    )


async def _exercise_interruption(case: _InterruptionCase) -> None:
    voice_client = _VoiceClient()
    guild = _Guild(voice_client)
    cog = _Cog(asyncio.get_running_loop())
    player = MusicPlayer(cog, guild, SimpleNamespace(id=20))
    cog.players[guild.id] = player

    source = (
        YouTubeSource(case.source_id, f"https://www.youtube.com/watch?v={case.source_id}")
        if case.source_id is not None
        else None
    )
    try:
        player.state.autoplay_enabled = case.autoplay_enabled
        player.state.recommendation_source = source
        player.state.automatic_history = set(case.history_ids)
        player.state.playback_version = case.playback_version
        player.loop_mode = case.loop_mode
        player.explicit_queue.extend(_explicit_track(track_id) for track_id in case.queue_ids)
        if case.current_origin == "explicit":
            player.current = _explicit_track(f"current-{case.suffix}")
        elif case.current_origin == "automatic":
            player.current = _automatic_track(f"current-{case.suffix}")

        if case.pending_recommendation:
            assert source is not None
            decision = RecommendationDecision(source, True, True, "off", case.playback_version)
            player._recommendation_decision = decision
            player._pending_automatic = (decision, _automatic_track(f"candidate-{case.suffix}"))

        previous_current = player.current
        previous_autoplay = player.state.autoplay_enabled
        previous_source = player.state.recommendation_source
        previous_history = set(player.state.automatic_history)
        previous_version = player.state.playback_version

        if case.operation == "stop":
            await player.stop()

            assert player.current is None
            assert list(player.explicit_queue) == []
            assert player.loop_mode == "off"
            assert player.state.playback_version == previous_version + 1
            assert player._recommendation_decision is None
            assert player._pending_automatic is None
        elif case.operation == "clear":
            removed = await player.clear_explicit_queue()

            assert removed == len(case.queue_ids)
            assert list(player.explicit_queue) == []
            assert player.current is previous_current
            assert player.state.autoplay_enabled is previous_autoplay
            assert player.state.recommendation_source == previous_source
            assert player.state.automatic_history == previous_history
            assert player._recommendation_decision is None
            assert player._pending_automatic is None
            expected_version = previous_version + int(case.pending_recommendation)
            assert player.state.playback_version == expected_version
        else:
            await player.destroy(None)

            assert guild.id not in cog.players
            assert player.current is None
            assert list(player.explicit_queue) == []
            assert player.state.recommendation_source is None
            assert player.state.automatic_history == set()
            assert player.state.playback_version == previous_version + 1
            assert player._recommendation_decision is None
            assert player._pending_automatic is None
            assert voice_client.connected is False
            assert voice_client.disconnect_calls == [True]
    finally:
        player._task.cancel()
        await asyncio.gather(player._task, return_exceptions=True)


# Feature: youtube-autoplay-queue, Property 5: Invariantes dos controles de interrupção
@settings(max_examples=100)
@given(case=_interruption_cases())
def test_interruption_controls_preserve_their_state_invariants(case: _InterruptionCase):
    """**Validates: Requirements 6.5, 6.6, 6.8**"""
    asyncio.run(_exercise_interruption(case))
