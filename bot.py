"""Ponto de entrada do bot de musica.

Modos de execucao:
  - Invisivel (dia a dia): DJLoulouBot.exe (atalho na pasta Inicializar) -> icone na bandeja,
    clique direito no icone > "Encerrar bot" para desligar.
  - Visivel (debug): start_bot.bat -> mostra o log no console.

O log tambem fica sempre em bot.log nesta pasta.
"""

import asyncio
import logging
import logging.handlers
import os
import subprocess
import sys
import threading

import discord
from discord.ext import commands
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("GUILD_ID", "").strip()

log = logging.getLogger("djloulou")


def setup_logging():
    fmt = logging.Formatter("[{asctime}] [{levelname:<8}] {name}: {message}",
                            "%Y-%m-%d %H:%M:%S", style="{")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(BASE_DIR, "bot.log"), maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if sys.stderr is not None:  # rodando com console visivel (start_bot.bat)
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)


def atualizar_ytdlp():
    """Atualiza o yt-dlp a cada inicio - essencial para acompanhar as mudancas do YouTube."""
    log.info("Atualizando yt-dlp...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp",
             "--quiet", "--disable-pip-version-check"],
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=180, check=False,
        )
    except Exception as exc:
        log.warning("Nao consegui atualizar o yt-dlp (%s) - seguindo com a versao instalada.", exc)


def iniciar_bandeja(holder: dict, parar: threading.Event):
    """Cria o icone na bandeja do Windows com a opcao de encerrar o bot."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("pystray/Pillow nao instalados - rodando sem icone na bandeja.")
        return None

    img = Image.new("RGB", (64, 64), (255, 73, 108))
    d = ImageDraw.Draw(img)
    d.ellipse((10, 38, 30, 56), fill="white")                       # cabeca da nota musical
    d.rectangle((26, 12, 31, 47), fill="white")                     # haste
    d.polygon([(26, 12), (50, 20), (50, 32), (31, 26)], fill="white")  # bandeirola

    def encerrar(icon, item):
        log.info("Encerrado pelo icone da bandeja.")
        parar.set()
        icon.stop()
        bot = holder.get("bot")
        if bot is not None and not bot.is_closed():
            asyncio.run_coroutine_threadsafe(bot.close(), bot.loop)

    icon = pystray.Icon(
        "djloulou", img, "DJ Loulou Bot - clique direito para encerrar",
        menu=pystray.Menu(pystray.MenuItem("Encerrar bot", encerrar)),
    )
    threading.Thread(target=icon.run, daemon=True, name="bandeja").start()
    return icon


class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        await self.load_extension("cogs.music")
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Comandos sincronizados no servidor %s (aparecem na hora).", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Comandos sincronizados globalmente (podem levar alguns minutos para aparecer).")

    async def on_ready(self):
        log.info("Logado como %s - bot pronto!", self.user)


def main():
    setup_logging()
    if not TOKEN or TOKEN == "COLE_SEU_TOKEN_AQUI":
        log.error("Token nao configurado. Abra o arquivo .env e defina DISCORD_TOKEN=<token do seu bot>.")
        raise SystemExit(1)

    atualizar_ytdlp()

    holder: dict = {}
    parar = threading.Event()
    icone = iniciar_bandeja(holder, parar)

    try:
        while not parar.is_set():
            bot = MusicBot()
            holder["bot"] = bot
            try:
                # logging proprio ja configurado; discord.py nao precisa criar outro handler
                bot.run(TOKEN, log_handler=None)
                break  # fechamento intencional (bandeja ou Ctrl+C)
            except Exception:
                log.exception("O bot caiu com erro.")
            if parar.wait(30):  # espera 30s (rede pode estar voltando) e tenta de novo
                break
            log.info("Tentando iniciar novamente...")
    finally:
        if icone is not None:
            icone.stop()
        log.info("Bot desligado.")


if __name__ == "__main__":
    main()
