# Сбои и обязательные тесты

Главный критерий: **ни одно действие не отправляется вслепую повторно.** Источник истины —
биржа: маркет-мейкер каждые 5 с берёт позицию и открытые заявки и приводит к ним своё состояние.

| Сценарий | Поведение | Тест |
|---|---|---|
| Исход действия неизвестен (таймаут, 5xx) | заявка «уточняется», повтора нет, разбирает сверка | `tests/hyperliquid/test_exchange_client.py::test_failures_are_classified_and_never_retried` |
| Post-only пересёк бы стакан | слот пуст, новая цена на следующем шаге | `tests/db/test_mm_maker.py` |
| Лишние заявки на монете (прошлый запуск, ручные) | отменяются при старте и сверке | `test_mm_maker.py::test_sync_adopts_and_cancels_extra_orders` |
| Повтор WS-исполнения | учитывается один раз (tid) | `test_mm_maker.py::test_fill_is_recorded_and_moves_position` |
| Дневной убыток | снять заявки, закрыть IOC reduce-only, запрет входов | `test_mm_maker.py::test_daily_loss_halts_flattens_and_blocks_entries` |
| Пауза / выключение из панели | заявки сняты / позиции закрыты | `test_mm_maker.py::test_quotes_both_sides_then_pause_cancels`, `tests/db/test_api.py::test_start_and_stop_for_market_maker` |
| Слишком частые действия | бюджет в минуту | `test_mm_maker.py::test_action_budget_limits_placements` |
| Устаревший стакан / обрыв WS | котировки снимаются, переподключение | `test_mm_maker.py::test_action_budget_limits_placements` (стакан устаревает) |
| scheduleCancel отклонён биржей | работа без него, предупреждение в панели | `tests/test_notify_and_dms.py::test_dead_mans_switch_rejection_is_reported` |
| Второй торговый процесс | не получает блокировку, nonce не пересекаются | `tests/db/test_nonce_and_lock.py::test_only_one_trader_lock_per_signer` |
| Недоступность PostgreSQL | блокировка снимается с соединением | `test_nonce_and_lock.py::test_lock_released_when_connection_dies` |
| Недоступность Telegram | уведомление не ломает работу | `test_notify_and_dms.py::test_telegram_failure_does_not_raise` |
| Подпись и белый список действий | только order/cancel/batchModify/leverage/scheduleCancel | `tests/hyperliquid/test_signing.py` |
