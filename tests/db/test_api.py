"""API панели (§13): аутентификация, чтение, управляющие команды и идемпотентность."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.app import create_api
from app.api.context import ApiContext
from app.config.settings import Network, Settings, TradingMode
from app.config.strategy import load_strategy
from app.db.models import AuditLogRecord
from app.db.repositories import control
from app.observability.notify import LogNotifier
from tests.db.constants import TEST_DB_URL

TOKEN = "operator-test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


@dataclass
class Env:
    sm: async_sessionmaker[AsyncSession]
    clock: Callable[[], datetime]
    notifier: LogNotifier


@pytest.fixture
async def env(committing_sessionmaker: async_sessionmaker[AsyncSession]) -> Env:
    async with committing_sessionmaker.begin() as db:
        state = await control.ensure_bot_state(db, Network.TESTNET)
        state.trading_mode = TradingMode.TESTNET.value
    return Env(committing_sessionmaker, lambda: NOW, LogNotifier())


def make_context(env: Env) -> ApiContext:
    settings = Settings(
        _env_file=None,
        environment="testnet",
        hyperliquid_network="testnet",
        database_url=TEST_DB_URL,
        api_token=TOKEN,
        hl_account_address="0x" + "12" * 20,
    )
    return ApiContext(
        settings=settings,
        sm=env.sm,
        strategy=load_strategy(Path("config/strategy.toml")),
        notifier=env.notifier,
        clock=env.clock,
    )


@asynccontextmanager
async def client_for(ctx: ApiContext) -> AsyncIterator[httpx.AsyncClient]:
    app = create_api(lambda: _noop())
    app.state.ctx = ctx  # ASGITransport не запускает lifespan — контекст подставляем сами
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@asynccontextmanager
async def _noop() -> AsyncIterator[ApiContext]:
    raise AssertionError("lifespan в тестах не используется")
    yield


@pytest.fixture
async def api(env: Env) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(make_context(env)) as client:
        yield client


def post(client: httpx.AsyncClient, path: str, body: dict[str, Any], key: str | None = "k1") -> Any:
    headers = dict(AUTH)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(path, json=body, headers=headers)


async def audit_count(env: Env, action: str, after_id: int = 0) -> int:
    """audit_log не очищается между тестами (append-only) — считаем записи после after_id."""
    async with env.sm() as db:
        stmt = select(func.count()).where(
            AuditLogRecord.action == action, AuditLogRecord.id > after_id
        )
        return int((await db.execute(stmt)).scalar_one())


async def last_audit_id(env: Env) -> int:
    async with env.sm() as db:
        return int(
            (await db.execute(select(func.coalesce(func.max(AuditLogRecord.id), 0)))).scalar_one()
        )


async def test_health_is_public(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/health/live")).json() == {"status": "ok"}
    assert (await api.get("/api/health/ready")).status_code == 200


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}]
)
async def test_reads_require_operator_token(
    api: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    if headers.get("Authorization") == TOKEN:  # без "Bearer " тоже принимается
        assert (await api.get("/api/status", headers=headers)).status_code == 200
    else:
        assert (await api.get("/api/status", headers=headers)).status_code == 401


async def test_status(api: httpx.AsyncClient) -> None:
    body = (await api.get("/api/status", headers=AUTH)).json()
    assert body["network"] == "testnet" and body["mode"] == "TESTNET"
    assert "trading_commands_available" not in body


async def test_account_reads_current_exchange_state(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decimal import Decimal
    from types import SimpleNamespace

    class Gateway:
        async def get_account_state(self, user: str) -> Any:
            return SimpleNamespace(
                exchange_time=NOW,
                account_value=Decimal("16.5"),
                withdrawable=Decimal("1.2"),
                positions=[
                    SimpleNamespace(
                        coin="JUP",
                        size=Decimal("54"),
                        entry_price=Decimal("0.22039"),
                        position_value=Decimal("11.9"),
                        unrealized_pnl=Decimal("0.03"),
                    )
                ],
            )

        async def get_open_orders(self, user: str) -> list[Any]:
            return [
                SimpleNamespace(
                    coin="JUP",
                    side=SimpleNamespace(value="sell"),
                    limit_price=Decimal("0.23"),
                    trigger_price=None,
                    size=Decimal("54"),
                    order_type="Limit",
                    reduce_only=True,
                    is_trigger=False,
                    oid=7,
                )
            ]

        async def get_all_mids(self) -> dict[str, Decimal]:
            return {"JUP": Decimal("0.221")}

    @asynccontextmanager
    async def fake_gateway(settings: Any, *, service: str) -> AsyncIterator[Gateway]:
        yield Gateway()

    monkeypatch.setattr("app.api.app.open_gateway", fake_gateway)
    response = await api.get("/api/account", headers=AUTH)
    assert response.status_code == 200
    data = response.json()
    assert data["account_value"] == "16.5"
    assert data["positions"][0]["mid"] == "0.221"
    assert data["orders"] == [
        {
            "coin": "JUP",
            "side": "sell",
            "price": "0.23",
            "size": "54",
            "order_type": "Limit",
            "reduce_only": True,
            "is_trigger": False,
            "oid": 7,
        }
    ]


async def test_pause_requires_key_and_reason_and_is_idempotent(
    env: Env, api: httpx.AsyncClient
) -> None:
    assert (await post(api, "/api/bot/pause", {"reason": "проверка"}, key=None)).status_code == 400
    assert (await post(api, "/api/bot/pause", {"reason": "x"})).status_code == 422
    assert (await api.post("/api/bot/pause", json={"reason": "проверка"})).status_code == 401

    before = await last_audit_id(env)
    first = await post(api, "/api/bot/pause", {"reason": "проверка панели"}, key="pause-1")
    assert first.status_code == 200
    async with env.sm() as db:
        state = await control.get_bot_state(db)
    assert state is not None and state.entries_blocked

    replay = await post(api, "/api/bot/pause", {"reason": "проверка панели"}, key="pause-1")
    assert replay.status_code == 200 and replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == first.json()
    assert await audit_count(env, "bot_state.entries_blocked", before) == 1  # выполнена один раз

    other = await post(api, "/api/bot/resume", {"reason": "другая команда"}, key="pause-1")
    assert other.status_code == 422

    resumed = await post(api, "/api/bot/resume", {"reason": "разобрались"}, key="resume-1")
    assert resumed.json()["steps"] == ["запрет входов снят"]


async def test_mode_change_rules_apply(api: httpx.AsyncClient) -> None:
    bad = await post(api, "/api/bot/mode", {"reason": "попытка", "mode": "SHADOW"}, key="m1")
    assert bad.status_code == 422  # SHADOW недопустим в testnet
    ok = await post(api, "/api/bot/mode", {"reason": "остановка", "mode": "DISABLED"}, key="m2")
    assert ok.status_code == 200 and ok.json()["steps"] == ["режим DISABLED"]


async def test_mm_endpoint(env: Env, api: httpx.AsyncClient) -> None:
    from app.db.repositories import control
    from app.db.repositories import mm as mm_repo
    from app.integrations.hyperliquid.parsing import datetime_to_ms, parse_fill

    now = env.clock()
    fill = parse_fill(
        {
            "coin": "FARTCOIN", "side": "A", "px": "0.0866", "sz": "140",
            "time": datetime_to_ms(now),
            "startPosition": "140", "dir": "Close Long", "closedPnl": "0.014", "fee": "0.0018",
            "feeToken": "USDC", "oid": 5, "tid": 9, "hash": "0x0", "crossed": False,
        }
    )  # fmt: skip
    async with env.sm.begin() as db:
        assert await mm_repo.add_fills(db, [fill]) == {9}
        await control.beat(
            db, component="mm", instance_id="t", started_at=now, now=now,
            details={"state": "котирует", "quoting": True, "coins": {}},
        )  # fmt: skip
    data = (await api.get("/api/mm", headers=AUTH)).json()
    assert data["config"]["coins"] == ["FARTCOIN", "VVV"]
    assert data["live"]["state"] == "котирует"
    assert data["fills"][0]["notional"] == "12.12" and data["fills"][0]["side"] == "sell"
    [day] = data["daily"]
    assert day["coin"] == "FARTCOIN" and day["fills"] == 1 and day["net"] == "0.0122"


async def test_start_and_stop_for_market_maker(env: Env, api: httpx.AsyncClient) -> None:
    await post(api, "/api/bot/pause", {"reason": "перед запуском"}, key="p0")
    bad = await post(
        api, "/api/bot/start", {"reason": "запуск", "confirmation_phrase": "да"}, key="s0"
    )
    assert bad.status_code == 422
    async with env.sm() as db:
        state = await control.get_bot_state(db)
    assert state is not None and state.entries_blocked  # ничего не изменилось

    # В testnet LIVE_ENABLED недопустим: снятие запрета откатывается вместе с отказом.
    phrase = {"reason": "запуск", "confirmation_phrase": "I_UNDERSTAND_REAL_MONEY"}
    assert (await post(api, "/api/bot/start", phrase, key="s1")).status_code == 422
    async with env.sm() as db:
        state = await control.get_bot_state(db)
    assert state is not None and state.entries_blocked

    stop = await post(api, "/api/bot/stop", {"reason": "конец дня"}, key="st")
    assert stop.status_code == 200 and "DISABLED" in stop.json()["steps"][0]
    async with env.sm() as db:
        state = await control.get_bot_state(db)
    assert state is not None and state.trading_mode == TradingMode.DISABLED.value


async def test_commands_from_panel(env: Env) -> None:
    import asyncio
    import sys

    from app.api.jobs import JobRunner

    ctx = make_context(env)
    # пауза: команда ещё выполняется, когда приходит второй запрос
    script = "import time; time.sleep(0.5); print({!r})"
    ctx.jobs = JobRunner(runner=lambda args: [sys.executable, "-c", script.format(args)])
    async with client_for(ctx) as client:
        catalog = (await client.get("/api/jobs", headers=AUTH)).json()
        assert any(c["name"] == "account" for c in catalog["commands"])
        assert (await post(client, "/api/jobs/api", {"args": {}})).status_code == 404
        bad = await post(client, "/api/jobs/wick-scan", {"args": {"top": "5"}})
        assert bad.status_code == 422
        before = await last_audit_id(env)
        ok = await post(client, "/api/jobs/wick-scan", {"args": {"top": "40"}})
        assert ok.status_code == 200 and ok.json()["job"]["state"] == "running"
        assert (await post(client, "/api/jobs/account", {"args": {}})).status_code == 409
        assert ctx.jobs._task is not None
        await asyncio.wait_for(ctx.jobs._task, 10)
        job = (await client.get("/api/jobs", headers=AUTH)).json()["job"]
        assert job["state"] == "done" and job["log"] == ["['wick-scan', '--top', '40']"]
        assert (await client.get("/api/wick/scan", headers=AUTH)).json()["job"]["state"] == "done"
    assert await audit_count(env, "panel.command", before) == 1
