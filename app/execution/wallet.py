"""Проверки перед торговлей: API-кошелёк и часы (§9 плана)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.integrations.hyperliquid.gateway import HyperliquidGateway

WARN_BEFORE_EXPIRY = timedelta(days=14)
REFUSE_BEFORE_EXPIRY = timedelta(days=1)
MAX_CLOCK_SKEW = timedelta(seconds=2)


class WalletCheckError(RuntimeError):
    """Ключ нельзя использовать для торговли."""


class ClockSkewError(RuntimeError):
    """Системные часы расходятся с биржей сильнее допустимого."""


@dataclass(frozen=True, slots=True)
class WalletStatus:
    signer: str  # адрес API-кошелька (из ключа)
    account: str  # торговый аккаунт (HL_ACCOUNT_ADDRESS)
    owner: str  # аккаунт, одобривший API-кошелёк
    vault_address: str | None  # = account, если торгуем субаккаунтом через ключ основного
    agent_name: str | None
    valid_until: datetime | None
    warnings: tuple[str, ...]


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def check_api_wallet(
    gateway: HyperliquidGateway,
    signer: str,
    account: str,
    *,
    now: datetime,
    warn_before: timedelta = WARN_BEFORE_EXPIRY,
    refuse_before: timedelta = REFUSE_BEFORE_EXPIRY,
) -> WalletStatus:
    signer, account = signer.lower(), account.lower()
    if signer == account:
        raise WalletCheckError(
            "ключ принадлежит самому аккаунту — это ключ основного кошелька. "
            "Для торговли нужен отдельный API-кошелёк (§9.1 плана)"
        )

    signer_role = await gateway.get_user_role(signer)
    if signer_role.role != "agent" or signer_role.agent_owner is None:
        raise WalletCheckError(
            f"адрес ключа {signer} не является API-кошельком в этой сети (role={signer_role.role})"
        )
    owner = signer_role.agent_owner

    if account == owner:
        vault_address = None
    else:
        account_role = await gateway.get_user_role(account)
        if account_role.role == "subAccount" and account_role.master == owner:
            vault_address = account  # действия подписываются от имени субаккаунта
        else:
            raise WalletCheckError(
                f"API-кошелёк одобрен аккаунтом {owner}, а торговый аккаунт — {account}"
            )

    warnings: list[str] = []
    agent = next((a for a in await gateway.get_extra_agents(owner) if a.address == signer), None)
    valid_until = agent.valid_until if agent else None
    if agent is None:
        warnings.append("API-кошелёк не найден среди именованных — срок действия неизвестен")
    elif valid_until is not None:
        left = valid_until - now
        if left <= refuse_before:
            raise WalletCheckError(
                f"срок действия API-кошелька {agent.name} истекает {valid_until:%Y-%m-%d %H:%M} UTC"
            )
        if left <= warn_before:
            warnings.append(
                f"API-кошелёк {agent.name} истекает через {left.days} дн. "
                f"({valid_until:%Y-%m-%d}) — создайте новый"
            )

    return WalletStatus(
        signer=signer,
        account=account,
        owner=owner,
        vault_address=vault_address,
        agent_name=agent.name if agent else None,
        valid_until=valid_until,
        warnings=tuple(warnings),
    )


async def measure_clock_skew(
    gateway: HyperliquidGateway,
    account: str,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> timedelta:
    """Время биржи минус локальное (по середине запроса). > 0 — локальные часы отстают."""
    before = clock()
    state = await gateway.get_account_state(account)
    after = clock()
    return state.exchange_time - (before + (after - before) / 2)


async def check_clock(
    gateway: HyperliquidGateway,
    account: str,
    *,
    max_skew: timedelta = MAX_CLOCK_SKEW,
    clock: Callable[[], datetime] = _utc_now,
) -> timedelta:
    skew = await measure_clock_skew(gateway, account, clock=clock)
    if abs(skew) > max_skew:
        raise ClockSkewError(
            f"часы расходятся с биржей на {skew.total_seconds():+.2f} с (допустимо ±"
            f"{max_skew.total_seconds():.0f} с) — синхронизируйте время (NTP)"
        )
    return skew
