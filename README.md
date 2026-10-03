# Crypto-telegram-alert-0x_lsr

Bot de alertas Telegram para tokens Solana, usando primeiro a API pública do [GeckoTerminal](https://api.geckoterminal.com/docs/index.html) e [DexScreener](https://docs.dexscreener.com/api/reference) como fallback. O GitHub Actions inicia o polling automaticamente a cada seis horas e também permite execução manual. Para execução realmente contínua sem reiniciar manualmente, use o Background Worker do Render descrito abaixo.

## Fase 1 — Alertas e consultas de tokens

- `/start`: menu principal em português com botões para consultar preço, alertas, adicionar token, lista e ajuda.
- `/help`: guia rápido em português e comandos de compatibilidade.
- `/price` (`/preço`, `/preco`): abre o seletor inline dos tokens monitorados neste chat. `/price <endereço>` e um endereço enviado sozinho consultam diretamente.
- `/watch <endereço>` (`/monitorar`, `/adicionar`): monitora imediatamente. Sem endereço, inicia o fluxo guiado com confirmação.
- `/unwatch <endereço>` (`/remover`): remove diretamente. Sem endereço, abre o seletor de remoção com confirmação.
- `/list` (`/lista`): mostra os tokens deste chat como botões; toque em um token para abrir os dados completos.

Os botões de navegação editam a mensagem atual, confirmam callbacks imediatamente e usam identificadores curtos, sem endereços completos nos dados do callback. A lista é paginada em grupos de dez tokens. O menu de comandos nativo do Telegram é configurado na inicialização com `/start`, `/price`, `/list`, `/watch`, `/unwatch` e `/help`, em português; aliases continuam aceitos como compatibilidade, sem aparecer no menu.

## Minha carteira — Fase 2 somente leitura

No Telegram, abra **/start → 💼 Minha carteira → ➕ Cadastrar carteira** e envie somente o endereço público Solana. O cadastro é validado e associado ao seu ID e chat do Telegram. Não envie seed phrase, chave privada ou qualquer credencial; o bot não armazena chaves, assina nem envia transações.

Se essa for a carteira que você usa no FOMO, informe o endereço público dela. O bot observa a blockchain pelo endereço e não presume vínculo nem usa uma API oficial do FOMO.

A carteira é consultada por RPC público Solana para saldo SOL, contas de tokens SPL e assinaturas recentes. Movimentações aparecem como **🔄 Movimentação detectada** porque RPC público não prova com segurança se uma operação foi compra, venda, swap ou transferência. Os alertas incluem link Solscan; valores USD de tokens usam as fontes de mercado já existentes quando há cotação e, nos demais casos, aparecem como indisponíveis. PnL fica indisponível nesta versão. O histórico mantém até 20 movimentações por carteira.

Nenhuma nova Secret é necessária. `SOLANA_RPC_URL` é opcional e usa `https://api.mainnet-beta.solana.com` por padrão; `WALLET_CHECK_INTERVAL_SECONDS` é opcional e vale `60` segundos por padrão (aceita de 15 a 3600). Preços USD usam GeckoTerminal e o fallback DexScreener já configurados no bot.

## Inteligência de mercado — Fase 3 somente analítica

O menu `/start` agora inclui **Scanner**, **Analisar token**, **Oportunidades**, **Setup de entrada**, **Setup de saída** e **Performance**. Também é possível enviar `/analisar <endereço>`, `/scanner`, `/oportunidades`, `/entrada`, `/saida` ou `/performance`. Scanner e oportunidades usam os tokens monitorados pelo chat e, quando existe uma carteira pública cadastrada, seus tokens com saldo e cotação disponível. O resultado da consulta da carteira fica em cache por dois minutos; o scanner não faz crawling do mercado.

O módulo separado `market_intelligence.py` guarda snapshots dos tokens monitorados junto com o ciclo de preços existente: timestamp, preço, volume 24h, market cap, FDV, liquidez e variação 24h. O histórico respeita `HISTORY_RETENTION_HOURS` (padrão 24 h) e sobrevive às reinicializações porque fica dentro de `data/state.json`, que o Actions já restaura e salva em cache. A coleta não dispara consultas extras às fontes de mercado.

A análise combina retornos observados, aceleração de preço e volume quando há amostras, estrutura recente, liquidez, volatilidade e relação volume/market cap. Ela apresenta score de confluência (0–100), Entry Score, Exit Risk, estado de mercado, confiança de cobertura dos dados, possível entrada tardia, rompimentos, pullbacks, perda de suporte e resultados observados após sinais para horizontes futuros. A confiança indica cobertura/qualidade dos dados, **não** probabilidade de lucro. Os sinais inteligentes só notificam mudanças relevantes de estado e respeitam o cabeçalho curto de alerta.

Todo token adicionado à watchlist também recebe automaticamente monitoramento de fluxo de mercado. Whale Flow usa trades públicos por pool do GeckoTerminal para estimar compras e vendas grandes, fluxo líquido, aceleração e sequências; quando os dados cobrem a janela, exibe agregados de 5 min, 15 min, 30 min, 1 h, 4 h e 24 h. As categorias de tamanho comparam o valor da operação com a liquidez e o volume recente do pool, em vez de usar um limite fixo em dólares. O score pode incluir Whale Flow como componente configurável e limitado; o fluxo não determina sozinho o score. Eventos relevantes são agrupados por ciclo e têm cooldown próprio, separado dos alertas de preço.

**Whale Flow é uma métrica de mercado baseada em dados públicos e não representa necessariamente atividade de uma única whale.** Ela não identifica entidades nem atribui intenção. O endpoint público do GeckoTerminal informa até 300 trades recentes por pool, com limite aproximado de 10 chamadas por minuto e cache de um minuto. O bot limita chamadas a um intervalo mínimo de 6,25 s, faz no máximo três consultas de fluxo por ciclo de preços e alterna os pools monitorados; com uma watchlist grande, cada token será revisitado com menor frequência. O histórico local mantém até 1.000 trades por token e pode ter lacunas se mais de 300 operações ocorrerem entre consultas; um retorno de 300 linhas é marcado como possivelmente truncado. As bandas de tamanho são heurísticas de participação relativa na liquidez/volume: 0,1% indica participação observável, 1% sinaliza impacto potencial material e 5% uma participação muito elevada; elas não medem impacto efetivo nem probabilidade. A aceleração compara o valor em USD dos últimos cinco minutos com os cinco minutos anteriores e só alerta se o fluxo também for material em relação à liquidez. O DexScreener fornece dados agregados de preço, volume, liquidez e contagens de transações, sem trades individuais na API documentada; portanto não é usado para inventar lado de trade. Se não houver pool GeckoTerminal, `kind` confiável ou trades suficientes, o fluxo/score de fluxo fica indisponível e nenhum alerta direcional é fabricado.

Níveis de entrada, invalidação e resistência/TP1 só aparecem quando derivados da amostra histórica; TP2 permanece indisponível enquanto não houver uma resistência observada confiável. Uma amostra curta mostra “Dados insuficientes” e não entra em Oportunidades. Sem fonte confiável, o modelo não estima holders, concentração ou idade do token. Tokens vistos apenas na carteira começam sem histórico próprio e têm baixa confiança até serem acompanhados por snapshots. As leituras de posição são somente saldos públicos e valor atual; PnL não é estimado sem custo de aquisição confiável. As classificações e limiares são heurísticas experimentais, não aconselhamento financeiro nem previsão.

Tudo é read-only. A inteligência não pede chaves, não assina transações e não executa compra, venda ou swap. GeckoTerminal continua como fonte principal, com DexScreener como fallback; se ambas falharem, o bot informa indisponibilidade em vez de gerar um sinal novo.

Variáveis de configuração (todas opcionais; os padrões também são aplicados se não forem definidas):

| Variável | Padrão | Uso |
| --- | --- | --- |
| `INTELLIGENCE_ENABLED` | `true` | Coleta snapshots e habilita análise. |
| `INTELLIGENCE_INTERVAL_SECONDS` | `60` | Intervalo mínimo entre snapshots; reutiliza o ciclo de preço, sem polling extra. |
| `HISTORY_RETENTION_HOURS` | `24` | Retenção do histórico local (1–720 h). |
| `MIN_INTELLIGENCE_SCORE` | `75` | Score mínimo exibido em Oportunidades. |
| `SMART_ALERTS_ENABLED` | `true` | Envia alertas em mudanças relevantes de estado. |
| `SMART_FLOW_COOLDOWN_MINUTES` | `30` | Cooldown dos alertas inteligentes de fluxo (1–1440 min). |
| `FLOW_REQUESTS_PER_CYCLE` | `3` | Limite de pools consultados por ciclo (1–3); alterna tokens da watchlist. |
| `WHALE_FLOW_WEIGHT_PERCENT` | `10` | Peso do componente Whale Flow no score (0–20%); aplicado somente com trades direcionais suficientes. |

O limiar é configurado por `ALERT_THRESHOLD_PERCENT` (padrão 10%) e o intervalo mínimo entre alertas por token por `ALERT_COOLDOWN_MINUTES` (padrão 30). A verificação de mercado ocorre a cada minuto por padrão e só consulta as APIs quando há tokens acompanhados. A API de token do GeckoTerminal inclui os pools principais; o monitor consulta em lotes de até 30 endereços. A variação de 24h é lida do pool com maior liquidez. Se a consulta falhar, o bot tenta DexScreener. A indisponibilidade das duas fontes não interrompe o polling do Telegram. Endereços são validados como chaves públicas Solana Base58 de 32 bytes.

O bot valida com segurança o formato de `TELEGRAM_BOT_TOKEN`, chama `getMe` e remove eventual webhook sem descartar atualizações pendentes na inicialização. Token rejeitado com HTTP 401 encerra o processo com erro claro, sem repetir o retry indefinidamente nem registrar o segredo. Ele então usa `getUpdates` com timeout Telegram de 1 segundo. A primeira consulta de cada execução começa em offset 0 para receber pendências mesmo se o offset do cache estiver inválido; atualizações são confirmadas e salvas individualmente após o processamento. Erros de rede/API aparecem nos logs com serviço, código HTTP e resposta; o token é removido dos diagnósticos.

## Execução manual pelo GitHub Actions

O workflow agenda execuções em `00:00`, `06:00`, `12:00` e `18:00 UTC`, além do botão manual em **Actions → Telegram Crypto Alerts → Run workflow**. Cada job recebe `TELEGRAM_BOT_TOKEN` dos GitHub Secrets e monitora por 5 horas e 50 minutos, salvando offset e watches no cache ao terminar.

O GitHub encerra jobs em runners hospedados após no máximo 6 horas; por isso, uma execução manual no Actions não consegue manter o bot 24 horas por dia sem intervenção. [Limites oficiais do GitHub Actions](https://docs.github.com/en/enterprise-cloud%40latest/actions/reference/limits). O evento `workflow_dispatch` habilita o botão **Run workflow**. [Iniciar workflows manualmente](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)

## Hospedagem contínua no Render

O arquivo `render.yaml` define um **Background Worker** Python de 0,5 CPU/512 MB com uma instância, deploy automático após commits e disco persistente montado em `/var/data`. O estado, incluindo `offset` e a lista de tokens, fica em `/var/data/state.json`. O processo se reconecta após falhas temporárias de rede; o Render executa o worker continuamente e reinicia processos que encerram por falha. Consulte a [documentação de Background Workers](https://render.com/docs/background-workers), [deploys](https://render.com/docs/deploys) e [discos persistentes](https://render.com/docs/disks).

### Primeira implantação

1. Crie uma conta no Render e escolha **New → Blueprint**. Conecte o repositório `0xLSR/Crypto-telegram-alert-0x_lsr` e a branch `main`; o Render lerá `render.yaml` e criará o worker.
2. No painel do worker, abra **Environment** e defina `TELEGRAM_BOT_TOKEN` com um token ativo fornecido pelo `@BotFather`. Nunca coloque o token em um commit, issue ou log. Não envie o token a terceiros.
3. Opcionalmente defina `TELEGRAM_ALLOWED_USER_IDS` como IDs numéricos separados por vírgula. Se ficar vazio, qualquer pessoa que encontrar o bot poderá enviar comandos.
4. Salve o ambiente e aguarde o deploy. Confira **Events** e **Logs**; um início válido registra que `getMe` autenticou o bot. Abra o chat do bot, pressione **Start** ou envie `/start`.
5. A partir daí, pushes na branch conectada fazem deploy automático. Para atualizar o código, faça commit e push em `main`; para mudar secrets/variáveis, edite **Environment** no Render.

O plano de worker e o disco persistente são pagos. Consulte [preços atuais do Render](https://render.com/pricing) antes de provisionar. O disco é necessário para manter o estado após reinicializações; mantenha `numInstances: 1`, pois múltiplas instâncias com o mesmo offset e disco não são suportadas por este consumidor único de `getUpdates`.

### Configuração do worker

| Variável | Tipo | Padrão | Uso |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Secret obrigatório | — | Token privado do BotFather. |
| `TELEGRAM_ALLOWED_USER_IDS` | Secret opcional | vazio | IDs autorizados, separados por vírgula. |
| `ALERT_THRESHOLD_PERCENT` | variável | `10` | Movimento percentual mínimo (0,1–1000). |
| `ALERT_COOLDOWN_MINUTES` | variável | `30` | Espera mínima entre alertas (1–10080 min). |
| `BOT_STATE_FILE` | variável | `/var/data/state.json` no Render | Caminho persistente para offset e watches. |
| `PRICE_CHECK_SECONDS` | variável opcional | `60` | Intervalo de monitoramento de preços, mínimo 15 s. |
| `LOG_LEVEL` | variável opcional | `INFO` | Nível dos logs. |

**Atenção ao erro `Telegram API 401: Unauthorized`:** o Telegram rejeitou o token efetivamente enviado pelo processo. O código não consegue consultar nem corrigir o conteúdo de um Secret write-only. Para o GitHub Actions, confira em **Settings → Secrets and variables → Actions → `TELEGRAM_BOT_TOKEN`** se o valor ainda é o token ativo do `@BotFather`; substitua-o ali caso tenha sido revogado ou copiado incorretamente. Para o Render, configure o valor separadamente em **Render → serviço → Environment**. Nunca cole o token em issues, commits ou logs. Um 401 encerra a execução com erro claro em vez de manter o bot aparentemente ativo.

## Desenvolvimento local

Requer Python 3.12 ou superior; o projeto usa apenas a biblioteca padrão. Configure o token no ambiente e execute `python bot.py`:

```powershell
$env:TELEGRAM_BOT_TOKEN = "token obtido no BotFather"
python bot.py
```

O estado local é `data/state.json`. Em produção, o Blueprint aponta para o disco persistente e o Actions restaura/salva o cache entre execuções. Não rode uma cópia local simultaneamente ao Render ou ao Actions: um bot Telegram só deve ter um consumidor de `getUpdates`.

## GitHub Actions

`.github/workflows/telegram-bot.yml` agenda o bot a cada seis horas e permite início manual. `.github/workflows/ci.yml` valida sintaxe e executa testes em `push` e `pull_request`. Não deixe o worker do Render e o workflow rodando ao mesmo tempo: ambos consumiriam `getUpdates` do mesmo bot.

## Testes

```sh
python -m py_compile bot.py wallet.py market_flow.py market_intelligence.py tests/test_telegram_api.py tests/test_wallet.py tests/test_market_flow.py tests/test_market_intelligence.py
python -m unittest discover -s tests -v
```
