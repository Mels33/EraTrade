"""CLI EraTrade. До появления React — основной интерфейс оператора."""

import asyncio
import getpass
import json
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

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
from app.domain.instruments import InstrumentRules
from app.execution.session import TradingSession, open_trading_session
from app.integrations.hyperliquid import actions as hl_actions
from app.integrations.hyperliquid.gateway import HyperliquidGateway, open_gateway
from app.observability.logging import configure_logging, correlation_scope
from app.observability.notify import build_notifier

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
    mm = strategy.mm
    typer.echo(
        f"strategy: spec v{strategy.spec_version}, маркет-мейкинг {', '.join(mm.coins) or '—'}: "
        f"заявка {mm.order_usd} USDC, позиция ≤ {mm.max_inventory_usd}, "
        f"дневной убыток ≤ {mm.daily_loss_usd} USDC"
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
    coin = (load_strategy(settings.strategy_config_path).mm.coins or ("BTC",))[0]
    network = settings.network.value.upper()
    if not yes and not typer.confirm(
        f"Отправить на {network} подписанную отмену несуществующего ордера {coin}?"
    ):
        raise typer.Exit(code=1)

    async def _verify(session: TradingSession, gw: HyperliquidGateway) -> tuple[bool, str]:
        _print_wallet(session)
        market = await gw.get_market(coin)
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


@app.command("api")
def run_api(
    host: str = typer.Option("127.0.0.1", help="Адрес (по умолчанию только локально)"),
    port: int = typer.Option(8000),
    allow_external: bool = typer.Option(
        False, "--allow-external", help="Разрешить не-локальный адрес (только за VPN/прокси)"
    ),
) -> None:
    """API и панель управления (§13). Нужен API_TOKEN; ключ — только для cancel/flatten/kill."""
    import uvicorn

    from app.api.app import create_api
    from app.api.context import build_context

    if host not in ("127.0.0.1", "localhost", "::1") and not allow_external:
        raise typer.BadParameter("панель не должна быть открыта наружу; см. --allow-external")
    settings = _load_settings()
    configure_logging(settings, service="api")
    if settings.api_token is None:
        typer.secho("Не задан API_TOKEN — API не запускается", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho(f"ВНИМАНИЕ: API слушает {host}", fg=typer.colors.RED, bold=True)
    uvicorn.run(create_api(lambda: build_context(settings)), host=host, port=port, log_level="info")


@app.command("notify-test")
def notify_test() -> None:
    """Отправить тестовое уведомление оператору (Telegram или лог)."""
    settings = _load_settings()
    configure_logging(settings, service="cli")
    notifier = build_notifier(settings)
    ok = asyncio.run(notifier.send("тестовое уведомление: канал оповещений работает ✅"))
    channel = type(notifier).__name__
    if ok:
        typer.secho(f"OK: отправлено через {channel}", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"НЕ отправлено через {channel} — проверьте токен и chat_id", fg=typer.colors.RED
        )
        raise typer.Exit(code=1)


@app.command("pause")
def pause_cmd(
    reason: str = typer.Option(..., "--reason", "-r", help="Причина (попадёт в аудит)"),
) -> None:
    """Пауза: запрет входов, LIVE_ENABLED → LIVE_PAUSED; маркет-мейкер снимает заявки."""
    settings = _load_settings()
    configure_logging(settings, service="cli")

    async def _pause(session: AsyncSession) -> list[str]:
        return await control.pause(session, settings.network, actor=_operator(), reason=reason)

    for step in _run_in_transaction(settings, _pause):
        typer.echo(step)


# --- маркет-мейкинг ---


@app.command("mm-scan")
def mm_scan(
    top: int = typer.Option(50, help="Сколько рынков с наибольшим оборотом проверить"),
    samples: int = typer.Option(3, help="Замеров стакана на рынок"),
) -> None:
    """Рынки Hyperliquid для маркет-мейкинга: спред, шаг цены, оборот (ключ не нужен)."""
    from app.mm.scanner import candidates, scan

    settings = _load_settings()
    configure_logging(settings, service="cli")

    async def _scan() -> list[Any]:
        async with open_gateway(settings) as gw:
            return await scan(gw, top=top, samples=samples)

    scans = asyncio.run(_scan())
    good = {s.coin for s in candidates(scans)}
    typer.echo(
        f"{'монета':10} {'цена':>10} {'спред bp':>8} {'шаг bp':>7} {'оборот $M':>9} "
        f"{'на лучшей $':>11}"
    )
    for s in sorted(scans, key=lambda s: -s.spread_bps):
        mark = "  ← кандидат" if s.coin in good else ""
        typer.echo(
            f"{s.coin:10} {float(s.mid):10.5g} {float(s.spread_bps):8.2f} {float(s.tick_bps):7.2f} "
            f"{float(s.day_volume_usd) / 1e6:9.1f} {float(s.top_depth_usd):11.0f}{mark}"
        )
    strategy = load_strategy(settings.strategy_config_path)
    typer.echo(f"\nВ конфиге [mm] coins: {', '.join(strategy.mm.coins) or '—'}")


@app.command("mm")
def mm_run(
    yes: bool = typer.Option(False, "--yes", help="Запуск без вопроса (для Docker)"),
) -> None:
    """Живой маркет-мейкер (нужен ключ). Котирует только в режиме LIVE_ENABLED."""
    from app.mm.maker import MarketMaker

    settings = _load_settings()
    configure_logging(settings, service="mm")
    strategy = load_strategy(settings.strategy_config_path)
    cfg = strategy.mm
    if not cfg.coins:
        typer.secho("В config/strategy.toml не заданы [mm] coins", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if settings.hl_account_address is None:
        typer.secho("Не задан HL_ACCOUNT_ADDRESS", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    typer.echo(
        f"Маркет-мейкер {settings.network.value}: {', '.join(cfg.coins)}; заявка {cfg.order_usd} "
        f"USDC, позиция ≤ {cfg.max_inventory_usd} USDC на монету, дневной убыток ≤ "
        f"{cfg.daily_loss_usd} USDC. Котирует только в режиме LIVE_ENABLED."
    )
    if not yes and not typer.confirm("Запустить с реальными заявками?"):
        raise typer.Exit(code=1)
    user = settings.hl_account_address.lower()

    async def _run() -> None:
        # SIGTERM (docker stop) → отмена задачи → заявки снимаются в finally маркет-мейкера.
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        if task is not None:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, task.cancel)
        engine = create_engine(settings)
        try:
            async with (
                open_gateway(settings) as gw,
                open_trading_session(settings, engine, gw) as session,
            ):
                markets = {m.coin: m for m in await gw.get_markets()}
                missing = [c for c in cfg.coins if c not in markets]
                if missing:
                    raise RuntimeError(f"нет таких рынков: {', '.join(missing)}")
                too_high = [c for c in cfg.coins if markets[c].max_leverage < cfg.leverage]
                if too_high:
                    raise RuntimeError(f"плечо {cfg.leverage} выше максимума: {too_high}")
                maker = MarketMaker(
                    session,
                    gw,
                    create_sessionmaker(engine),
                    cfg,
                    network=settings.network,
                    user=user,
                    ws_url=settings.hyperliquid_ws_url,
                    rules={c: InstrumentRules.for_perp(markets[c]) for c in cfg.coins},
                    notifier=build_notifier(settings),
                )
                await maker.run()
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    typer.echo("Маркет-мейкер остановлен.")


# --- модель «спрос и предложение»: публичные данные Binance ---


@app.command("micro-data")
def micro_data_cmd(
    since: str = typer.Option("2023-01-01", help="С какого дня (UTC)"),
    until: str | None = typer.Option(None, help="По какой день включительно (по умолчанию вчера)"),
    out: str = typer.Option("data/micro/bars", help="Каталог минутных файлов"),
    raw: str = typer.Option("data/binance_public/raw", help="Каталог сырых архивов"),
    keep_raw: bool = typer.Option(False, help="Не удалять сырые zip после агрегации"),
    workers: int = typer.Option(4, help="Сколько дней обрабатывать параллельно"),
) -> None:
    """Скачать дневные архивы Binance (aggTrades, bookDepth) с проверкой sha256 и собрать
    минутные строки «спрос и предложение». Идемпотентно: готовые дни пропускаются."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import date, timedelta
    from pathlib import Path

    import httpx

    from app.integrations.binance_public import download
    from app.research.micro_bars import bars_path, build_day

    first = date.fromisoformat(since)
    last = date.fromisoformat(until) if until else datetime.now(UTC).date() - timedelta(days=1)
    out_dir, raw_dir = Path(out), Path(raw)
    out_dir.mkdir(parents=True, exist_ok=True)
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    # свежие дни первыми: данные с ближним стаканом (2026) готовы раньше всего
    todo = [d for d in reversed(days) if not bars_path(out_dir, d.isoformat()).exists()]
    typer.echo(f"Дней: {len(days)}, готово: {len(days) - len(todo)}, к обработке: {len(todo)}")

    def job(day: date, client: httpx.Client) -> tuple[str, str]:
        trades = download(client, "aggTrades", day, raw_dir)
        if trades is None:
            return day.isoformat(), "нет сделок в архиве"
        depth = download(client, "bookDepth", day, raw_dir)
        bars = build_day(trades, depth, day.isoformat())
        target = bars_path(out_dir, day.isoformat())
        tmp = target.with_name(target.name + ".part")
        bars.to_csv(tmp, index=False, compression="gzip")
        tmp.replace(target)
        if not keep_raw:
            for f in (trades, depth):
                if f is not None:
                    f.unlink(missing_ok=True)
        trades_ok, depth_ok = int(bars["trades_ok"].sum()), int(bars["depth_ok"].sum())
        return day.isoformat(), f"сделки {trades_ok}/1440, ближний стакан {depth_ok}/1440"

    problems = []
    with (
        httpx.Client(timeout=120, follow_redirects=True) as client,
        ThreadPoolExecutor(workers) as pool,
    ):
        futures = {pool.submit(job, d, client): d for d in todo}
        for n, future in enumerate(as_completed(futures), 1):
            try:
                day, status = future.result()
            except Exception as exc:
                day, status = futures[future].isoformat(), f"ошибка: {exc}"
            if not status.startswith("сделки 1440/") or n % 50 == 0:
                typer.echo(f"  [{n}/{len(todo)}] {day}: {status}")
            if not status.startswith("сделки 1440/"):
                problems.append((day, status))
    typer.secho(f"Готово. Дней с неполными сделками или ошибками: {len(problems)}", bold=True)


@app.command("walk-forward-micro")
def walk_forward_micro_cmd(
    model: str = typer.Option(
        "flow", help="flow — поток сделок (с 2023); book — + стакан ±0.2% (с 2026)"
    ),
    variants: str = typer.Option("ret1,ret5,ret15,barrier5,barrier15", help="Варианты метки"),
    targets: str = typer.Option("1,5,20", help="Целевые частоты сделок в день"),
    deep: bool = typer.Option(False, help="Добавить глубокие уровни ±1…5% (только исследование)"),
    bars_dir: str = typer.Option("data/micro/bars", help="Каталог минутных файлов"),
    out: str = typer.Option("data/experiments", help="Каталог отчётов"),
) -> None:
    """Скользящая проверка модели «спрос и предложение» (решение каждую минуту, горизонты
    1/5/15 мин, исполнение taker и maker). Ничего в mainnet не меняет."""
    from pathlib import Path

    from app.research.micro_bars import load_bars
    from app.research.walkforward_micro import VARIANTS, walk_forward_micro

    if model not in ("flow", "book"):
        raise typer.BadParameter("model: flow или book")
    names = [v.strip() for v in variants.split(",") if v.strip()]
    unknown = [n for n in names if n not in VARIANTS]
    if unknown:
        raise typer.BadParameter(f"неизвестные варианты: {unknown}; есть {list(VARIANTS)}")
    target_list = [float(t) for t in targets.split(",")]
    typer.echo("Загружаю минутные данные…")
    bars = load_bars(Path(bars_dir))
    depth = model == "book"
    report = walk_forward_micro(
        bars,
        [VARIANTS[n] for n in names],
        depth=depth,
        deep=deep,
        trades_per_day=target_list,
        test_months=1 if depth else 3,
        min_train_months=4 if depth else 12,
        max_train_months=None if depth else 12,
        progress=lambda msg: typer.echo(f"  {msg}"),
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(out) / f"walk-forward-micro-{model}{'-deep' if deep else ''}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )

    def pct(v: float | None, spec: str = "+.3%") -> str:
        return "—" if v is None else format(v, spec)

    typer.secho(
        f"Модель {model}{' + глубокий стакан' if deep else ''} · окон {report['folds']} "
        f"с {(report['first_test'] or '')[:10]} · валидных минут {report['rows_valid']}",
        bold=True,
    )
    typer.echo(
        f"{'вариант':<10}{'исп.':<6}{'цель':>5}{'сделок':>8}{'в день':>8}{'угадано':>8}"
        f"{'ср.нетто':>10}{'t':>7}{'сумма':>9}{'приб.окон':>10}{'исполн.':>8}"
        f"{'long t':>8}{'short t':>8}"
    )
    for v in report["variants"]:
        for r in v["results"]:
            t = r["total"]
            typer.echo(
                f"{v['variant']:<10}{r['cost_model']:<6}{r['trades_per_day_target']:>5g}"
                f"{t['trades']:>8}{t['per_day']:>8.2f}{pct(t['hit_rate'], '.0%'):>8}"
                f"{pct(t['mean_net']):>10}{pct(t['t_stat'], '+.2f'):>7}{t['sum_net']:>+9.0%}"
                f"{r['profitable_folds']:>5}/{len(r['folds']):<4}{pct(r['fill_rate'], '.0%'):>8}"
                f"{pct(r['long']['t_stat'], '+.1f'):>8}{pct(r['short']['t_stat'], '+.1f'):>8}"
            )
        rnd = v["random_entry"]
        typer.echo(
            f"{'':<10}случайный вход: "
            + "; ".join(
                f"{cm} long {pct(sides['long'])} short {pct(sides['short'])}"
                for cm, sides in rnd.items()
            )
        )
    typer.echo(
        "gross — без издержек: есть ли у признаков сила предсказания вообще. "
        "t: |t| > 2 — вряд ли случайность. «исполн.» — доля исполненных maker-заявок."
    )
    typer.echo(report["note"])
    typer.echo(f"Отчёт: {out_dir / 'report.json'}")


if __name__ == "__main__":
    app()
