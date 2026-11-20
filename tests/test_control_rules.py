import pytest

from app.config.settings import Network, TradingMode
from app.domain.control import Actor, ActorKind, TransitionError, validate_transition

OPERATOR = Actor(ActorKind.OPERATOR, "mels")
SYSTEM = Actor(ActorKind.SYSTEM, "risk")
CONFIG = Actor(ActorKind.CONFIG, "startup")

M = TradingMode


def check(
    current: TradingMode,
    target: TradingMode,
    actor: Actor,
    *,
    network: Network = Network.MAINNET,
    reason: str | None = "test",
) -> None:
    validate_transition(network=network, current=current, target=target, actor=actor, reason=reason)


@pytest.mark.parametrize("current", [M.DISABLED, M.SHADOW, M.LIVE_PAUSED])
def test_operator_enables_live_from_any_mode(current: TradingMode) -> None:
    check(current, M.LIVE_ENABLED, OPERATOR)
    check(M.LIVE_ENABLED, current, OPERATOR)  # и выключает в любой режим


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_operator_needs_reason(reason: str | None) -> None:
    with pytest.raises(TransitionError, match="причину"):
        check(M.DISABLED, M.SHADOW, OPERATOR, reason=reason)


@pytest.mark.parametrize("actor", [OPERATOR, SYSTEM, CONFIG])
def test_nobody_enables_live_on_testnet(actor: Actor) -> None:
    with pytest.raises(TransitionError, match="недопустим"):
        check(M.TESTNET, M.LIVE_ENABLED, actor, network=Network.TESTNET)


@pytest.mark.parametrize("target", [M.SHADOW, M.LIVE_PAUSED])
def test_mainnet_modes_rejected_on_testnet(target: TradingMode) -> None:
    with pytest.raises(TransitionError, match="недопустим"):
        check(M.DISABLED, target, OPERATOR, network=Network.TESTNET)


def test_system_can_downgrade() -> None:
    check(M.LIVE_ENABLED, M.LIVE_PAUSED, SYSTEM, reason=None)
    check(M.LIVE_ENABLED, M.DISABLED, SYSTEM, reason=None)
    check(M.TESTNET, M.DISABLED, SYSTEM, network=Network.TESTNET, reason=None)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (M.LIVE_PAUSED, M.LIVE_ENABLED),
        (M.DISABLED, M.SHADOW),
        (M.SHADOW, M.LIVE_PAUSED),
        (M.DISABLED, M.LIVE_PAUSED),
    ],
)
def test_system_cannot_upgrade(current: TradingMode, target: TradingMode) -> None:
    with pytest.raises(TransitionError, match="автоматика"):
        check(current, target, SYSTEM)


def test_config_cannot_set_live_enabled() -> None:
    with pytest.raises(TransitionError, match="конфигурация"):
        check(M.LIVE_PAUSED, M.LIVE_ENABLED, CONFIG)


def test_config_sets_startup_modes() -> None:
    check(M.DISABLED, M.SHADOW, CONFIG, reason=None)
    check(M.DISABLED, M.TESTNET, CONFIG, network=Network.TESTNET, reason=None)
