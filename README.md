# EraTrade

AI-торговый бот для Hyperliquid (BTC perp, 1h). План: `План_разработки_AI-торгового_бота_Hyperliquid.md`, спецификация: `docs/spec.md`.

## Быстрый старт (testnet, локально)

```bash
cp .env.example.testnet .env
docker-compose up -d          # PostgreSQL на 127.0.0.1:5435
uv sync
uv run eratrade config-check
uv run eratrade db-check
uv run alembic upgrade head
```

## Фоновые сервисы (mainnet, постоянно)

Профиль `collector` поднимает три сервиса без доступа к ключу API-кошелька:
- `collector-mainnet` — свечи BTC 1h и funding/OI в `eratrade_mainnet`;
- `reconciler-mainnet` — сверка с биржей раз в минуту (критичное расхождение → запрет входов, `LIVE_PAUSED`);
- `pg-backup` — ежедневный бэкап обеих БД.

Коллектор собирает публичные данные BTC 1h и funding/OI с mainnet в `eratrade_mainnet`
(режим `DISABLED`, ключи не нужны). Плюс ежедневный бэкап обеих БД в `backups/` (14 дней).

```bash
cp .env.example.mainnet .env.mainnet            # пароль БД: eratrade_local для локального compose
docker-compose --profile collector up -d --build
docker logs -f eratrade-collector-mainnet
ERATRADE_ENV_FILE=.env.mainnet uv run eratrade data-status
```

## Управление (CLI)

```bash
uv run eratrade status                          # режим, запрет входов, heartbeat, аудит
uv run eratrade set-mode TESTNET -r "причина"   # смена режима, пишется в аудит
uv run eratrade market                          # meta, funding/OI, свечи; meta сохраняется в БД
uv run eratrade account                         # нужен HL_ACCOUNT_ADDRESS в .env
uv run eratrade collect                         # коллектор в терминале (обычно — в Docker)
uv run eratrade data-status                     # накоплено, пропуски, heartbeat коллектора
uv run eratrade backfill --hours 48             # догрузить окно свечей через REST
```

Окружение выбирается через `ERATRADE_ENV_FILE` (один файл или несколько через запятую).
Testnet не используется — работаем на mainnet с минимальными суммами (см. план).

| Файл | Что внутри | Кто читает |
|---|---|---|
| `.env.mainnet` | сеть, БД, адрес аккаунта, токен Logfire | коллектор, CLI |
| `.env.mainnet.secrets` | ключ API-кошелька (права 600) | только торговый процесс |

```bash
ERATRADE_ENV_FILE=.env.mainnet uv run eratrade account
ERATRADE_ENV_FILE=.env.mainnet,.env.mainnet.secrets uv run eratrade config-check
ERATRADE_ENV_FILE=.env.mainnet,.env.mainnet.secrets uv run eratrade wallet-check     # только чтение
ERATRADE_ENV_FILE=.env.mainnet,.env.mainnet.secrets uv run eratrade verify-signing   # no-op отмена
```

Ручная торговля (этап 6; каждый ордер подтверждается в терминале):

```bash
E=".env.mainnet,.env.mainnet.secrets"
ERATRADE_ENV_FILE=$E uv run eratrade open long --notional 12   # IOC-вход + биржевой stop-market
ERATRADE_ENV_FILE=$E uv run eratrade close                     # reduceOnly-закрытие, снятие стопа
ERATRADE_ENV_FILE=$E uv run eratrade cancel <cloid>
ERATRADE_ENV_FILE=$E uv run eratrade resolve                   # сверка незавершённых ордеров по cloid
ERATRADE_ENV_FILE=.env.mainnet uv run eratrade orders
```

Перед `open` и `close` всегда выполняется сверка с биржей (вход отменяется при критичных
расхождениях). Разбор расхождений:

```bash
ERATRADE_ENV_FILE=.env.mainnet uv run eratrade reconcile                     # разовая сверка
ERATRADE_ENV_FILE=.env.mainnet uv run eratrade accept-position -r "закрыл вручную"
ERATRADE_ENV_FILE=.env.mainnet uv run eratrade unblock -r "разобрался"
```

Риск и аварийные команды (этап 8):

```bash
ERATRADE_ENV_FILE=.env.mainnet uv run eratrade risk-status           # equity, PnL дня/недели, лимиты
ERATRADE_ENV_FILE=.env.mainnet uv run eratrade pause -r "..."        # запрет входов (ключ не нужен)
ERATRADE_ENV_FILE=$E uv run eratrade flatten                         # закрыть позицию
ERATRADE_ENV_FILE=$E uv run eratrade cancel-all -r "..."             # отменить все ордера (и стопы!)
ERATRADE_ENV_FILE=$E uv run eratrade kill -r "..."                   # pause → flatten → cancel → DISABLED
```

Размер позиции считается от риска до стопа: `min(10 USDC, 0.5% equity) / дистанция`, затем
урезается лимитом номинала, маржой и `--notional`. Equity = spot USDC + perp account value.
Уведомления — Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` в `.env.mainnet`), иначе в лог.

Новые входы разрешены только в режиме `LIVE_ENABLED` (включается `set-mode` из `LIVE_PAUSED`
с подтверждением); закрытие и стопы — также в `LIVE_PAUSED`.

Защита торгового процесса (этап 5):
- подписываются только `order`, `cancel`, `cancelByCloid`, `updateLeverage`, `scheduleCancel` —
  вывод средств и переводы невозможны даже при ошибке в коде;
- nonce выдаётся атомарно в PostgreSQL (`max(now_ms, last + 1)`), переживает рестарт;
- второй торговый процесс с тем же API-кошельком не запустится (advisory lock);
- при старте проверяются роль ключа (agent, не основной кошелёк), срок действия, часы (±2 с).

## Проверки

```bash
uv run pytest          # БД-тесты используют eratrade_pytest_testnet и пропускаются без PostgreSQL
uv run alembic check   # модели и миграции совпадают
ERATRADE_LIVE_TESTS=1 uv run pytest tests/live   # формат ответов живого testnet API
uv run ruff check .
uv run mypy
```

## Бэкапы

```bash
scripts/backup_db.sh eratrade_testnet
scripts/restore_check.sh backups/<файл>.dump
```
