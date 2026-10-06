"""Testes de integração controlada do ciclo completo de Autoplay.

A suíte conecta comandos, controles, seleção do player e restauração usando os
fakes compartilhados, sem chamar Discord, YouTube ou FFmpeg reais.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from cogs.music import (
    Music,
    MusicPlayer,
    NoQualifiedRecommendation,
    PlayerControls,
    RecommendationResult,
    StateRepository,
    Track,
    TrackOrigin,
    YouTubeSource,
)
from conftest import FakeInteraction, FakeMessage, FakeTextChannel, FakeVoiceChannel


class IntegrationRepository:
    """Repositório falso que registra o commit do toggle sem acessar o disco."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.published: list[dict[str, Any]] = []

    def serialize(self, players: dict[int, MusicPlayer]) -> dict[str, Any]:
        self.events.append("serialize")
        return {
            "schema_version": StateRepository.SCHEMA_VERSION,
            "guilds": {
                str(guild_id): player.snapshot()
                for guild_id, player in players.items()
                if player.snapshot() is not None
            },
        }

    def publish(self, document: dict[str, Any]) -> None:
        self.events.append("publish")
        self.published.append(document)


class RestoreRepository:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document

    def load(self) -> dict[str, Any]:
        return self.document


class StaticProvider:
    """Fornecedor fake que registra se a busca ocorreu depois do commit."""

    def __init__(self, result: object, repository: IntegrationRepository | None = None) -> None:
        self.result = result
        self.repository = repository
        self.calls: list[tuple[YouTubeSource, frozenset[str], str | None, tuple[str, ...]]] = []

    async def fetch_qualified(
        self, source: YouTubeSource, excluded_ids: frozenset[str], current_id: str | None
    ) -> object:
        events = tuple(self.repository.events) if self.repository is not None else ()
        self.calls.append((source, excluded_ids, current_id, events))
        return self.result


class IntegrationCog:
    def __init__(self, loop: asyncio.AbstractEventLoop, repository: IntegrationRepository) -> None:
        self.bot = SimpleNamespace(loop=loop)
        self.players: dict[int, MusicPlayer] = {}
        self.state_repository = repository
        self.ffmpeg = "ffmpeg"
        self.persist_calls = 0

    def persist_states(self) -> bool:
        self.persist_calls += 1
        return True


class FakeWatchedAudio:
    """Substitui a cadeia FFmpeg/discord para permitir observar voice_client.play."""

    def __init__(self, original: Any, volume: float) -> None:
        self.original = original
        self.volume = volume
        self.last_read = 0.0
        self.cleaned = False

    def cleanup(self) -> None:
        self.cleaned = True


@dataclass
class RestorableGuild:
    id: int = 77
    voice_client: Any | None = None

    def __post_init__(self) -> None:
        self.channels: dict[int, Any] = {}

    def get_channel(self, channel_id: int) -> Any | None:
        return self.channels.get(channel_id)


class RestorableVoiceChannel:
    def __init__(self, guild: RestorableGuild, members: list[Any]) -> None:
        self.id = 55
        self.guild = guild
        self.members = members
        self.connect_calls = 0

    async def connect(self, *, self_deaf: bool) -> Any:
        assert self_deaf is True
        self.connect_calls += 1
        self.guild.voice_client = SimpleNamespace(channel=self, is_connected=lambda: True)
        return self.guild.voice_client


class RestoreBot:
    def __init__(self, loop: asyncio.AbstractEventLoop, guild: RestorableGuild) -> None:
        self.loop = loop
        self.guild = guild

    def get_guild(self, guild_id: int) -> RestorableGuild | None:
        return self.guild if guild_id == self.guild.id else None


def _explicit(video_id: str, *, requested_by: str = "Ana") -> Track:
    return Track(
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=f"Pedido {video_id}",
        duration=180,
        requested_by_id=1,
        requested_by_name=requested_by,
        origin=TrackOrigin.EXPLICIT,
        provider="youtube",
        video_id=video_id,
    )


def _automatic(video_id: str) -> Track:
    return Track(
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=f"Recomendação {video_id}",
        duration=180,
        origin=TrackOrigin.AUTOMATIC,
        provider="youtube",
        video_id=video_id,
    )


async def _new_player(fake_guild, fake_text_channel, repository: IntegrationRepository, *, run_loop: bool = False):
    cog = IntegrationCog(asyncio.get_running_loop(), repository)
    player = MusicPlayer(cog, fake_guild, fake_text_channel)
    cog.players[fake_guild.id] = player
    if not run_loop:
        player._task.cancel()
        await asyncio.gather(player._task, return_exceptions=True)
    return player, cog


async def _wait_for(predicate, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("A condição assíncrona não foi atingida.")


def _command_interaction(fake_guild, channel: FakeTextChannel) -> FakeInteraction:
    interaction = FakeInteraction()
    interaction.guild = fake_guild
    interaction.channel = channel
    interaction.user = SimpleNamespace(
        id=1,
        display_name="Ana",
        voice=SimpleNamespace(channel=fake_guild.voice_client.channel),
    )
    return interaction


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["fim", "skip"], ids=["termino", "pulo"])
async def test_play_and_playnext_during_automatic_choose_explicit_priority_after_completion(
    fake_guild, fake_text_channel, monkeypatch, completion: str
):
    """Validates: Requirements 2.2, 2.3, 2.4, 2.5, 6.3"""
    repository = IntegrationRepository()
    player, cog = await _new_player(fake_guild, fake_text_channel, repository)
    player.current = _automatic("auto-atual")
    player.state.recommendation_source = YouTubeSource(
        "manual-origem", "https://www.youtube.com/watch?v=manual-origem"
    )
    music = Music.__new__(Music)
    music.bot = cog.bot
    music.players = cog.players
    music.state_repository = repository
    music.ffmpeg = "ffmpeg"

    def extract(_options: dict[str, Any], query: str) -> dict[str, Any]:
        return {
            "id": query,
            "title": f"Faixa {query}",
            "duration": 180,
            "extractor_key": "Youtube",
            "webpage_url": f"https://www.youtube.com/watch?v={query}",
        }

    monkeypatch.setattr("cogs.music._extract", extract)

    await Music.play.callback(music, _command_interaction(fake_guild, fake_text_channel), "play-01")
    await Music.playnext.callback(music, _command_interaction(fake_guild, fake_text_channel), "next-01")
    await Music.playnext.callback(music, _command_interaction(fake_guild, fake_text_channel), "next-02")

    assert [track.video_id for track in player.explicit_queue] == ["next-02", "next-01", "play-01"]
    if completion == "skip":
        await player.skip()
    await player._finish_track(player.current, "fim")
    action, selected = await player.select_next_action()

    assert action == "explicit"
    assert selected is not None and selected.video_id == "next-02"
    assert player.recommendation_task is None


@pytest.mark.asyncio
async def test_eligible_toggle_commits_before_requesting_recommendation(
    fake_guild, fake_text_channel
):
    """Validates: Requirements 1.3, 1.4, 1.7, 3.1"""
    repository = IntegrationRepository()
    player, _ = await _new_player(fake_guild, fake_text_channel, repository)
    player.state.autoplay_enabled = False
    player.state.recommendation_source = YouTubeSource(
        "manual-origem", "https://www.youtube.com/watch?v=manual-origem"
    )
    provider = StaticProvider(NoQualifiedRecommendation(), repository)
    player.recommendation_provider = provider
    controls = PlayerControls(player)
    player._now_msg = FakeMessage(view=controls)
    interaction = FakeInteraction()
    autoplay_button = next(button for button in controls.children if button.custom_id == "music:autoplay-toggle")

    await autoplay_button.callback(interaction)
    assert player.recommendation_task is not None
    await player.recommendation_task

    assert repository.events == ["serialize", "publish"]
    assert repository.published[0]["guilds"][str(fake_guild.id)]["autoplay_enabled"] is True
    assert player.state.autoplay_enabled is True
    assert provider.calls == [
        (
            player.state.recommendation_source,
            frozenset(),
            None,
            ("serialize", "publish"),
        )
    ]
    assert player._now_msg.edits == [{"view": controls}]
    assert interaction.followup.messages[0].content == "Autoplay: ligado."


@pytest.mark.asyncio
async def test_qualified_recommendation_starts_one_voice_play_and_never_enters_explicit_queue(
    fake_guild, fake_text_channel, monkeypatch
):
    """Validates: Requirements 3.3, 3.5, 4.2"""
    repository = IntegrationRepository()
    player, _ = await _new_player(fake_guild, fake_text_channel, repository, run_loop=True)
    source = YouTubeSource("manual-origem", "https://www.youtube.com/watch?v=manual-origem")
    candidate = _automatic("auto-unica")
    player.state.recommendation_source = source
    player.recommendation_provider = StaticProvider(RecommendationResult(candidate))
    monkeypatch.setattr(
        "cogs.music._extract",
        lambda *_args: {"title": candidate.title, "duration": 180, "url": "https://stream.invalid/audio"},
    )
    monkeypatch.setattr("cogs.music.discord.FFmpegPCMAudio", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("cogs.music.WatchedAudio", FakeWatchedAudio)

    try:
        assert await player.maybe_request_recommendation() is True
        await _wait_for(lambda: len(fake_guild.voice_client.play_calls) == 1)

        assert player.current is candidate
        assert player.state.recommendation_source == source
        assert player.state.automatic_history == {"auto-unica"}
        assert list(player.explicit_queue) == []
        assert await player.maybe_request_recommendation() is False
        await asyncio.sleep(0)
        assert len(fake_guild.voice_client.play_calls) == 1
    finally:
        await player.destroy(None)
        await asyncio.gather(player._task, return_exceptions=True)


def _restore_document() -> dict[str, Any]:
    source = YouTubeSource("manual-origem", "https://www.youtube.com/watch?v=manual-origem")
    return {
        "schema_version": StateRepository.SCHEMA_VERSION,
        "guilds": {
            "77": {
                "voice_channel_id": 55,
                "text_channel_id": 56,
                "current": _automatic("auto-atual").to_snapshot(),
                "explicit_queue": [_explicit("pedido-pendente").to_snapshot()],
                "autoplay_enabled": True,
                "recommendation_source": {
                    "video_id": source.video_id,
                    "webpage_url": source.webpage_url,
                },
                "automatic_history": ["auto-anterior"],
                "volume": 0.75,
                "loop_mode": "off",
            }
        },
    }


@pytest.mark.asyncio
async def test_restore_resumes_valid_snapshot_and_discards_snapshot_for_channel_without_humans(monkeypatch):
    """Validates: Requirements 7.4, 7.5, 7.6"""
    async def dormant_loop(self) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("cogs.music.discord.VoiceChannel", RestorableVoiceChannel)
    monkeypatch.setattr(MusicPlayer, "_player_loop", dormant_loop)

    async def restore_with_members(members: list[Any]):
        guild = RestorableGuild()
        voice = RestorableVoiceChannel(guild, members)
        text = FakeTextChannel(channel_id=56)
        guild.channels = {55: voice, 56: text}
        music = Music.__new__(Music)
        music.bot = RestoreBot(asyncio.get_running_loop(), guild)
        music.players = {}
        music.state_repository = RestoreRepository(_restore_document())
        music.ffmpeg = "ffmpeg"
        await music._restore_states()
        return music, voice

    restored_music, restored_voice = await restore_with_members([SimpleNamespace(bot=False)])
    restored_player = restored_music.players[77]
    try:
        assert restored_voice.connect_calls == 1
        assert restored_player.state.recommendation_source == YouTubeSource(
            "manual-origem", "https://www.youtube.com/watch?v=manual-origem"
        )
        assert restored_player.state.automatic_history == {"auto-anterior"}
        assert [track.video_id for track in restored_player.explicit_queue] == ["pedido-pendente"]
        assert restored_player._pending_automatic is not None
        assert restored_player._pending_automatic[1].video_id == "auto-atual"
    finally:
        restored_player._task.cancel()
        await asyncio.gather(restored_player._task, return_exceptions=True)

    abandoned_music, abandoned_voice = await restore_with_members([SimpleNamespace(bot=True)])
    assert abandoned_music.players == {}
    assert abandoned_voice.connect_calls == 0
