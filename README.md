# Bot de Música para Discord (YouTube)

Bot pessoal de música hospedado no seu próprio PC. Toca áudio do **YouTube** e do **SoundCloud** por streaming (sem baixar arquivos), com fila, busca por nome, playlists e links de faixa do **Spotify** (convertidos em busca no YouTube, já que o Spotify tem DRM).

**Stack:** Python 3.12 · discord.py · yt-dlp · FFmpeg

## Como usar no dia a dia

O bot **inicia sozinho junto com o Windows**, sem nenhuma janela — só um **ícone de nota musical na bandeja** (canto direito da barra de tarefas, talvez atrás da setinha `^`). Ele atualiza o yt-dlp a cada início e tenta reconectar sozinho se cair.

- **Desligar o bot:** clique direito no ícone da bandeja → **Encerrar bot**. (Alternativa: Gerenciador de Tarefas → finalizar **DJLoulouBot.exe**.)
- **Ligar manualmente (invisível):** dois cliques no atalho "DJ Loulou Bot" em `shell:startup`, ou rode `.venv\Scripts\DJLoulouBot.exe bot.py` na pasta do projeto.
- **Ligar em modo visível (debug):** dois cliques em `start_bot.bat` — mostra o log no console.
- **Ver o que aconteceu:** o log fica sempre em `bot.log` nesta pasta.
- **Desativar o início automático:** aperte `Win+R`, digite `shell:startup` e apague o atalho "DJ Loulou Bot".
- **Atenção:** se o PC entrar em suspensão/hibernação, o bot fica offline até o PC acordar.

## Comandos

| Comando | O que faz |
|---|---|
| `/play <link ou nome>` | Toca YouTube/SoundCloud (link ou playlist), faixa do Spotify, ou busca por nome |
| `/playnext <link ou nome>` | Igual ao `/play`, mas fura a fila (toca logo após a música atual) |
| `/pause` / `/resume` | Pausa / retoma |
| `/skip` | Pula para a próxima da fila |
| `/stop` | Para e limpa a fila (bot continua no canal) |
| `/queue` | Mostra a fila |
| `/clear` | Limpa a fila (a música atual continua tocando) |
| `/remove <posição>` | Remove uma música da fila |
| `/shuffle` | Embaralha a fila |
| `/loop <off\|musica\|fila>` | Repetição |
| `/volume <0-150>` | Volume (padrão 50%) |
| `/nowplaying` | Mostra o que está tocando |
| `/leave` | Bot sai do canal de voz |
| `/help` | Lista todos os comandos |

A mensagem de **"Tocando agora" tem botões** de pausar/retomar, pular e parar — dá para controlar sem digitar comando.

**Fila persistente:** se o bot ou o PC reiniciar no meio da sessão, o bot volta ao canal de voz e retoma a fila de onde parou (a música atual recomeça do início) — desde que ainda tenha alguém no canal. A fila fica salva em `queue_state.json`.

O bot sai sozinho do canal após 5 minutos sem nada na fila, ou 1 minuto sozinho no canal.

**Spotify:** apenas links de *faixa* (`open.spotify.com/track/...`) — o bot lê artista + nome e toca o equivalente do YouTube. Playlists/álbuns do Spotify precisariam de chave de API; use playlists do YouTube.

**Sobre playlists e Mixes:** um link de *vídeo* que carrega um Mix/rádio junto (`&list=RD...`, `start_radio=1`) toca **apenas o vídeo**. Só links de *playlist* explícita (`youtube.com/playlist?list=...`) entram inteiros, limitados a 100 músicas por vez.

## Instalação (já feita, referência para reinstalar)

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Gyan.FFmpeg -e
# em C:\Projetos\bot_musica:
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
# recria o executavel invisivel (copia do pythonw do venv):
copy .venv\Scripts\pythonw.exe .venv\Scripts\DJLoulouBot.exe
```

O início automático é um atalho em `shell:startup` chamado "DJ Loulou Bot" apontando para `.venv\Scripts\DJLoulouBot.exe "C:\Projetos\bot_musica\bot.py"`.

Depois, configure o arquivo `.env` com o token do bot (veja abaixo).

## Configuração no Discord (uma vez só)

1. Acesse https://discord.com/developers/applications e clique em **New Application**.
2. Na aba **Bot**, clique em **Reset Token** e copie o token → cole no arquivo `.env` (`DISCORD_TOKEN=...`). **Nunca compartilhe esse token.**
3. Na aba **OAuth2 → URL Generator**: marque os scopes `bot` e `applications.commands`; em permissões marque **Connect**, **Speak**, **Send Messages** e **Embed Links**. Abra a URL gerada e adicione o bot ao seu servidor.
4. (Opcional) No `.env`, preencha `GUILD_ID` com o ID do seu servidor para os comandos aparecerem na hora (senão a sincronização global pode levar alguns minutos na primeira vez).

## Solução de problemas

**"Sign in to confirm you're not a bot" ao tocar** — o YouTube desconfiou do seu IP (raro em IP residencial com uso leve). Abra `cogs/music.py` e descomente a linha `"cookiesfrombrowser": ("chrome",)` trocando pelo navegador em que você usa o YouTube logado. Isso envia a sessão do seu navegador junto e resolve.

**Músicas param de funcionar do nada** — quase sempre é o YouTube que mudou algo. O `start_bot.bat` já atualiza o yt-dlp a cada início; reinicie o bot. Se persistir, rode `.venv\Scripts\pip install -U --pre yt-dlp` para a versão de desenvolvimento.

**"FFmpeg não encontrado"** — reinstale com `winget install Gyan.FFmpeg` e abra um terminal novo. O bot também procura o FFmpeg na pasta do winget automaticamente.

**Comandos não aparecem no Discord** — aguarde alguns minutos (sync global) ou configure o `GUILD_ID` no `.env`. Confira também se o bot foi convidado com o scope `applications.commands`.

**Áudio picotando** — normalmente é a rede ou o PC sobrecarregado. O bot já usa reconexão automática do FFmpeg; evite upload pesado (torrent, backup em nuvem) durante o uso.
