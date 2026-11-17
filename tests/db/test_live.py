"""Этап 14: сдвиг рынка после подтверждения, доказательства shadow, готовность, разбор сделок."""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.config.strategy import load_strategy
from app.db.models import ModelVersionRecord, SignalRecord
from app.db.repositories import market as market_repo
from app.domain.account import Side
from app.domain.orders import OrderStatus
from app.execution.engine import OrderPreview
from app.live.evidence import shadow_evidence
from app.live.readiness import CheckStatus, readiness, summary
from app.live.review import round_trips
from tests.db.constants import TEST_DB_URL
from tests.db.trading_env import ACCOUNT, NOW, TEST_KEY, Env
from tests.factories import HOUR, T0, market_meta

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = load_strategy(ROOT / "config" / "strategy.toml")


async def test_entry_blocked_if_market_moved_after_confirmation(env: Env) -> None:
    async def slow_operator(preview: OrderPreview) -> bool:
        env.fake.bid += Decimal("130")  # +20 bps, пока оператор думал
        env.fake.ask += Decimal("130")
        return True

    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=slow_operator)
    assert result.order is not None and result.order.status is OrderStatus.BLOCKED
    assert any("рынок сдвинулся" in r for r in result.order.guard_reasons)
    assert env.order_actions() == []


async def test_small_move_after_confirmation_is_fine(env: Env) -> None:
    async def operator(preview: OrderPreview) -> bool:
        env.fake.bid += Decimal("10")  # ~1.5 bps
        env.fake.ask += Decimal("10")
        return True

    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=operator)
    assert result.order is not None and result.order.status is OrderStatus.FILLED


def _model(version: str = "cb-ev", shadow_at: datetime | None = T0) -> ModelVersionRecord:
    return ModelVersionRecord(
        version=version,
        status="shadow",
        feature_set_version="v1",
        model_sha256="0" * 64,
        artifact_path=version,
        artifact={
            "target": {"horizon_candles": 4},
            "train_class_distribution": {"DOWN": 0.25, "FLAT": 0.5, "UP": 0.25},
        },
        shadow_at=shadow_at,
    )


async def _signals(
    session: AsyncSession, market_id: int, n: int, *, good: bool, errors: int = 0
) -> None:
    for i in range(n):
        t = T0 + i * HOUR
        probs = (
            {"DOWN": 0.1, "FLAT": 0.2, "UP": 0.7} if good else {"DOWN": 0.7, "FLAT": 0.2, "UP": 0.1}
        )
        failed = i < errors
        session.add(
            SignalRecord(
                market_id=market_id,
                decision_time=t,
                model_version="cb-ev",
                model_role="shadow",
                policy_version="p1",
                probabilities=None if failed else probs,
                decision="HOLD",
                reason="HOLD из-за ошибки: признаки: пропуск" if failed else "ok",
                horizon_candles=4,
                created_at=t + timedelta(seconds=10),
                outcome_return=0.01,
                outcome_label="UP",
            )
        )
    await session.flush()


async def test_shadow_evidence(session: AsyncSession) -> None:
    market_id = await market_repo.upsert_market(session, market_meta())
    model = _model()
    session.add(model)
    await session.flush()
    now = T0 + 59 * HOUR

    await _signals(session, market_id, 60, good=True)
    evidence = await shadow_evidence(
        session, model, STRATEGY.live, now=now, step=HOUR, cost_bps=14.5
    )
    assert evidence.ok, evidence.failures


async def test_shadow_evidence_failures(session: AsyncSession) -> None:
    market_id = await market_repo.upsert_market(session, market_meta())
    model = _model()
    session.add(model)
    await session.flush()
    empty = await shadow_evidence(session, model, STRATEGY.live, now=T0, step=HOUR, cost_bps=14.5)
    assert not empty.ok and "нет ни одного сигнала" in empty.failures[0]

    await _signals(session, market_id, 30, good=False, errors=5)
    evidence = await shadow_evidence(
        session, model, STRATEGY.live, now=T0 + 59 * HOUR, step=HOUR, cost_bps=14.5
    )
    text = " | ".join(evidence.failures)
    assert "результатов в shadow 25 из нужных 50" in text
    assert "HOLD из-за ошибок" in text
    assert "покрытие свечей 50%" in text
    assert "не лучше baseline" in text


async def test_readiness_reports_failures_without_changing_anything(
    env: Env, tmp_path: Path
) -> None:
    settings = Settings(
        _env_file=None,
        environment="testnet",
        hyperliquid_network="testnet",
        database_url=TEST_DB_URL,
        hl_account_address=ACCOUNT,
        hl_api_wallet_private_key=TEST_KEY,
    )
    db_name = TEST_DB_URL.rsplit("/", 1)[-1]
    fresh = (NOW - timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ")
    (tmp_path / f"{db_name}_{fresh}.dump").write_bytes(b"x")
    (tmp_path / "restore_checks.log").write_text(
        f"{(NOW - timedelta(days=1)).isoformat()} OK {db_name}_{fresh}.dump tables=20\n"
    )
    checks = await readiness(
        settings,
        STRATEGY,
        env.sm,
        env.engine.gateway,
        market_id=env.engine.market_id,
        backups_dir=tmp_path,
        now=NOW,
    )
    by_name = {c.name: c for c in checks}
    assert by_name["сеть"].status is CheckStatus.FAIL  # testnet
    assert by_name["ручное подтверждение каждого ордера"].status is CheckStatus.OK
    assert by_name["минимальные лимиты"].status is CheckStatus.OK
    assert by_name["уведомления оператору"].status is CheckStatus.FAIL
    assert by_name["активная модель"].status is CheckStatus.FAIL
    assert by_name["бэкап БД"].status is CheckStatus.OK
    assert by_name["проверка восстановления"].status is CheckStatus.OK
    # audit_log общий для всех тестов (append-only): учения могли пройти в другом тесте
    assert by_name["kill switch"].status in (CheckStatus.OK, CheckStatus.WARN)
    assert summary(checks) is CheckStatus.FAIL
    assert env.fake.exchange_actions == []


async def test_round_trip_review(env: Env) -> None:
    opened = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert opened.stop is not None
    await env.engine.close_position(confirm=env.yes)
    await env.engine.sync_fills(NOW - timedelta(hours=1))

    async with env.sm() as db:
        trips = await round_trips(db, env.engine.market_id, STRATEGY)
    (trip,) = trips
    assert trip.side == "long" and trip.closed_at is not None
    assert trip.size == Decimal("0.00023") and trip.bot_entry
    assert trip.exit_kind == "exit"
    assert trip.checks == [], trip.checks  # размер от риска, стоп на всю позицию
    assert trip.entry_slippage_vs_preview_bps is not None
    assert trip.net == trip.realized_pnl - trip.fees + trip.funding


async def test_manual_trade_is_flagged(env: Env) -> None:
    env.fake.fills.append(
        {
            "coin": "BTC",
            "px": "65000",
            "sz": "0.0002",
            "side": "B",
            "time": int(NOW.timestamp() * 1000) - 1000,
            "startPosition": "0",
            "dir": "Open Long",
            "closedPnl": "0",
            "hash": "0x" + "ab" * 32,
            "oid": 999999,
            "crossed": True,
            "fee": "0.005",
            "tid": 424242,
            "feeToken": "USDC",
        }
    )
    await env.engine.sync_fills(NOW - timedelta(hours=1))
    async with env.sm() as db:
        (trip,) = await round_trips(db, env.engine.market_id, STRATEGY)
    assert not trip.bot_entry and "вход не ботом" in trip.notes[0]
    assert "позиция открыта" in trip.notes
