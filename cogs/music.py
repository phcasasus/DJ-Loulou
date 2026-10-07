"""Cog de musica: toca audio do YouTube via yt-dlp + FFmpeg (streaming, sem download)."""

import asyncio
import glob
import html as html_lib
import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Literal
from urllib.parse import parse_qs, urlparse

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("djloulou.music")

IDLE_TIMEOUT = 300  # segundos sem nada na fila ate o bot sair do canal
ALONE_TIMEOUT = 60  # segundos sozinho no canal de voz ate o bot sair
PLAYLIST_MAX = 100  # maximo de musicas adicionadas de uma playlist de uma vez
STALL_TIMEOUT = 15  # segundos sem o FFmpeg produzir audio ate considerar travado
LIVE_RECONNECT_MAX = 5  # falhas seguidas ao reconectar numa live antes de desistir

# Fila salva em disco para sobreviver a reinicios do bot/PC
STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "queue_state.json")

# Opcoes para resolver o stream na hora de tocar (video unico)
YTDL_PLAY_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "default_search": "ytsearch",
    "socket_timeout": 15,
    # Se o YouTube pedir login ("Sign in to confirm you're not a bot"),
    # descomente a linha abaixo trocando pelo navegador em que voce usa o YouTube logado
    # ("chrome", "edge", "firefox", ...):
    # "cookiesfrombrowser": ("chrome",),
}

# Opcoes para enfileirar: lista playlists rapido (sem resolver cada video).
# Com noplaylist=True, so um link de PLAYLIST (youtube.com/playlist?list=...) entra inteira,
# limitado a PLAYLIST_MAX musicas. Links de video com &list= sao tratados por link_da_playlist().
YTDL_QUEUE_OPTS = {**YTDL_PLAY_OPTS, "extract_flat": "in_playlist", "playlistend": PLAYLIST_MAX}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
# Live e HLS (.m3u8): as flags -reconnect valem so para download HTTP direto e podem
# prender o FFmpeg re-baixando segmento vencido. A recuperacao de live fica por conta
# do watchdog do player, que mata o processo e reconecta com um link novo.
FFMPEG_BEFORE_LIVE = None
FFMPEG_OPTS = "-vn"

COR_EMBED = discord.Color.from_rgb(255, 73, 108)


def _extract(opts: dict, query: str) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(query, download=False)


def _is_live(info: dict) -> bool:
    """True se o resultado do yt-dlp e uma transmissao ao vivo."""
    return bool(info.get("is_live")) or info.get("live_status") == "is_live"


def link_da_playlist(url: str) -> str | None:
    """Se o link for um video do YouTube com &list= de uma playlist real, retorna o link da playlist.

    O YouTube nao usa um link proprio quando voce toca uma playlist: ele abre o video
    com &list=<id> no final. Convertemos para youtube.com/playlist?list=<id> para a
    fila receber todas as musicas. IDs comecando com "RD" sao Mix/radio (lista infinita
    gerada automaticamente) e "WL" e o Watch Later: nesses casos toca so o video.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not (host.endswith("youtube.com") or host == "youtu.be"):
        return None
    list_id = parse_qs(parsed.query).get("list", [""])[0]
    if not list_id or list_id.startswith("RD") or list_id == "WL":
        return None
    return f"https://www.youtube.com/playlist?list={list_id}"


def _find_ffmpeg() -> str:
    """Localiza o ffmpeg no PATH ou na pasta padrao do winget."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    links = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe")
    if os.path.isfile(links):
        return links
    pattern = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\**\bin\ffmpeg.exe"
    )
    matches = glob.glob(pattern, recursive=True)
    if matches:
        return matches[0]
    raise RuntimeError("FFmpeg nao encontrado. Instale com: winget install Gyan.FFmpeg")


def short_title(text: str, limit: int = 60) -> str:
    """Encurta o titulo e troca colchetes (que quebram links markdown em embeds)."""
    text = text.replace("[", "(").replace("]", ")")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def fmt_duration(seconds) -> str:
    if not seconds:
        return "?"
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def _meta_tag(pagina: str, prop: str) -> str | None:
    m = re.search(rf'<meta (?:property|name)="{re.escape(prop)}" content="([^"]*)"', pagina)
    return html_lib.unescape(m.group(1)) if m else None


async def spotify_para_busca(url: str) -> tuple[str | None, str | None]:
    """Converte um link de faixa do Spotify em termo de busca no YouTube.

    O Spotify tem DRM (nao da para tocar direto), entao lemos artista + nome da faixa
    nas meta tags da pagina e buscamos o equivalente no YouTube. Retorna (busca, erro).
    """
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                final_url = str(resp.url)
                pagina = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("Falha ao acessar o Spotify (%s): %s", url, exc)
        return None, "Nao consegui acessar esse link do Spotify."
    if "/track/" not in final_url:
        return None, (
            "Do Spotify eu toco apenas links de **faixa** (open.spotify.com/track/...). "
            "Playlist/album do Spotify precisa de chave de API - use uma playlist do YouTube."
        )
    titulo = _meta_tag(pagina, "og:title")
    artista = _meta_tag(pagina, "music:musician_description") or ""
    if not titulo:
        return None, "Nao consegui identificar a faixa nesse link do Spotify."
    return f"{artista} {titulo}".strip(), None


class TrackOrigin(str, Enum):
    """Origem de uma faixa no player."""

    EXPLICIT = "explicit"
    AUTOMATIC = "automatic"


@dataclass(frozen=True)
class YouTubeSource:
    """Identidade estável de um vídeo do YouTube para recomendações."""

    video_id: str
    webpage_url: str


@dataclass(frozen=True)
class RecommendationDecision:
    """Instantâneo imutável das condições que autorizam uma recomendação."""

    source: YouTubeSource
    autoplay_enabled: bool
    explicit_queue_empty: bool
    loop_mode: str
    playback_version: int


@dataclass
class Track:
    """Faixa normalizada, sem URL temporária de stream."""

    url: str  # link da página; o stream é resolvido somente na hora de tocar
    title: str
    duration: int | None
    requested_by_id: int | None = None
    requested_by_name: str | None = None
    origin: TrackOrigin = TrackOrigin.EXPLICIT
    provider: str = "unknown"
    video_id: str | None = None
    is_live: bool = False

    @property
    def requested_by(self) -> str:
        """Compatibilidade de apresentação para os comandos existentes."""
        return self.requested_by_name or "Desconhecido"

    @property
    def youtube_source(self) -> YouTubeSource | None:
        """Retorna a fonte apenas para vídeos YouTube identificados e reproduzíveis."""
        if self.provider == "youtube" and self.video_id and self.url:
            return YouTubeSource(video_id=self.video_id, webpage_url=self.url)
        return None

    def to_snapshot(self) -> dict:
        """Representação JSON compatível enquanto a persistência é migrada."""
        payload = asdict(self)
        payload["origin"] = self.origin.value
        return payload

    @classmethod
    def from_snapshot(cls, payload: dict) -> "Track":
        """Lê snapshots legados sem assumir que fontes antigas são YouTube."""
        data = dict(payload)
        if "requested_by_name" not in data and "requested_by" in data:
            data["requested_by_name"] = data.pop("requested_by")
        origin = data.get("origin", TrackOrigin.EXPLICIT.value)
        data["origin"] = TrackOrigin(origin)
        return cls(**data)


@dataclass
class PlaybackState:
    """Estado persistível e isolado de reprodução de um servidor."""

    autoplay_enabled: bool = True
    recommendation_source: YouTubeSource | None = None
    automatic_history: set[str] = field(default_factory=set)
    current: Track | None = None
    loop_mode: Literal["off", "musica", "fila"] = "off"
    volume: float = 0.5
    playback_version: int = 0


class PersistenceError(RuntimeError):
    """Falha ao validar ou publicar um instantâneo de reprodução."""


class StateRepository:
    """Armazena snapshots completos por guild com validação e troca atômica."""

    SCHEMA_VERSION = 1
    _SNAPSHOT_FIELDS = frozenset(
        {
            "voice_channel_id",
            "text_channel_id",
            "current",
            "explicit_queue",
            "autoplay_enabled",
            "recommendation_source",
            "automatic_history",
            "volume",
            "loop_mode",
        }
    )
    _TRACK_FIELDS = frozenset(
        {
            "url",
            "title",
            "duration",
            "requested_by_id",
            "requested_by_name",
            "origin",
            "provider",
            "video_id",
            "is_live",
        }
    )

    def __init__(self, path: str = STATE_FILE):
        self.path = path

    def serialize(self, players: dict[int, "MusicPlayer"]) -> dict:
        """Serializa todos os players conectados no formato de esquema atual."""
        guilds: dict[str, dict] = {}
        for guild_id, player in players.items():
            snapshot = player.snapshot()
            if snapshot is not None:
                guilds[str(guild_id)] = snapshot
        return {"schema_version": self.SCHEMA_VERSION, "guilds": guilds}

    @classmethod
    def _validate_id(cls, value: object, field_name: str, *, nullable: bool = False) -> int | None:
        if value is None and nullable:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise PersistenceError(f"{field_name} deve ser um ID positivo")
        return value

    @classmethod
    def _validate_source(cls, payload: object) -> dict | None:
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise PersistenceError("recommendation_source deve ser um objeto ou null")
        video_id = payload.get("video_id")
        webpage_url = payload.get("webpage_url")
        if set(payload) != {"video_id", "webpage_url"} or not isinstance(video_id, str) or not video_id.strip():
            raise PersistenceError("recommendation_source.video_id inválido")
        if not isinstance(webpage_url, str) or not webpage_url.strip():
            raise PersistenceError("recommendation_source.webpage_url inválido")
        return {"video_id": video_id, "webpage_url": webpage_url}

    @classmethod
    def _validate_track(cls, payload: object, *, location: str) -> dict:
        if not isinstance(payload, dict) or set(payload) != cls._TRACK_FIELDS:
            raise PersistenceError(f"{location} contém campos ausentes ou desconhecidos")
        try:
            track = Track.from_snapshot(payload)
        except (TypeError, ValueError) as exc:
            raise PersistenceError(f"{location} é inválida: {exc}") from exc
        if not isinstance(track.url, str) or not track.url.strip():
            raise PersistenceError(f"{location}.url inválida")
        if not isinstance(track.title, str) or not track.title.strip():
            raise PersistenceError(f"{location}.title inválido")
        if track.duration is not None and (isinstance(track.duration, bool) or not isinstance(track.duration, int) or track.duration < 0):
            raise PersistenceError(f"{location}.duration inválida")
        if track.requested_by_id is not None:
            cls._validate_id(track.requested_by_id, f"{location}.requested_by_id")
        if track.requested_by_name is not None and not isinstance(track.requested_by_name, str):
            raise PersistenceError(f"{location}.requested_by_name inválido")
        if not isinstance(track.provider, str) or not track.provider.strip():
            raise PersistenceError(f"{location}.provider inválido")
        if track.video_id is not None and (not isinstance(track.video_id, str) or not track.video_id.strip()):
            raise PersistenceError(f"{location}.video_id inválido")
        if not isinstance(track.is_live, bool):
            raise PersistenceError(f"{location}.is_live inválido")
        return track.to_snapshot()

    @classmethod
    def validate(cls, document: object) -> dict:
        """Valida e normaliza o documento completo antes de publicar ou restaurar."""
        if not isinstance(document, dict):
            raise PersistenceError("documento de estado deve ser um objeto")
        if set(document) != {"schema_version", "guilds"}:
            raise PersistenceError("documento de estado contém campos ausentes ou desconhecidos")
        if document.get("schema_version") != cls.SCHEMA_VERSION:
            raise PersistenceError("versão de esquema não suportada")
        guilds = document.get("guilds")
        if not isinstance(guilds, dict):
            raise PersistenceError("guilds deve ser um objeto")

        validated_guilds: dict[str, dict] = {}
        for guild_id, snapshot in guilds.items():
            if not isinstance(guild_id, str) or not guild_id.isdecimal() or int(guild_id) <= 0:
                raise PersistenceError("ID de guild inválido")
            if not isinstance(snapshot, dict) or set(snapshot) != cls._SNAPSHOT_FIELDS:
                raise PersistenceError(f"snapshot da guild {guild_id} incompleto ou desconhecido")

            voice_channel_id = cls._validate_id(snapshot["voice_channel_id"], "voice_channel_id")
            text_channel_id = cls._validate_id(snapshot["text_channel_id"], "text_channel_id", nullable=True)
            autoplay_enabled = snapshot["autoplay_enabled"]
            if not isinstance(autoplay_enabled, bool):
                raise PersistenceError("autoplay_enabled deve ser booleano")
            source = cls._validate_source(snapshot["recommendation_source"])
            history = snapshot["automatic_history"]
            if not isinstance(history, list) or any(not isinstance(video_id, str) or not video_id.strip() for video_id in history):
                raise PersistenceError("automatic_history deve conter IDs não vazios")
            if len(history) != len(set(history)):
                raise PersistenceError("automatic_history contém IDs duplicados")
            loop_mode = snapshot["loop_mode"]
            if loop_mode not in {"off", "musica", "fila"}:
                raise PersistenceError("loop_mode inválido")
            volume = snapshot["volume"]
            if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 1.5:
                raise PersistenceError("volume inválido")

            current_payload = snapshot["current"]
            current = None if current_payload is None else cls._validate_track(current_payload, location="current")
            queue_payload = snapshot["explicit_queue"]
            if not isinstance(queue_payload, list):
                raise PersistenceError("explicit_queue deve ser uma lista")
            explicit_queue = [cls._validate_track(track, location="explicit_queue") for track in queue_payload]
            if any(track["origin"] != TrackOrigin.EXPLICIT.value for track in explicit_queue):
                raise PersistenceError("explicit_queue não pode conter faixas automáticas")
            if source is None and history:
                raise PersistenceError("automatic_history requer recommendation_source válida")
            if current is not None and current["origin"] == TrackOrigin.AUTOMATIC.value and source is None:
                raise PersistenceError("faixa automática atual requer recommendation_source válida")

            validated_guilds[guild_id] = {
                "voice_channel_id": voice_channel_id,
                "text_channel_id": text_channel_id,
                "current": current,
                "explicit_queue": explicit_queue,
                "autoplay_enabled": autoplay_enabled,
                "recommendation_source": source,
                "automatic_history": list(history),
                "volume": float(volume),
                "loop_mode": loop_mode,
            }
        return {"schema_version": cls.SCHEMA_VERSION, "guilds": validated_guilds}

    def publish(self, document: object) -> None:
        """Publica um documento validado sem expor gravações parciais ao leitor."""
        validated = self.validate(document)
        directory = os.path.dirname(os.path.abspath(self.path))
        temporary_path: str | None = None
        try:
            fd, temporary_path = tempfile.mkstemp(prefix=".queue_state-", suffix=".tmp", dir=directory, text=True)
            with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
                json.dump(validated, temporary_file, ensure_ascii=False)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except (OSError, TypeError, ValueError) as exc:
            raise PersistenceError(f"não consegui publicar o estado: {exc}") from exc
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def load(self) -> dict | None:
        """Lê somente snapshots completos e válidos; estados ruins são descartados."""
        try:
            with open(self.path, encoding="utf-8") as state_file:
                document = json.load(state_file)
            return self.validate(document)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, PersistenceError) as exc:
            log.warning("Não consegui carregar o estado persistido: %s", exc)
            return None


RECOMMENDATION_TIMEOUT = 15
RECOMMENDATION_CANDIDATE_LIMIT = 10
YTDL_RECOMMENDATION_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "playlistend": RECOMMENDATION_CANDIDATE_LIMIT,
    "socket_timeout": RECOMMENDATION_TIMEOUT,
}


@dataclass(frozen=True)
class RecommendationResult:
    """Uma candidata automática normalizada e pronta para a validação do player."""

    track: Track


@dataclass(frozen=True)
class NoQualifiedRecommendation:
    """O extractor respondeu, mas nenhuma entrada era uma candidata utilizável."""


@dataclass(frozen=True)
class RecommendationFailure:
    """A obtenção falhou sem reter detalhes sensíveis do extractor."""

    kind: str


class TrackResolver:
    """Converte metadados do yt-dlp em faixas persistíveis, sem URLs de stream.

    A URL guardada sempre representa a página da faixa. A resolução de ``info['url']``
    (temporária) continua exclusivamente em ``MusicPlayer._play_track``.
    """

    _YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

    @staticmethod
    def provider(info: dict, webpage_url: str) -> str:
        extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
        host = (urlparse(webpage_url).hostname or "").lower()
        if "youtube" in extractor or host.endswith("youtube.com") or host == "youtu.be":
            return "youtube"
        if "soundcloud" in extractor or host.endswith("soundcloud.com"):
            return "soundcloud"
        return extractor.split(":", maxsplit=1)[0] if extractor else "unknown"

    @classmethod
    def _video_id(cls, info: dict, provider: str) -> str | None:
        if provider != "youtube":
            return None
        value = info.get("id") or info.get("video_id")
        video_id = str(value).strip() if value is not None else ""
        return video_id if cls._YOUTUBE_ID_RE.fullmatch(video_id) else None

    @staticmethod
    def _page_url(info: dict, fallback_url: str | None) -> str | None:
        for value in (info.get("webpage_url"), info.get("original_url"), fallback_url):
            if not isinstance(value, str) or not value.strip():
                continue
            parsed = urlparse(value.strip())
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                return value.strip()
        return None

    @classmethod
    def from_extractor_info(
        cls,
        info: dict,
        *,
        origin: TrackOrigin,
        requested_by_id: int | None = None,
        requested_by_name: str | None = None,
        fallback_url: str | None = None,
        require_youtube: bool = False,
    ) -> Track | None:
        """Normaliza uma entrada do extractor e descarta metadados insuficientes.

        ``require_youtube`` é usado por recomendações: sem uma página e um ID de vídeo
        YouTube estáveis, a entrada não pode se tornar candidata automática.
        """
        if not isinstance(info, dict):
            return None
        webpage_url = cls._page_url(info, fallback_url)
        provider = cls.provider(info, webpage_url or "")
        video_id = cls._video_id(info, provider)
        # Entradas ``extract_flat`` de Mix podem trazer apenas o ID e o extractor.
        # Para YouTube, o ID é suficiente para construir a página canônica antes
        # da validação obrigatória de recomendações.
        if provider == "youtube" and video_id and webpage_url is None:
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"
        if require_youtube and (provider != "youtube" or video_id is None or webpage_url is None):
            return None
        if webpage_url is None:
            return None
        title = info.get("title")
        if not isinstance(title, str) or not title.strip():
            return None if require_youtube else Track(
                url=webpage_url,
                title="Sem titulo",
                duration=None,
                requested_by_id=requested_by_id,
                requested_by_name=requested_by_name,
                origin=origin,
                provider=provider,
                video_id=video_id,
                is_live=_is_live(info),
            )
        duration = info.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration < 0:
            duration = None
        else:
            duration = int(duration)
        return Track(
            url=webpage_url,
            title=title.strip(),
            duration=duration,
            requested_by_id=requested_by_id,
            requested_by_name=requested_by_name,
            origin=origin,
            provider=provider,
            video_id=video_id,
            is_live=_is_live(info),
        )

    @classmethod
    def explicit_track(
        cls, info: dict, *, requested_by_id: int, requested_by_name: str, fallback_url: str | None
    ) -> Track | None:
        return cls.from_extractor_info(
            info,
            origin=TrackOrigin.EXPLICIT,
            requested_by_id=requested_by_id,
            requested_by_name=requested_by_name,
            fallback_url=fallback_url,
        )

    @classmethod
    def recommendation_track(cls, info: dict) -> Track | None:
        return cls.from_extractor_info(info, origin=TrackOrigin.AUTOMATIC, require_youtube=True)


class RecommendationProvider:
    """Obtém uma única recomendação YouTube sem reter respostas ou streams temporários."""

    def __init__(
        self,
        *,
        extractor=None,
        resolver: type[TrackResolver] = TrackResolver,
        timeout: float = RECOMMENDATION_TIMEOUT,
        candidate_limit: int = RECOMMENDATION_CANDIDATE_LIMIT,
    ):
        self._extractor = extractor or _extract
        self._resolver = resolver
        self.timeout = timeout
        self.candidate_limit = max(1, candidate_limit)

    def _query_for(self, source: YouTubeSource) -> str | None:
        video_id = source.video_id.strip()
        if not TrackResolver._YOUTUBE_ID_RE.fullmatch(video_id):
            return None
        # A rádio/Mix deriva apenas do ID normalizado. ``start_radio=1`` instrui
        # o YouTube a materializar as recomendações, sem enfileirar o Mix inteiro.
        return f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}&start_radio=1"

    def _options(self) -> dict:
        return {**YTDL_RECOMMENDATION_OPTS, "playlistend": self.candidate_limit}

    @staticmethod
    def _entries(info: object) -> list[dict]:
        if not isinstance(info, dict):
            return []
        entries = info.get("entries")
        if isinstance(entries, list):
            return [entry for entry in entries if isinstance(entry, dict)]
        return [info]

    async def fetch_qualified(
        self,
        source: YouTubeSource,
        excluded_ids: frozenset[str],
        current_id: str | None,
    ) -> RecommendationResult | NoQualifiedRecommendation | RecommendationFailure:
        """Busca em thread e devolve só uma candidata estável e não excluída."""
        query = self._query_for(source)
        if query is None:
            log.warning("Fonte de recomendação possui video_id inválido")
            return RecommendationFailure("invalid_source")
        try:
            async with asyncio.timeout(self.timeout):
                info = await asyncio.to_thread(self._extractor, self._options(), query)
        except TimeoutError:
            log.warning("Tempo esgotado ao obter recomendação para vídeo %s", source.video_id)
            return RecommendationFailure("timeout")
        except Exception as exc:
            log.warning(
                "Falha ao obter recomendação para vídeo %s: %s",
                source.video_id,
                type(exc).__name__,
            )
            return RecommendationFailure("extractor_error")

        excluded = {video_id for video_id in excluded_ids if isinstance(video_id, str)}
        excluded.add(source.video_id)
        if current_id:
            excluded.add(current_id)
        for entry in self._entries(info)[: self.candidate_limit]:
            # A resposta flat de uma rádio do YouTube pode conter somente ``id`` e
            # ``title``. Como esta lista foi extraída de um Mix YouTube, um ID de
            # vídeo válido identifica a página canônica sem depender de metadados
            # que essa forma de resposta deliberadamente omite.
            value = entry.get("id") or entry.get("video_id")
            video_id = str(value).strip() if value is not None else ""
            candidate = entry
            if (
                TrackResolver._YOUTUBE_ID_RE.fullmatch(video_id)
                and not entry.get("webpage_url")
                and not entry.get("original_url")
            ):
                candidate = {**entry, "webpage_url": f"https://www.youtube.com/watch?v={video_id}"}
            track = self._resolver.recommendation_track(candidate)
            if track is not None and track.video_id not in excluded:
                return RecommendationResult(track)
        return NoQualifiedRecommendation()


# Compatibilidade para eventuais consumidores internos dos helpers anteriores.
def _track_provider(info: dict, url: str) -> str:
    return TrackResolver.provider(info, url)


def _track_video_id(info: dict, provider: str) -> str | None:
    return TrackResolver._video_id(info, provider)


def fmt_track_duration(track: Track) -> str:
    return "🔴 AO VIVO" if track.is_live else fmt_duration(track.duration)


def fmt_autoplay(enabled: bool) -> str:
    """Rótulo visível e uniforme para a preferência por servidor."""
    return "ligado" if enabled else "desligado"


def fmt_track_origin(track: Track) -> str:
    """Identifica se a faixa atual foi pedida por um membro ou recomendada."""
    if track.origin is TrackOrigin.AUTOMATIC:
        return "Recomendação automática"
    return f"solicitação de {track.requested_by}"


class WatchedAudio(discord.PCMVolumeTransformer):
    """Fonte de audio que registra quando o FFmpeg entregou audio pela ultima vez.

    Quando um stream engasga (comum em live), o FFmpeg pode ficar mudo sem encerrar,
    e o player ficaria esperando para sempre. O watchdog usa last_read para detectar
    isso e destravar via cleanup() - vc.stop() sozinho nao mata o processo travado.
    """

    def __init__(self, original: discord.AudioSource, volume: float):
        super().__init__(original, volume)
        self.last_read = time.monotonic()

    def read(self) -> bytes:
        data = super().read()
        if data:
            self.last_read = time.monotonic()
        return data


class PlayerControls(discord.ui.View):
    """Controles persistentes da mensagem de "Tocando agora"."""

    def __init__(self, player: "MusicPlayer"):
        super().__init__(timeout=None)
        self.player = player
        self.autoplay.label = f"Autoplay: {fmt_autoplay(player.state.autoplay_enabled)}"

    def _vc(self):
        return self.player.guild.voice_client

    @discord.ui.button(
        emoji="\N{DOUBLE VERTICAL BAR}",
        label="Pausar/Retomar",
        style=discord.ButtonStyle.secondary,
        custom_id="music:pause-resume",
    )
    async def pausar(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc()
        if vc is None:
            return await interaction.response.send_message("Nao estou no canal de voz.", ephemeral=True)
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message(f"Pausado por {interaction.user.display_name}.")
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message(f"Retomado por {interaction.user.display_name}.")
        else:
            await interaction.response.send_message("Nada tocando.", ephemeral=True)

    @discord.ui.button(
        emoji="\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}",
        label="Pular",
        style=discord.ButtonStyle.secondary,
        custom_id="music:skip",
    )
    async def pular(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc()
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            return await interaction.response.send_message("Nada tocando.", ephemeral=True)
        await self.player.skip()
        await interaction.response.send_message(f"{interaction.user.display_name} pulou a musica.")

    @discord.ui.button(
        emoji="\N{BLACK SQUARE FOR STOP}",
        label="Parar",
        style=discord.ButtonStyle.secondary,
        custom_id="music:stop",
    )
    async def parar(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc()
        if vc is None:
            return await interaction.response.send_message("Nao estou no canal de voz.", ephemeral=True)
        await self.player.stop()
        await interaction.response.send_message(f"{interaction.user.display_name} parou e limpou a fila.")

    @discord.ui.button(
        label="Autoplay: ligado",
        style=discord.ButtonStyle.secondary,
        custom_id="music:autoplay-toggle",
    )
    async def autoplay(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Alterna Autoplay somente após publicar o snapshot preparado."""
        await interaction.response.defer(ephemeral=True)
        committed, enabled = await self.player.toggle_autoplay_transactionally()
        if not committed:
            return await interaction.followup.send(
                "Não consegui salvar a preferência de Autoplay. O estado anterior foi mantido.",
                ephemeral=True,
            )

        button.label = f"Autoplay: {fmt_autoplay(enabled)}"
        if self.player._now_msg is not None:
            try:
                await self.player._now_msg.edit(view=self)
            except discord.HTTPException:
                log.warning("Não consegui atualizar o botão de Autoplay da guild %s.", self.player.guild.id)

        await interaction.followup.send(f"Autoplay: {fmt_autoplay(enabled)}.", ephemeral=True)
        if enabled:
            await self.player.maybe_request_recommendation()


class MusicPlayer:
    """Máquina de estados de reprodução isolada por servidor.

    Toda mutação que decide a próxima faixa é serializada por ``lock``. Operações
    potencialmente lentas (yt-dlp, FFmpeg e Discord) ocorrem fora dele e voltam para
    uma revalidação antes de poderem iniciar uma faixa automática.
    """

    def __init__(self, cog: "Music", guild: discord.Guild, text_channel: discord.abc.Messageable):
        self.cog = cog
        self.bot = cog.bot
        self.guild = guild
        self.text_channel = text_channel
        self.state = PlaybackState()
        self.explicit_queue: deque[Track] = deque()
        self.lock = asyncio.Lock()
        self.recommendation_provider = RecommendationProvider()
        self.recommendation_task: asyncio.Task | None = None
        self._recommendation_decision: RecommendationDecision | None = None
        self._pending_automatic: tuple[RecommendationDecision, Track] | None = None
        self._recommendation_failure_version: int | None = None
        self._last_finished: Track | None = None
        self._skipping = False
        self._expected_kill = False
        self._source: WatchedAudio | None = None
        self._last_announced: Track | None = None
        self._now_msg: discord.Message | None = None
        self._now_view: PlayerControls | None = None
        self._wakeup = asyncio.Event()
        self._song_done = asyncio.Event()
        self._task = self.bot.loop.create_task(self._player_loop())

    @property
    def current(self) -> Track | None:
        return self.state.current

    @current.setter
    def current(self, value: Track | None):
        self.state.current = value

    @property
    def volume(self) -> float:
        return self.state.volume

    @volume.setter
    def volume(self, value: float):
        self.state.volume = value

    @property
    def loop_mode(self) -> Literal["off", "musica", "fila"]:
        return self.state.loop_mode

    @loop_mode.setter
    def loop_mode(self, value: Literal["off", "musica", "fila"]):
        self.state.loop_mode = value

    def _persist(self) -> None:
        persist = getattr(self.cog, "persist_states", None)
        if callable(persist):
            persist()

    def _connected_locked(self) -> bool:
        vc = self.guild.voice_client
        return vc is not None and vc.is_connected()

    def _invalidate_recommendation_locked(self) -> None:
        """Invalida resultado pendente sem permitir uma segunda busca concorrente."""
        self._recommendation_decision = None
        self._pending_automatic = None
        task = self.recommendation_task
        if task is None:
            return
        if task.done():
            self.recommendation_task = None
        elif task is not asyncio.current_task():
            task.cancel()

    def _decision_valid_locked(self, decision: RecommendationDecision) -> bool:
        return (
            self._connected_locked()
            and self.current is None
            and self.state.autoplay_enabled == decision.autoplay_enabled
            and self.state.autoplay_enabled
            and not self.explicit_queue
            and self.loop_mode == decision.loop_mode == "off"
            and self.state.recommendation_source == decision.source
            and self.state.playback_version == decision.playback_version
        )

    def _new_recommendation_decision_locked(self) -> RecommendationDecision | None:
        if (
            self.current is not None
            or not self._connected_locked()
            or not self.state.autoplay_enabled
            or self.loop_mode != "off"
            or self.explicit_queue
            or self.state.recommendation_source is None
            or self._recommendation_failure_version == self.state.playback_version
        ):
            return None
        return RecommendationDecision(
            source=self.state.recommendation_source,
            autoplay_enabled=True,
            explicit_queue_empty=True,
            loop_mode="off",
            playback_version=self.state.playback_version,
        )

    async def enqueue_explicit(self, tracks: list[Track], priority: bool = False) -> None:
        """Enfileira solicitações de membros e invalida qualquer recomendação atrasada."""
        if any(track.origin is not TrackOrigin.EXPLICIT for track in tracks):
            raise ValueError("explicit_queue aceita apenas faixas de origem explícita")
        if not tracks:
            return
        async with self.lock:
            if priority:
                self.explicit_queue.extendleft(reversed(tracks))
            else:
                self.explicit_queue.extend(tracks)
            self.state.playback_version += 1
            self._recommendation_failure_version = None
            self._invalidate_recommendation_locked()
            self._wakeup.set()
        self._persist()

    def enqueue(self, tracks: list[Track], proxima: bool = False) -> asyncio.Task:
        """Compatibilidade temporária; novos chamadores devem aguardar enqueue_explicit."""
        if any(track.origin is not TrackOrigin.EXPLICIT for track in tracks):
            raise ValueError("explicit_queue aceita apenas faixas de origem explícita")
        return self.bot.loop.create_task(self.enqueue_explicit(tracks, priority=proxima))

    async def toggle_autoplay_transactionally(self) -> tuple[bool, bool]:
        """Publica a preferência preparada antes de alterar o estado ativo.

        O lock mantém o snapshot e a aplicação em memória como uma única transição
        para este player. A publicação é síncrona e atômica no repositório, portanto
        nenhuma outra coroutine pode observar o valor preparado como valor ativo.
        """
        async with self.lock:
            prepared = not self.state.autoplay_enabled
            try:
                repository = self.cog.state_repository
                document = repository.serialize(self.cog.players)
                snapshot = self.snapshot()
                if snapshot is None:
                    raise PersistenceError("player sem canal de voz para persistir Autoplay")
                snapshot["autoplay_enabled"] = prepared
                document["guilds"][str(self.guild.id)] = snapshot
                repository.publish(document)
            except (AttributeError, PersistenceError) as exc:
                log.warning("Não consegui salvar Autoplay da guild %s: %s", self.guild.id, exc)
                return False, self.state.autoplay_enabled

            self.state.autoplay_enabled = prepared
            self.state.playback_version += 1
            self._recommendation_failure_version = None
            self._invalidate_recommendation_locked()
            self._wakeup.set()
            return True, prepared

    async def set_autoplay(self, enabled: bool) -> None:
        """Aplica a transição em memória; a publicação transacional fica no controle UI."""
        async with self.lock:
            if self.state.autoplay_enabled == enabled:
                return
            self.state.autoplay_enabled = enabled
            self.state.playback_version += 1
            self._recommendation_failure_version = None
            self._invalidate_recommendation_locked()
            self._wakeup.set()
        self._persist()

    async def set_loop_mode(self, mode: Literal["off", "musica", "fila"]) -> None:
        async with self.lock:
            if self.loop_mode == mode:
                return
            self.loop_mode = mode
            self.state.playback_version += 1
            self._recommendation_failure_version = None
            self._invalidate_recommendation_locked()
            self._wakeup.set()
        self._persist()

    async def clear_explicit_queue(self) -> int:
        async with self.lock:
            removed = len(self.explicit_queue)
            self.explicit_queue.clear()
            if self.recommendation_task is not None or self._pending_automatic is not None:
                self.state.playback_version += 1
                self._invalidate_recommendation_locked()
            self._wakeup.set()
        self._persist()
        return removed

    async def remove_explicit_at(self, position: int) -> Track | None:
        """Remove uma posição da fila explícita sem alterar a faixa atual."""
        async with self.lock:
            if position < 1 or position > len(self.explicit_queue):
                return None
            removed = self.explicit_queue[position - 1]
            del self.explicit_queue[position - 1]
            self._wakeup.set()
        self._persist()
        return removed

    async def shuffle_explicit_queue(self) -> int:
        """Embaralha somente solicitações explícitas pendentes."""
        async with self.lock:
            size = len(self.explicit_queue)
            if size < 2:
                return size
            shuffled = list(self.explicit_queue)
            random.shuffle(shuffled)
            self.explicit_queue.clear()
            self.explicit_queue.extend(shuffled)
            self._wakeup.set()
        self._persist()
        return size

    async def set_volume(self, volume: float) -> None:
        """Atualiza o volume sem alterar decisões ou contexto de recomendação."""
        async with self.lock:
            self.volume = volume
        self._persist()

    async def skip(self) -> None:
        async with self.lock:
            self._skipping = True
            self.state.playback_version += 1
            self._recommendation_failure_version = None
            self._invalidate_recommendation_locked()
        self.stop_current()

    async def stop(self) -> None:
        async with self.lock:
            self._skipping = True
            self.current = None
            self.explicit_queue.clear()
            self.loop_mode = "off"
            self._last_finished = None
            self.state.playback_version += 1
            self._recommendation_failure_version = None
            self._invalidate_recommendation_locked()
            self._wakeup.set()
        self.stop_current()
        self._persist()

    async def select_next_action(self) -> tuple[str, Track | RecommendationDecision | tuple[RecommendationDecision, Track] | None]:
        """Seleciona sob lock: repetição, explícita, Autoplay ou inatividade."""
        async with self.lock:
            if self.current is not None:
                return "playing", None
            if self.loop_mode == "musica" and self._last_finished is not None:
                return "repeat", self._last_finished
            if self.explicit_queue:
                return "explicit", self.explicit_queue.popleft()
            # Repetição ligada bloqueia Autoplay mesmo quando não há faixa repetível.
            if self.loop_mode != "off":
                return "idle", None
            if self._pending_automatic is not None:
                pending = self._pending_automatic
                self._pending_automatic = None
                return "automatic", pending
            if self.recommendation_task is not None and not self.recommendation_task.done():
                return "waiting", None
            if self.recommendation_task is not None and self.recommendation_task.done():
                self.recommendation_task = None
            decision = self._new_recommendation_decision_locked()
            return ("recommendation", decision) if decision is not None else ("idle", None)

    async def maybe_request_recommendation(self, decision: RecommendationDecision | None = None) -> bool:
        """Inicia no máximo uma busca; a tarefa revalida o resultado antes de aceitá-lo."""
        async with self.lock:
            if self.recommendation_task is not None and not self.recommendation_task.done():
                return False
            if self.recommendation_task is not None:
                self.recommendation_task = None
            decision = decision or self._new_recommendation_decision_locked()
            if decision is None or not self._decision_valid_locked(decision):
                return False
            self._recommendation_decision = decision
            current_id = self.current.video_id if self.current else None
            excluded_ids = frozenset(self.state.automatic_history)
            self.recommendation_task = self.bot.loop.create_task(
                self._fetch_recommendation(decision, excluded_ids, current_id)
            )
            return True

    async def _fetch_recommendation(
        self,
        decision: RecommendationDecision,
        excluded_ids: frozenset[str],
        current_id: str | None,
    ) -> None:
        try:
            result = await self.recommendation_provider.fetch_qualified(
                decision.source, excluded_ids, current_id
            )
            if isinstance(result, RecommendationResult):
                await self.accept_recommendation(decision, result.track)
                return
            async with self.lock:
                valid = self._decision_valid_locked(decision) and self._recommendation_decision == decision
                if valid:
                    self._recommendation_failure_version = self.state.playback_version
            if valid:
                await self._announce("Nao encontrei uma recomendacao para continuar. Vou aguardar novas musicas.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Falha inesperada ao coordenar recomendação da guild %s: %s", self.guild.id, type(exc).__name__)
            async with self.lock:
                valid = self._decision_valid_locked(decision) and self._recommendation_decision == decision
                if valid:
                    self._recommendation_failure_version = self.state.playback_version
            if valid:
                await self._announce("Nao consegui buscar uma recomendacao. Vou aguardar novas musicas.")
        finally:
            async with self.lock:
                if self.recommendation_task is asyncio.current_task():
                    self.recommendation_task = None
                if self._recommendation_decision == decision and self._pending_automatic is None:
                    self._recommendation_decision = None
                self._wakeup.set()

    async def accept_recommendation(self, decision: RecommendationDecision, candidate: Track) -> bool:
        """Aceita uma única candidata somente se o contexto capturado ainda for válido."""
        async with self.lock:
            valid_candidate = (
                candidate.origin is TrackOrigin.AUTOMATIC
                and candidate.youtube_source is not None
                and candidate.video_id != decision.source.video_id
                and candidate.video_id not in self.state.automatic_history
            )
            if (
                not valid_candidate
                or self._recommendation_decision != decision
                or not self._decision_valid_locked(decision)
                or self._pending_automatic is not None
            ):
                return False
            self._pending_automatic = (decision, candidate)
            self._wakeup.set()
            return True

    def snapshot(self) -> dict | None:
        """Estado serializável para retomar a fila após um reinício."""
        vc = self.guild.voice_client
        if vc is None or vc.channel is None:
            return None
        return {
            "voice_channel_id": vc.channel.id,
            "text_channel_id": getattr(self.text_channel, "id", None),
            "current": self.current.to_snapshot() if self.current else None,
            "explicit_queue": [track.to_snapshot() for track in self.explicit_queue],
            "autoplay_enabled": self.state.autoplay_enabled,
            "recommendation_source": asdict(self.state.recommendation_source)
            if self.state.recommendation_source else None,
            "automatic_history": sorted(self.state.automatic_history),
            "volume": self.volume,
            "loop_mode": self.loop_mode,
        }

    def stop_current(self):
        """Interrompe a fonte de áudio; a transição de estado é feita pelos métodos async."""
        vc = self.guild.voice_client
        if vc:
            vc.stop()
        if self._source is not None:
            self._expected_kill = True
            self._source.cleanup()

    async def _mark_track_started_locked(self, track: Track) -> None:
        if self.current is track:  # reconexão de live: não altera contexto novamente
            return
        self.current = track
        if track.origin is TrackOrigin.EXPLICIT:
            source = track.youtube_source
            if source is not None:
                self.state.recommendation_source = source
                self.state.automatic_history.clear()
                self.state.playback_version += 1
                self._recommendation_failure_version = None
                self._invalidate_recommendation_locked()
        elif track.video_id:
            self.state.automatic_history.add(track.video_id)
            self._recommendation_decision = None

    async def _finish_track(self, track: Track, result: str) -> None:
        async with self.lock:
            skipped = self._skipping
            if self.current is track:
                self.current = None
            if result in {"fim", "travou"} and not skipped:
                self._last_finished = track
                if self.loop_mode == "fila" and track.origin is TrackOrigin.EXPLICIT:
                    self.explicit_queue.append(track)
            else:
                self._last_finished = None
            if result == "erro" and track.origin is TrackOrigin.AUTOMATIC:
                self._recommendation_failure_version = self.state.playback_version
            self._skipping = False
            self._wakeup.set()
        self._persist()

    async def _wait_for_wakeup(self) -> bool:
        async with self.lock:
            self._wakeup.clear()
        try:
            async with asyncio.timeout(IDLE_TIMEOUT):
                await self._wakeup.wait()
            return True
        except TimeoutError:
            await self.destroy("Fila vazia ha um tempo, sai do canal. Ate mais!")
            return False

    async def _should_retry_live(self, track: Track, result: str) -> bool:
        async with self.lock:
            return (
                result in {"fim", "travou"}
                and track.is_live
                and not self._skipping
                and self._connected_locked()
            )

    async def _player_loop(self):
        while True:
            action, payload = await self.select_next_action()
            if action == "recommendation":
                await self.maybe_request_recommendation(payload if isinstance(payload, RecommendationDecision) else None)
                continue
            if action in {"idle", "waiting", "playing"}:
                if not await self._wait_for_wakeup():
                    return
                continue

            decision: RecommendationDecision | None = None
            if action == "automatic":
                decision, track = payload  # type: ignore[misc]
            else:
                track = payload  # type: ignore[assignment]
            if not isinstance(track, Track):
                continue

            async with self.lock:
                self._skipping = False
            started = time.monotonic()
            result = await self._play_track(track, anunciar=True, decision=decision)

            attempts = 0
            while await self._should_retry_live(track, result):
                if time.monotonic() - started > 60:
                    attempts = 0
                attempts += 1
                if attempts > LIVE_RECONNECT_MAX:
                    await self._announce(
                        f"A live **{short_title(track.title)}** parece ter encerrado ou caido. Parei de reconectar."
                    )
                    break
                log.info("Live '%s' parou (%s); reconectando (tentativa %d).", track.title, result, attempts)
                await asyncio.sleep(2 * attempts)
                started = time.monotonic()
                result = await self._play_track(track, anunciar=False)

            if result == "sem_voz":
                await self.destroy(None)
                return
            await self._finish_track(track, result)

    async def _play_track(
        self, track: Track, anunciar: bool, decision: RecommendationDecision | None = None
    ) -> str:
        """Resolve um stream fora do lock e valida a transição antes de ``voice.play``."""
        try:
            info = await asyncio.to_thread(_extract, YTDL_PLAY_OPTS, track.url)
            if "entries" in info:
                info = info["entries"][0]
        except Exception as exc:
            await self._announce(f"Nao consegui tocar **{short_title(track.title)}**, pulando. (`{exc}`)")
            return "erro"

        track.title = info.get("title") or track.title
        track.duration = info.get("duration") or track.duration
        track.is_live = _is_live(info)
        stream_url = info.get("url")
        if not isinstance(stream_url, str) or not stream_url:
            await self._announce(f"Nao consegui tocar **{short_title(track.title)}**, pulando.")
            return "erro"

        try:
            source = WatchedAudio(
                discord.FFmpegPCMAudio(
                    stream_url,
                    executable=self.cog.ffmpeg,
                    before_options=FFMPEG_BEFORE_LIVE if track.is_live else FFMPEG_BEFORE,
                    options=FFMPEG_OPTS,
                ),
                volume=self.volume,
            )
        except Exception as exc:
            log.warning("Não consegui preparar áudio para '%s': %s", track.title, type(exc).__name__)
            await self._announce(f"Nao consegui tocar **{short_title(track.title)}**, pulando.")
            return "erro"

        async with self.lock:
            if decision is not None and not self._decision_valid_locked(decision):
                source.cleanup()
                return "discarded"
            vc = self.guild.voice_client
            if vc is None or not vc.is_connected():
                source.cleanup()
                return "sem_voz"
            self._source = source
            self._song_done.clear()
            self._expected_kill = False
            try:
                vc.play(source, after=self._on_song_end)
            except Exception as exc:
                self._source = None
                source.cleanup()
                log.warning("Não consegui iniciar '%s': %s", track.title, type(exc).__name__)
                return "erro"
            await self._mark_track_started_locked(track)

        if anunciar and not (self.loop_mode == "musica" and track is self._last_announced):
            self._last_announced = track
            await self._announce_now_playing(track)

        try:
            while True:
                try:
                    async with asyncio.timeout(STALL_TIMEOUT):
                        await self._song_done.wait()
                    return "fim"
                except TimeoutError:
                    vc = self.guild.voice_client
                    if vc is not None and vc.is_paused():
                        source.last_read = time.monotonic()
                        continue
                    if time.monotonic() - source.last_read < STALL_TIMEOUT:
                        continue
                    log.warning("FFmpeg mudo ha %ds em '%s'; matando o processo.", STALL_TIMEOUT, track.title)
                    self._expected_kill = True
                    source.cleanup()
                    await self._song_done.wait()
                    return "travou"
        finally:
            if self._source is source:
                self._source = None

    def _on_song_end(self, error):
        if error:
            if self._expected_kill:
                log.info("FFmpeg encerrado de proposito (skip/stop/watchdog): %s", error)
            else:
                log.error("Erro na reproducao: %s", error)
        self.bot.loop.call_soon_threadsafe(self._song_done.set)

    async def _announce(self, message: str):
        try:
            await self.text_channel.send(message)
        except discord.HTTPException:
            pass

    async def _clear_controls(self):
        """Remove os botoes da mensagem de 'Tocando agora' anterior."""
        if self._now_view is not None:
            self._now_view.stop()
            self._now_view = None
        if self._now_msg is not None:
            try:
                await self._now_msg.edit(view=None)
            except discord.HTTPException:
                pass
            self._now_msg = None

    async def _announce_now_playing(self, track: Track):
        await self._clear_controls()
        embed = discord.Embed(
            title="Tocando agora",
            description=f"[{short_title(track.title)}]({track.url})",
            color=COR_EMBED,
        )
        embed.add_field(name="Duracao", value=fmt_track_duration(track))
        embed.add_field(name="Origem", value=fmt_track_origin(track), inline=False)
        embed.add_field(name="Autoplay", value=fmt_autoplay(self.state.autoplay_enabled))
        self._now_view = PlayerControls(self)
        try:
            self._now_msg = await self.text_channel.send(embed=embed, view=self._now_view)
        except discord.HTTPException:
            self._now_msg = None

    async def destroy(self, message: str | None):
        """Invalida transições, limpa estado transitório e desconecta o player."""
        async with self.lock:
            self.cog.players.pop(self.guild.id, None)
            self.explicit_queue.clear()
            self.current = None
            self._last_finished = None
            self.state.recommendation_source = None
            self.state.automatic_history.clear()
            self.state.playback_version += 1
            self._recommendation_failure_version = None
            self._invalidate_recommendation_locked()
            self._wakeup.set()
        self._persist()
        await self._clear_controls()
        if self._source is not None:
            self._expected_kill = True
            self._source.cleanup()
        vc = self.guild.voice_client
        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
        if message:
            await self._announce(message)
        if self._task is not asyncio.current_task():
            self._task.cancel()


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, MusicPlayer] = {}
        self.state_repository = StateRepository()
        self.ffmpeg = _find_ffmpeg()
        self._restored = False

    # ---------- persistencia da fila ----------

    def persist_states(self) -> bool:
        """Serializa e publica todos os estados de player em uma única transação."""
        try:
            self.state_repository.publish(self.state_repository.serialize(self.players))
        except PersistenceError as exc:
            log.warning("Não consegui salvar a fila em disco: %s", exc)
            return False
        return True

    async def _restore_states(self):
        """Restaura snapshots completos sem expor estados parcialmente aplicados."""
        document = self.state_repository.load()
        if document is None:
            return

        for guild_id, raw_snapshot in document.get("guilds", {}).items():
            # Mesmo que ``load`` já valide o documento, validar cada unidade aqui evita
            # que um repositório alternativo ou uma mudança futura mutem um player com
            # um snapshot parcial.
            try:
                snapshot = StateRepository.validate(
                    {
                        "schema_version": StateRepository.SCHEMA_VERSION,
                        "guilds": {guild_id: raw_snapshot},
                    }
                )["guilds"][guild_id]
                current = (
                    None
                    if snapshot["current"] is None
                    else Track.from_snapshot(snapshot["current"])
                )
                queued_tracks = [
                    Track.from_snapshot(track_payload)
                    for track_payload in snapshot["explicit_queue"]
                ]
            except (KeyError, TypeError, ValueError, PersistenceError) as exc:
                log.warning("Snapshot da guild %s ignorado durante a restauração: %s", guild_id, exc)
                continue

            guild = self.bot.get_guild(int(guild_id))
            if guild is None or guild.voice_client is not None:
                continue
            channel = guild.get_channel(snapshot["voice_channel_id"])
            if not isinstance(channel, discord.VoiceChannel) or all(member.bot for member in channel.members):
                log.info("Fila salva do servidor %s descartada (canal vazio ou inexistente).", guild_id)
                continue

            source_payload = snapshot["recommendation_source"]
            source = None if source_payload is None else YouTubeSource(**source_payload)
            restored_current: tuple[RecommendationDecision | None, Track] | None = None

            if source is None:
                # Sem contexto YouTube não há automática recuperável. Reaproveitamos
                # somente pedidos explícitos e deixamos o player aplicar a inatividade
                # normal caso não reste trabalho.
                restored_state = PlaybackState()
                if current is not None and current.origin is TrackOrigin.EXPLICIT:
                    queued_tracks.insert(0, current)
            else:
                restored_state = PlaybackState(
                    autoplay_enabled=snapshot["autoplay_enabled"],
                    recommendation_source=source,
                    automatic_history=set(snapshot["automatic_history"]),
                    loop_mode=snapshot["loop_mode"],
                    volume=snapshot["volume"],
                )
                if current is not None:
                    if current.origin is TrackOrigin.EXPLICIT:
                        # A faixa interrompida volta à cabeça da fila explícita.
                        queued_tracks.insert(0, current)
                    else:
                        # Automáticas não entram na fila explícita. A ausência de uma
                        # decisão indica uma retomada já autorizada, inclusive se o
                        # Autoplay foi desligado enquanto ela tocava.
                        restored_current = (None, current)

            try:
                await channel.connect(self_deaf=True)
            except discord.DiscordException as exc:
                log.warning("Nao consegui voltar ao canal de voz do servidor %s: %s", guild_id, exc)
                continue

            text = guild.get_channel(snapshot["text_channel_id"] or 0) or channel
            player = MusicPlayer(self, guild, text)
            async with player.lock:
                # Todos os campos observáveis são publicados de uma vez antes de o
                # loop poder selecionar uma faixa ou solicitar recomendação.
                player.state = restored_state
                player.explicit_queue = deque(queued_tracks)
                player._pending_automatic = restored_current
                player._recommendation_decision = None
                self.players[guild.id] = player
                player._wakeup.set()

            restored_count = len(queued_tracks) + (1 if restored_current is not None else 0)
            log.info("Fila do servidor %s retomada com %d musica(s).", guild_id, restored_count)
            try:
                await text.send(f"Voltei! Retomando a fila de onde parou ({restored_count} musica(s)).")
            except discord.HTTPException:
                pass

    # ---------- helpers ----------

    def _player_for(self, interaction: discord.Interaction) -> MusicPlayer:
        player = self.players.get(interaction.guild.id)
        if player is None:
            player = MusicPlayer(self, interaction.guild, interaction.channel)
            self.players[interaction.guild.id] = player
        return player

    async def _require_player(self, interaction: discord.Interaction):
        """Retorna (player, voice_client) ou responde com erro e retorna (None, None)."""
        if interaction.guild is None:
            await interaction.response.send_message("Use este comando dentro do servidor.", ephemeral=True)
            return None, None
        vc = interaction.guild.voice_client
        player = self.players.get(interaction.guild.id)
        if vc is None or player is None:
            await interaction.response.send_message("Nao estou tocando nada agora.", ephemeral=True)
            return None, None
        return player, vc

    async def _handle_play(self, interaction: discord.Interaction, busca: str, proxima: bool):
        if interaction.guild is None:
            return await interaction.response.send_message("Use este comando dentro do servidor.", ephemeral=True)
        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None:
            return await interaction.response.send_message("Entre em um canal de voz primeiro.", ephemeral=True)

        await interaction.response.defer()

        if "open.spotify.com" in busca or "spotify.link" in busca:
            termo, erro = await spotify_para_busca(busca)
            if erro:
                return await interaction.followup.send(erro)
            busca = termo  # vira busca por nome no YouTube

        vc = interaction.guild.voice_client
        if vc is None:
            await voice.channel.connect(self_deaf=True)
        elif vc.channel != voice.channel:
            await vc.move_to(voice.channel)

        if busca.startswith(("http://", "https://")):
            playlist = link_da_playlist(busca)
            if playlist:
                busca = playlist

        try:
            info = await asyncio.to_thread(_extract, YTDL_QUEUE_OPTS, busca)
        except yt_dlp.utils.DownloadError as exc:
            return await interaction.followup.send(f"Nao consegui acessar isso. (`{exc}`)")

        requester_id = interaction.user.id
        requester_name = interaction.user.display_name
        is_url = busca.startswith(("http://", "https://"))
        tracks: list[Track] = []

        def make_explicit_track(entry: dict, fallback_url: str | None) -> Track | None:
            return TrackResolver.explicit_track(
                entry,
                requested_by_id=requester_id,
                requested_by_name=requester_name,
                fallback_url=fallback_url,
            )

        if info and "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not is_url:
                entries = entries[:1]  # busca por nome: usa o primeiro resultado
            for entry in entries:
                track = make_explicit_track(entry, entry.get("webpage_url"))
                if track is not None:
                    tracks.append(track)
        elif info:
            track = make_explicit_track(info, info.get("webpage_url") or busca)
            if track is not None:
                tracks.append(track)

        if not tracks:
            return await interaction.followup.send("Nao encontrei nada com isso.")

        player = self._player_for(interaction)
        player.text_channel = interaction.channel
        await player.enqueue_explicit(tracks, priority=proxima)

        if len(tracks) > 1:
            desc = f"**{len(tracks)}** musicas de [{short_title(info.get('title') or 'playlist')}]({busca})"
            if len(tracks) >= PLAYLIST_MAX:
                desc += f"\n(limite de {PLAYLIST_MAX} musicas por playlist)"
            titulo = "Playlist fura-fila: toca em seguida" if proxima else "Playlist adicionada a fila"
            embed = discord.Embed(title=titulo, description=desc, color=COR_EMBED)
        else:
            t = tracks[0]
            embed = discord.Embed(
                title="Proxima a tocar (fura-fila)" if proxima else "Adicionada a fila",
                description=f"[{short_title(t.title)}]({t.url})",
                color=COR_EMBED,
            )
            embed.add_field(name="Duracao", value=fmt_track_duration(t))
            embed.add_field(name="Posicao na fila", value="1" if proxima else str(len(player.explicit_queue)))
        await interaction.followup.send(embed=embed)

    # ---------- comandos ----------

    @app_commands.command(name="play", description="Toca musica: YouTube/SoundCloud (link ou playlist), faixa do Spotify, ou nome para buscar")
    @app_commands.describe(busca="Link (YouTube, SoundCloud, faixa do Spotify, playlist) ou nome da musica")
    async def play(self, interaction: discord.Interaction, busca: str):
        await self._handle_play(interaction, busca, proxima=False)

    @app_commands.command(name="playnext", description="Igual ao /play, mas fura a fila: toca logo apos a musica atual")
    @app_commands.describe(busca="Link (YouTube, SoundCloud, faixa do Spotify, playlist) ou nome da musica")
    async def playnext(self, interaction: discord.Interaction, busca: str):
        await self._handle_play(interaction, busca, proxima=True)

    @app_commands.command(name="pause", description="Pausa a musica atual")
    async def pause(self, interaction: discord.Interaction):
        player, vc = await self._require_player(interaction)
        if not player:
            return
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("Pausado.")
        else:
            await interaction.response.send_message("Nada tocando para pausar.", ephemeral=True)

    @app_commands.command(name="resume", description="Retoma a musica pausada")
    async def resume(self, interaction: discord.Interaction):
        player, vc = await self._require_player(interaction)
        if not player:
            return
        if vc.is_paused():
            vc.resume()
            await interaction.response.send_message("Retomando.")
        else:
            await interaction.response.send_message("Nao esta pausado.", ephemeral=True)

    @app_commands.command(name="skip", description="Pula para a proxima musica da fila")
    async def skip(self, interaction: discord.Interaction):
        player, vc = await self._require_player(interaction)
        if not player:
            return
        if not vc.is_playing() and not vc.is_paused():
            return await interaction.response.send_message("Nada tocando para pular.", ephemeral=True)
        await player.skip()
        await interaction.response.send_message("Pulando...")

    @app_commands.command(name="stop", description="Para de tocar e limpa a fila (o bot continua no canal)")
    async def stop(self, interaction: discord.Interaction):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        await player.stop()
        await interaction.response.send_message("Parei e limpei a fila.")

    @app_commands.command(name="queue", description="Mostra a fila de musicas")
    async def queue(self, interaction: discord.Interaction):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        linhas = [f"Autoplay: {fmt_autoplay(player.state.autoplay_enabled)}"]
        if player.current:
            current = player.current
            linhas.append(
                f"**Tocando agora:** [{short_title(current.title)}]({current.url}) "
                f"`{fmt_track_duration(current)}`\nOrigem: {fmt_track_origin(current)}"
            )
        if player.explicit_queue:
            linhas.append(f"\n**Proximas ({len(player.explicit_queue)}):**")
            for i, track in enumerate(list(player.explicit_queue)[:10], start=1):
                linhas.append(f"`{i}.` [{short_title(track.title)}]({track.url}) `{fmt_track_duration(track)}`")
            resto = len(player.explicit_queue) - 10
            if resto > 0:
                linhas.append(f"... e mais {resto} musica(s)")
        elif player.current is None:
            linhas.append("Fila vazia.")
        embed = discord.Embed(title="Fila", description="\n".join(linhas), color=COR_EMBED)
        if player.loop_mode != "off":
            embed.set_footer(text=f"Repeticao: {player.loop_mode}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="clear", description="Limpa a fila (a musica atual continua tocando)")
    async def clear(self, interaction: discord.Interaction):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        removidas = await player.clear_explicit_queue()
        await interaction.response.send_message(f"Fila limpa: {removidas} musica(s) removida(s).")

    @app_commands.command(name="remove", description="Remove uma musica da fila pela posicao")
    @app_commands.describe(posicao="Posicao na fila (veja /queue)")
    async def remove(self, interaction: discord.Interaction, posicao: app_commands.Range[int, 1]):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        removida = await player.remove_explicit_at(posicao)
        if removida is None:
            return await interaction.response.send_message(
                f"A fila tem apenas {len(player.explicit_queue)} musica(s).", ephemeral=True
            )
        await interaction.response.send_message(f"Removida: **{short_title(removida.title)}**")

    @app_commands.command(name="shuffle", description="Embaralha a fila")
    async def shuffle(self, interaction: discord.Interaction):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        size = await player.shuffle_explicit_queue()
        if size < 2:
            return await interaction.response.send_message("Fila pequena demais para embaralhar.", ephemeral=True)
        await interaction.response.send_message(f"Fila embaralhada ({size} musicas).")

    @app_commands.command(name="loop", description="Repete a musica atual, a fila toda, ou desliga a repeticao")
    @app_commands.describe(modo="O que repetir")
    @app_commands.choices(modo=[
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="musica", value="musica"),
        app_commands.Choice(name="fila", value="fila"),
    ])
    async def loop(self, interaction: discord.Interaction, modo: app_commands.Choice[str]):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        await player.set_loop_mode(modo.value)
        msg = {
            "off": "Repeticao desligada.",
            "musica": "Repetindo a musica atual.",
            "fila": "Repetindo a fila toda.",
        }[modo.value]
        await interaction.response.send_message(msg)

    @app_commands.command(name="volume", description="Ajusta o volume (0 a 150, padrao 50)")
    @app_commands.describe(nivel="Volume em porcentagem")
    async def volume(self, interaction: discord.Interaction, nivel: app_commands.Range[int, 0, 150]):
        player, vc = await self._require_player(interaction)
        if not player:
            return
        await player.set_volume(nivel / 100)
        source = getattr(vc, "source", None)
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = player.volume
        await interaction.response.send_message(f"Volume: {nivel}%")

    @app_commands.command(name="nowplaying", description="Mostra a musica que esta tocando")
    async def nowplaying(self, interaction: discord.Interaction):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        if not player.current:
            return await interaction.response.send_message("Nada tocando agora.", ephemeral=True)
        track = player.current
        embed = discord.Embed(
            title="Tocando agora",
            description=f"[{short_title(track.title)}]({track.url})",
            color=COR_EMBED,
        )
        embed.add_field(name="Duracao", value=fmt_track_duration(track))
        embed.add_field(name="Origem", value=fmt_track_origin(track), inline=False)
        embed.add_field(name="Autoplay", value=fmt_autoplay(player.state.autoplay_enabled))
        embed.add_field(name="Volume", value=f"{int(player.volume * 100)}%")
        if player.loop_mode != "off":
            embed.set_footer(text=f"Repeticao: {player.loop_mode}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leave", description="Faz o bot sair do canal de voz")
    async def leave(self, interaction: discord.Interaction):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        await interaction.response.send_message("Saindo. Ate mais!")
        await player.destroy(None)

    @app_commands.command(name="help", description="Lista todos os comandos do bot")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Comandos do bot", color=COR_EMBED)
        for cmd in sorted(self.get_app_commands(), key=lambda c: c.name):
            embed.add_field(name=f"/{cmd.name}", value=cmd.description, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- eventos ----------

    @commands.Cog.listener()
    async def on_ready(self):
        if self._restored:
            return
        self._restored = True
        await self._restore_states()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        """Sai do canal se ficar sozinho por ALONE_TIMEOUT segundos."""
        if member.bot:
            return
        vc = member.guild.voice_client
        if vc is None or vc.channel is None:
            return
        if before.channel == vc.channel and after.channel != vc.channel:
            if all(m.bot for m in vc.channel.members):
                await asyncio.sleep(ALONE_TIMEOUT)
                vc = member.guild.voice_client
                if vc and vc.channel and all(m.bot for m in vc.channel.members):
                    player = self.players.get(member.guild.id)
                    if player:
                        await player.destroy("Fiquei sozinho no canal, entao sai.")
                    else:
                        await vc.disconnect(force=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
