"""Fakes compartilhados para testes sem Discord, voz ou sistema de arquivos reais."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pytest


@dataclass
class FakeUser:
    id: int = 1
    display_name: str = "Membro de teste"


@dataclass
class FakeVoiceChannel:
    id: int = 10
    members: list[FakeUser] = field(default_factory=list)


@dataclass
class FakeMessage:
    content: str | None = None
    embed: Any | None = None
    view: Any | None = None
    ephemeral: bool = False
    edits: list[dict[str, Any]] = field(default_factory=list)

    async def edit(self, **kwargs: Any) -> "FakeMessage":
        self.edits.append(kwargs)
        for field_name in ("content", "embed", "view"):
            if field_name in kwargs:
                setattr(self, field_name, kwargs[field_name])
        return self


class FakeTextChannel:
    def __init__(self, channel_id: int = 20):
        self.id = channel_id
        self.sent_messages: list[FakeMessage] = []

    async def send(
        self,
        content: str | None = None,
        *,
        embed: Any | None = None,
        view: Any | None = None,
        ephemeral: bool = False,
        **_: Any,
    ) -> FakeMessage:
        message = FakeMessage(content=content, embed=embed, view=view, ephemeral=ephemeral)
        self.sent_messages.append(message)
        return message


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages)

    async def send_message(self, content: str | None = None, **kwargs: Any) -> FakeMessage:
        message = FakeMessage(content=content, ephemeral=kwargs.get("ephemeral", False))
        self.messages.append(message)
        return message

    async def defer(self, **_: Any) -> None:
        self.deferred = True


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []

    async def send(self, content: str | None = None, **kwargs: Any) -> FakeMessage:
        message = FakeMessage(content=content, ephemeral=kwargs.get("ephemeral", False))
        self.messages.append(message)
        return message


class FakeInteraction:
    def __init__(self, user: FakeUser | None = None) -> None:
        self.user = user or FakeUser()
        self.response = FakeInteractionResponse()
        self.followup = FakeFollowup()


class FakeVoiceClient:
    def __init__(self, channel: FakeVoiceChannel | None = None) -> None:
        self.channel = channel or FakeVoiceChannel()
        self.play_calls: list[tuple[Any, Callable[[Exception | None], None] | None]] = []
        self.pause_calls = 0
        self.resume_calls = 0
        self.stop_calls = 0
        self.disconnect_calls: list[bool] = []
        self._connected = True
        self._playing = False
        self._paused = False
        self._after: Callable[[Exception | None], None] | None = None

    def is_connected(self) -> bool:
        return self._connected

    def is_playing(self) -> bool:
        return self._playing

    def is_paused(self) -> bool:
        return self._paused

    def play(self, source: Any, *, after: Callable[[Exception | None], None] | None = None) -> None:
        if not self._connected:
            raise RuntimeError("Voice client is disconnected")
        if self._playing or self._paused:
            raise RuntimeError("Voice client is already playing")
        self.play_calls.append((source, after))
        self._after = after
        self._playing = True

    def pause(self) -> None:
        if self._playing:
            self.pause_calls += 1
            self._playing = False
            self._paused = True

    def resume(self) -> None:
        if self._paused:
            self.resume_calls += 1
            self._paused = False
            self._playing = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.finish()

    def finish(self, error: Exception | None = None) -> None:
        was_active = self._playing or self._paused
        after = self._after
        self._playing = False
        self._paused = False
        self._after = None
        if was_active and after is not None:
            after(error)

    async def disconnect(self, *, force: bool = False) -> None:
        self.disconnect_calls.append(force)
        self.finish()
        self._connected = False
        self.channel = None


@dataclass
class FakeGuild:
    id: int = 30
    voice_client: FakeVoiceClient | None = None


class FakeFileStore:
    """Sistema de arquivos em memória com substituição atômica injetável."""

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.write_history: list[tuple[str, str]] = []
        self.replace_history: list[tuple[str, str]] = []
        self.fail_next_write = False
        self.fail_next_replace = False

    def write_text(self, path: str, content: str) -> None:
        self.write_history.append((path, content))
        if self.fail_next_write:
            self.fail_next_write = False
            raise OSError("Injected write failure")
        self.files[path] = content

    def read_text(self, path: str) -> str:
        return self.files[path]

    def exists(self, path: str) -> bool:
        return path in self.files

    def replace(self, source: str, destination: str) -> None:
        self.replace_history.append((source, destination))
        if self.fail_next_replace:
            self.fail_next_replace = False
            raise OSError("Injected replace failure")
        self.files[destination] = self.files.pop(source)


@pytest.fixture
def fake_user() -> FakeUser:
    return FakeUser()


@pytest.fixture
def fake_voice_channel() -> FakeVoiceChannel:
    return FakeVoiceChannel()


@pytest.fixture
def fake_voice_client(fake_voice_channel: FakeVoiceChannel) -> FakeVoiceClient:
    return FakeVoiceClient(channel=fake_voice_channel)


@pytest.fixture
def fake_guild(fake_voice_client: FakeVoiceClient) -> FakeGuild:
    return FakeGuild(voice_client=fake_voice_client)


@pytest.fixture
def fake_text_channel() -> FakeTextChannel:
    return FakeTextChannel()


@pytest.fixture
def fake_interaction(fake_user: FakeUser) -> FakeInteraction:
    return FakeInteraction(user=fake_user)


@pytest.fixture
def fake_file_store() -> FakeFileStore:
    return FakeFileStore()
