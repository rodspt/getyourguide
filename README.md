# GetYourGuide Monitor

Monitor automático de vagas no [GetYourGuide](https://www.getyourguide.com). A aplicação consulta periodicamente a página de uma atividade e envia alertas no Telegram quando detecta disponibilidade para a data configurada.

Roda em Docker com Playwright (navegador headless), pois o site renderiza parte do conteúdo no cliente.

## Como funciona

A cada ciclo de verificação, o monitor:

1. Monta a URL da atividade com parâmetros de data, idioma e participantes
2. Abre a página no Chromium (headless)
3. Lê os elementos `span.input-title` da barra de reserva
4. Compara o label da data com o valor esperado (ex.: `18 de jul. de 2026`)
5. Se a data estiver disponível, clica em **Verificar disponibilidade** e extrai os horários da página
6. Envia mensagem no Telegram com o resultado
7. Aguarda o intervalo configurado e repete

Enquanto a vaga continuar disponível, **um novo alerta é enviado a cada verificação**, até você parar o container.

### Critério de disponibilidade da data

A vaga é considerada encontrada quando algum `span.input-title` exibe exatamente o label configurado em `GYG_EXPECTED_DATE_LABEL`. Se essa variável não for definida, o label é gerado automaticamente a partir de `GYG_TARGET_DATE` (formato português: `18 de jul. de 2026`).

### URL montada automaticamente

A partir de `GYG_URL` (sem query string), a aplicação adiciona:

| Parâmetro   | Origem              |
|------------|---------------------|
| `date_from` | `GYG_TARGET_DATE`   |
| `lang`      | `GYG_LANG`          |
| `_pc`       | `GYG_PARTICIPANTS`  |

Exemplo genérico:

```
https://www.getyourguide.com/.../atividade-t123456/?date_from=2026-07-18&lang=pt&_pc=2
```

## Modos de busca

Configure com `GYG_CHECK_MODE`:

### `day` — somente dia (padrão)

- Verifica se a data aparece no seletor da página
- Clica em **Verificar disponibilidade** e lista todos os horários encontrados no alerta
- `GYG_PREFERRED_TIME` é ignorado neste modo

### `time` — dia + horário planejado

- Exige `GYG_PREFERRED_TIME` (formato `HH:MM`, ex.: `14:30`)
- Compara o horário planejado com os slots disponíveis na página
- A mensagem informa se o horário planejado está ou não disponível e, quando possível, lista os demais horários

Aliases aceitos: `dia`, `data`, `horario`, `horário` (todos mapeados para `day` ou `time`).

## Telegram

### Credenciais usadas

| Variável               | Obrigatória | Função                                      |
|------------------------|-------------|---------------------------------------------|
| `TELEGRAM_BOT_TOKEN`   | Sim         | Autenticação do bot na Bot API              |
| `TELEGRAM_CHANNEL_ID`  | Sim*        | Canal principal de destino                  |
| `DRAMAFLEX_CHANNEL`    | Não         | Canal alternativo (fallback)                |
| `TELEGRAM_CHANNEL_NAME`| Não         | Alias para ID de canal                      |
| `TELEGRAM_CHAT_ID`     | Não         | Chat privado ou outro destino               |

\* Pelo menos um ID de canal/chat deve estar configurado.

### Ordem de envio

Os canais são tentados nesta ordem: `TELEGRAM_CHANNEL_ID` → `TELEGRAM_CHANNEL_NAME` → `DRAMAFLEX_CHANNEL` → `TELEGRAM_CHAT_ID`.

O envio **para no primeiro canal que funcionar**. Canais extras só são usados se o anterior falhar.

### Pré-requisitos

1. Criar um bot com [@BotFather](https://t.me/BotFather) e obter o token
2. Adicionar o bot como **administrador** do canal
3. Conceder permissão **Postar mensagens**
4. Usar o ID numérico do canal (formato `-100...`)

Na inicialização, a aplicação valida o token (`getMe`) e testa acesso a cada canal configurado (`getChat`). Se nenhum canal for acessível, o container encerra com instruções no log.

## Variáveis de ambiente

Copie o template e preencha com seus valores:

```bash
cp .env.example .env
```

| Variável                  | Descrição                                              | Exemplo genérico        |
|---------------------------|--------------------------------------------------------|-------------------------|
| `GYG_URL`                 | URL base da atividade (sem query string)               | URL da página no GYG    |
| `GYG_TARGET_DATE`         | Data desejada                                          | `18/07/2026`            |
| `GYG_EXPECTED_DATE_LABEL` | Label exato no seletor (opcional)                      | `18 de jul. de 2026`    |
| `GYG_PARTICIPANTS`        | Número de participantes                                | `1`                     |
| `GYG_LANG`                | Idioma da URL                                          | `pt`                    |
| `GYG_CHECK_MODE`          | `day` ou `time`                                        | `day`                   |
| `GYG_PREFERRED_TIME`      | Horário planejado (obrigatório se `time`)              | `14:30`                 |
| `CHECK_INTERVAL_SECONDS`  | Intervalo entre verificações, em segundos              | `300`                   |
| `TELEGRAM_BOT_TOKEN`      | Token do bot                                           | obtido no BotFather     |
| `TELEGRAM_CHANNEL_ID`     | ID do canal de alertas                                 | `-100xxxxxxxxxx`      |
| `STATE_FILE`              | Caminho do arquivo de estado (log interno)             | `data/state.json`       |

> **Segurança:** nunca commite o arquivo `.env`. Ele já está listado no `.gitignore`.

## Executando com Docker

### Subir o monitor

```bash
docker compose up -d --build
```

### Ver logs

```bash
docker compose logs -f
```

### Parar

```bash
docker compose down
```

## Estrutura do projeto

```
.
├── monitor.py          # Lógica principal
├── Dockerfile          # Imagem com Python + Playwright
├── docker-compose.yml  # Orquestração
├── requirements.txt    # Dependências Python
├── .env.example        # Template de configuração
└── data/               # Estado persistente (montado como volume)
    └── state.json
```

## Logs úteis

Durante a operação normal, você verá mensagens como:

```
[INFO] Verificando: https://www.getyourguide.com/...?date_from=...
[INFO] Label esperado: 18 de jul. de 2026
[INFO] Labels encontrados: ['1 participante', '18 de jul. de 2026', 'Português']
[INFO] Vaga detectada para 18 de jul. de 2026
[INFO] Horários extraídos: 10:00, 13:00, 16:30, 17:30
[INFO] Alerta enviado ao Telegram
[INFO] Próxima verificação em 300 segundos
```

## Limitações

- Depende da estrutura HTML atual do GetYourGuide; mudanças no site podem exigir ajustes nos seletores
- O intervalo mínimo recomendado é de alguns minutos, para não sobrecarregar o site
- Alertas repetidos enquanto a vaga existir são intencionais; aumente `CHECK_INTERVAL_SECONDS` se quiser menos mensagens
