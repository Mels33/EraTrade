# Проверка сбоев и обязательные тесты (этап 12, §16 плана)

Главный критерий: **ни один сбой не приводит к слепому повтору ордера.** Механизмы:

- намерение ордера (cloid) сохраняется в БД до отправки; повторная отправка не делается
  никогда — неизвестный исход разбирает `OrderTracker` по cloid;
- active-сигнал забирается на исполнение атомарно (`signals.execution_status`) до первого
  обращения к бирже; повторный проход, рестарт или второй вызов его не исполняют;
- только один trader-процесс (advisory lock PostgreSQL), nonce выдаёт БД;
- любые ошибки данных, признаков или модели дают `HOLD`.

## §16.3 Сбои

| Сценарий | Поведение | Тест |
|---|---|---|
| Потеря соединения до отправки | ордер `NOT_SENT`, повтора нет | `tests/db/test_execution_engine.py::test_connection_refused_is_not_sent` |
| Потеря соединения после отправки, до ответа | `UNKNOWN` → разрешается по cloid, без дубля | `test_execution_engine.py::test_response_lost_after_fill_is_recovered_without_duplicate`, `tests/db/test_trading_cycle.py::test_unknown_send_outcome_is_not_retried` |
| Задержка ответа / запрос потерян | таймаут → `UNKNOWN` → `REJECTED` по истечении ожидания | `test_execution_engine.py::test_request_lost_before_processing_resolves_to_rejected_after_timeout` |
| Дубликат WebSocket-события | идемпотентный upsert, закрытая свеча неизменяема | `tests/db/test_collector.py::test_websocket_lifecycle_rollover_and_reconnect`, `tests/db/test_market_repository.py::test_candle_upsert_lifecycle` |
| Пропуск WebSocket-события | REST-досинхронизация после переподключения и по таймеру | `test_collector.py::test_backfill_then_incremental_sync`, `::test_silence_triggers_reconnect` |
| Перезапуск после создания ордера | брошенное намерение закрывается безопасно | `tests/db/test_reconciliation.py::test_abandoned_intent_is_closed_safely` |
| Перезапуск после отправки / fill | ордер и fills восстанавливаются, стоп проверяется | `test_execution_engine.py::test_crash_after_send_is_recovered_on_restart`, `test_reconciliation.py::test_crash_after_entry_resolves_order_and_flags_missing_stop` |
| Перезапуск посреди исполнения сигнала | сигнал остаётся `executing`, не повторяется | `test_trading_cycle.py::test_crash_mid_execution_is_never_retried` |
| Недоступность PostgreSQL | проход падает до отправки, ордеров нет; блокировка снимается с соединением | `test_trading_cycle.py::test_database_failure_before_send_sends_nothing`, `tests/db/test_nonce_and_lock.py::test_lock_released_when_connection_dies` |
| Недоступность Logfire | экспорт в фоне, работа не прерывается | `tests/test_logfire_outage.py` |
| Недоступность Telegram | уведомление не ломает команду | `tests/db/test_risk_and_emergency.py::test_telegram_failure_does_not_raise` |
| Отказ модели | `HOLD` с причиной | `test_trading_cycle.py::test_model_failure_gives_hold`, `tests/test_training.py::test_tampered_artifact_rejected`, `::test_pipeline_change_rejected` |
| Неверный feature vector | вне границ обучения / нечисловой → `HOLD` | `tests/test_training.py::test_out_of_bounds_feature_rejected`, `tests/test_features.py::test_bad_candles_raise` |
| Устаревшие рыночные данные | нет свечи / funding устарел / решение опоздало → `HOLD`, не исполняется | `test_trading_cycle.py::test_missing_candle_gives_hold`, `::test_late_decision_is_not_executed`, `tests/test_features.py::test_stale_or_sparse_funding_raises` |
| Резкое изменение спреда | guard блокирует вход до отправки | `test_execution_engine.py::test_guard_blocks_before_anything_is_sent[wide_spread]` |
| Ручное закрытие позиции на бирже | сверка видит, локальный стоп снимается | `test_reconciliation.py::test_manual_close_via_exchange_ui` |
| Срабатывание dead man’s switch | снимает защитные стопы → поэтому выключен (решение этапа 8) | `test_risk_and_emergency.py::test_dead_mans_switch_cancels_protective_stop_when_it_fires` |
| Второй trader-процесс | не получает блокировку, nonce не пересекаются | `test_nonce_and_lock.py::test_only_one_trader_lock_per_signer`, `::test_two_processes_same_signer_never_collide` |
| Критичное расхождение сверки | входы запрещены, выходы разрешены | `test_trading_cycle.py::test_critical_reconciliation_blocks_entry`, `test_reconciliation.py::test_critical_difference_pauses_live_trading` |

## §16.1 Модульные тесты

| Требование | Тест |
|---|---|
| Округление цены и размера | `tests/test_instruments.py` (свойства и примеры из документации) |
| asset ID из meta | `tests/hyperliquid/test_parsing.py::test_meta_asset_id_is_universe_index`, `tests/db/test_metadata_sync.py` |
| nonce | `test_nonce_and_lock.py::test_nonces_unique_and_increasing_under_concurrency`, `::test_nonce_follows_clock_and_survives_restart` |
| cloid: генерация и уникальность | `tests/hyperliquid/test_signing.py::test_cloids_are_unique_and_valid` |
| Размер позиции | `tests/test_sizing.py` |
| Дневной лимит убытка | `test_risk_and_emergency.py::test_daily_loss_limit_blocks_entry_without_sending`, `::test_unrealized_loss_counts_and_blocks` |
| Запрет превышения номинала | `test_execution_engine.py::test_requested_notional_above_limit_is_capped`, `tests/test_sizing.py::test_size_only_decreases_by_caps` |
| reduce-only логика | `test_execution_engine.py::test_close_position_cancels_stop`, `tests/hyperliquid/test_signing.py::test_reduce_only_stop_loss_matches_sdk` |
| Переходы состояний ордера | `tests/test_orders_domain.py::test_transitions`, `::test_final_states` |
| Дедупликация fills | `tests/hyperliquid/test_gateway.py::test_fills_paginate_and_deduplicate`, `test_reconciliation.py::test_funding_and_ledger_are_synced_once` |
| Snapshot после reconnect | `test_collector.py::test_websocket_lifecycle_rollover_and_reconnect` |
| Запрет сигнала на незакрытой свече | `tests/test_features.py::test_bad_candles_raise[не закрыта]`, `tests/test_dataset.py::test_unclosed_candles_ignored` |
| Неизвестный ответ биржи | `test_execution_engine.py::test_unknown_response_format_blocks_new_entries`, `tests/hyperliquid/test_exchange_client.py::test_format_error_means_outcome_unknown` |
| Восстановление после рестарта | `test_reconciliation.py::test_open_position_with_stop_after_restart_is_ok`, `test_execution_engine.py::test_crash_after_send_is_recovered_on_restart` |

## §16.2 Минимальный mainnet (основной аккаунт, ~12 USDC, ручное подтверждение)

| # | Сценарий | Статус |
|---|---|---|
| 1–4 | Открыть long, fill, позиция, защитный stop | ✅ 2026-11-17 (этап 6) |
| 6 | Полностью закрыть, биржа сама снимает reduce-only стоп | ✅ 2026-11-17 (этап 6) |
| 14–15 | Перезапуск с открытой позицией, сверка с PostgreSQL | ✅ сверка каждую минуту (этап 7) |
| 5 | Частичное закрытие | симулятор: `test_execution_engine.py::test_partial_close_replaces_stop_with_remaining_size`; на mainnet при лимите 20 USDC невозможно — половина позиции меньше минимального ордера 10 USDC |
| 7 | Открыть short | ⏳ ручная проверка |
| 8 | Отменить resting-ордер (защитный стоп) | ⏳ ручная проверка |
| 9–10 | Намеренный rejection, reduce-only rejection | ⏳ ручная проверка |
| 11 | Ограничение размера (номинал > 20 USDC урезается) | ⏳ ручная проверка |
| 12 | Обновить leverage | ✅ при каждом входе (`ensure_leverage`) |
| 13 | scheduleCancel | — выключен намеренно (снимает защитные стопы) |
| — | Торговый цикл `eratrade cycle` на mainnet | ✅ проход без активной модели (только shadow) |
