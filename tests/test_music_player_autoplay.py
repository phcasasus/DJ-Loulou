"""Testes unitários dos fluxos do MusicPlayer com Autoplay."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cogs.music import (
    Music,
    MusicPlayer,
    NoQualifiedRecommendation,
    RecommendationDecision,
    RecommendationFailure,
    Track,
    TrackOrigin,
    YouTubeSource,
)


class FakeCog:
    """Colaboração mínima para testar o player sem Discord ou persistência reais."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.bot = SimpleNamespace(loop=loop)
        self.ffmpeg = "ffmpeg"
        self.players: dict[int, MusicPlayer] = {}
        self.persist_calls = 0

    def persist_states(self) -> None:
        self.persist_calls += 1


class CommandHarness:
    """Expõe somente a dependência dos comandos pause/resume."""

    def __init__(self, player: MusicPlayer, voice_client) -> None:
        self.player = player
        self.voice_client = voice_client

    async def _require_player(self, interaction):
        return self.player, self.voice_client


class StaticRecommendationProvider:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[YouTubeSource, frozenset[str], str | None]] = []

    async def fetch_qualified(
        self, source: YouTubeSource, excluded_ids: frozenset[str], current_id: str | None
    ) -> object:
        self.calls.append((source, excluded_ids, current_id))
        return self.result


def _explicit(video_id: str = "manual-01") -> Track:
    return Track(
        url=f"https://www.youtube.com/watch?v={video_id}",
        title="Solicitação manual",
        duration=180,
        requested_by_id=7,
        requested_by_name="Membro",
        origin=TrackOrigin.EXPLICIT,
        provider="youtube",
        video_id=video_id,
    )


def _automatic(video_id: str = "auto-01") -> Track:
    return Track(
        url=f"https://www.youtube.com/watch?v={video_id}",
        title="Recomendação automática",
        duration=180,
        origin=TrackOrigin.AUTOMATIC,
        provider="youtube",
        video_id=video_id,
    )


async def _new_player(fake_guild, fake_text_channel) -> MusicPlayer:
    player = MusicPlayer(FakeCog(asyncio.get_running_loop()), fake_guild, fake_text_channel)
    player._task.cancel()
    await asyncio.gather(player._task, return_exceptions=True)
    return player


@pytest.mark.asyncio
async def test_pause_and_resume_keep_automatic_track_and_recommendation_context(
    fake_guild, fake_text_channel, fake_interaction
):
    """Validates: Requirements 6.1, 6.2"""
    player = await _new_player(fake_guild, fake_text_channel)
    automatic = _automatic()
    source = YouTubeSource("manual-01", "https://www.youtube.com/watch?v=manual-01")
    player.current = automatic
    player.state.recommendation_source = source
    player.state.automatic_history = {automatic.video_id}
    player.state.playback_version = 11
    fake_guild.voice_client.play(object())
    commands = CommandHarness(player, fake_guild.voice_client)

    await Music.pause.callback(commands, fake_interaction)
    await Music.resume.callback(commands, fake_interaction)

    assert fake_guild.voice_client.pause_calls == 1
    assert fake_guild.voice_client.resume_calls == 1
    assert player.current is automatic
    assert player.state.recommendation_source == source
    assert player.state.automatic_history == {"auto-01"}
    assert player.state.playback_version == 11
    assert player.recommendation_task is None
    assert [message.content for message in fake_interaction.response.messages] == ["Pausado.", "Retomando."]


@pytest.mark.asyncio
async def test_explicit_stream_error_preserves_existing_recommendation_context(
    fake_guild, fake_text_channel, monkeypatch
):
    """Validates: Requirements 4.4"""
    player = await _new_player(fake_guild, fake_text_channel)
    previous_source = YouTubeSource("anterior-01", "https://www.youtube.com/watch?v=anterior-01")
    player.state.recommendation_source = previous_source
    player.state.automatic_history = {"auto-anterior"}

    def failing_extract(*_args, **_kwargs):
        raise RuntimeError("stream indisponível")

    monkeypatch.setattr("cogs.music._extract", failing_extract)

    result = await player._play_track(_explicit(), anunciar=False)

    assert result == "erro"
    assert player.current is None
    assert player.state.recommendation_source == previous_source
    assert player.state.automatic_history == {"auto-anterior"}
    assert "Nao consegui tocar **Solicitação manual**" in fake_text_channel.sent_messages[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_result", "expected_message"),
    [
        (NoQualifiedRecommendation(), "Nao encontrei uma recomendacao"),
        (RecommendationFailure("extractor_error"), "Nao encontrei uma recomendacao"),
    ],
)
async def test_missing_or_failed_provider_announces_and_does_not_start_automatic_track(
    fake_guild, fake_text_channel, provider_result, expected_message
):
    """Validates: Requirements 3.6"""
    player = await _new_player(fake_guild, fake_text_channel)
    source = YouTubeSource("manual-01", "https://www.youtube.com/watch?v=manual-01")
    player.state.recommendation_source = source
    decision = RecommendationDecision(source, True, True, "off", player.state.playback_version)
    player._recommendation_decision = decision
    player.recommendation_provider = StaticRecommendationProvider(provider_result)

    await player._fetch_recommendation(decision, frozenset(), None)

    assert player.current is None
    assert player._pending_automatic is None
    assert player._recommendation_failure_version == player.state.playback_version
    assert len(fake_text_channel.sent_messages) == 1
    assert expected_message in fake_text_channel.sent_messages[0].content


@pytest.mark.asyncio
async def test_finished_automatic_selects_explicit_queue_before_another_recommendation(
    fake_guild, fake_text_channel
):
    """Validates: Requirements 2.5, 6.3"""
    player = await _new_player(fake_guild, fake_text_channel)
    automatic = _automatic()
    queued = _explicit("manual-proxima")
    player.current = automatic
    player.state.recommendation_source = YouTubeSource("manual-01", "https://www.youtube.com/watch?v=manual-01")
    await player.enqueue_explicit([queued])

    await player._finish_track(automatic, "fim")
    action, selected = await player.select_next_action()

    assert action == "explicit"
    assert selected is queued
    assert player.current is None
    assert list(player.explicit_queue) == []
    assert player.recommendation_task is None


@pytest.mark.asyncio
async def test_delayed_recommendation_is_discarded_after_explicit_request_changes_context(
    fake_guild, fake_text_channel
):
    """Validates: Requirements 3.4"""
    player = await _new_player(fake_guild, fake_text_channel)
    source = YouTubeSource("manual-01", "https://www.youtube.com/watch?v=manual-01")
    player.state.recommendation_source = source
    decision = RecommendationDecision(source, True, True, "off", player.state.playback_version)
    player._recommendation_decision = decision

    await player.enqueue_explicit([_explicit("nova-solicitacao")])
    accepted = await player.accept_recommendation(decision, _automatic("atrasada-01"))

    assert accepted is False
    assert player.current is None
    assert player._pending_automatic is None
    assert [track.video_id for track in player.explicit_queue] == ["nova-solicitacao"]
    assert player.state.automatic_history == set()


class _RestorableVoiceChannel:
    def __init__(self, guild, members):
        self.id = 55
        self._guild = guild
        self.members = members
        self.connect_calls = 0

    async def connect(self, *, self_deaf: bool):
        assert self_deaf is True
        self.connect_calls += 1
        self._guild.voice_client = SimpleNamespace(channel=self, is_connected=lambda: True)
        return self._guild.voice_client


class _RestorableGuild:
    def __init__(self):
        self.id = 77
        self.voice_client = None
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


class _RestoreBot:
    def __init__(self, loop, guild):
        self.loop = loop
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild if guild_id == self._guild.id else None


class _RestoreRepository:
    def __init__(self, document):
        self.document = document

    def load(self):
        return self.document


def _restore_snapshot(*, source, current, explicit_queue, autoplay_enabled=True):
    return {
        "schema_version": 1,
        "guilds": {
            "77": {
                "voice_channel_id": 55,
                "text_channel_id": 56,
                "current": current.to_snapshot() if current else None,
                "explicit_queue": [track.to_snapshot() for track in explicit_queue],
                "autoplay_enabled": autoplay_enabled,
                "recommendation_source": source,
                "automatic_history": ["auto-anterior"] if source else [],
                "volume": 0.85,
                "loop_mode": "fila",
            }
        },
    }


@pytest.mark.asyncio
async def test_restore_applies_complete_snapshot_before_waking_selection(monkeypatch):
    """Validates: Requirements 7.4"""
    guild = _RestorableGuild()
    voice = _RestorableVoiceChannel(guild, [SimpleNamespace(bot=False)])
    text = SimpleNamespace(id=56, sent_messages=[])

    async def send(message):
        text.sent_messages.append(message)

    text.send = send
    guild.channels = {55: voice, 56: text}
    source = YouTubeSource("fonte-manual", "https://www.youtube.com/watch?v=fonte-manual")
    automatic = _automatic("auto-atual")
    queued = _explicit("pedido-pendente")
    document = _restore_snapshot(
        source={"video_id": source.video_id, "webpage_url": source.webpage_url},
        current=automatic,
        explicit_queue=[queued],
        autoplay_enabled=False,
    )
    music = Music.__new__(Music)
    music.bot = _RestoreBot(asyncio.get_running_loop(), guild)
    music.players = {}
    music.state_repository = _RestoreRepository(document)
    music.ffmpeg = "ffmpeg"

    async def dormant_loop(self):
        await asyncio.Event().wait()

    monkeypatch.setattr("cogs.music.discord.VoiceChannel", _RestorableVoiceChannel)
    monkeypatch.setattr(MusicPlayer, "_player_loop", dormant_loop)

    await music._restore_states()
    player = music.players[guild.id]
    player._task.cancel()
    await asyncio.gather(player._task, return_exceptions=True)

    assert player.state.autoplay_enabled is False
    assert player.state.recommendation_source == source
    assert player.state.automatic_history == {"auto-anterior"}
    assert player.state.volume == 0.85
    assert player.state.loop_mode == "fila"
    assert [track.video_id for track in player.explicit_queue] == ["pedido-pendente"]
    assert player._pending_automatic == (None, automatic)
    assert voice.connect_calls == 1


@pytest.mark.asyncio
async def test_restore_without_source_keeps_only_explicit_tracks(monkeypatch):
    """Validates: Requirements 7.5"""
    guild = _RestorableGuild()
    voice = _RestorableVoiceChannel(guild, [SimpleNamespace(bot=False)])
    text = SimpleNamespace(id=56)

    async def send(_message):
        return None

    text.send = send
    guild.channels = {55: voice, 56: text}
    current = _explicit("interrompida")
    queued = _explicit("pendente")
    music = Music.__new__(Music)
    music.bot = _RestoreBot(asyncio.get_running_loop(), guild)
    music.players = {}
    music.state_repository = _RestoreRepository(
        _restore_snapshot(source=None, current=current, explicit_queue=[queued])
    )
    music.ffmpeg = "ffmpeg"

    async def dormant_loop(self):
        await asyncio.Event().wait()

    monkeypatch.setattr("cogs.music.discord.VoiceChannel", _RestorableVoiceChannel)
    monkeypatch.setattr(MusicPlayer, "_player_loop", dormant_loop)

    await music._restore_states()
    player = music.players[guild.id]
    player._task.cancel()
    await asyncio.gather(player._task, return_exceptions=True)

    assert player.state == player.state.__class__()
    assert [track.video_id for track in player.explicit_queue] == ["interrompida", "pendente"]
    assert player._pending_automatic is None


@pytest.mark.asyncio
async def test_restore_validates_snapshot_before_connecting_or_creating_player(monkeypatch):
    """Validates: Requirements 7.4, 7.5"""
    guild = _RestorableGuild()
    voice = _RestorableVoiceChannel(guild, [SimpleNamespace(bot=False)])
    guild.channels = {55: voice}
    invalid_document = _restore_snapshot(source=None, current=None, explicit_queue=[])
    del invalid_document["guilds"]["77"]["autoplay_enabled"]
    music = Music.__new__(Music)
    music.bot = _RestoreBot(asyncio.get_running_loop(), guild)
    music.players = {}
    music.state_repository = _RestoreRepository(invalid_document)
    music.ffmpeg = "ffmpeg"

    monkeypatch.setattr("cogs.music.discord.VoiceChannel", _RestorableVoiceChannel)

    await music._restore_states()

    assert music.players == {}
    assert voice.connect_calls == 0
