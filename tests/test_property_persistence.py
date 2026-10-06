"""Testes baseados em propriedades para persistência de estado recuperável."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from hypothesis import given, settings, strategies as st

from cogs.music import PlaybackState, StateRepository, Track, TrackOrigin, YouTubeSource


_ID = st.from_regex(r"[A-Za-z0-9_-]{1,16}", fullmatch=True)
_REQUESTER_ID = st.one_of(st.none(), st.integers(min_value=1, max_value=1_000_000))
_REQUESTER_NAME = st.one_of(st.none(), st.text(min_size=1, max_size=30))
_DURATION = st.one_of(st.none(), st.integers(min_value=0, max_value=86_400))


@dataclass(frozen=True)
class _SnapshotPlayer:
    """Dublê mínimo que expõe o contrato de serialização do repositório."""

    state_snapshot: dict[str, Any]

    def snapshot(self) -> dict[str, Any]:
        return self.state_snapshot


def _track_payload(origin: TrackOrigin, video_id: str, provider: str) -> dict[str, Any]:
    return Track(
        url=f"https://example.test/watch/{video_id}",
        title=f"Faixa {video_id}",
        duration=180,
        requested_by_id=101,
        requested_by_name="Membro",
        origin=origin,
        provider=provider,
        video_id=video_id if provider == "youtube" else None,
    ).to_snapshot()


@st.composite
def _recoverable_snapshots(draw: st.DrawFn) -> dict[str, Any]:
    source_id = draw(st.one_of(st.none(), _ID))
    source = (
        None
        if source_id is None
        else {
            "video_id": source_id,
            "webpage_url": f"https://www.youtube.com/watch?v={source_id}",
        }
    )
    history = draw(st.lists(_ID, unique=True, max_size=6)) if source else []

    current_origin = draw(st.sampled_from(tuple(TrackOrigin)))
    if current_origin is TrackOrigin.AUTOMATIC and source is None:
        source_id = draw(_ID)
        source = {
            "video_id": source_id,
            "webpage_url": f"https://www.youtube.com/watch?v={source_id}",
        }
        history = draw(st.lists(_ID, unique=True, max_size=6))
    current = draw(st.booleans())
    current_payload = None
    if current:
        current_id = draw(_ID)
        current_payload = _track_payload(
            current_origin,
            current_id,
            "youtube" if current_origin is TrackOrigin.AUTOMATIC else draw(st.sampled_from(("youtube", "soundcloud"))),
        )
        current_payload["duration"] = draw(_DURATION)
        current_payload["requested_by_id"] = draw(_REQUESTER_ID)
        current_payload["requested_by_name"] = draw(_REQUESTER_NAME)

    queue_size = draw(st.integers(min_value=0, max_value=5))
    explicit_queue = []
    for _ in range(queue_size):
        track_id = draw(_ID)
        payload = _track_payload(
            TrackOrigin.EXPLICIT,
            track_id,
            draw(st.sampled_from(("youtube", "soundcloud"))),
        )
        payload["duration"] = draw(_DURATION)
        payload["requested_by_id"] = draw(_REQUESTER_ID)
        payload["requested_by_name"] = draw(_REQUESTER_NAME)
        explicit_queue.append(payload)

    return {
        "voice_channel_id": draw(st.integers(min_value=1, max_value=1_000_000)),
        "text_channel_id": draw(st.one_of(st.none(), st.integers(min_value=1, max_value=1_000_000))),
        "current": current_payload,
        "explicit_queue": explicit_queue,
        "autoplay_enabled": draw(st.booleans()),
        "recommendation_source": source,
        "automatic_history": history,
        "volume": draw(st.floats(min_value=0, max_value=1.5, allow_nan=False, allow_infinity=False)),
        "loop_mode": draw(st.sampled_from(("off", "musica", "fila"))),
    }


def _deserialize_snapshot(snapshot: dict[str, Any]) -> tuple[PlaybackState, deque[Track]]:
    """Reconstrói o estado validado sem incluir automáticas na fila explícita."""

    source_payload = snapshot["recommendation_source"]
    source = None if source_payload is None else YouTubeSource(**source_payload)
    state = PlaybackState(
        autoplay_enabled=snapshot["autoplay_enabled"],
        recommendation_source=source,
        automatic_history=set(snapshot["automatic_history"]),
        current=None if snapshot["current"] is None else Track.from_snapshot(snapshot["current"]),
        loop_mode=snapshot["loop_mode"],
        volume=snapshot["volume"],
    )
    explicit_queue = deque(Track.from_snapshot(track) for track in snapshot["explicit_queue"])
    return state, explicit_queue


# Feature: youtube-autoplay-queue, Property 6: Round-trip do estado recuperável
@settings(max_examples=100)
@given(snapshot=_recoverable_snapshots())
def test_recoverable_state_round_trip_preserves_state_without_automatic_queue(snapshot):
    """Validates: Requirements 7.1, 7.4"""
    repository = StateRepository()
    document = repository.serialize({1: _SnapshotPlayer(snapshot)})

    validated = repository.validate(document)
    restored_state, restored_queue = _deserialize_snapshot(validated["guilds"]["1"])

    assert restored_state.autoplay_enabled is snapshot["autoplay_enabled"]
    assert restored_state.recommendation_source == (
        None if snapshot["recommendation_source"] is None else YouTubeSource(**snapshot["recommendation_source"])
    )
    assert restored_state.automatic_history == set(snapshot["automatic_history"])
    assert restored_state.current == (
        None if snapshot["current"] is None else Track.from_snapshot(snapshot["current"])
    )
    assert restored_state.loop_mode == snapshot["loop_mode"]
    assert restored_state.volume == float(snapshot["volume"])
    assert list(restored_queue) == [Track.from_snapshot(track) for track in snapshot["explicit_queue"]]
    assert all(track.origin is TrackOrigin.EXPLICIT for track in restored_queue)
