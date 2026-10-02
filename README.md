# Crypto-telegram-alert-0x_lsr

Bot de alertas de criptomoedas no Telegram, com suporte inicial a tokens Solana e dados públicos do [DexScreener](https://docs.dexscreener.com/api/reference).

## Recursos

- `/start` e `/help`: instruções.
- `/price <TOKEN ou endereço>`: preço, variação de 24h, liquidez, volume, market cap/FDV e link do DexScreener.
- `/watch <endereço Solana>` e `/unwatch <endereço>`: iniciar/parar monitoramento.
- `/list`: listar os tokens que este chat acompanha.
- Alertas por variação percentual configurável, com referência atualizada após cada alerta e período mínimo entre alertas.
- Estado persistido atomicamente em `data/state.json` (offset do Telegram e tokens acompanhados).

Para buscas por símbolo, o bot escolhe o par Solana de maior liquidez, priorizando correspondência exata do símbolo/nome. Endereços são a forma recomendada para identificar tokens sem ambiguidade. Os dados e sua disponibilidade dependem do DexScreener.

## Configuração local

Requer Python 3.10 ou superior. O bot usa apenas a biblioteca padrão do Python; `requirements.txt` não tem dependências externas.

1. Crie um bot no Telegram falando com [@BotFather](https://t.me/BotFather) e copie o token.
2. Defina o token no ambiente, sem colocá-lo no código ou em arquivos versionados:

   ```powershell
   $env:TELEGRAM_BOT_TOKEN = "seu-token"
   python bot.py
   ```

   No macOS/Linux: `export TELEGRAM_BOT_TOKEN="seu-token"` e depois `python3 bot.py`.
3. Abra o bot no Telegram e envie `/start`.

O estado local é criado em `data/state.json`. Para outro local, defina `BOT_STATE_FILE`. Faça backup desse arquivo para preservar watches e atualizações processadas.

## Configuração no GitHub Actions

O workflow `.github/workflows/telegram-bot.yml` inicia o bot em execuções agendadas, a cada cinco minutos, e cada execução monitora por quatro minutos. Configure no repositório em **Settings → Secrets and variables → Actions**:

### Secrets obrigatórios

| Nome | Conteúdo |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token fornecido pelo BotFather. |

### Secrets opcionais

| Nome | Conteúdo |
| --- | --- |
| `TELEGRAM_ALLOWED_USER_IDS` | IDs numéricos autorizados separados por vírgula, por exemplo `12345678,98765432`. Vazio permite comandos de qualquer pessoa que encontre o bot. |

### Variables opcionais

| Nome | Padrão | Significado |
| --- | --- | --- |
| `ALERT_THRESHOLD_PERCENT` | `10` | Variação absoluta mínima em % desde o último alerta (de `0.1` a `1000`). |
| `ALERT_COOLDOWN_MINUTES` | `30` | Intervalo mínimo entre alertas do mesmo token (de `1` a `10080`). |

Depois de salvar o secret, habilite **Actions** no repositório e execute o workflow manualmente uma vez em **Actions → Telegram Crypto Alerts → Run workflow**. Agendamentos do GitHub podem atrasar, ser suspensos em repositórios inativos e não oferecem execução contínua. O cache do Actions preserva o arquivo de estado entre execuções, mas pode expirar ou ser removido; exporte/guarde `data/state.json` se precisar de persistência garantida. Para disponibilidade contínua e estado durável, rode `python bot.py` em um servidor/serviço sempre ligado com armazenamento persistente.

Não imprima nem compartilhe o token. O bot evita registrá-lo nos logs. Se ele for exposto, revogue-o pelo BotFather e atualize o Secret.

## Execução e configuração

```text
TELEGRAM_BOT_TOKEN                 obrigatório
TELEGRAM_ALLOWED_USER_IDS          opcional; IDs separados por vírgula
ALERT_THRESHOLD_PERCENT            padrão 10
ALERT_COOLDOWN_MINUTES             padrão 30
BOT_STATE_FILE                     padrão data/state.json
RUN_FOR_SECONDS                    padrão 0 (executa até ser encerrado)
```

O Telegram permite um único consumidor de `getUpdates` por bot. Não execute ao mesmo tempo uma cópia local e o workflow, pois ambos podem disputar as atualizações.
