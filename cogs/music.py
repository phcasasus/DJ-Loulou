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
import time
from collections import deque
from dataclasses import asdict, dataclass
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


@dataclass
class Track:
    url: str  # link da pagina do video (o stream e resolvido de novo na hora de tocar)
    title: str
    duration: int | None
    requested_by: str
    is_live: bool = False


def fmt_track_duration(track: Track) -> str:
    return "🔴 AO VIVO" if track.is_live else fmt_duration(track.duration)


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
    """Botoes na mensagem de "Tocando agora": pausar/retomar, pular e parar."""

    def __init__(self, player: "MusicPlayer"):
        super().__init__(timeout=None)
        self.player = player

    def _vc(self):
        return self.player.guild.voice_client

    @discord.ui.button(emoji="\N{DOUBLE VERTICAL BAR}", label="Pausar/Retomar", style=discord.ButtonStyle.secondary)
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

    @discord.ui.button(emoji="\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR}", label="Pular", style=discord.ButtonStyle.secondary)
    async def pular(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc()
        if vc is None or (not vc.is_playing() and not vc.is_paused()):
            return await interaction.response.send_message("Nada tocando.", ephemeral=True)
        self.player._skipping = True
        self.player.stop_current()
        await interaction.response.send_message(f"{interaction.user.display_name} pulou a musica.")

    @discord.ui.button(emoji="\N{BLACK SQUARE FOR STOP}", label="Parar", style=discord.ButtonStyle.secondary)
    async def parar(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self._vc()
        if vc is None:
            return await interaction.response.send_message("Nao estou no canal de voz.", ephemeral=True)
        self.player.queue.clear()
        self.player.loop_mode = "off"
        self.player._skipping = True
        self.player.stop_current()
        self.player.cog.save_states()
        await interaction.response.send_message(f"{interaction.user.display_name} parou e limpou a fila.")


class MusicPlayer:
    """Fila e reproducao de um servidor (guild)."""

    def __init__(self, cog: "Music", guild: discord.Guild, text_channel: discord.abc.Messageable):
        self.cog = cog
        self.bot = cog.bot
        self.guild = guild
        self.text_channel = text_channel
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.volume = 0.5
        self.loop_mode = "off"  # off | musica | fila
        self._skipping = False
        self._expected_kill = False  # matamos o FFmpeg de proposito: nao logar como erro
        self._source: WatchedAudio | None = None
        self._last_announced: Track | None = None
        self._now_msg: discord.Message | None = None
        self._now_view: PlayerControls | None = None
        self._wakeup = asyncio.Event()
        self._song_done = asyncio.Event()
        self._task = self.bot.loop.create_task(self._player_loop())

    def enqueue(self, tracks: list[Track], proxima: bool = False):
        if proxima:
            self.queue.extendleft(reversed(tracks))
        else:
            self.queue.extend(tracks)
        self._wakeup.set()
        self.cog.save_states()

    def snapshot(self) -> dict | None:
        """Estado serializavel para retomar a fila apos um reinicio."""
        vc = self.guild.voice_client
        if vc is None or vc.channel is None:
            return None
        return {
            "voice_channel_id": vc.channel.id,
            "text_channel_id": getattr(self.text_channel, "id", None),
            "current": asdict(self.current) if self.current else None,
            "queue": [asdict(t) for t in self.queue],
            "volume": self.volume,
            "loop_mode": self.loop_mode,
        }

    def stop_current(self):
        """Para a faixa atual de verdade: mata o FFmpeg alem do vc.stop().

        Se o FFmpeg estiver travado (live que engasgou), vc.stop() sozinho nao o
        encerra - a thread de audio fica presa lendo um processo mudo para sempre.
        """
        vc = self.guild.voice_client
        if vc:
            vc.stop()
        if self._source is not None:
            self._expected_kill = True
            self._source.cleanup()

    async def _player_loop(self):
        while True:
            while not self.queue:
                self._wakeup.clear()
                try:
                    async with asyncio.timeout(IDLE_TIMEOUT):
                        await self._wakeup.wait()
                except TimeoutError:
                    await self.destroy("Fila vazia ha um tempo, sai do canal. Ate mais!")
                    return

            track = self.queue.popleft()
            self.current = track
            self.cog.save_states()
            self._skipping = False

            inicio = time.monotonic()
            resultado = await self._play_track(track, anunciar=True)

            # Live engasga/cai de tempos em tempos: reconecta sozinho com um link
            # novo, sem reanunciar. Se falhar varias vezes seguidas, desiste.
            tentativas = 0
            while (
                resultado in ("fim", "travou")
                and track.is_live
                and not self._skipping
                and self.guild.voice_client is not None
            ):
                if time.monotonic() - inicio > 60:
                    tentativas = 0  # tocou um bom tempo: zera a conta de falhas seguidas
                tentativas += 1
                if tentativas > LIVE_RECONNECT_MAX:
                    await self._announce(
                        f"A live **{short_title(track.title)}** parece ter encerrado ou caido. Parei de reconectar."
                    )
                    break
                log.info("Live '%s' parou (%s); reconectando (tentativa %d).", track.title, resultado, tentativas)
                await asyncio.sleep(2 * tentativas)
                if self._skipping or self.guild.voice_client is None:
                    break
                inicio = time.monotonic()
                resultado = await self._play_track(track, anunciar=False)

            if resultado == "sem_voz":
                await self.destroy(None)
                return

            if resultado != "erro":
                if self.loop_mode == "musica" and not self._skipping:
                    self.queue.appendleft(track)
                elif self.loop_mode == "fila":
                    self.queue.append(track)
            self.current = None
            self.cog.save_states()

    async def _play_track(self, track: Track, anunciar: bool) -> str:
        """Resolve o stream e toca a faixa ate o fim.

        Retorna "fim" (terminou/foi parada), "travou" (FFmpeg ficou mudo e o watchdog
        o matou), "erro" (nao resolveu o stream) ou "sem_voz" (bot fora do canal).
        """
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
        stream_url = info["url"]

        vc = self.guild.voice_client
        if vc is None or not vc.is_connected():
            return "sem_voz"

        source = WatchedAudio(
            discord.FFmpegPCMAudio(
                stream_url,
                executable=self.cog.ffmpeg,
                before_options=FFMPEG_BEFORE_LIVE if track.is_live else FFMPEG_BEFORE,
                options=FFMPEG_OPTS,
            ),
            volume=self.volume,
        )
        self._source = source
        self._song_done.clear()
        self._expected_kill = False
        vc.play(source, after=self._on_song_end)

        if anunciar and not (self.loop_mode == "musica" and track is self._last_announced):
            self._last_announced = track
            await self._announce_now_playing(track)

        # Espera a faixa acabar, checando periodicamente se o FFmpeg travou
        # (processo vivo, mas sem produzir audio ha STALL_TIMEOUT segundos).
        try:
            while True:
                try:
                    async with asyncio.timeout(STALL_TIMEOUT):
                        await self._song_done.wait()
                    return "fim"
                except TimeoutError:
                    vc = self.guild.voice_client
                    if vc is not None and vc.is_paused():
                        source.last_read = time.monotonic()  # pausa nao e travamento
                        continue
                    if time.monotonic() - source.last_read < STALL_TIMEOUT:
                        continue
                    log.warning("FFmpeg mudo ha %ds em '%s'; matando o processo.", STALL_TIMEOUT, track.title)
                    self._expected_kill = True
                    source.cleanup()  # destrava a thread de audio e dispara o after
                    await self._song_done.wait()
                    return "travou"
        finally:
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
        embed.add_field(name="Pedida por", value=track.requested_by)
        self._now_view = PlayerControls(self)
        try:
            self._now_msg = await self.text_channel.send(embed=embed, view=self._now_view)
        except discord.HTTPException:
            self._now_msg = None

    async def destroy(self, message: str | None):
        """Desconecta, limpa o estado e encerra o loop de reproducao."""
        self.cog.players.pop(self.guild.id, None)
        self.queue.clear()
        self.current = None
        self.cog.save_states()
        await self._clear_controls()
        if self._source is not None:
            self._expected_kill = True
            self._source.cleanup()  # mata o FFmpeg mesmo se estiver travado
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
        self.ffmpeg = _find_ffmpeg()
        self._restored = False

    # ---------- persistencia da fila ----------

    def save_states(self):
        data = {}
        for gid, player in self.players.items():
            snap = player.snapshot()
            if snap and (snap["queue"] or snap["current"]):
                data[str(gid)] = snap
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError as exc:
            log.warning("Nao consegui salvar a fila em disco: %s", exc)

    async def _restore_states(self):
        """Reconecta e retoma filas salvas, se ainda houver gente no canal de voz."""
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Nao consegui ler a fila salva: %s", exc)
            return

        for gid, snap in data.items():
            guild = self.bot.get_guild(int(gid))
            if guild is None or guild.voice_client is not None:
                continue
            channel = guild.get_channel(snap.get("voice_channel_id") or 0)
            if not isinstance(channel, discord.VoiceChannel) or all(m.bot for m in channel.members):
                log.info("Fila salva do servidor %s descartada (canal vazio ou inexistente).", gid)
                continue

            tracks = []
            if snap.get("current"):
                tracks.append(Track(**snap["current"]))
            tracks.extend(Track(**t) for t in snap.get("queue", []))
            if not tracks:
                continue

            try:
                await channel.connect(self_deaf=True)
            except discord.DiscordException as exc:
                log.warning("Nao consegui voltar ao canal de voz do servidor %s: %s", gid, exc)
                continue

            text = guild.get_channel(snap.get("text_channel_id") or 0) or channel
            player = MusicPlayer(self, guild, text)
            player.volume = snap.get("volume", 0.5)
            player.loop_mode = snap.get("loop_mode", "off")
            self.players[guild.id] = player
            player.enqueue(tracks)
            log.info("Fila do servidor %s retomada com %d musica(s).", gid, len(tracks))
            try:
                await text.send(f"Voltei! Retomando a fila de onde parou ({len(tracks)} musica(s)).")
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

        requester = interaction.user.display_name
        is_url = busca.startswith(("http://", "https://"))
        tracks: list[Track] = []

        if info and "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not is_url:
                entries = entries[:1]  # busca por nome: usa o primeiro resultado
            for e in entries:
                url = e.get("url") or e.get("webpage_url")
                if url:
                    tracks.append(Track(url, e.get("title") or "Sem titulo", e.get("duration"),
                                        requester, is_live=_is_live(e)))
        elif info:
            tracks.append(Track(info.get("webpage_url") or busca, info.get("title") or "Sem titulo",
                                info.get("duration"), requester, is_live=_is_live(info)))

        if not tracks:
            return await interaction.followup.send("Nao encontrei nada com isso.")

        player = self._player_for(interaction)
        player.text_channel = interaction.channel
        player.enqueue(tracks, proxima=proxima)

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
            embed.add_field(name="Posicao na fila", value="1" if proxima else str(len(player.queue)))
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
        player._skipping = True
        player.stop_current()
        await interaction.response.send_message("Pulando...")

    @app_commands.command(name="stop", description="Para de tocar e limpa a fila (o bot continua no canal)")
    async def stop(self, interaction: discord.Interaction):
        player, vc = await self._require_player(interaction)
        if not player:
            return
        player.queue.clear()
        player.loop_mode = "off"
        player._skipping = True
        player.stop_current()
        player.cog.save_states()
        await interaction.response.send_message("Parei e limpei a fila.")

    @app_commands.command(name="queue", description="Mostra a fila de musicas")
    async def queue(self, interaction: discord.Interaction):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        linhas = []
        if player.current:
            c = player.current
            linhas.append(f"**Tocando agora:** [{short_title(c.title)}]({c.url}) `{fmt_track_duration(c)}`")
        if player.queue:
            linhas.append(f"\n**Proximas ({len(player.queue)}):**")
            for i, t in enumerate(list(player.queue)[:10], start=1):
                linhas.append(f"`{i}.` [{short_title(t.title)}]({t.url}) `{fmt_track_duration(t)}`")
            resto = len(player.queue) - 10
            if resto > 0:
                linhas.append(f"... e mais {resto} musica(s)")
        if not linhas:
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
        removidas = len(player.queue)
        player.queue.clear()
        self.save_states()
        await interaction.response.send_message(f"Fila limpa: {removidas} musica(s) removida(s).")

    @app_commands.command(name="remove", description="Remove uma musica da fila pela posicao")
    @app_commands.describe(posicao="Posicao na fila (veja /queue)")
    async def remove(self, interaction: discord.Interaction, posicao: app_commands.Range[int, 1]):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        if posicao > len(player.queue):
            return await interaction.response.send_message(
                f"A fila tem apenas {len(player.queue)} musica(s).", ephemeral=True
            )
        removida = player.queue[posicao - 1]
        del player.queue[posicao - 1]
        self.save_states()
        await interaction.response.send_message(f"Removida: **{short_title(removida.title)}**")

    @app_commands.command(name="shuffle", description="Embaralha a fila")
    async def shuffle(self, interaction: discord.Interaction):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        if len(player.queue) < 2:
            return await interaction.response.send_message("Fila pequena demais para embaralhar.", ephemeral=True)
        random.shuffle(player.queue)
        self.save_states()
        await interaction.response.send_message(f"Fila embaralhada ({len(player.queue)} musicas).")

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
        player.loop_mode = modo.value
        self.save_states()
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
        player.volume = nivel / 100
        if vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = player.volume
        self.save_states()
        await interaction.response.send_message(f"Volume: {nivel}%")

    @app_commands.command(name="nowplaying", description="Mostra a musica que esta tocando")
    async def nowplaying(self, interaction: discord.Interaction):
        player, _ = await self._require_player(interaction)
        if not player:
            return
        if not player.current:
            return await interaction.response.send_message("Nada tocando agora.", ephemeral=True)
        t = player.current
        embed = discord.Embed(title="Tocando agora", description=f"[{short_title(t.title)}]({t.url})", color=COR_EMBED)
        embed.add_field(name="Duracao", value=fmt_track_duration(t))
        embed.add_field(name="Pedida por", value=t.requested_by)
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
