"""CLI EraTrade. До появления React — основной интерфейс оператора."""

import asyncio
import json

import typer
from sqlalchemy import text

from app.config.settings import ConfigurationError, Settings, get_settings
from app.config.strategy import load_strategy
from app.db.session import create_engine
from app.observability.logging import configure_logging

# show_locals=False: локальные переменные (настройки, ключи) не печатаются в трейсбеке.
app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_show_locals=False)


def _load_settings() -> Settings:
    try:
        return get_settings()
    except ConfigurationError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


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


if __name__ == "__main__":
    app()
