"""Testes unitários das views e da transparência de controles do player."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from cogs.music import (
    Music,
    MusicPlayer,
    PersistenceError,
    PlaybackState,
    PlayerControls,
    Track,
    TrackOrigin,
)
from conftest import FakeMessage, FakeInteraction


class RecordingRepository:
    """Repositório falso que registra a preferência publicada pelo toggle."""

    def __init__(self, *, fail_publish: bool = False) -> None:
        self.fail_publish = fail_publish
        self.events: list[tuple[str, bool, bool] | tuple[str, bool]] = []
        self.player: MusicPlayer | None = None

    def serialize(self, _players: dict[int, MusicPlayer]) -> dict[str, Any]:
        assert self.player is not None
        self.events.append(("serialize", self.player.state.autoplay_enabled))
        return {"schema_version": 1, "guilds": {}}

    def publish(self, document: dict[str, Any]) -> None:
        assert self.player is not None
        prepared = document["guilds"][str(self.player.guild.id)]["autoplay_enabled"]
        self.events.append(("publish", prepared, self.player.state.autoplay_enabled))
        if self.fail_publish:
            raise PersistenceError("falha injetada")


class FakeCog:
    def __init__(self, loop: asyncio.AbstractEventLoop, repository: RecordingRepository) -> None:
        self.bot = SimpleNamespace(loop=loop)
        self.state_repository = repository
        self.players: dict[int, MusicPlayer] = {}

    def persist_states(self) -> bool:
        return True


class CommandHarness:
    def __init__(self, player: MusicPlayer) -> None:
        self.player = player

    async def _require_player(self, _interaction):
        return self.player, self.player.guild.voice_client


class EmbedInteraction:
    """Interação falsa que preserva os embeds enviados pelos comandos."""

    def __init__(self) -> None:
        self.user = SimpleNamespace(display_name="Membro de teste")
        self.response = self
        self.messages: list[FakeMessage] = []

    async def send_message(self, content: str | None = None, **kwargs: Any) -> FakeMessage:
        message = FakeMessage(content=content, ephemeral=kwargs.get("ephemeral", False))
        message.embed = kwargs.get("embed")
        self.messages.append(message)
        return message


async def _new_player(fake_guild, fake_text_channel, repository: RecordingRepository) -> MusicPlayer:
    player = MusicPlayer(FakeCog(asyncio.get_running_loop(), repository), fake_guild, fake_text_channel)
    player.cog.players[player.guild.id] = player
    repository.player = player
    player._task.cancel()
    await asyncio.gather(player._task, return_exceptions=True)
    return player


def _button(view: PlayerControls):
    matches = [child for child in view.children if child.custom_id == "music:autoplay-toggle"]
    assert len(matches) == 1
    return matches[0]


def _explicit() -> Track:
    return Track(
        url="https://www.youtube.com/watch?v=explicit-01",
        title="Pedido manual",
        duration=180,
        requested_by_id=1,
        requested_by_name="Ana",
        origin=TrackOrigin.EXPLICIT,
        provider="youtube",
        video_id="explicit-01",
    )


def _automatic() -> Track:
    return Track(
        url="https://www.youtube.com/watch?v=automatic-01",
        title="Próxima recomendação",
        duration=180,
        origin=TrackOrigin.AUTOMATIC,
        provider="youtube",
        video_id="automatic-01",
    )


def _fields(embed) -> dict[str, str]:
    return {field.name: field.value for field in embed.fields}


@pytest.mark.parametrize(
    ("enabled", "expected_label"),
    [(True, "Autoplay: ligado"), (False, "Autoplay: desligado")],
)
def test_controls_expose_exactly_one_autoplay_button_with_current_label(enabled: bool, expected_label: str):
    """Validates: Requirements 1.2"""
    player = SimpleNamespace(state=PlaybackState(autoplay_enabled=enabled))

    controls = PlayerControls(player)

    autoplay = _button(controls)
    assert autoplay.label == expected_label
    assert [child.custom_id for child in controls.children].count("music:autoplay-toggle") == 1


@pytest.mark.asyncio
async def test_autoplay_toggle_publishes_prepared_value_before_applying_and_confirms_ephemerally(
    fake_guild, fake_text_channel
):
    """Validates: Requirements 1.3, 1.4"""
    repository = RecordingRepository()
    player = await _new_player(fake_guild, fake_text_channel, repository)
    controls = PlayerControls(player)
    player._now_msg = FakeMessage(view=controls)
    interaction = FakeInteraction()

    await _button(controls).callback(interaction)

    assert repository.events == [("serialize", True), ("publish", False, True)]
    assert player.state.autoplay_enabled is False
    assert _button(controls).label == "Autoplay: desligado"
    assert player._now_msg.edits == [{"view": controls}]
    assert interaction.response.deferred is True
    assert [(message.content, message.ephemeral) for message in interaction.followup.messages] == [
        ("Autoplay: desligado.", True)
    ]


@pytest.mark.asyncio
async def test_failed_autoplay_toggle_keeps_state_and_label_and_reports_ephemeral_failure(
    fake_guild, fake_text_channel
):
    """Validates: Requirements 1.3, 1.5"""
    repository = RecordingRepository(fail_publish=True)
    player = await _new_player(fake_guild, fake_text_channel, repository)
    controls = PlayerControls(player)
    player._now_msg = FakeMessage(view=controls)
    interaction = FakeInteraction()

    await _button(controls).callback(interaction)

    assert repository.events == [("serialize", True), ("publish", False, True)]
    assert player.state.autoplay_enabled is True
    assert _button(controls).label == "Autoplay: ligado"
    assert player._now_msg.edits == []
    assert interaction.response.deferred is True
    assert len(interaction.followup.messages) == 1
    assert interaction.followup.messages[0].ephemeral is True
    assert "estado anterior foi mantido" in interaction.followup.messages[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("track", "expected_origin"),
    [(_automatic(), "Recomendação automática"), (_explicit(), "solicitação de Ana")],
)
async def test_now_playing_announcement_identifies_track_origin_and_autoplay(
    fake_guild, fake_text_channel, track: Track, expected_origin: str
):
    """Validates: Requirements 5.1, 5.2"""
    repository = RecordingRepository()
    player = await _new_player(fake_guild, fake_text_channel, repository)
    player.state.autoplay_enabled = False

    await player._announce_now_playing(track)

    message = fake_text_channel.sent_messages[-1]
    assert message.embed.title == "Tocando agora"
    assert _fields(message.embed)["Origem"] == expected_origin
    assert _fields(message.embed)["Autoplay"] == "desligado"
    assert _button(message.view).label == "Autoplay: desligado"


@pytest.mark.asyncio
async def test_nowplaying_and_queue_show_origin_autoplay_and_only_explicit_pending_tracks(
    fake_guild, fake_text_channel
):
    """Validates: Requirements 5.3, 5.4"""
    repository = RecordingRepository()
    player = await _new_player(fake_guild, fake_text_channel, repository)
    player.state.autoplay_enabled = False
    player.current = _automatic()
    player.explicit_queue = deque([_explicit()])
    commands = CommandHarness(player)

    nowplaying_interaction = EmbedInteraction()
    await Music.nowplaying.callback(commands, nowplaying_interaction)
    nowplaying_fields = _fields(nowplaying_interaction.messages[0].embed)

    queue_interaction = EmbedInteraction()
    await Music.queue.callback(commands, queue_interaction)
    queue_description = queue_interaction.messages[0].embed.description

    assert nowplaying_fields["Origem"] == "Recomendação automática"
    assert nowplaying_fields["Autoplay"] == "desligado"
    assert "Autoplay: desligado" in queue_description
    assert "Origem: Recomendação automática" in queue_description
    assert "`1.` [Pedido manual]" in queue_description
    pending_description = queue_description.split("**Proximas", maxsplit=1)[1]
    assert "Próxima recomendação" not in pending_description
