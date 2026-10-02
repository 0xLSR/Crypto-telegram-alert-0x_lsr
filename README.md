# Crypto-telegram-alert-0x_lsr

Bot de alertas Telegram para tokens Solana, usando primeiro a API pública do [GeckoTerminal](https://api.geckoterminal.com/docs/index.html) e [DexScreener](https://docs.dexscreener.com/api/reference) como fallback. O GitHub Actions inicia o polling automaticamente a cada seis horas e também permite execução manual. Para execução realmente contínua sem reiniciar manualmente, use o Background Worker do Render descrito abaixo.

## Comandos e menus interativos

- `/start`: menu principal em português com botões para consultar preço, alertas, adicionar token, lista e ajuda.
- `/help`: guia rápido em português e comandos de compatibilidade.
- `/price` (`/preço`, `/preco`): abre o seletor inline dos tokens monitorados neste chat. `/price <endereço>` e um endereço enviado sozinho consultam diretamente.
- `/watch <endereço>` (`/monitorar`, `/adicionar`): monitora imediatamente. Sem endereço, inicia o fluxo guiado com confirmação.
- `/unwatch <endereço>` (`/remover`): remove diretamente. Sem endereço, abre o seletor de remoção com confirmação.
- `/list` (`/lista`): mostra os tokens deste chat como botões; toque em um token para abrir os dados completos.

Os botões de navegação editam a mensagem atual, confirmam callbacks imediatamente e usam identificadores curtos, sem endereços completos nos dados do callback. A lista é paginada em grupos de dez tokens. O menu de comandos nativo do Telegram é configurado na inicialização com `/start`, `/price`, `/list`, `/watch`, `/unwatch` e `/help`, em português; aliases continuam aceitos como compatibilidade, sem aparecer no menu.

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
python -m py_compile bot.py tests/test_telegram_api.py
python -m unittest discover -s tests -v
```
