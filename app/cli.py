"""CLI EraTrade. До появления React — основной интерфейс оператора."""

import asyncio
import getpass
import json
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import typer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import (
    MAINNET_CONFIRMATION_PHRASE,
    ConfigurationError,
    Settings,
    TradingMode,
    get_settings,
)
from app.config.strategy import load_strategy
from app.db.models import AuditLogRecord
from app.db.repositories import control
from app.db.repositories import market as market_repo
from app.db.repositories import trading as trading_repo
from app.db.repositories.market import upsert_market
from app.db.session import create_engine, create_sessionmaker
from app.domain.account import Side
from app.domain.control import Actor, ActorKind, TransitionError
from app.domain.instruments import InstrumentRules, Rounding
from app.domain.orders import OrderStatus
from app.execution.engine import ExecutionEngine, OrderPreview, TradeResult
from app.execution.session import TradingSession, open_trading_session
from app.integrations.hyperliquid import actions as hl_actions
from app.integrations.hyperliquid.gateway import INTERVALS, HyperliquidGateway, open_gateway
from app.market_data.collector import COMPONENT as COLLECTOR
from app.market_data.collector import CollectorConfig, MarketDataCollector, floor_time
from app.market_data.gaps import compress_ranges
from app.market_data.metadata import sync_market
from app.observability.logging import configure_logging, correlation_scope
from app.reconciliation.reconciler import (
    Reconciler,
    ReconciliationReport,
    Severity,
)

# show_locals=False: локальные переменные (настройки, ключи) не печатаются в трейсбеке.
app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_show_locals=False)


def _load_settings() -> Settings:
    try:
        return get_settings()
    except ConfigurationError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


def _run_in_transaction[T](settings: Settings, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Выполняет fn в одной транзакции: commit при успехе, rollback при ошибке."""

    async def _run() -> T:
        engine = create_engine(settings)
        try:
            async with create_sessionmaker(engine).begin() as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _operator() -> Actor:
    return Actor(ActorKind.OPERATOR, getpass.getuser())


@app.command("config-check")
def config_check() -> None:
    """Проверить настройки окружения и стратегии (без вывода секретов)."""
    settings = _load_settings()
    strategy = load_strategy(settings.strategy_config_path)
    typer.echo(json.dumps(settings.safe_summary(), indent=2, ensure_ascii=False))
    typer.echo(
        f"strategy: spec v{strategy.spec_version}, "
        f"{strategy.market.coin} {strategy.market.interval}, "
        f"max notional {strategy.limits.max_position_notional_usdc} USDC"
    )
    if settings.is_mainnet:
        typer.secho("ВНИМАНИЕ: MAINNET", fg=typer.colors.RED, bold=True)
    typer.secho("OK", fg=typer.colors.GREEN)


@app.command("db-check")
def db_check() -> None:
    """Проверить подключение к PostgreSQL."""
    settings = _load_settings()
    configure_logging(settings, service="cli")

    async def _check() -> tuple[str, str]:
        engine = create_engine(settings)
        try:
            async with engine.connect() as conn:
                db = (await conn.execute(text("select current_database()"))).scalar_one()
                version = (await conn.execute(text("show server_version"))).scalar_one()
                return str(db), str(version)
        finally:
            await engine.dispose()

    db, version = asyncio.run(_check())
    typer.echo(f"PostgreSQL {version}, database={db}")
    typer.secho("OK", fg=typer.colors.GREEN)


@app.command("status")
def status() -> None:
    """Текущий режим бота, запрет входов, heartbeat компонентов и последние записи аудита."""
    settings = _load_settings()

    async def _status(session: AsyncSession) -> None:
        state = await control.ensure_bot_state(session, settings.network)
        color = typer.colors.RED if settings.is_mainnet else typer.colors.YELLOW
        typer.secho(f"{settings.network.value.upper()} · {state.trading_mode}", fg=color, bold=True)
        if state.entries_blocked:
            typer.secho(f"Новые входы ЗАПРЕЩЕНЫ: {state.block_reason}", fg=typer.colors.RED)
        else:
            typer.echo("Новые входы: разрешены режимом")
        typer.echo(f"Изменено: {state.updated_at:%Y-%m-%d %H:%M:%S %Z} · {state.updated_by}")

        now = datetime.now(UTC)
        heartbeats = await control.get_heartbeats(session)
        typer.echo("\nHeartbeat:" + ("" if heartbeats else " нет компонентов"))
        for hb in heartbeats:
            age = (now - hb.last_beat_at).total_seconds()
            typer.echo(f"  {hb.component:<12} {age:>8.0f} с назад  ({hb.instance_id})")

        stmt = select(AuditLogRecord).order_by(AuditLogRecord.id.desc()).limit(5)
        typer.echo("\nПоследние события аудита:")
        for a in (await session.execute(stmt)).scalars():
            reason = f" — {a.reason}" if a.reason else ""
            typer.echo(f"  {a.occurred_at:%Y-%m-%d %H:%M:%S} {a.actor:<18} {a.action}{reason}")

    _run_in_transaction(settings, _status)


@app.command("set-mode")
def set_mode(
    mode: TradingMode,
    reason: str = typer.Option(..., "--reason", "-r", help="Причина (попадёт в аудит)"),
) -> None:
    """Сменить режим работы бота (с записью в аудит)."""
    settings = _load_settings()
    if mode is TradingMode.LIVE_ENABLED:
        typer.secho("Включение РЕАЛЬНОЙ торговли.", fg=typer.colors.RED, bold=True)
        typed = typer.prompt(f"Для подтверждения введите {MAINNET_CONFIRMATION_PHRASE}")
        if typed != MAINNET_CONFIRMATION_PHRASE:
            typer.secho("Подтверждение не совпало, режим не изменён.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

    async def _set(session: AsyncSession) -> bool:
        with correlation_scope():
            return await control.set_trading_mode(
                session, settings.network, mode, actor=_operator(), reason=reason
            )

    try:
        changed = _run_in_transaction(settings, _set)
    except TransitionError as exc:
        typer.secho(f"Запрещено: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Режим: {mode.value}" if changed else f"Режим уже {mode.value}")


@app.command("market")
def market(
    hours: int = typer.Option(6, help="Сколько последних часовых свечей показать"),
) -> None:
    """Метаданные, рыночный контекст и свечи инструмента стратегии; meta сохраняется в БД."""
    settings = _load_settings()
    configure_logging(settings, service="cli")
    strategy = load_strategy(settings.strategy_config_path)
    coin, interval = strategy.market.coin, strategy.market.interval

    async def _fetch() -> None:
        async with open_gateway(settings) as gw:
            markets, contexts = await gw.get_markets_with_contexts()
            meta = next((m for m in markets if m.coin == coin), None)
            if meta is None:
                raise typer.BadParameter(f"{coin} нет в meta сети {settings.network}")
            now = datetime.now(UTC)
            candles = await gw.get_candles(coin, interval, start=now - timedelta(hours=hours))
        ctx = contexts[coin]
        rules = InstrumentRules.for_perp(meta)

        engine = create_engine(settings)
        try:
            async with create_sessionmaker(engine).begin() as session:
                market_id = await upsert_market(session, meta)
        finally:
            await engine.dispose()

        typer.secho(f"{settings.network.value.upper()} · {coin}", bold=True)
        typer.echo(
            f"asset_id={meta.asset_id}  szDecimals={meta.sz_decimals}  "
            f"цена до {rules.max_price_decimals} знаков / 5 значащих  "
            f"maxLeverage={meta.max_leverage}  onlyIsolated={meta.only_isolated}  "
            f"(markets.id={market_id})"
        )
        funding_apr = ctx.funding * 24 * 365 * 100
        typer.echo(
            f"mark={ctx.mark_price}  oracle={ctx.oracle_price}  mid={ctx.mid_price}  "
            f"funding/ч={ctx.funding} (~{funding_apr:.2f}% годовых)  OI={ctx.open_interest}"
        )
        slip = strategy.limits.max_slippage_bps / Decimal(10_000)
        mid = ctx.mid_price or ctx.mark_price
        buy_px = rules.round_price(mid * (1 + slip), Rounding.DOWN)
        sell_px = rules.round_price(mid * (1 - slip), Rounding.UP)
        min_size = rules.min_size_for_notional(mid, strategy.sizing.min_order_notional_usdc)
        typer.echo(
            f"IOC-границы при {strategy.limits.max_slippage_bps} bps: buy ≤ {buy_px}, "
            f"sell ≥ {sell_px};  мин. размер {min_size} {coin} (номинал ≥ "
            f"{strategy.sizing.min_order_notional_usdc} USDC)"
        )
        typer.echo(f"\nСвечи {interval}:")
        for c in candles:
            mark = "" if c.is_closed else "  ← не закрыта"
            typer.echo(
                f"  {c.open_time:%Y-%m-%d %H:%M}  O {c.open:>9} H {c.high:>9} "
                f"L {c.low:>9} C {c.close:>9}  V {c.volume}{mark}"
            )

    asyncio.run(_fetch())


@app.command("account")
def account() -> None:
    """Состояние аккаунта HL_ACCOUNT_ADDRESS: маржа, позиции, ордера, fills за сутки, лимиты."""
    settings = _load_settings()
    configure_logging(settings, service="cli")
    user = settings.hl_account_address
    if user is None:
        typer.secho(
            "HL_ACCOUNT_ADDRESS не задан. Укажите адрес основного аккаунта или субаккаунта "
            "(не API-кошелька) в .env",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    async def _fetch() -> None:
        async with open_gateway(settings) as gw:
            state = await gw.get_account_state(user)
            orders = await gw.get_open_orders(user)
            fills = await gw.get_fills(user, start=datetime.now(UTC) - timedelta(days=1))
            limits = await gw.get_user_rate_limit(user)

        typer.secho(f"{settings.network.value.upper()} · {user[:6]}…{user[-4:]}", bold=True)
        typer.echo(
            f"accountValue={state.account_value}  marginUsed={state.total_margin_used}  "
            f"withdrawable={state.withdrawable}  (время биржи {state.exchange_time:%H:%M:%S} UTC)"
        )
        typer.echo(f"\nПозиции: {len(state.positions)}")
        for p in state.positions:
            typer.echo(
                f"  {p.coin:<8} size={p.size} entry={p.entry_price} uPnL={p.unrealized_pnl} "
                f"liq={p.liquidation_price} {p.leverage_type} x{p.leverage}"
            )
        typer.echo(f"\nОткрытые ордера: {len(orders)}")
        for o in orders:
            flags = " reduceOnly" if o.reduce_only else ""
            trigger = f" trigger={o.trigger_price}" if o.is_trigger else ""
            typer.echo(
                f"  {o.coin:<8} {o.side.value:<4} {o.size}@{o.limit_price} {o.order_type}"
                f"{trigger}{flags} oid={o.oid} cloid={o.cloid}"
            )
        fees = sum((f.fee for f in fills), Decimal(0))
        pnl = sum((f.closed_pnl for f in fills), Decimal(0))
        typer.echo(f"\nFills за 24 ч: {len(fills)}, closedPnl={pnl}, комиссии={fees}")
        typer.echo(
            f"Лимит запросов адреса: использовано {limits.requests_used} из "
            f"{limits.requests_cap + limits.requests_surplus} (осталось {limits.remaining})"
        )

    asyncio.run(_fetch())


@app.command("collect")
def collect() -> None:
    """Запустить коллектор данных (свечи + funding/OI). Работает до остановки (Ctrl+C / SIGTERM)."""
    settings = _load_settings()
    configure_logging(settings, service="collector")
    strategy = load_strategy(settings.strategy_config_path)
    config = CollectorConfig(
        coin=strategy.market.coin,
        interval=strategy.market.interval,
        max_ws_silence=float(strategy.freshness.max_ws_silence_seconds),
    )

    async def _run() -> None:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        if task is not None:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, task.cancel)
        engine = create_engine(settings)
        try:
            async with open_gateway(settings) as gw:
                collector = MarketDataCollector(
                    gw, create_sessionmaker(engine), settings.hyperliquid_ws_url, config
                )
                await collector.run()
        finally:
            await engine.dispose()

    typer.secho(
        f"Коллектор {settings.network.value.upper()} {config.coin} {config.interval}", bold=True
    )
    try:
        asyncio.run(_run())
    except asyncio.CancelledError:
        typer.echo("Коллектор остановлен")


@app.command("backfill")
def backfill(
    hours: int = typer.Option(
        ..., help="Сколько часов истории загрузить (биржа хранит ~5000 свечей)"
    ),
) -> None:
    """Догрузить окно свечей через REST (идемпотентно; закрытые свечи не перезаписываются)."""
    settings = _load_settings()
    configure_logging(settings, service="cli")
    strategy = load_strategy(settings.strategy_config_path)
    coin, interval = strategy.market.coin, strategy.market.interval

    async def _run() -> tuple[int, int]:
        engine = create_engine(settings)
        try:
            async with open_gateway(settings) as gw:
                start = datetime.now(UTC) - timedelta(hours=hours)
                candles = await gw.get_candles(coin, interval, start=start)
                async with create_sessionmaker(engine).begin() as session:
                    market_id, _, _ = await sync_market(gw, session, coin)
                    written = await market_repo.upsert_candles(session, market_id, candles)
                return len(candles), written
        finally:
            await engine.dispose()

    received, written = asyncio.run(_run())
    typer.echo(f"Получено свечей: {received}, записано/обновлено: {written}")


@app.command("data-status")
def data_status(
    days: int = typer.Option(30, help="За сколько дней искать пропуски"),
) -> None:
    """Сколько данных накоплено, пропуски, свежесть контекста и heartbeat коллектора."""
    settings = _load_settings()
    strategy = load_strategy(settings.strategy_config_path)
    coin, interval = strategy.market.coin, strategy.market.interval
    step = INTERVALS[interval]

    async def _status(session: AsyncSession) -> None:
        now = datetime.now(UTC)
        record = await market_repo.get_market(session, settings.network, coin)
        typer.secho(f"{settings.network.value.upper()} · {coin} {interval}", bold=True)
        if record is None:
            typer.echo("Рынка нет в БД — запустите `eratrade collect` или `eratrade market`.")
            return
        count, first, last = await market_repo.candle_stats(session, record.id, interval)
        typer.echo(
            f"Закрытых свечей: {count}"
            + (f"  ({first:%Y-%m-%d %H:%M} … {last:%Y-%m-%d %H:%M} UTC)" if first and last else "")
        )
        if last is not None:
            lag = now - (last + step)
            typer.echo(
                f"Последняя закрытая свеча закончилась {lag.total_seconds() / 60:.1f} мин назад"
            )

        window_start = floor_time(max(first or now, now - timedelta(days=days)), step)
        missing = await market_repo.find_missing_candles(
            session,
            record.id,
            interval,
            step,
            window_start,
            (last + step) if last else window_start,
        )
        if missing:
            typer.secho(f"Пропуски за {days} дн.: {len(missing)} свечей", fg=typer.colors.RED)
            for a, b in compress_ranges(missing, step)[:10]:
                typer.echo(f"  {a:%Y-%m-%d %H:%M} … {b:%Y-%m-%d %H:%M}")
        else:
            typer.secho(f"Пропусков за {days} дн. нет", fg=typer.colors.GREEN)

        ctx = await market_repo.get_latest_context(session, record.id)
        if ctx is None:
            typer.echo("Снимков funding/OI нет")
        else:
            age = (now - ctx.received_at).total_seconds()
            typer.echo(
                f"Последний снимок контекста: {age:.0f} с назад ({ctx.source}), "
                f"funding={ctx.funding}, OI={ctx.open_interest}"
            )

        heartbeat = next(
            (hb for hb in await control.get_heartbeats(session) if hb.component == COLLECTOR), None
        )
        if heartbeat is None:
            typer.secho("Коллектор ни разу не запускался", fg=typer.colors.YELLOW)
        else:
            age = (now - heartbeat.last_beat_at).total_seconds()
            alive = age < 60
            typer.secho(
                f"Коллектор: heartbeat {age:.0f} с назад, WS={heartbeat.details.get('ws_state')}, "
                f"переподключений={heartbeat.details.get('reconnects')}",
                fg=typer.colors.GREEN if alive else typer.colors.RED,
            )
            if heartbeat.details.get("last_error"):
                typer.echo(f"  последняя ошибка: {heartbeat.details['last_error']}")

    _run_in_transaction(settings, _status)


async def _with_trading_session[T](
    settings: Settings, fn: Callable[[TradingSession, HyperliquidGateway], Awaitable[T]]
) -> T:
    engine = create_engine(settings)
    try:
        async with (
            open_gateway(settings) as gw,
            open_trading_session(settings, engine, gw) as session,
        ):
            return await fn(session, gw)
    finally:
        await engine.dispose()


def _print_wallet(session: TradingSession) -> None:
    w = session.wallet
    typer.echo(f"API-кошелёк (signer): {w.signer}  имя: {w.agent_name or '—'}")
    typer.echo(f"Торговый аккаунт:     {w.account}" + ("  (субаккаунт)" if w.vault_address else ""))
    if w.valid_until:
        typer.echo(f"Действителен до:      {w.valid_until:%Y-%m-%d %H:%M} UTC")
    typer.echo(f"Расхождение часов:    {session.clock_skew.total_seconds():+.3f} с")
    for warning in w.warnings:
        typer.secho(f"ВНИМАНИЕ: {warning}", fg=typer.colors.YELLOW)


@app.command("wallet-check")
def wallet_check() -> None:
    """Проверить ключ API-кошелька, срок действия, часы и блокировку. Ничего не подписывает."""
    settings = _load_settings()
    configure_logging(settings, service="cli")

    async def _check(session: TradingSession, gw: HyperliquidGateway) -> int | None:
        _print_wallet(session)
        return await session.nonces.last()

    try:
        last_nonce = asyncio.run(_with_trading_session(settings, _check))
    except Exception as exc:
        typer.secho(f"Проверка не пройдена: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Последний nonce:      {last_nonce or 'ещё не выдавался'}")
    typer.secho(
        "OK: ключ годен для торговли, второй торговый процесс не запущен", fg=typer.colors.GREEN
    )


@app.command("verify-signing")
def verify_signing(
    yes: bool = typer.Option(False, "--yes", help="Подтвердить отправку без вопроса"),
) -> None:
    """Проверить подпись на бирже: отмена несуществующего ордера (ничего не меняет)."""
    settings = _load_settings()
    configure_logging(settings, service="cli")
    strategy = load_strategy(settings.strategy_config_path)
    network = settings.network.value.upper()
    if not yes and not typer.confirm(
        f"Отправить на {network} подписанную отмену несуществующего ордера {strategy.market.coin}?"
    ):
        raise typer.Exit(code=1)

    async def _verify(session: TradingSession, gw: HyperliquidGateway) -> tuple[bool, str]:
        _print_wallet(session)
        market = await gw.get_market(strategy.market.coin)
        response = await session.submit(hl_actions.cancel_action(market.asset_id, 1))
        if not response.ok:
            return False, f"биржа отклонила запрос: {response.error}"
        statuses = (response.data or {}).get("statuses") or [None]
        status = statuses[0]
        if isinstance(status, dict) and "never placed" in str(status.get("error", "")):
            return True, "биржа узнала API-кошелёк и проверила подпись (ордер не найден — ожидаемо)"
        return False, f"неожиданный ответ: {response.raw}"

    try:
        ok, message = asyncio.run(_with_trading_session(settings, _verify))
    except Exception as exc:
        typer.secho(f"Ошибка: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    typer.secho(
        ("OK: " if ok else "НЕ ПРОЙДЕНО: ") + message,
        fg=typer.colors.GREEN if ok else typer.colors.RED,
    )
    if not ok:
        raise typer.Exit(code=1)


async def _with_engine[T](settings: Settings, fn: Callable[[ExecutionEngine], Awaitable[T]]) -> T:
    strategy = load_strategy(settings.strategy_config_path)

    async def _run(session: TradingSession, gw: HyperliquidGateway) -> T:
        engine = await ExecutionEngine.create(
            session, gw, create_sessionmaker(create_engine(settings)), strategy
        )
        with correlation_scope():
            return await fn(engine)

    return await _with_trading_session(settings, _run)


def _make_confirm(settings: Settings) -> Callable[[OrderPreview], Awaitable[bool]]:
    async def confirm(p: OrderPreview) -> bool:
        if settings.is_mainnet:
            typer.secho("MAINNET — реальные деньги", fg=typer.colors.RED, bold=True)
        spread = f"{p.spread_bps:.2f} bps" if p.spread_bps is not None else "нет данных"
        typer.echo(
            f"{p.purpose.value.upper()} {p.side.value.upper()} {p.size} @ лимит {p.limit_price} "
            f"(≈{p.notional:.2f} USDC), mid {p.mid_price}, спред {spread}"
        )
        typer.echo(
            f"позиция сейчас {p.position_size}, плечо isolated {p.leverage_target}x, "
            f"доступно {p.available_to_trade:.2f} USDC"
        )
        if p.stop_trigger is not None:
            typer.echo(
                f"stop-market reduceOnly: срабатывание {p.stop_trigger}, "
                f"убыток до стопа ≈ {p.max_loss_at_stop:.2f} USDC (без проскальзывания)"
            )
        return typer.confirm("Отправить ордер?", default=False)

    return confirm


def _print_trade(result: TradeResult) -> None:
    for label, order in (("ордер", result.order), ("stop", result.stop)):
        if order is None:
            continue
        color = {
            OrderStatus.FILLED: typer.colors.GREEN,
            OrderStatus.RESTING: typer.colors.GREEN,
            OrderStatus.PARTIALLY_FILLED: typer.colors.YELLOW,
            OrderStatus.UNKNOWN: typer.colors.YELLOW,
        }.get(order.status, typer.colors.RED)
        typer.secho(
            f"{label}: {order.status.value} исполнено {order.filled_size} "
            f"@ {order.avg_price or '—'}  cloid={order.cloid}",
            fg=color,
        )
        for reason in order.guard_reasons:
            typer.echo(f"  отклонено: {reason}")
        if order.error and not order.guard_reasons:
            typer.echo(f"  ошибка: {order.error}")
    for warning in result.warnings:
        typer.secho(warning, fg=typer.colors.RED, bold=True)


def _print_report(report: ReconciliationReport) -> None:
    color = {"ok": typer.colors.GREEN, "warnings": typer.colors.YELLOW}.get(
        report.status, typer.colors.RED
    )
    typer.secho(
        f"Сверка #{report.run_id}: {report.status.upper()} · позиция на бирже "
        f"{report.exchange_position}, ожидаемая {report.expected_position}",
        fg=color,
        bold=True,
    )
    marks = {Severity.INFO: "·", Severity.WARNING: "!", Severity.CRITICAL: "✖"}
    for diff in report.differences:
        repaired = " (исправлено)" if diff.repaired else ""
        typer.echo(f"  {marks[diff.severity]} {diff.message}{repaired}")
    if report.entries_blocked:
        typer.secho("Новые входы заблокированы", fg=typer.colors.RED)


async def _pre_trade_reconcile(engine: ExecutionEngine) -> ReconciliationReport:
    reconciler = Reconciler(
        engine.gateway,
        engine.sm,
        engine.tracker,
        network=engine.network,
        market_id=engine.market_id,
        coin=engine.coin,
        user=engine.user,
    )
    report = await reconciler.run("pre_trade")
    _print_report(report)
    return report


async def _with_reconciler[T](settings: Settings, fn: Callable[[Reconciler], Awaitable[T]]) -> T:
    if settings.hl_account_address is None:
        raise typer.BadParameter("не задан HL_ACCOUNT_ADDRESS")
    strategy = load_strategy(settings.strategy_config_path)
    engine = create_engine(settings)
    try:
        async with open_gateway(settings) as gw:
            reconciler = await Reconciler.create(
                gw, create_sessionmaker(engine), strategy, user=settings.hl_account_address
            )
            return await fn(reconciler)
    finally:
        await engine.dispose()


@app.command("reconcile")
def reconcile(
    every: int = typer.Option(0, help="Повторять каждые N секунд (0 — один раз)"),
) -> None:
    """Сверить локальное состояние с биржей. Ключ API-кошелька не нужен."""
    settings = _load_settings()
    configure_logging(settings, service="reconciler")

    async def _once(reconciler: Reconciler) -> None:
        if every > 0:
            await reconciler.run_forever(every)
        else:
            with correlation_scope():
                _print_report(await reconciler.run("manual"))

    try:
        asyncio.run(_with_reconciler(settings, _once))
    except (KeyboardInterrupt, asyncio.CancelledError):
        typer.echo("Сверка остановлена")


@app.command("accept-position")
def accept_position(
    reason: str = typer.Option(..., "--reason", "-r", help="Почему позиция изменилась вне бота"),
) -> None:
    """Признать текущую позицию на бирже точкой отсчёта (после ручной сделки)."""
    settings = _load_settings()
    configure_logging(settings, service="cli")

    async def _accept(reconciler: Reconciler) -> Decimal:
        return await reconciler.accept_exchange_position(actor=_operator(), reason=reason)

    size = asyncio.run(_with_reconciler(settings, _accept))
    typer.echo(f"Точка отсчёта: позиция {size}. Запрет входов снимается отдельно: eratrade unblock")


@app.command("unblock")
def unblock(
    reason: str = typer.Option(..., "--reason", "-r", help="Причина (попадёт в аудит)"),
) -> None:
    """Снять запрет новых входов (только оператор, после разбора причины)."""
    settings = _load_settings()

    async def _unblock(session: AsyncSession) -> tuple[bool, str | None]:
        state = await control.ensure_bot_state(session, settings.network)
        previous = state.block_reason
        changed = await control.unblock_entries(
            session, settings.network, actor=_operator(), reason=reason
        )
        return changed, previous

    changed, previous = _run_in_transaction(settings, _unblock)
    if changed:
        typer.secho(f"Запрет снят (был: {previous})", fg=typer.colors.GREEN)
    else:
        typer.echo("Запрета не было")


@app.command("open")
def open_position(
    direction: str = typer.Argument(..., help="long или short"),
    notional: Decimal = typer.Option(..., help="Номинал в USDC (лимит — в config/strategy.toml)"),
    stop_price: Decimal | None = typer.Option(None, help="Цена стопа (по умолчанию из ATR)"),
) -> None:
    """Открыть позицию IOC-ордером и поставить биржевой stop-market (с подтверждением)."""
    if direction not in ("long", "short"):
        raise typer.BadParameter("direction: long или short")
    settings = _load_settings()
    configure_logging(settings, service="cli")
    side = Side.BUY if direction == "long" else Side.SELL

    async def _open(engine: ExecutionEngine) -> TradeResult:
        report = await _pre_trade_reconcile(engine)
        if report.critical:
            return TradeResult(None, warnings=["вход отменён: сверка нашла критичные расхождения"])
        return await engine.open_position(
            side, notional, confirm=_make_confirm(settings), stop_price=stop_price
        )

    _print_trade(asyncio.run(_with_engine(settings, _open)))


@app.command("close")
def close_position() -> None:
    """Закрыть позицию reduceOnly IOC-ордером, снять или пересоздать stop (с подтверждением)."""
    settings = _load_settings()
    configure_logging(settings, service="cli")

    async def _close(engine: ExecutionEngine) -> TradeResult:
        await _pre_trade_reconcile(engine)  # закрытие разрешено и при расхождениях
        return await engine.close_position(confirm=_make_confirm(settings))

    _print_trade(asyncio.run(_with_engine(settings, _close)))


@app.command("cancel")
def cancel_order(cloid: str = typer.Argument(..., help="cloid ордера")) -> None:
    """Отменить ордер по cloid."""
    settings = _load_settings()
    configure_logging(settings, service="cli")

    async def _cancel(engine: ExecutionEngine) -> TradeResult:
        async with engine.sm() as db:
            order = await trading_repo.get_order_by_cloid(db, cloid)
        if order is None:
            raise typer.BadParameter(f"ордер {cloid} не найден в БД")
        return TradeResult(await engine.cancel(order.id))

    _print_trade(asyncio.run(_with_engine(settings, _cancel)))


@app.command("resolve")
def resolve_orders(
    hours: int = typer.Option(24, help="За сколько часов синхронизировать исполнения"),
) -> None:
    """Сверить незавершённые ордера с биржей по cloid и сохранить исполнения."""
    settings = _load_settings()
    configure_logging(settings, service="cli")

    async def _resolve(engine: ExecutionEngine) -> tuple[list[TradeResult], int]:
        results = [TradeResult(r) for r in await engine.resolve_unresolved()]
        new_fills = await engine.sync_fills(datetime.now(UTC) - timedelta(hours=hours))
        await engine.snapshot()
        return results, new_fills

    results, new_fills = asyncio.run(_with_engine(settings, _resolve))
    for result in results:
        _print_trade(result)
    typer.echo(f"Незавершённых ордеров проверено: {len(results)}, новых исполнений: {new_fills}")


@app.command("orders")
def list_orders_cmd(limit: int = typer.Option(20, help="Сколько последних ордеров")) -> None:
    """Последние ордера из БД."""
    settings = _load_settings()

    async def _list(session: AsyncSession) -> None:
        for o in await trading_repo.list_orders(session, limit=limit):
            trigger = f" trig {o.trigger_price}" if o.trigger_price else ""
            typer.echo(
                f"{o.created_at:%m-%d %H:%M:%S} {o.purpose:<5} {o.side:<4} {o.size} @ {o.price}"
                f"{trigger} {o.status:<16} filled {o.filled_size} {o.cloid}"
                + (f"  ({o.error[:60]})" if o.error else "")
            )

    _run_in_transaction(settings, _list)


if __name__ == "__main__":
    app()
