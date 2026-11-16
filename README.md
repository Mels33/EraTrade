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

## Коллектор данных (mainnet, постоянно)

Собирает публичные данные BTC 1h и funding/OI с mainnet в `eratrade_mainnet`
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
```

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
