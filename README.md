# EraTrade

AI-торговый бот для Hyperliquid (BTC perp, 1h). План: `План_разработки_AI-торгового_бота_Hyperliquid.md`, спецификация: `docs/spec.md`.

## Запуск одной командой

```bash
scripts/eratrade.sh up        # БД + API с панелью + коллектор + сверка + бэкапы (Docker)
scripts/eratrade.sh status    # контейнеры и сводка состояния бота
scripts/eratrade.sh logs api-mainnet
scripts/eratrade.sh down      # остановить всё (данные БД сохраняются)
scripts/eratrade.sh dev       # разработка: БД в Docker, API и панель локально (http://127.0.0.1:5173)
```

Нужны `.env.mainnet` и `.env.mainnet.secrets`; если в секретах нет `API_TOKEN`, скрипт сгенерирует его.
Панель: http://127.0.0.1:8000, вход — `API_TOKEN` из `.env.mainnet.secrets`.

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

## Панель управления (этап 9)

Сервис `api-mainnet` (профиль `collector`) — FastAPI + React на http://127.0.0.1:8000
(только локально; снаружи — через SSH-туннель или VPN). Вход — `API_TOKEN` из
`.env.mainnet.secrets`. Документация API: http://127.0.0.1:8000/api/docs

- Видно: сеть и режим, запрет входов и причина, heartbeat коллектора/сверки, свежесть данных,
  результат сверки, equity, PnL дня/недели против лимитов, позиция и стоп, ордера, fills, аудит.
- Команды: pause, снять запрет, flatten, cancel all, kill — с причиной, подтверждением
  (ввод имени команды) и Idempotency-Key. Открыть позицию через панель нельзя.

Разработка панели: `cd frontend && npm install && npm run dev` (проксирует /api на :8000).

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

## Признаки и обучающий набор (этап 10)

```bash
export ERATRADE_ENV_FILE=.env.mainnet
uv run eratrade features                 # вектор признаков на последнюю закрытую свечу + хеш входа
uv run eratrade features --at 2026-11-17T12:00   # на конкретный момент решения
uv run eratrade dataset                  # набор в data/datasets/<сеть>-BTC-1h-v1-<время>/
```

`dataset` строит признаки и target (порог из `[target]` в `config/strategy.toml`), делит
строки по времени на train / validation / calibration / test с purge-промежутком в горизонт
и пишет `dataset.csv` + `manifest.json` (версия признаков, хеш кода pipeline, параметры
target, распределение классов, baseline log loss / Brier, sha256 исходных данных и набора).
Funding копит коллектор; история загружается `eratrade backfill --hours 5100`.

## Модели и сигналы (этап 11)

```bash
export ERATRADE_ENV_FILE=.env.mainnet
uv run eratrade train                    # обучить на свежем наборе → models/<версия>/, candidate
uv run eratrade models                   # версии и статусы
uv run eratrade model-shadow <версия> --reason "..."    # сигналы считаются, не исполняются
uv run eratrade model-activate <версия> --reason "..."  # только после shadow (или откат)
uv run eratrade model-retire <версия> --reason "..."
uv run eratrade signal                   # сигнал на последнюю закрытую свечу (в БД, без ордеров)
uv run eratrade signal --at 2026-11-17T12:00   # пробный расчёт без сохранения
uv run eratrade signal-outcomes          # фактический результат сигналов после горизонта
```

`train` печатает log loss / Brier / ECE против baseline на validation, calibration и test,
сигналы политики и их среднюю доходность за горизонт минус издержки, перебор порога
вероятности. Артефакт (`model.cbm` + `artifact.json`) хранит признаки и хеш кода pipeline,
границы признаков, температуру калибровки, пороги, метрики и хеши набора и модели.
Если код pipeline изменился после обучения, модель не загрузится — нужна новая версия.

## Торговый цикл (этап 12)

```bash
export ERATRADE_ENV_FILE=.env.mainnet,.env.mainnet.secrets   # нужен ключ API-кошелька
uv run eratrade cycle      # один проход по последней закрытой свече
uv run eratrade trader     # цикл: проход через 5 с после закрытия каждой свечи, Ctrl+C — стоп
```

Проход: дождаться свечи в БД → сверка (критичное расхождение запрещает входы) → позиция с
биржи → сигнал active и shadow → исполнение только active-сигнала с подтверждением в
терминале (ответ позже `--confirm-timeout` секунд = отказ: цена устарела) → результаты
прошлых сигналов → heartbeat `trader`. Сигнал забирается на исполнение атомарно в БД до
обращения к бирже, поэтому повтор прохода или рестарт не отправят ордер второй раз.
Решение старше 90 с после закрытия свечи не исполняется. Без активной модели — только
shadow-сигналы, ордеров нет. Матрица сбоев и тестов: [`docs/failure_matrix.md`](docs/failure_matrix.md).

## Mainnet shadow (этап 13)

Сервис `shadow-mainnet` (в `scripts/eratrade.sh up`) работает **без ключа** и на каждую
закрытую свечу сохраняет сигнал active- и shadow-модели вместе с bid/ask, спредом, ценой,
по которой реально началось бы исполнение, проскальзыванием относительно close и причинами,
по которым risk engine / guard отказали бы во входе. Модели берёт из `./models` (только чтение).

```bash
export ERATRADE_ENV_FILE=.env.mainnet
uv run eratrade shadow-report --days 7       # покрытие, решения, калибровка, задержки, дрейф
uv run eratrade shadow-report --json
uv run eratrade shadow --once                # один проход вручную (обычно не нужен)
```

Отчёт (и карточка «Shadow» в панели): покрытие свечей, HOLD из-за ошибок, распределение
вероятностей, log loss / Brier / ECE по накопленным результатам против baseline train
(метрики осмысленны от 20 результатов), итог направленных сигналов минус издержки,
задержка решения, спред и проскальзывание против заложенных в target, отказы risk/guard,
дрейф признаков (сдвиг среднего в σ train и доля значений вне границ обучения).

## Живая торговля с ручным подтверждением (этап 14)

Регламент — [`docs/runbook.md`](docs/runbook.md). Кратко:

```bash
export ERATRADE_ENV_FILE=.env.mainnet,.env.mainnet.secrets
uv run eratrade live-check          # чек-лист готовности §17.1; любой FAIL — live нельзя
uv run eratrade trades              # разбор сделок §17.2: размер, стоп, fills, комиссии, нетто
scripts/restore_check.sh backups/<последний дамп>   # проверка восстановления (видна в live-check)
```

- `model-activate` (CLI и API) откажет, если в shadow недостаточно доказательств
  (секция `[live]` в `config/strategy.toml`: ≥ 50 результатов, ≤ 5% ошибок, покрытие ≥ 95%,
  log loss лучше baseline). Откат на прежнюю активную модель доказательств не требует.
- Между превью и отправкой ордера движок перепроверяет рынок: сдвиг больше
  `max_slippage_bps` (10 bps) блокирует вход — размер и стоп считались по старой цене.
- Панель: карточки «Готовность к live» и «Сделки».

## Автоматическое исполнение (этап 15)

`trader --auto` / `scripts/eratrade.sh auto-start` — ордера без подтверждения, только после
ручного этапа: барьер `[auto]` в `config/strategy.toml` (включено явно, ручное подтверждение
выключено, `live-check` без FAIL, ≥ 10 закрытых сделок бота без замечаний). Любое
необъяснимое расхождение сверки, ошибка исполнения или замечание разбора — автопауза.
После рестарта режим понижается до LIVE_PAUSED. Подробно: [`docs/runbook.md`](docs/runbook.md) §8.

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
