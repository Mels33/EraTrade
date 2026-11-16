import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import ConfigurationError, Network, TradingMode
from app.db.models import AuditLogRecord
from app.db.repositories import control as repo
from app.domain.control import Actor, ActorKind, TransitionError
from app.observability.logging import correlation_scope
from tests.factories import HOUR, T0

OPERATOR = Actor(ActorKind.OPERATOR, "mels")
SYSTEM = Actor(ActorKind.SYSTEM, "risk")
MAINNET = Network.MAINNET


async def _audit(session: AsyncSession) -> list[AuditLogRecord]:
    stmt = select(AuditLogRecord).order_by(AuditLogRecord.id)
    return list((await session.execute(stmt)).scalars())


async def test_ensure_creates_disabled_state_with_audit(session: AsyncSession) -> None:
    state = await repo.ensure_bot_state(session, Network.TESTNET)
    assert state.trading_mode == "DISABLED"
    assert state.entries_blocked is False
    again = await repo.ensure_bot_state(session, Network.TESTNET)
    assert again.version == state.version
    assert [a.action for a in await _audit(session)] == ["bot_state.created"]


async def test_database_of_other_network_rejected(session: AsyncSession) -> None:
    await repo.ensure_bot_state(session, Network.TESTNET)
    with pytest.raises(ConfigurationError, match="принадлежит сети testnet"):
        await repo.ensure_bot_state(session, MAINNET)


async def test_mode_change_is_audited_with_correlation_id(session: AsyncSession) -> None:
    with correlation_scope("cid-mode"):
        changed = await repo.set_trading_mode(
            session, MAINNET, TradingMode.SHADOW, actor=OPERATOR, reason="start shadow"
        )
    assert changed
    state = await repo.get_bot_state(session)
    assert state is not None and state.trading_mode == "SHADOW"
    assert state.updated_by == "operator:mels"

    audit = (await _audit(session))[-1]
    assert audit.action == "bot_state.mode_changed"
    assert audit.actor == "operator:mels"
    assert audit.reason == "start shadow"
    assert audit.correlation_id == "cid-mode"
    assert audit.details == {"from": "DISABLED", "to": "SHADOW"}


async def test_same_mode_is_noop(session: AsyncSession) -> None:
    await repo.ensure_bot_state(session, MAINNET)
    before = len(await _audit(session))
    assert not await repo.set_trading_mode(
        session, MAINNET, TradingMode.DISABLED, actor=OPERATOR, reason="x"
    )
    assert len(await _audit(session)) == before


async def test_forbidden_transition_changes_nothing(session: AsyncSession) -> None:
    await repo.set_trading_mode(session, MAINNET, TradingMode.SHADOW, actor=OPERATOR, reason="r")
    with pytest.raises(TransitionError):
        await repo.set_trading_mode(
            session, MAINNET, TradingMode.LIVE_ENABLED, actor=OPERATOR, reason="r"
        )
    state = await repo.get_bot_state(session)
    assert state is not None and state.trading_mode == "SHADOW"


async def test_restart_downgrades_live_enabled(session: AsyncSession) -> None:
    await repo.set_trading_mode(
        session, MAINNET, TradingMode.LIVE_PAUSED, actor=OPERATOR, reason="r"
    )
    await repo.set_trading_mode(
        session, MAINNET, TradingMode.LIVE_ENABLED, actor=OPERATOR, reason="go live"
    )

    state = await repo.apply_startup_mode(session, MAINNET, TradingMode.LIVE_PAUSED)
    assert state.trading_mode == "LIVE_PAUSED"
    last = (await _audit(session))[-1]
    assert last.actor == "system:startup"
    assert last.reason == "restart"
    assert last.details == {"from": "LIVE_ENABLED", "to": "LIVE_PAUSED"}


async def test_startup_applies_configured_mode(session: AsyncSession) -> None:
    state = await repo.apply_startup_mode(session, MAINNET, TradingMode.SHADOW)
    assert state.trading_mode == "SHADOW"
    assert (await _audit(session))[-1].actor == "config:startup"


async def test_entries_block_survives_restart_and_needs_operator(session: AsyncSession) -> None:
    assert await repo.block_entries(session, MAINNET, actor=SYSTEM, reason="daily loss limit")
    assert not await repo.block_entries(session, MAINNET, actor=SYSTEM, reason="again")

    state = await repo.apply_startup_mode(session, MAINNET, TradingMode.SHADOW)
    assert state.entries_blocked is True
    assert state.block_reason == "daily loss limit"

    with pytest.raises(PermissionError):
        await repo.unblock_entries(session, MAINNET, actor=SYSTEM, reason="auto")
    with pytest.raises(ValueError, match="причина"):
        await repo.unblock_entries(session, MAINNET, actor=OPERATOR, reason=" ")

    assert await repo.unblock_entries(session, MAINNET, actor=OPERATOR, reason="reviewed")
    assert state.entries_blocked is False and state.block_reason is None
    last = (await _audit(session))[-1]
    assert last.details == {"previous_block_reason": "daily loss limit"}


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_log SET action = 'tampered'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ],
)
async def test_audit_log_is_append_only(session: AsyncSession, sql: str) -> None:
    await repo.record_audit(session, actor=OPERATOR, action="test.event")
    with pytest.raises(DBAPIError, match="append-only"):
        async with session.begin_nested():
            await session.execute(text(sql))
    assert [a.action for a in await _audit(session)] == ["test.event"]


async def test_heartbeat_upsert(session: AsyncSession) -> None:
    await repo.beat(session, component="collector", instance_id="a", started_at=T0, now=T0)
    await repo.beat(
        session,
        component="collector",
        instance_id="a",
        started_at=T0,
        now=T0 + HOUR,
        details={"ws": "healthy"},
    )
    [hb] = await repo.get_heartbeats(session)
    assert hb.last_beat_at == T0 + HOUR
    assert hb.details == {"ws": "healthy"}


async def test_updated_at_changes_on_update(session: AsyncSession) -> None:
    await repo.ensure_bot_state(session, MAINNET)
    # now() в PostgreSQL — время начала транзакции, поэтому сдвигаем created-время вручную.
    await session.execute(text("UPDATE bot_state SET updated_at = now() - interval '1 hour'"))
    await repo.set_trading_mode(session, MAINNET, TradingMode.SHADOW, actor=OPERATOR, reason="r")
    await session.flush()
    state = await repo.get_bot_state(session)
    assert state is not None
    await session.refresh(state)
    db_now = (await session.execute(text("select now()"))).scalar_one()
    assert state.updated_at == db_now
