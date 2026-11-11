# Loong Telegram Harness

Plugin client-side de wakeup em Python 3.10+, sem dependências externas. Recebe
mensagens de texto, fotos e documentos via `getUpdates` (long polling, sem
servidor público) e envia um `wakeup` JSON-RPC por mensagem. Loong cria uma
sessão por chat/tópico. Fotos e documentos de imagem são encaminhados ao modelo
quando o cliente anuncia suporte a imagens e o modelo suporta visão.

## Preparar

1. Crie um bot com o **@BotFather** e obtenha o token.
2. Crie `bot-token.txt` contendo somente o token, sem aspas. O arquivo
   está ignorado pelo Git; restrinja suas permissões ao usuário que roda Loong.
   Não coloque o token em prompts, argumentos de shell ou arquivos versionados.
   Como alternativa, exporte `TELEGRAM_BOT_TOKEN` no ambiente do cliente **e do
   daemon**; o arquivo é mais simples para um daemon já em execução.
3. Obtenha seu ID numérico de usuário, por exemplo com **@userinfobot**. No
   PowerShell, defina a lista de usuários que podem acionar o agente:

   ```powershell
   $env:TELEGRAM_ALLOWED_USER_IDS = "123456789"
   ```

   Bash: `export TELEGRAM_ALLOWED_USER_IDS="123456789"`. Para mais de um usuário,
   separe os IDs por vírgula. Sem essa lista, o worker se recusa a iniciar.
4. Mescle as seções de [config.example.yaml](config.example.yaml) em seu config
   principal ou em `.loong/config.yaml`. Preserve seu provider/modelo atual e
   outros agentes/wakeups. Os caminhos do exemplo pressupõem a raiz deste
   repositório como workspace. Se necessário, troque `command: python` por
   `python3` ou pelo caminho absoluto do interpretador. Use o mesmo Python no
   [prompt do agente](prompt.md).
5. Na raiz do repositório, execute uma versão do Loong com suporte a wakeups:

   ```powershell
   # Instale Loong separadamente e deixe-o no PATH.
   loong --headless
   # Ou: loong --config CAMINHO_DO_CONFIG --headless
   ```

   Não use `--config config.example.yaml` isoladamente: o fragmento
   não contém providers. O plugin também pode rodar com o cliente TUI.
6. Abra o chat privado com o bot, pressione **Start** e envie uma mensagem.

## Responder ao Telegram

Wakeups só entregam entradas: `accepted` significa admissão na mailbox, não
conclusão. A resposta final do agente não volta ao Telegram automaticamente.
O agente de exemplo usa este helper para enviar sua resposta em texto puro:

```powershell
python worker.py send --chat-id 123456789 --reply-to 7 --text "Olá!"
```

Em tópicos, acrescente `--thread-id ID`. Respostas longas são divididas em partes
com até 4000 unidades UTF-16, sem `parse_mode`. O helper usa o mesmo token do
worker. IDs de destino vêm do contexto do evento, não do texto da mensagem.

O agente do exemplo tem `run_command` e `read_file`; o primeiro permite executar
shell e **não é um sandbox**. Ele requer confirmação por padrão. Em headless,
sem um operador para aprovar, respostas podem ficar bloqueadas/falhar. Valide o
fluxo na TUI primeiro. `--yolo` dispensa confirmações, mas também dá execução de
shell ao agente acionado remotamente: só use conscientemente, com allowlist e
usuário/workspace isolados. A allowlist não impede prompt injection.

## Nova conversa após 3 horas

O worker Python inicia uma nova sessão Loong na primeira mensagem autorizada
recebida **após 3 horas ou mais de inatividade naquele chat/tópico**. Antes desse
intervalo, reutiliza a mesma sessão. Não há timer nem criação de sessão enquanto
o bot está ocioso: o worker muda a `session_key` somente na chegada da mensagem.
Requer `session_scope: key`, como no exemplo.

O relógio é o horário local de recebimento/processamento de mensagens de entrada,
não o horário enviado pelo Telegram nem o fim da resposta do agente. Respostas
via `send` não renovam o prazo. Mensagens filtradas não renovam; mensagens
admitidas para tentativa de wakeup renovam mesmo se o ACK falhar, pois a entrega
pode ser ambígua. A chave é persistida **antes** do RPC para ser reutilizada no
retry após reiniciar. No primeiro recebimento após atualizar, sem histórico de
atividade, a chave antiga é preservada e o prazo começa naquele momento.

Estado em `conversations.json`, ao lado de `TELEGRAM_STATE_FILE` (por padrão
`state/conversations.json`). Preserve esse arquivo junto do cursor e
dos vínculos do cliente Loong. Estado corrompido gera erro explícito, não reset
silencioso. Reinicie o cliente para carregar essa mudança; não precisa recompilar.
O log mostra `New conversation after 3h idle` quando ocorre a troca.

Sessões anteriores e seus anexos não são apagados. Cada rotação ocupa um novo
vínculo no cliente Loong; o limite atual é de 128 vínculos por workspace (inclui
outros plugins). Ao atingir esse limite, a admissão falha e exige gerenciamento
dos vínculos/sessões pelo operador. Essa política de 3h é exclusiva do worker
Telegram; não altera o protocolo nem o padrão dos demais plugins.

## Indicador digitando

Ao receber uma mensagem autorizada, o worker chama `sendChatAction` com
`action=typing`, incluindo o tópico quando presente. É best-effort, com timeout
de 3 segundos e sem retry: uma falha não impede o wakeup nem o avanço do cursor.
O Telegram mostra o indicador por cerca de 5 segundos ou até uma resposta ser
enviada. Não há renovação automática durante o turno: o protocolo de wakeup não
informa o progresso/conclusão da execução ao plugin.

O agente pode renovar pontualmente com:

```powershell
python worker.py typing --chat-id 123456789
```

Adicione `--thread-id ID` para tópicos. O comando segue as mesmas permissões de
shell dos helpers de envio. Reinicie o cliente para carregar o worker atualizado;
sessões antigas não recebem automaticamente a instrução nova no prompt.

## Imagens e arquivos

Instale uma versão do [Loong](https://github.com/Kruceo/loong) com suporte a
imagens em wakeups e reinicie o cliente para habilitar
`image_paths` no protocolo. Reinicie também o daemon se ele estiver executando
um binário antigo. Use um modelo com suporte a visão. Clientes antigos recebem
os metadados do arquivo e um aviso no contexto/log, mas não a imagem para visão.

- Fotos (maior variante entregue pelo Telegram) e documentos são baixados **após
  autorização**, com limite fixo de 20 MiB por arquivo, conferido nos metadados e
  durante o streaming. Legendas tornam-se o prompt; sem legenda há um prompt
  padrão. Álbuns geram um turno por item, sem agrupamento.
- Downloads ficam em `state/attachments/event-ID/`, com nomes gerados,
  gravação atômica e sem usar o nome original como caminho. MIME de imagens é
  identificado pela assinatura. JPEG/PNG/GIF/WebP seguem no campo `images`;
  demais documentos seguem em `context.attachments`, disponíveis para `read_file`.
- O agente não extrai arquivos, não faz OCR/conversão de PDF e não executa anexos
  automaticamente. Documentos binários podem exigir uma ferramenta apropriada.
- Falha/limite de download gera `attachment_error` no contexto para que o agente
  possa informar o problema, sem travar os demais chats.
- Não há limpeza automática de anexos: sessões podem reutilizar imagens do
  histórico. Monitore o uso de disco e apague somente após descartar as sessões
  que os referenciam. O limite por arquivo não é uma cota global de armazenamento.
- Proteja essa pasta (contém dados privados). Os arquivos e o daemon precisam
  compartilhar o mesmo workspace/filesystem; não há transporte binário remoto.

Para enviar anexos locais:

```powershell
python worker.py send-photo --chat-id 123456789 --file "foto.png" --caption "Resultado"
python worker.py send-document --chat-id 123456789 --file "relatorio.pdf" --caption "Relatório"
```

Ambos aceitam `--reply-to` e `--thread-id`. Fotos: JPEG/PNG até 10 MiB; documentos:
até 20 MiB; legendas em texto puro até 1024 unidades UTF-16. Não há retry automático.
O token continua no arquivo/ambiente, nunca nos eventos. O nome de upload é gerado
(`attachment` + extensão para documentos), não o nome local original.

O exemplo agora permite `read_file`. Atualize agentes e recrie a sessão Telegram
se ela já existia: sessões persistidas conservam sua declaração anterior; só
reiniciar o cliente não atualiza ferramentas/prompt de uma sessão existente.

## Teste sem restrição de usuários

Para liberar qualquer usuário temporariamente, adicione
`TELEGRAM_ALLOW_ALL_USERS: "true"` em `wakeups.servers.telegram.env` e reinicie o
cliente Loong. O log exibirá um aviso de allowlist desativada. Chats continuam
restritos: sem `TELEGRAM_ALLOWED_CHAT_IDS`, somente privados; bots continuam
ignorados. Qualquer pessoa que encontrar o bot poderá acionar o agente e gerar
custos/ações. Não combine esse teste com permissões amplas ou `--yolo`.

Para reverter, remova a opção ou use `"false"` e reinicie. A lista
`TELEGRAM_ALLOWED_USER_IDS` volta a ser obrigatória; lista vazia não libera acesso.

## Grupos e tópicos

Por padrão, apenas chats privados são aceitos. Para habilitar grupos, defina
também `TELEGRAM_ALLOWED_CHAT_IDS` com IDs numéricos de chats (normalmente
negativos), separados por vírgula. Quando preenchida, essa lista restringe
**todos** os chats, inclusive privados. A lista de usuários continua obrigatória.
O worker processa todas as mensagens de texto autorizadas recebidas; não filtra
por menção ao bot. Em grupos, a entrega também depende do privacy mode definido
no BotFather. Bots, edições, callbacks, áudios e vídeos não geram wakeups.

## Estado, segurança e limites

- Um único consumidor `getUpdates` por token. Não rode duas instâncias/clientes
  com o mesmo bot. Webhook ativo ou outro consumidor causa erro 409; remova o
  webhook conscientemente antes de usar polling. O worker não o apaga sozinho.
- O cursor fica em `state/cursor.json`, gravado atomicamente após o
  ACK. Atualizações filtradas também avançam o cursor. Sem cursor, o Telegram
  entrega o backlog ainda disponível; o worker não descarta mensagens no boot.
- Falha/timeout no wakeup interrompe o worker sem avançar aquele evento e sem
  retry automático. Reiniciar pode reenviá-lo: ACK perdido/crash entre admissão
  e persistência pode duplicar trabalho. ACK não é persistência durável.
- Não há spool durável de prompts, histórico de saída nem retry de respostas.
  Rede instável em `send` pode causar entrega parcial/ambígua. Não repita envios
  automaticamente. Long polling usa timeout, backoff e respeita `retry_after`.
- O cursor é independente dos vínculos chat/sessão persistidos pelo cliente
  Loong em seu diretório de configuração. Preserve ambos. O worker não recria
  sessões que tenham sido removidas nem resolve vínculos obsoletos sozinho.
- Token é necessário para chamar a API; nunca aparece nos eventos, stdout ou
  diagnósticos do worker. O contexto enviado ao agente contém IDs de roteamento
  e metadados dos anexos quando presentes. Texto, nomes e conteúdo de arquivos
  continuam sendo dados externos não confiáveis.
- Stdout pertence exclusivamente ao JSON-RPC. O worker grava logs em
  `state/worker.log`, com rotação de 1 MiB e três backups. Os logs
  mostram inicialização, handshake, resultado do polling, updates filtrados,
  envio/ACK de wakeups, cursor e erros — sem token, texto de mensagens ou IDs
  de usuários/chats. O ID numérico da atualização é registrado para correlação.
  `accepted` só confirma admissão, não execução nem resposta no Telegram.
  Erros também vão para stderr, que atualmente é descartado pelo Loong.
  Não execute `run` diretamente esperando um terminal interativo: ele exige
  handshake do cliente. Depois de atualizar o script/corrigir um erro fatal,
  reinicie o cliente para iniciar um novo processo Python.
- Para acompanhar no PowerShell:
  `Get-Content state/worker.log -Tail 50 -Wait`.
  Bash: `tail -f state/worker.log`. Se não existir após reiniciar,
  confira se o plugin está habilitado, o workspace e o caminho do Python.
  O helper `send` mantém seu feedback no terminal, sem disputar a rotação do
  arquivo do worker. Uma falha ao abrir o log impede o worker de iniciar.
- EOF do cliente e SIGINT/SIGTERM interrompem o worker. A requisição de polling
  em curso tem timeout de 35 segundos; o encerramento pelo cliente também mata
  o processo. Estado local e credenciais estão no `.gitignore` desta pasta.

## Diagnóstico: bot não responde

1. **Confirme qual bot pertence ao token.** Um token válido pode ser de outro
   bot. No nosso diagnóstico, o polling funcionava, mas as mensagens eram
   enviadas a um bot diferente. Confira o `username` retornado por `getMe` e
   abra o chat privado com esse usuário exato. Pressione **Start/Iniciar** e
   envie uma nova mensagem de uma conta de usuário, não de outro bot.
   Não cole o token em URLs no navegador, comandos ou logs: URLs da API contêm
   a credencial. Consultas de diagnóstico devem ler o token do arquivo e exibir
   somente os campos necessários. `getMe` e `getWebhookInfo` não consomem mensagens.
2. **Reinicie após trocar o token.** O worker mantém o token carregado na memória;
   editar `bot-token.txt` não atualiza o processo já em execução. Se
   `TELEGRAM_BOT_TOKEN` estiver definido, ele tem precedência sobre o arquivo.
3. **Leia o log Python**, não só o terminal do headless:

   ```powershell
   Get-Content state/worker.log -Tail 50 -Wait
   ```

   Atualmente `loong --headless` imprime a inicialização, mas não transmite os
   turnos nem stderr do plugin ao terminal. Pode continuar aberto mesmo depois
   de o plugin morrer: `headless client running` não é prova de que o worker
   continua saudável.
4. **Identifique onde o fluxo parou:**

   | Log | Significado / próximo passo |
   | --- | --- |
   | Sem `Worker starting` | Reinicie o cliente; confira workspace, plugin habilitado, Python e permissões do log |
   | Sem `Loong handshake complete` | Falha no início do protocolo/processo; confira a versão do cliente |
   | `Poll complete updates=0` | A API retornou uma lista vazia **antes dos filtros locais**; tirar a allowlist não resolve isso |
   | `Update filtered` | A atualização chegou, mas não passou nos filtros de usuário, chat ou tipo de mensagem |
   | `Wakeup submitting`, sem `accepted` | Problema na admissão pelo Loong; confira o próximo erro e o daemon |
   | `Wakeup accepted` | Só confirma admissão: confira modelo, execução e permissões do agente se não houver resposta |
   | `Worker failed` | Corrija a causa e reinicie o cliente; não há reinício automático do plugin |

5. **Se `updates=0` persistir**, confira identidade do bot, chat privado e novo
   envio. Em grupos, privacy mode pode impedir a entrega de mensagens comuns.
   Consulte `getWebhookInfo`: um webhook ativo é incompatível com polling.
   Garanta também que nenhum outro programa/cliente usa `getUpdates` com o mesmo
   token. Não rode um polling de diagnóstico em paralelo: pode disputar ou
   confirmar mensagens antes do worker. Erro 409 é indício de webhook/conflito.
6. **Se o evento foi aceito, mas não respondeu**, lembre que wakeup não transporta
   a resposta de volta. O agente precisa chamar `send`/`send-photo`/`send-document`.
   Confirmação de shell pode bloquear o envio no headless. Alterações no prompt,
   ferramentas ou modelo exigem nova sessão; reiniciar o cliente sozinho conserva
   a declaração das sessões persistidas.

Não publique logs com conteúdo externo, estado de conversas ou credenciais.
Se um token foi enviado em chat ou exposto em histórico de comandos, revogue-o
no BotFather e grave o substituto diretamente no arquivo local protegido.

## Variáveis

| Variável | Uso / padrão |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token opcional no ambiente; precede o arquivo |
| `TELEGRAM_BOT_TOKEN_FILE` | `bot-token.txt` |
| `TELEGRAM_ALLOWED_USER_IDS` | Obrigatória por padrão; IDs de usuários separados por vírgula |
| `TELEGRAM_ALLOW_ALL_USERS` | Somente `true` desativa a allowlist de usuários para teste; padrão desativado |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Vazia: somente privado; preenchida: somente esses chats |
| `TELEGRAM_STATE_FILE` | `state/cursor.json` |
| `TELEGRAM_LOG_FILE` | `state/worker.log` (somente `run`) |

`--token-file CAMINHO` antes de `run`/`send` sobrescreve o caminho do arquivo.
Caminhos relativos são resolvidos no workspace, não na pasta do script. Variáveis
em `wakeups.servers.telegram.env` só configuram o processo do plugin; elas não
configuram o ambiente do daemon nem do helper executado pelo agente. Para usar
um token em caminho diferente, ajuste ambos os comandos/ambientes.

## Testes

```powershell
python -m unittest discover -s . -p "test_*.py" -v
```

Os testes usam API simulada e handshake real via subprocesso; não precisam de
token nem acessam o Telegram. Contrato: [wakeup-protocol.md](docs/wakeup-protocol.md).
