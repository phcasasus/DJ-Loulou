# Requirements Document

## Introduction

O Bot_de_Música deve reintroduzir Autoplay por servidor para manter a reprodução com recomendações do YouTube depois que uma Fila_Explícita terminar. A Fila_Explícita continua tendo prioridade absoluta: faixas solicitadas por membros devem tocar antes de qualquer recomendação e uma nova solicitação durante uma Faixa_Automática deve tocar logo após a faixa em reprodução. A recomendação seguinte deve refletir a última faixa explícita que efetivamente começou a tocar.

**Decisão de UX inicial:** Autoplay será ligado por padrão em novos servidores e controlado por um botão persistente e com estado visível na mensagem **Tocando agora**. O botão evita memorizar um comando adicional, permite alternar a preferência em um toque e mantém a mudança localizada no contexto em que o comportamento é percebido. O controle não interrompe a faixa que já toca; desativar impede somente futuras recomendações.

## Glossary

- **Bot_de_Música**: Sistema de reprodução de áudio do bot Discord, incluindo comandos, botões e player por servidor.
- **Servidor**: Guild do Discord que possui estado de reprodução isolado.
- **Player_do_Servidor**: Estado de reprodução associado a um único Servidor.
- **Fila_Explícita**: Sequência de faixas solicitadas por membros por meio de `/play` ou `/playnext`, excluindo a Faixa_Atual.
- **Faixa_Explícita**: Faixa aceita a partir de uma solicitação de membro.
- **Faixa_Automática**: Faixa recomendada pelo YouTube e iniciada pelo Autoplay, sem solicitação de membro.
- **Faixa_Atual**: Faixa que o Player_do_Servidor está reproduzindo no momento.
- **Autoplay**: Preferência por Servidor que autoriza a obtenção e a reprodução de uma Faixa_Automática quando não há Faixa_Explícita pendente.
- **Fonte_de_Recomendação**: Vídeo do YouTube correspondente à Faixa_Explícita mais recente que começou a tocar; usado para obter recomendações posteriores.
- **Histórico_de_Faixas_Automáticas**: Conjunto de vídeos reproduzidos como Faixa_Automática desde a definição da Fonte_de_Recomendação vigente.
- **Recomendação_Qualificada**: Vídeo do YouTube reproduzível que não é a Faixa_Atual e que não pertence ao Histórico_de_Faixas_Automáticas da Fonte_de_Recomendação vigente.
- **Contexto_de_Decisão_de_Recomendação**: Registro, criado antes de solicitar uma recomendação, da Fonte_de_Recomendação, do estado ligado de Autoplay, da Fila_Explícita vazia, do Modo_de_Repetição desligado e da versão de reprodução do Player_do_Servidor.
- **Versão_de_Reprodução**: Identificador monotonicamente crescente do estado de reprodução do Player_do_Servidor, alterado quando uma ação pode invalidar uma recomendação em obtenção.
- **Modo_de_Repetição**: Estado por Servidor que pode ser desligado, repetir uma faixa ou repetir a Fila_Explícita.
- **Estado_Persistido**: Instantâneo completo, validável e gravado de forma atômica dos dados por Servidor necessários para recuperação após reinicialização do Bot_de_Música.
- **Período_de_Inatividade**: Intervalo existente de 300 segundos sem Faixa_Atual e sem Faixa_Explícita, após o qual o Bot_de_Música sai do canal de voz.

## Requirements

### Requirement 1: Preferência de Autoplay por servidor

**User Story:** Como membro de um Servidor, quero ativar ou desativar o Autoplay no contexto da reprodução, para controlar se o bot continua com recomendações após as músicas solicitadas.

#### Acceptance Criteria

1. WHEN um Player_do_Servidor é criado sem uma preferência salva de Autoplay, THE Bot_de_Música SHALL definir Autoplay como ligado e SHALL incluir esse valor no próximo Estado_Persistido bem-sucedido do Servidor.
2. WHEN a mensagem **Tocando agora** é publicada ou atualizada, THE Bot_de_Música SHALL exibir exatamente um botão identificado com `Autoplay: ligado` ou `Autoplay: desligado`, de acordo com o valor de Autoplay do Servidor.
3. WHEN um membro aciona o botão de Autoplay, THE Bot_de_Música SHALL preparar o valor oposto de Autoplay e SHALL tentar gravar um Estado_Persistido que contenha o valor preparado antes de alterar o Autoplay ativo do Player_do_Servidor.
4. WHEN a gravação do Estado_Persistido preparada para uma alteração de Autoplay é concluída com êxito, THE Bot_de_Música SHALL aplicar o valor preparado ao Player_do_Servidor, atualizar o rótulo do botão e confirmar o novo estado ao membro em uma resposta efêmera.
5. IF a gravação do Estado_Persistido preparada para uma alteração de Autoplay falha, THEN THE Bot_de_Música SHALL manter o valor anterior de Autoplay no Player_do_Servidor e no rótulo do botão e SHALL informar a falha ao membro em uma resposta efêmera.
6. WHEN Autoplay é desligado durante uma Faixa_Automática, THE Bot_de_Música SHALL concluir a Faixa_Atual, salvo ação de pular, parar ou sair, e SHALL impedir o início de outra Faixa_Automática.
7. WHEN Autoplay é ligado com a Fila_Explícita vazia, a Faixa_Atual concluída e o Modo_de_Repetição desligado, THE Bot_de_Música SHALL iniciar o fluxo de recomendação descrito na Requirement 3 após a gravação bem-sucedida da preferência.

### Requirement 2: Prioridade e ordenação da Fila_Explícita

**User Story:** Como membro de um Servidor, quero que músicas solicitadas tenham prioridade sobre recomendações, para que o Autoplay não atrase as escolhas das pessoas.

#### Acceptance Criteria

1. WHILE a Fila_Explícita contém pelo menos uma Faixa_Explícita e o Modo_de_Repetição está desligado, THE Bot_de_Música SHALL iniciar a primeira Faixa_Explícita da Fila_Explícita antes de iniciar uma Faixa_Automática.
2. WHEN uma solicitação `/play` válida é aceita durante a reprodução de uma Faixa_Automática, THE Bot_de_Música SHALL acrescentar a Faixa_Explícita à última posição da Fila_Explícita.
3. WHEN uma solicitação `/playnext` válida é aceita durante a reprodução de uma Faixa_Automática, THE Bot_de_Música SHALL inserir a Faixa_Explícita na primeira posição da Fila_Explícita, fazendo com que a solicitação `/playnext` aceita mais recentemente seja a próxima após a Faixa_Atual.
4. WHEN uma Faixa_Automática está em reprodução, THE Bot_de_Música SHALL ordenar a Fila_Explícita com as Faixas_Explícitas aceitas por `/playnext` em ordem cronológica inversa de aceitação, seguidas pelas Faixas_Explícitas aceitas por `/play` em ordem cronológica de aceitação.
5. WHEN uma Faixa_Automática termina e a Fila_Explícita contém pelo menos uma Faixa_Explícita, THE Bot_de_Música SHALL iniciar a primeira Faixa_Explícita sem iniciar outra Faixa_Automática.
6. WHEN uma solicitação `/play` válida é aceita durante uma Faixa_Explícita, THE Bot_de_Música SHALL acrescentar a Faixa_Explícita à última posição da Fila_Explícita.
7. WHEN uma solicitação `/playnext` válida é aceita durante uma Faixa_Explícita, THE Bot_de_Música SHALL inserir a Faixa_Explícita na primeira posição da Fila_Explícita, fazendo com que a solicitação `/playnext` aceita mais recentemente seja a próxima após a Faixa_Atual.

### Requirement 3: Obtenção de recomendações somente ao fim da fila

**User Story:** Como ouvinte, quero que o bot continue com recomendações do YouTube somente depois das solicitações pendentes, para ouvir músicas relacionadas sem perder controle sobre a fila.

#### Acceptance Criteria

1. WHEN uma Faixa_Explícita termina, a Fila_Explícita está vazia, Autoplay está ligado, o Modo_de_Repetição está desligado e existe uma Fonte_de_Recomendação, THE Bot_de_Música SHALL criar um Contexto_de_Decisão_de_Recomendação e SHALL solicitar uma Recomendação_Qualificada do YouTube usando a Fonte_de_Recomendação registrada.
2. WHEN uma Faixa_Automática termina, Autoplay está ligado, a Fila_Explícita está vazia, o Modo_de_Repetição está desligado e existe uma Fonte_de_Recomendação, THE Bot_de_Música SHALL criar um Contexto_de_Decisão_de_Recomendação e SHALL solicitar uma Recomendação_Qualificada do YouTube usando a Fonte_de_Recomendação registrada.
3. WHEN o YouTube fornece uma Recomendação_Qualificada para um Contexto_de_Decisão_de_Recomendação ainda válido, THE Bot_de_Música SHALL iniciar uma única Faixa_Automática correspondente à Recomendação_Qualificada e SHALL acrescentar o vídeo iniciado ao Histórico_de_Faixas_Automáticas.
4. IF Autoplay é desligado, a Fila_Explícita deixa de estar vazia, o Modo_de_Repetição deixa de estar desligado, a Fonte_de_Recomendação é alterada, a Versão_de_Reprodução é alterada ou o Player_do_Servidor sai do canal de voz antes de o YouTube responder, THEN THE Bot_de_Música SHALL descartar a recomendação recebida e SHALL impedir o início de uma Faixa_Automática a partir daquela solicitação.
5. WHILE uma Faixa_Automática está em reprodução, THE Bot_de_Música SHALL manter qualquer recomendação posterior fora da Fila_Explícita.
6. IF o YouTube não fornece uma Recomendação_Qualificada ou ocorre uma falha ao obter uma recomendação para um Contexto_de_Decisão_de_Recomendação ainda válido, THEN THE Bot_de_Música SHALL informar a falha no canal de texto do Player_do_Servidor e SHALL aplicar o Período_de_Inatividade sem iniciar outra Faixa_Automática.

### Requirement 4: Atualização da fonte das recomendações

**User Story:** Como ouvinte, quero que recomendações posteriores reflitam a última música escolhida por uma pessoa, para que uma nova direção musical substitua o contexto anterior do Autoplay.

#### Acceptance Criteria

1. WHEN uma Faixa_Explícita com um vídeo do YouTube reproduzível começa a tocar, THE Bot_de_Música SHALL definir esse vídeo como Fonte_de_Recomendação, SHALL definir o Histórico_de_Faixas_Automáticas como vazio e SHALL aumentar a Versão_de_Reprodução antes de solicitar outra recomendação.
2. WHEN uma Faixa_Automática começa a tocar, THE Bot_de_Música SHALL manter a Fonte_de_Recomendação e o Histórico_de_Faixas_Automáticas vigentes sem alteração, exceto pelo acréscimo do vídeo iniciado ao Histórico_de_Faixas_Automáticas.
3. WHEN uma Faixa_Explícita aceita durante uma Faixa_Automática começa a tocar, THE Bot_de_Música SHALL substituir a Fonte_de_Recomendação e redefinir o Histórico_de_Faixas_Automáticas conforme a Faixa_Explícita iniciada antes de solicitar a próxima recomendação.
4. IF uma Faixa_Explícita não possui um vídeo do YouTube reproduzível, THEN THE Bot_de_Música SHALL informar que a Faixa_Explícita foi ignorada e SHALL manter a Fonte_de_Recomendação e o Histórico_de_Faixas_Automáticas anteriores.

### Requirement 5: Transparência para os membros

**User Story:** Como membro de um Servidor, quero distinguir músicas solicitadas de recomendações e verificar o estado do Autoplay, para entender por que o bot escolheu a próxima música.

#### Acceptance Criteria

1. WHEN uma Faixa_Automática começa a tocar, THE Bot_de_Música SHALL publicar ou atualizar a mensagem **Tocando agora** com os textos `Origem: Recomendação automática` e `Autoplay: ligado` ou `Autoplay: desligado` conforme o valor ativo do Servidor.
2. WHEN uma Faixa_Explícita começa a tocar, THE Bot_de_Música SHALL publicar ou atualizar a mensagem **Tocando agora** com o texto `Origem: solicitação de <membro>` que identifique o membro que adicionou a Faixa_Explícita e com `Autoplay: ligado` ou `Autoplay: desligado` conforme o valor ativo do Servidor.
3. WHEN o comando `/queue` é executado, THE Bot_de_Música SHALL exibir `Autoplay: ligado` ou `Autoplay: desligado` e SHALL identificar a Faixa_Atual como `Recomendação automática` ou como uma solicitação do membro que adicionou a Faixa_Atual.
4. WHEN o comando `/queue` é executado, THE Bot_de_Música SHALL listar apenas Faixas_Explícitas pendentes como próximas posições da fila e SHALL omitir Faixas_Automáticas que não tenham começado a tocar.

### Requirement 6: Compatibilidade com controles e modos existentes

**User Story:** Como membro de um Servidor, quero que os controles atuais continuem previsíveis com Autoplay, para evitar reproduções inesperadas.

#### Acceptance Criteria

1. WHEN uma Faixa_Automática é pausada, THE Bot_de_Música SHALL manter a Faixa_Automática como Faixa_Atual sem solicitar uma recomendação até que um comando ou botão de retomar, pular, parar ou sair seja processado.
2. WHEN uma Faixa_Automática é retomada, THE Bot_de_Música SHALL continuar a reprodução da mesma Faixa_Automática sem alterar a Fonte_de_Recomendação ou o Histórico_de_Faixas_Automáticas.
3. WHEN uma Faixa_Automática é pulada e a Fila_Explícita contém pelo menos uma Faixa_Explícita, THE Bot_de_Música SHALL aumentar a Versão_de_Reprodução e SHALL iniciar a primeira Faixa_Explícita antes de solicitar uma Recomendação_Qualificada.
4. WHEN uma Faixa_Automática é pulada, Autoplay está ligado, a Fila_Explícita está vazia e o Modo_de_Repetição está desligado, THE Bot_de_Música SHALL aumentar a Versão_de_Reprodução e SHALL iniciar o fluxo de recomendação descrito na Requirement 3 depois de concluir o processamento do pulo.
5. WHEN o comando ou botão de parar é acionado, THE Bot_de_Música SHALL interromper a Faixa_Atual, limpar a Fila_Explícita, invalidar solicitações de recomendação em obtenção, aumentar a Versão_de_Reprodução e aplicar o Período_de_Inatividade sem iniciar uma Faixa_Automática.
6. WHEN o comando `/clear` é acionado, THE Bot_de_Música SHALL remover somente as Faixas_Explícitas pendentes e SHALL manter inalteradas a Faixa_Atual, a preferência de Autoplay, a Fonte_de_Recomendação e o Histórico_de_Faixas_Automáticas.
7. WHILE o Modo_de_Repetição é `musica` ou `fila`, THE Bot_de_Música SHALL executar o comportamento de repetição selecionado, SHALL invalidar solicitações de recomendação em obtenção e SHALL impedir a solicitação de uma Recomendação_Qualificada.
8. WHEN o Bot_de_Música sai do canal de voz, THE Bot_de_Música SHALL descartar a Fila_Explícita, a Fonte_de_Recomendação em memória, o Histórico_de_Faixas_Automáticas e qualquer solicitação de recomendação em obtenção e SHALL aumentar a Versão_de_Reprodução.

### Requirement 7: Persistência e recuperação segura

**User Story:** Como membro de um Servidor, quero que a preferência de Autoplay e o contexto da fila sobrevivam a uma reinicialização, para que a retomada não mude inesperadamente o comportamento escolhido.

#### Acceptance Criteria

1. WHEN o Bot_de_Música grava um Estado_Persistido de um Servidor com reprodução recuperável, THE Bot_de_Música SHALL incluir Autoplay, a Fonte_de_Recomendação, o Histórico_de_Faixas_Automáticas, a origem explícita ou automática da Faixa_Atual, a Fila_Explícita, o Modo_de_Repetição e o volume no mesmo instantâneo validável.
2. WHEN o Bot_de_Música conclui a gravação de um Estado_Persistido, THE Bot_de_Música SHALL tornar o instantâneo completo disponível de forma atômica, de modo que uma leitura posterior obtenha o instantâneo anterior completo ou o novo instantâneo completo.
3. IF a gravação de um Estado_Persistido falha antes de se tornar disponível de forma atômica, THEN THE Bot_de_Música SHALL manter o último Estado_Persistido completo disponível, SHALL manter o estado ativo em memória e SHALL registrar a falha para diagnóstico.
4. WHEN o Bot_de_Música restaura um Player_do_Servidor elegível após uma reinicialização, THE Bot_de_Música SHALL validar o Estado_Persistido completo antes de alterar o Player_do_Servidor, SHALL restaurar Autoplay, a Fila_Explícita, o Modo_de_Repetição, o volume, a Fonte_de_Recomendação, o Histórico_de_Faixas_Automáticas e a origem da Faixa_Atual como uma única transição de estado e SHALL iniciar a próxima faixa somente após essa transição ser concluída.
5. IF o Estado_Persistido está ausente, incompleto, inválido ou não contém uma Fonte_de_Recomendação válida, THEN THE Bot_de_Música SHALL criar um Player_do_Servidor sem Faixa_Automática pendente, SHALL restaurar somente as Faixas_Explícitas disponíveis e SHALL aplicar o Período_de_Inatividade quando não houver Faixa_Explícita.
6. WHEN o Estado_Persistido pertence a um Servidor sem membros humanos no canal de voz, THE Bot_de_Música SHALL descartar o Estado_Persistido de reprodução daquele Servidor e SHALL impedir a restauração de qualquer Faixa_Atual, Faixa_Explícita ou Faixa_Automática daquele Estado_Persistido.
