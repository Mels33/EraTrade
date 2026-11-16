"""Правила смены режима работы бота (docs/spec.md, §6). Чистые функции без БД."""

from dataclasses import dataclass
from enum import StrEnum

from app.config.settings import ALLOWED_STARTUP_MODES, Network, TradingMode

# Все режимы, допустимые в сети во время работы (в отличие от старта, здесь есть LIVE_ENABLED).
ALLOWED_RUNTIME_MODES: dict[Network, frozenset[TradingMode]] = {
    Network.TESTNET: ALLOWED_STARTUP_MODES[Network.TESTNET],
    Network.MAINNET: ALLOWED_STARTUP_MODES[Network.MAINNET] | {TradingMode.LIVE_ENABLED},
}


class ActorKind(StrEnum):
    OPERATOR = "operator"  # человек через CLI / панель
    CONFIG = "config"  # применение TRADING_MODE из конфигурации при старте процесса
    SYSTEM = "system"  # автоматика: лимиты, сбои, рестарт


@dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    name: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.name}"


class TransitionError(ValueError):
    """Смена режима запрещена правилами."""


def validate_transition(
    *,
    network: Network,
    current: TradingMode,
    target: TradingMode,
    actor: Actor,
    reason: str | None,
) -> None:
    """Бросает TransitionError, если переход current → target запрещён."""
    if target not in ALLOWED_RUNTIME_MODES[network]:
        raise TransitionError(f"режим {target} недопустим в сети {network}")

    if actor.kind is ActorKind.SYSTEM:
        # Автоматика может только понижать режим.
        downgrade = target is TradingMode.DISABLED or (
            target is TradingMode.LIVE_PAUSED and current is TradingMode.LIVE_ENABLED
        )
        if not downgrade:
            raise TransitionError(f"автоматика не может переводить {current} → {target}")
        return

    if actor.kind is ActorKind.CONFIG:
        if target not in ALLOWED_STARTUP_MODES[network]:
            raise TransitionError(f"конфигурация не может задать режим {target}")
        return

    # Оператор.
    if not reason or not reason.strip():
        raise TransitionError("оператор должен указать причину смены режима")
    if target is TradingMode.LIVE_ENABLED and current is not TradingMode.LIVE_PAUSED:
        raise TransitionError("LIVE_ENABLED включается только из LIVE_PAUSED")
