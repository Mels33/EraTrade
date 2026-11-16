"""CLI EraTrade. До появления React — основной интерфейс оператора."""

import asyncio
import getpass
import json
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
from app.db.repositories.market import upsert_market
from app.db.session import create_engine, create_sessionmaker
from app.domain.control import Actor, ActorKind, TransitionError
from app.domain.instruments import InstrumentRules, Rounding
from app.integrations.hyperliquid.gateway import open_gateway
from app.observability.logging import configure_logging, correlation_scope

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


if __name__ == "__main__":
    app()
