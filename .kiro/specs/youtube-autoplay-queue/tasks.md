# Implementation Plan: YouTube Autoplay Queue

## Overview

Implementar em Python o Autoplay por servidor sem misturar recomendações na fila de solicitações. A refatoração evolui `cogs/music.py` de uma fila única para estado versionado, fila explícita, recomendação validada em duas fases e persistência atômica. Os testes usam `pytest`, `pytest-asyncio` e Hypothesis, com integrações simuladas para Discord, `yt-dlp` e o sistema de arquivos.

## Tasks

- [x] 1. Preparar a base de tipos, dependências e testes para o player versionado
  - [x] 1.1 Adicionar dependências de teste com versões exatas em `requirements.txt`, criar a configuração única do pytest e os fakes reutilizáveis de Discord/voz/arquivo em `tests/conftest.py`.
    - Usar dependências fixadas compatíveis para `pytest`, `pytest-asyncio` e `hypothesis`; a suíte não deve acessar Discord ou YouTube reais.
    - _Requirements: 3.6, 7.2, 7.3_
  - [x] 1.2 Refatorar os modelos em `cogs/music.py` para introduzir `TrackOrigin`, `YouTubeSource`, `RecommendationDecision` e `PlaybackState`; ampliar `Track` com origem, metadados do provedor/YouTube e solicitante; substituir a fila única por `explicit_queue` com a invariante de conter somente faixas explícitas.
    - Inicializar Autoplay como ligado, fonte/histórico vazios e uma versão monotônica por player.
    - Manter compatibilidade de resolução para fontes não YouTube, sem deixá-las substituir a fonte de recomendação.
    - _Requirements: 1.1, 2.1–2.7, 4.1–4.4, 7.1_
  - [x] 1.3 Escrever testes unitários dos modelos e do estado inicial em `tests/test_playback_state.py`.
    - Cobrir Autoplay ligado sem estado persistido, a invariante da fila explícita e a serialização mínima de origem/metadados de uma faixa.
    - _Requirements: 1.1, 2.1, 7.1_

- [x] 2. Implementar persistência validada e publicação atômica do estado do servidor
  - [x] 2.1 Criar `StateRepository` em `cogs/music.py` e substituir `save_states` por serialização versionada, validação e publicação atômica de `queue_state.json` via arquivo temporário, flush/fsync e `os.replace`.
    - Persistir no mesmo snapshot por guild Autoplay, fonte, histórico, atual e sua origem, fila explícita, repetição, volume e IDs dos canais.
    - Rejeitar snapshots inválidos, enums desconhecidos, automáticas na fila explícita e automáticas sem fonte válida; preservar o arquivo publicado anteriormente diante de erro.
    - _Requirements: 1.1, 1.3–1.5, 7.1–7.3_
  - [x] 2.2 Escrever testes de integração de `StateRepository` em `tests/test_state_repository.py` usando `tmp_path` e falhas injetadas antes da substituição atômica.
    - Verificar publicação completa, conservação do snapshot anterior após falha, validação de campos obrigatórios e ausência de automáticas em `explicit_queue`.
    - _Requirements: 1.3–1.5, 7.1–7.3_
  - [x] 2.3 Escrever o teste Hypothesis da **Property 6: Round-trip do estado recuperável** em `tests/test_property_persistence.py`.
    - Gerar estados recuperáveis válidos e confirmar preservação de Autoplay, fonte, histórico, origem, fila explícita, repetição e volume, sem introduzir automática na fila.
    - **Validates: Requirements 7.1, 7.4**

- [x] 3. Isolar resolução de faixas e obtenção qualificada de recomendações
  - [x] 3.1 Implementar `TrackResolver` e `RecommendationProvider` em `cogs/music.py`, executando o extractor em thread e normalizando candidatos sem persistir respostas brutas ou URLs temporárias de stream.
    - Consultar relacionados/radio somente pelo `video_id` normalizado da fonte; aplicar timeout, limite de candidatos e filtros para origem YouTube, IDs válidos, atual, fonte vigente e histórico automático.
    - Retornar resultados explícitos para candidata qualificada, ausência de candidata e falha de obtenção; qualificar a reprodução antes de aceitar a candidata.
    - _Requirements: 3.1–3.3, 3.6, 4.1–4.4_
  - [x] 3.2 Escrever testes de integração controlada do provedor em `tests/test_recommendation_provider.py` com respostas gravadas/fakes de `yt-dlp`.
    - Cobrir normalização da fonte, filtragem de atual/histórico/inválidos, ausência de candidata e exceção do extractor sem chamada de rede real.
    - _Requirements: 3.3, 3.6, 4.2_

- [x] 4. Converter o loop de reprodução em máquina de estados com prioridade explícita
  - [x] 4.1 Refatorar `MusicPlayer` em `cogs/music.py` para aplicar transições sob `asyncio.Lock`, manter no máximo uma tarefa de recomendação e selecionar sempre repetição aplicável, faixa explícita, Autoplay ou inatividade nessa ordem.
    - Implementar `enqueue_explicit`, `select_next_action`, `maybe_request_recommendation`, validação completa de `RecommendationDecision` e `accept_recommendation`; nunca enfileirar uma automática em `explicit_queue`.
    - Incrementar `playback_version` ao aceitar explícitas, iniciar explícita YouTube, pular, parar, limpar decisão pendente, alterar repetição, alternar Autoplay e sair; não incrementar em pausar/retomar.
    - Ao iniciar explícita YouTube, redefinir fonte e histórico antes da próxima decisão; ao iniciar automática, preservar ambos e acrescentar somente o ID iniciado; avisar e ignorar explícita não reproduzível do YouTube.
    - Ao falhar/ficar sem candidata numa decisão ainda válida, avisar o canal e armar a inatividade sem nova busca especulativa.
    - _Requirements: 1.6, 1.7, 2.1–2.7, 3.1–3.6, 4.1–4.4, 6.1–6.4, 6.7_
  - [x] 4.2 Escrever o teste Hypothesis da **Property 1: Ordenação e prioridade absoluta da fila explícita** em `tests/test_property_explicit_queue.py`.
    - Gerar sequências de `/play` e `/playnext`, incluindo faixas atuais explícitas e automáticas, e confirmar prepend LIFO, append FIFO e seleção da cabeça antes de Autoplay.
    - **Validates: Requirements 2.1–2.7, 6.3**
  - [x] 4.3 Escrever o teste Hypothesis da **Property 2: Elegibilidade e invalidação de recomendações** em `tests/test_property_recommendation_decision.py`.
    - Gerar estados, término/skip e mutações concorrentes de versão, fonte, Autoplay, fila, repetição e conexão; confirmar criação somente quando elegível e descarte seguro do resultado inválido.
    - **Validates: Requirements 1.6, 3.1, 3.2, 3.4, 3.5, 6.4, 6.7**
  - [x] 4.4 Escrever o teste Hypothesis da **Property 3: Aceitação de recomendação qualificada** em `tests/test_property_recommendation_acceptance.py`.
    - Gerar candidatas qualificadas e confirmar uma única inicialização automática, atualização exata do histórico e ausência da faixa em `explicit_queue`.
    - **Validates: Requirements 3.3, 3.5, 4.2, 5.4**
  - [x] 4.5 Escrever o teste Hypothesis da **Property 4: Contexto manual de recomendação** em `tests/test_property_recommendation_context.py`.
    - Gerar inícios explícitos YouTube, explícitos sem vídeo válido e automáticos para confirmar a atualização/preservação correta de fonte, histórico e versão.
    - **Validates: Requirements 4.1–4.4**
  - [x] 4.6 Escrever testes unitários de fluxos de reprodução em `tests/test_music_player_autoplay.py`.
    - Cobrir pausa/retomada de automática, erro de stream explícito, falha/ausência do provedor, término com fila explícita e decisão atrasada descartada.
    - _Requirements: 3.4, 3.6, 4.4, 6.1–6.4_

- [x] 5. Integrar controles persistentes e transparência de origem na interface Discord
  - [x] 5.1 Atualizar `PlayerControls`, `_announce_now_playing`, `/nowplaying` e `/queue` em `cogs/music.py` para exibir origem e exatamente um botão persistente `Autoplay: ligado/desligado`.
    - Implementar o toggle como transação prepare → publicar snapshot → aplicar em memória → atualizar somente a view/rótulo → confirmar efemeramente; em falha, manter memória e view anteriores.
    - Quando o toggle bem-sucedido ligar Autoplay em estado elegível, iniciar a avaliação somente após o commit; desligar durante automática preserva a atual e bloqueia futuras automáticas.
    - Exibir origem como recomendação automática ou solicitação do membro; `/queue` deve listar somente a atual e faixas explícitas pendentes.
    - _Requirements: 1.2–1.7, 5.1–5.4_
  - [x] 5.2 Escrever testes unitários de views, embeds e toggle em `tests/test_player_controls.py` com interações e mensagens falsas.
    - Verificar botão único e rótulos possíveis, ordem transacional do toggle, resposta efêmera de sucesso/falha e conteúdo de origem/Autoplay em “Tocando agora”, `/nowplaying` e `/queue`.
    - _Requirements: 1.2–1.5, 5.1–5.4_

- [x] 6. Adaptar comandos, interrupções e recuperação ao novo estado
  - [x] 6.1 Atualizar em `cogs/music.py` os comandos e callbacks de `play`, `playnext`, `skip`, `stop`, `clear`, `remove`, `shuffle`, `loop`, `volume` e `leave` para operar exclusivamente na fila explícita, invalidar decisões quando aplicável e manter os efeitos exigidos de Autoplay/fonte/histórico.
    - `stop` limpa atual/fila e só arma inatividade; `clear` remove somente pendentes explícitas; `leave` descarta fila, fonte, histórico e tarefa de recomendação antes de desconectar.
    - Impedir busca de recomendação durante os modos `musica` e `fila`; manter a reconexão de live compatível com a origem da faixa atual.
    - _Requirements: 2.1–2.7, 6.3–6.8_
  - [x] 6.2 Reescrever `_restore_states` em `cogs/music.py` para validar o snapshot completo antes de mutar o player, aplicar todos os campos em uma única transição sob lock e só então despertar a seleção.
    - Restaurar automáticas apenas com fonte válida e humanos presentes; para snapshot ausente, incompleto, inválido ou sem fonte, reaproveitar somente explícitas válidas e aplicar inatividade se necessário.
    - _Requirements: 7.4–7.6_
  - [x] 6.3 Escrever o teste Hypothesis da **Property 5: Invariantes dos controles de interrupção** em `tests/test_property_interruption_invariants.py`.
    - Gerar estados válidos e verificar os pós-estados de `stop`, `clear` e `leave`, incluindo limpeza/invalidação e incrementos de versão aplicáveis.
    - **Validates: Requirements 6.5, 6.6, 6.8**
  - [x] 6.4 Escrever testes de integração do ciclo completo em `tests/test_music_autoplay_integration.py` com fakes de `VoiceClient`, `Interaction`, `Message`, repositório e provedor.
    - Cobrir `/play` e `/playnext` durante automática, prioridade após término/skip, toggle elegível, não duplicação de `voice_client.play`, restauração válida e descarte de canal sem humanos.
    - _Requirements: 1.7, 2.2–2.5, 3.3–3.6, 6.3–6.8, 7.4–7.6_

- [x] 7. Checkpoint — Ensure all tests pass, ask the user if questions arise.
  - Executar uma vez a suíte automatizada (`pytest`) e os checks configurados, sem modo watch; corrigir falhas de implementação antes de avançar.

## Notes

- As tarefas usam Python porque `design.md` especifica dataclasses, `asyncio`, `discord.py` e `yt-dlp`; não foi necessário selecionar linguagem adicional.
- Tarefas marcadas com `*` são testes opcionais para execução acelerada, mas devem permanecer no plano para cobrir exemplos, integrações e as seis propriedades do design.
- Cada propriedade é deliberadamente um teste Hypothesis separado, com no mínimo 100 exemplos e o comentário `Feature: youtube-autoplay-queue, Property N: ...`.
- Não chamar Discord ou YouTube reais na suíte regular; usar fakes, fixtures e respostas gravadas.
- O plano cobre somente código e testes. Nenhuma tarefa exige deploy, validação manual, documentação de usuário ou operação de produção.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["1.3", "2.1"] },
    { "id": 3, "tasks": ["2.2", "2.3", "3.1"] },
    { "id": 4, "tasks": ["3.2", "4.1"] },
    { "id": 5, "tasks": ["4.2", "4.3", "4.4", "4.5", "4.6", "5.1"] },
    { "id": 6, "tasks": ["5.2", "6.1"] },
    { "id": 7, "tasks": ["6.2", "6.3"] },
    { "id": 8, "tasks": ["6.4"] }
  ]
}
```
