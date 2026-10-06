"""Testes de integração para publicação atômica de StateRepository."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from cogs.music import PersistenceError, StateRepository, Track, TrackOrigin


def _track_snapshot(*, origin: TrackOrigin, video_id: str, title: str) -> dict:
    return Track(
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=title,
        duration=210,
        requested_by_id=42 if origin is TrackOrigin.EXPLICIT else None,
        requested_by_name="Membro" if origin is TrackOrigin.EXPLICIT else None,
        origin=origin,
        provider="youtube",
        video_id=video_id,
    ).to_snapshot()


def _document(*, autoplay_enabled: bool = True) -> dict:
    return {
        "schema_version": StateRepository.SCHEMA_VERSION,
        "guilds": {
            "123": {
                "voice_channel_id": 10,
                "text_channel_id": 20,
                "current": _track_snapshot(
                    origin=TrackOrigin.AUTOMATIC,
                    video_id="automatico-atual",
                    title="Recomendação atual",
                ),
                "explicit_queue": [
                    _track_snapshot(
                        origin=TrackOrigin.EXPLICIT,
                        video_id="pedido-seguinte",
                        title="Pedido seguinte",
                    )
                ],
                "autoplay_enabled": autoplay_enabled,
                "recommendation_source": {
                    "video_id": "fonte-manual",
                    "webpage_url": "https://www.youtube.com/watch?v=fonte-manual",
                },
                "automatic_history": ["automatico-anterior", "automatico-atual"],
                "volume": 0.75,
                "loop_mode": "off",
            }
        },
    }


def test_publish_writes_complete_validated_snapshot_to_tmp_path(tmp_path: Path):
    """Validates: Requirements 7.1, 7.2"""
    state_path = tmp_path / "queue_state.json"
    repository = StateRepository(str(state_path))
    document = _document()

    repository.publish(document)

    assert json.loads(state_path.read_text(encoding="utf-8")) == repository.validate(document)
    assert repository.load() == repository.validate(document)
    assert list(tmp_path.glob(".queue_state-*.tmp")) == []


def test_publish_failure_before_replace_keeps_previous_snapshot_and_candidate_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Validates: Requirements 1.3, 1.4, 1.5, 7.2, 7.3"""
    state_path = tmp_path / "queue_state.json"
    repository = StateRepository(str(state_path))
    previous = _document(autoplay_enabled=True)
    candidate = _document(autoplay_enabled=False)
    candidate_before_publish = copy.deepcopy(candidate)
    repository.publish(previous)
    prior_file_contents = state_path.read_text(encoding="utf-8")

    def fail_before_replace(_: str, __: str) -> None:
        raise OSError("falha injetada antes da substituição atômica")

    monkeypatch.setattr("cogs.music.os.replace", fail_before_replace)

    with pytest.raises(PersistenceError, match="não consegui publicar"):
        repository.publish(candidate)

    assert state_path.read_text(encoding="utf-8") == prior_file_contents
    assert repository.load() == repository.validate(previous)
    assert candidate == candidate_before_publish
    assert list(tmp_path.glob(".queue_state-*.tmp")) == []


def test_validate_rejects_snapshot_with_required_field_missing(tmp_path: Path):
    """Validates: Requirements 7.1, 7.3"""
    repository = StateRepository(str(tmp_path / "queue_state.json"))
    invalid_document = _document()
    del invalid_document["guilds"]["123"]["autoplay_enabled"]

    with pytest.raises(PersistenceError, match="incompleto ou desconhecido"):
        repository.publish(invalid_document)

    assert not (tmp_path / "queue_state.json").exists()


def test_validate_rejects_automatic_track_in_explicit_queue(tmp_path: Path):
    """Validates: Requirements 7.1, 7.3"""
    repository = StateRepository(str(tmp_path / "queue_state.json"))
    invalid_document = _document()
    invalid_document["guilds"]["123"]["explicit_queue"] = [
        _track_snapshot(
            origin=TrackOrigin.AUTOMATIC,
            video_id="automatica-na-fila",
            title="Automática inválida na fila",
        )
    ]

    with pytest.raises(PersistenceError, match="explicit_queue não pode conter faixas automáticas"):
        repository.publish(invalid_document)

    assert not (tmp_path / "queue_state.json").exists()
