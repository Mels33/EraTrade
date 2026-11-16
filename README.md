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

## Управление (CLI)

```bash
uv run eratrade status                          # режим, запрет входов, heartbeat, аудит
uv run eratrade set-mode TESTNET -r "причина"   # смена режима, пишется в аудит
uv run eratrade market                          # meta, funding/OI, свечи; meta сохраняется в БД
uv run eratrade account                         # нужен HL_ACCOUNT_ADDRESS в .env
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
