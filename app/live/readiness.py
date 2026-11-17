"""Готовность к живой торговле (§17.1): автоматический чек-лист.

FAIL — запускать live нельзя; WARN — допустимо, но оператор должен понимать риск.
Проверка только читает (биржа, БД, файлы бэкапов) и ничего не меняет.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from eth_account import Account
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings, TradingMode
from app.config.strategy import StrategyConfig
from app.db.models import AuditLogRecord
from app.db.repositories import control
from app.db.repositories import market as market_repo
from app.db.repositories import models as models_repo
from app.db.repositories import reconciliation as rec_repo
from app.execution.wallet import WalletCheckError, check_api_wallet
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.live.evidence import shadow_evidence

RESTORE_LOG = "restore_checks.log"
_DUMP_RE = re.compile(r"_(\d{8}T\d{6}Z)\.dump$")


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: CheckStatus
    detail: str


def _latest_backup(backups: Path, database: str) -> datetime | None:
    stamps = []
    for path in backups.glob(f"{database}_*.dump"):
        if m := _DUMP_RE.search(path.name):
            stamps.append(datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC))
    return max(stamps, default=None)


def _latest_restore_check(backups: Path, database: str) -> datetime | None:
    """Строки журнала: `<ISO-время> OK <файл дампа>` (пишет scripts/restore_check.sh)."""
    log = backups / RESTORE_LOG
    if not log.is_file():
        return None
    latest = None
    for line in log.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "OK" and database in parts[2]:
            t = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
            latest = t if latest is None or t > latest else latest
    return latest


async def readiness(
    settings: Settings,
    strategy: StrategyConfig,
    sm: async_sessionmaker[AsyncSession],
    gateway: HyperliquidGateway,
    *,
    market_id: int,
    backups_dir: Path,
    now: datetime,
) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str, *, warn: bool = False) -> None:
        status = CheckStatus.OK if ok else (CheckStatus.WARN if warn else CheckStatus.FAIL)
        checks.append(Check(name, status, detail))

    live, limits = strategy.live, strategy.limits
    step = timedelta(hours=1)

    # --- конфигурация и лимиты ---
    add("сеть", settings.is_mainnet, f"{settings.network.value}")
    add(
        "ручное подтверждение каждого ордера",
        strategy.execution.require_manual_confirmation,
        "require_manual_confirmation = "
        + str(strategy.execution.require_manual_confirmation).lower()
        + " (этап 14 — только с подтверждением)",
    )
    add(
        "минимальные лимиты",
        limits.max_position_notional_usdc <= 25 and limits.max_leverage <= 2,
        f"номинал ≤ {limits.max_position_notional_usdc} USDC, плечо ≤ {limits.max_leverage}x, "
        f"дневной убыток ≤ {limits.max_daily_loss_fraction:.0%}, "
        f"сделок в день ≤ {limits.max_trades_per_day}",
    )
    add(
        "уведомления оператору",
        settings.telegram_bot_token is not None and bool(settings.telegram_chat_id),
        "Telegram настроен — проверьте доставку: eratrade notify-test"
        if settings.telegram_bot_token
        else "Telegram не настроен (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)",
    )

    # --- API-кошелёк ---
    key = settings.hl_api_wallet_private_key
    if key is None or settings.hl_account_address is None:
        add("API-кошелёк", False, "ключ или адрес аккаунта не подключены")
    else:
        signer = Account.from_key(key.get_secret_value()).address
        try:
            wallet = await check_api_wallet(gateway, signer, settings.hl_account_address, now=now)
        except WalletCheckError as exc:
            add("API-кошелёк", False, str(exc))
        else:
            if wallet.valid_until is None:
                add("API-кошелёк", False, "срок действия неизвестен", warn=True)
            else:
                days = (wallet.valid_until - now).days
                add(
                    "API-кошелёк",
                    days >= live.min_wallet_valid_days,
                    f"{wallet.agent_name}: действует ещё {days} дн. "
                    f"(до {wallet.valid_until:%Y-%m-%d})",
                )

    async with sm() as session:
        # --- состояние бота ---
        state = await control.get_bot_state(session)
        if state is None:
            add("состояние бота", False, "не инициализировано")
        else:
            mode = TradingMode(state.trading_mode)
            add(
                "режим",
                mode in (TradingMode.LIVE_PAUSED, TradingMode.LIVE_ENABLED),
                f"{mode.value} (live включает оператор: eratrade set-mode LIVE_ENABLED)",
            )
            add(
                "запрет входов",
                not state.entries_blocked,
                state.block_reason or "нет",
            )

        # --- сверка, компоненты, данные ---
        run = await rec_repo.latest_run(session, market_id, finished_only=True)
        if run is None or run.finished_at is None:
            add("сверка", False, "ещё не выполнялась")
        else:
            age = (now - run.finished_at).total_seconds()
            add(
                "сверка",
                run.status == "ok" and age < 300,
                f"#{run.id}: {run.status}, {age:.0f} с назад",
            )
        beats = {h.component: h for h in await control.get_heartbeats(session)}
        for component, limit in (("collector", 60), ("reconciler", 120), ("shadow", 120)):
            hb = beats.get(component)
            beat_age = (now - hb.last_beat_at).total_seconds() if hb else None
            add(
                f"компонент {component}",
                beat_age is not None and beat_age < limit,
                "нет heartbeat" if beat_age is None else f"heartbeat {beat_age:.0f} с назад",
            )
        last = await market_repo.get_last_closed_candle(
            session, market_id, strategy.market.interval
        )
        candle_age = (now - last.close_time) if last else None
        add(
            "свежесть свечей",
            candle_age is not None and candle_age < step + timedelta(minutes=2),
            "нет свечей" if last is None else f"последняя закрыта {last.close_time:%H:%M} UTC",
        )

        # --- модель ---
        active, shadow = await models_repo.models_for_signal(session)
        if active is None:
            add(
                "активная модель",
                False,
                "нет — активируйте после shadow: eratrade model-activate <версия>"
                + (f" (в shadow: {shadow.version})" if shadow else ""),
            )
        else:
            evidence = await shadow_evidence(
                session,
                active,
                live,
                now=now,
                step=step,
                cost_bps=float(strategy.target.cost_bps),
            )
            add(
                "доказательства shadow",
                evidence.ok,
                f"{active.version}: " + ("; ".join(evidence.failures) or "достаточно"),
            )
        if shadow is not None:
            evidence = await shadow_evidence(
                session, shadow, live, now=now, step=step, cost_bps=float(strategy.target.cost_bps)
            )
            add(
                "shadow-модель",
                evidence.ok,
                f"{shadow.version}: " + ("; ".join(evidence.failures) or "готова к активации"),
                warn=True,
            )

        # --- аварийные механизмы ---
        kill = (
            await session.execute(
                select(AuditLogRecord.occurred_at)
                .where(AuditLogRecord.action == "emergency.full_kill")
                .order_by(AuditLogRecord.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        add(
            "kill switch",
            kill is not None,
            f"последний прогон {kill:%Y-%m-%d %H:%M} UTC"
            if kill
            else "проверен в тестах, живого прогона не было (docs/runbook.md, шаг 3)",
            warn=True,
        )

    # --- бэкапы ---
    database = make_url(settings.database_url.get_secret_value()).database or ""
    backup = _latest_backup(backups_dir, database)
    add(
        "бэкап БД",
        backup is not None and now - backup < timedelta(hours=live.max_backup_age_hours),
        "нет бэкапов" if backup is None else f"последний {backup:%Y-%m-%d %H:%M} UTC",
    )
    restored = _latest_restore_check(backups_dir, database)
    add(
        "проверка восстановления",
        restored is not None and now - restored < timedelta(days=live.max_restore_check_age_days),
        "не проверялось — scripts/restore_check.sh <последний дамп>"
        if restored is None
        else f"последняя {restored:%Y-%m-%d %H:%M} UTC",
    )
    return checks


def summary(checks: list[Check]) -> CheckStatus:
    if any(c.status is CheckStatus.FAIL for c in checks):
        return CheckStatus.FAIL
    if any(c.status is CheckStatus.WARN for c in checks):
        return CheckStatus.WARN
    return CheckStatus.OK
