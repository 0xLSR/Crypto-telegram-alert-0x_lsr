# Crypto-telegram-alert-0x_lsr

Bot de alertas Telegram para tokens Solana, usando os dados públicos do [DexScreener](https://docs.dexscreener.com/api/reference). O processo foi projetado para permanecer em execução como um worker contínuo. GitHub Actions executa somente CI; não faz polling do Telegram.

## Comandos

- `/start` e `/help`: instruções.
- `/price SOL` ou `/price <endereço>`: preço, variação em 24h, liquidez, volume e market cap/FDV quando disponíveis.
- `/watch <endereço Solana>` e `/unwatch <endereço>`: acompanhar/remover um token.
- `/list`: listar os tokens acompanhados por este chat.

O limiar é configurado por `ALERT_THRESHOLD_PERCENT` (padrão 10%) e o intervalo mínimo entre alertas por token por `ALERT_COOLDOWN_MINUTES` (padrão 30). A verificação de mercado ocorre a cada minuto por padrão e só chama o DexScreener quando há tokens acompanhados. Consultas de monitoramento são agrupadas em lotes de até 30 endereços. Falhas do DexScreener são registradas e tentadas novamente; não interrompem o polling do Telegram.

O bot chama `getMe` e remove eventual webhook sem descartar atualizações pendentes na inicialização. Ele então usa `getUpdates` com timeout Telegram de 25 segundos, o que permite respostas imediatas sem polling agressivo. Erros de rede/API aparecem nos logs com serviço, código HTTP e resposta; o token é removido dos diagnósticos.

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

**Atenção ao erro `Telegram API 401: Unauthorized`:** esse resultado de `getMe` significa que o token atualmente configurado no serviço é inválido, foi revogado ou foi copiado incorretamente. O token não pode ser corrigido por uma alteração no código. Gere/consulte um token ativo no BotFather e substitua o valor diretamente em **Render → serviço → Environment → `TELEGRAM_BOT_TOKEN`**, sem compartilhá-lo. O valor existente de `TELEGRAM_BOT_TOKEN` no GitHub não é copiado automaticamente para o Render (secrets são write-only) e o bot só responderá depois que a autenticação passar. O bot nunca registra o token.

## Desenvolvimento local

Requer Python 3.12 ou superior; o projeto usa apenas a biblioteca padrão. Configure o token no ambiente e execute `python bot.py`:

```powershell
$env:TELEGRAM_BOT_TOKEN = "token obtido no BotFather"
python bot.py
```

O estado local é `data/state.json`. Em produção, o Blueprint aponta para o disco persistente. Não rode uma cópia local simultaneamente ao worker: um bot Telegram só deve ter um consumidor de `getUpdates`.

## GitHub Actions

`.github/workflows/telegram-bot.yml` roda validação de sintaxe e testes em `push` e `pull_request`. Não há schedule, polling, token secreto ou processo do bot no Actions. O Render é responsável pela execução contínua e pelo deploy automático.

## Testes

```sh
python -m py_compile bot.py tests/test_telegram_api.py
python -m unittest discover -s tests -v
```
