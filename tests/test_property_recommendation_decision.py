"""Testes baseados em propriedades para decisão de recomendações."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal

from hypothesis import given, settings, strategies as st

from cogs.music import MusicPlayer, RecommendationDecision, Track, TrackOrigin, YouTubeSource


_ID = st.from_regex(r"[A-Za-z0-9_-]{1,16}", fullmatch=True)
_LOOP_MODE = st.sampled_from(("off", "musica", "fila"))
_EVENT = st.sampled_from(("fim", "skip"))
_MUTATION = st.sampled_from(("none", "version", "source", "autoplay", "queue", "loop", "connection"))


class _VoiceClient:
    """Dublê mínimo da conexão de voz usada pelo predicado de elegibilidade."""

    def __init__(self, connected: bool) -> None:
        self.connected = connected

    def is_connected(self) -> bool:
        return self.connected

    def stop(self) -> None:
        return None


@dataclass
class _Guild:
    voice_client: _VoiceClient
    id: int = 1


class _Cog:
    """Colaboração mínima que impede persistência e I/O externos no teste."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.bot = SimpleNamespace(loop=loop)

    def persist_states(self) -> bool:
        return True


@dataclass(frozen=True)
class _Case:
    autoplay_enabled: bool
    has_source: bool
    queue_has_track: bool
    loop_mode: Literal["off", "musica", "fila"]
    connected: bool
    event: Literal["fim", "skip"]
    mutation: Literal["none", "version", "source", "autoplay", "queue", "loop", "connection"]
    source_suffix: str


@st.composite
def _decision_cases(draw: st.DrawFn) -> _Case:
    return _Case(
        autoplay_enabled=draw(st.booleans()),
        has_source=draw(st.booleans()),
        queue_has_track=draw(st.booleans()),
        loop_mode=draw(_LOOP_MODE),
        connected=draw(st.booleans()),
        event=draw(_EVENT),
        mutation=draw(_MUTATION),
        source_suffix=draw(_ID),
    )


def _explicit_track(video_id: str) -> Track:
    return Track(
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=f"Faixa {video_id}",
        duration=180,
        origin=TrackOrigin.EXPLICIT,
        provider="youtube",
        video_id=video_id,
    )


def _automatic_candidate(case: _Case) -> Track:
    video_id = f"candidate-{case.source_suffix}"
    return Track(
        url=f"https://www.youtube.com/watch?v={video_id}",
        title="Recomendação candidata",
        duration=180,
        origin=TrackOrigin.AUTOMATIC,
        provider="youtube",
        video_id=video_id,
    )


async def _exercise_recommendation_decision(case: _Case) -> None:
    voice_client = _VoiceClient(connected=case.connected)
    player = MusicPlayer(_Cog(asyncio.get_running_loop()), _Guild(voice_client), object())
    source = YouTubeSource(
        video_id=f"source-{case.source_suffix}",
        webpage_url=f"https://www.youtube.com/watch?v=source-{case.source_suffix}",
    )
    finished_track = _explicit_track(f"finished-{case.source_suffix}")

    try:
        player.state.autoplay_enabled = case.autoplay_enabled
        player.state.recommendation_source = source if case.has_source else None
        player.loop_mode = case.loop_mode
        if case.queue_has_track:
            player.explicit_queue.append(_explicit_track(f"queued-{case.source_suffix}"))
        player.current = finished_track

        if case.event == "skip":
            await player.skip()
        await player._finish_track(finished_track, "fim")

        action, payload = await player.select_next_action()
        expected_eligible = (
            case.autoplay_enabled
            and case.has_source
            and not case.queue_has_track
            and case.loop_mode == "off"
            and case.connected
        )

        assert (action == "recommendation") is expected_eligible
        if not expected_eligible:
            if action in {"explicit", "repeat"}:
                assert isinstance(payload, Track)
            else:
                assert action == "idle"
                assert payload is None
            return

        assert isinstance(payload, RecommendationDecision)
        decision = payload
        assert decision.source == source
        assert decision.autoplay_enabled is True
        assert decision.explicit_queue_empty is True
        assert decision.loop_mode == "off"
        assert decision.playback_version == player.state.playback_version

        # Simula uma resposta do provedor chegando após uma mutação concorrente.
        player._recommendation_decision = decision
        candidate = _automatic_candidate(case)
        if case.mutation == "version":
            player.state.playback_version += 1
        elif case.mutation == "source":
            player.state.recommendation_source = YouTubeSource(
                video_id=f"replacement-{case.source_suffix}",
                webpage_url=f"https://www.youtube.com/watch?v=replacement-{case.source_suffix}",
            )
        elif case.mutation == "autoplay":
            player.state.autoplay_enabled = False
        elif case.mutation == "queue":
            player.explicit_queue.append(_explicit_track(f"late-{case.source_suffix}"))
        elif case.mutation == "loop":
            player.loop_mode = "musica"
        elif case.mutation == "connection":
            voice_client.connected = False

        accepted = await player.accept_recommendation(decision, candidate)
        if case.mutation == "none":
            assert accepted is True
            assert player._pending_automatic == (decision, candidate)
        else:
            assert accepted is False
            assert player._pending_automatic is None
    finally:
        player._task.cancel()
        await asyncio.gather(player._task, return_exceptions=True)


# Feature: youtube-autoplay-queue, Property 2: Elegibilidade e invalidação de recomendações
@settings(max_examples=100)
@given(case=_decision_cases())
def test_recommendation_decision_is_created_only_when_eligible_and_discards_invalid_results(case: _Case):
    """Validates: Requirements 1.6, 3.1, 3.2, 3.4, 3.5, 6.4, 6.7"""
    asyncio.run(_exercise_recommendation_decision(case))
