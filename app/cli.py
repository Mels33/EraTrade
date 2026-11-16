"""CLI EraTrade. До появления React — основной интерфейс оператора."""

import asyncio
import getpass
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

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
from app.db.session import create_engine, create_sessionmaker
from app.domain.control import Actor, ActorKind, TransitionError
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


if __name__ == "__main__":
    app()
