"""Testes de integração controlada para RecommendationProvider sem rede real."""

from __future__ import annotations

import pytest

from cogs.music import (
    NoQualifiedRecommendation,
    RecommendationFailure,
    RecommendationProvider,
    RecommendationResult,
    TrackOrigin,
    YouTubeSource,
    YTDL_RECOMMENDATION_OPTS,
)


class RecordedExtractor:
    """Dublê de yt-dlp que devolve uma resposta gravada e registra a consulta."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[dict, str]] = []

    def __call__(self, options: dict, query: str) -> object:
        self.calls.append((options, query))
        return self.response


def _source() -> YouTubeSource:
    return YouTubeSource(
        video_id="fonte-manual_01",
        webpage_url="https://www.youtube.com/watch?v=fonte-manual_01",
    )


def _youtube_entry(video_id: str, title: str = "Recomendação") -> dict:
    return {
        "id": video_id,
        "title": title,
        "duration": 242.9,
        "extractor_key": "Youtube",
        "webpage_url": f"https://www.youtube.com/watch?v={video_id}",
        "url": "https://stream.example.test/temporario",
    }


@pytest.mark.asyncio
async def test_provider_normalizes_recorded_ytdlp_candidate_after_filtering_current_history_and_invalid():
    """Validates: Requirements 3.3, 4.2"""
    recorded_extractor = RecordedExtractor(
        {
            "entries": [
                _youtube_entry("faixa-atual", "Atual"),
                _youtube_entry("ja-reproduzida", "Histórico"),
                _youtube_entry("fonte-manual_01", "Fonte"),
                {
                    "id": "soundcloud-track",
                    "title": "Outra plataforma",
                    "extractor_key": "Soundcloud",
                    "webpage_url": "https://soundcloud.com/artista/faixa",
                },
                {
                    "id": "sem-titulo",
                    "extractor_key": "Youtube",
                    "webpage_url": "https://www.youtube.com/watch?v=sem-titulo",
                },
                _youtube_entry("recomendacao-valida", "  Próxima faixa  "),
            ]
        }
    )
    provider = RecommendationProvider(extractor=recorded_extractor, candidate_limit=10)

    result = await provider.fetch_qualified(
        _source(), frozenset({"ja-reproduzida"}), current_id="faixa-atual"
    )

    assert isinstance(result, RecommendationResult)
    assert result.track.url == "https://www.youtube.com/watch?v=recomendacao-valida"
    assert result.track.title == "Próxima faixa"
    assert result.track.duration == 242
    assert result.track.origin is TrackOrigin.AUTOMATIC
    assert result.track.provider == "youtube"
    assert result.track.video_id == "recomendacao-valida"
    assert result.track.requested_by_id is None
    assert result.track.requested_by_name is None
    assert recorded_extractor.calls == [
        (
            {**YTDL_RECOMMENDATION_OPTS, "playlistend": 10},
            "https://www.youtube.com/watch?v=fonte-manual_01&list=RDfonte-manual_01&start_radio=1",
        )
    ]


@pytest.mark.asyncio
async def test_provider_synthesizes_page_url_for_flat_youtube_candidate():
    """Entradas flat de um Mix devem ser reproduzíveis mesmo sem webpage_url."""
    recorded_extractor = RecordedExtractor(
        {
            "entries": [
                _youtube_entry("faixa-atual"),
                {"id": "sem-pagina", "title": "Entrada flat"},
            ]
        }
    )
    provider = RecommendationProvider(extractor=recorded_extractor)

    result = await provider.fetch_qualified(_source(), frozenset(), current_id="faixa-atual")

    assert isinstance(result, RecommendationResult)
    assert result.track.video_id == "sem-pagina"
    assert result.track.url == "https://www.youtube.com/watch?v=sem-pagina"


@pytest.mark.asyncio
async def test_provider_returns_no_qualified_recommendation_for_recorded_invalid_candidates():
    """Validates: Requirements 3.6"""
    recorded_extractor = RecordedExtractor(
        {
            "entries": [
                _youtube_entry("faixa-atual"),
                {"id": "id com espaco", "title": "Incompleta"},
                {"id": "nao-youtube", "title": "Outra", "webpage_url": "https://example.test/video"},
            ]
        }
    )
    provider = RecommendationProvider(extractor=recorded_extractor)

    result = await provider.fetch_qualified(_source(), frozenset(), current_id="faixa-atual")

    assert isinstance(result, NoQualifiedRecommendation)
    assert len(recorded_extractor.calls) == 1


@pytest.mark.asyncio
async def test_provider_returns_extractor_failure_without_network_when_fake_ytdlp_raises():
    """Validates: Requirements 3.6"""
    calls: list[tuple[dict, str]] = []

    def failing_extractor(options: dict, query: str) -> object:
        calls.append((options, query))
        raise RuntimeError("resposta gravada indisponível")

    provider = RecommendationProvider(extractor=failing_extractor)

    result = await provider.fetch_qualified(_source(), frozenset(), current_id=None)

    assert result == RecommendationFailure("extractor_error")
    assert calls == [
        (
            {**YTDL_RECOMMENDATION_OPTS, "playlistend": 10},
            "https://www.youtube.com/watch?v=fonte-manual_01&list=RDfonte-manual_01&start_radio=1",
        )
    ]
