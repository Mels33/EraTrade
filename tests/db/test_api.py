"""API панели (§13): аутентификация, чтение, управляющие команды и идемпотентность."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.api.app import create_api
from app.api.context import ApiContext
from app.config.settings import Settings, TradingMode
from app.db.models import AuditLogRecord
from app.db.repositories import control
from app.domain.account import Side
from app.execution.engine import ExecutionEngine
from tests.db.constants import TEST_DB_URL
from tests.db.trading_env import ACCOUNT, Env

TOKEN = "operator-test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_context(env: Env, *, with_engine: bool = True) -> ApiContext:
    settings = Settings(
        _env_file=None,
        environment="testnet",
        hyperliquid_network="testnet",
        database_url=TEST_DB_URL,
        hl_account_address=ACCOUNT,
        api_token=TOKEN,
    )

    @asynccontextmanager
    async def engine_factory() -> AsyncIterator[ExecutionEngine]:
        yield env.engine

    return ApiContext(
        settings=settings,
        sm=env.sm,
        gateway=env.engine.gateway,
        strategy=env.engine.strategy,
        market_id=env.engine.market_id,
        coin=env.engine.coin,
        user=ACCOUNT,
        risk=env.engine.risk,
        notifier=env.notifier,
        engine_factory=engine_factory if with_engine else None,
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
    assert body["coin"] == "BTC" and body["trading_commands_available"] is True
    assert body["data"]["last_closed_candle"] is not None


async def test_account_and_orders_use_string_decimals(env: Env, api: httpx.AsyncClient) -> None:
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    account = (await api.get("/api/account", headers=AUTH)).json()
    assert account["position"]["size"] == "0.00023"
    assert account["risk"]["equity"] == "98.45"
    assert [o["reduce_only"] for o in account["open_orders"]] == [True]

    orders = (await api.get("/api/orders", headers=AUTH)).json()
    assert {o["purpose"] for o in orders} == {"entry", "stop"}
    assert all(isinstance(o["size"], str) for o in orders)
    assert (await api.get("/api/positions", headers=AUTH)).json()["position"]["size"] == "0.00023"
    assert "daily_limit" in (await api.get("/api/risk", headers=AUTH)).json()
    assert (await api.get("/api/audit", headers=AUTH)).status_code == 200
    assert (await api.get("/api/fills", headers=AUTH)).status_code == 200
    assert (await api.get("/api/signals", headers=AUTH)).json()["items"] == []


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


async def test_dangerous_commands_require_explicit_confirm(
    env: Env, api: httpx.AsyncClient
) -> None:
    opened = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert opened.stop is not None
    missing = await post(api, "/api/bot/cancel-all", {"reason": "тест отмены"}, key="c1")
    assert missing.status_code == 422 and "CANCEL_ALL" in missing.json()["detail"]

    done = await post(
        api, "/api/bot/cancel-all", {"reason": "тест отмены", "confirm": "CANCEL_ALL"}, key="c2"
    )
    assert done.status_code == 200
    assert any("БЕЗ стопа" in w for w in done.json()["warnings"])


async def test_kill_switch_via_api(env: Env, api: httpx.AsyncClient) -> None:
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    before = await last_audit_id(env)
    response = await post(
        api, "/api/bot/kill-switch", {"reason": "авария", "confirm": "KILL"}, key="k"
    )
    assert response.status_code == 200, response.text
    assert env.fake.position == 0
    async with env.sm() as db:
        state = await control.get_bot_state(db)
    assert state is not None and state.trading_mode == TradingMode.DISABLED.value
    assert await audit_count(env, "emergency.full_kill", before) == 1


async def test_trading_commands_unavailable_without_key(env: Env) -> None:
    async with client_for(make_context(env, with_engine=False)) as client:
        response = await post(
            client, "/api/bot/flatten", {"reason": "нет ключа", "confirm": "FLATTEN"}, key="f"
        )
    assert response.status_code == 503


async def test_model_activation_not_yet_available(api: httpx.AsyncClient) -> None:
    assert (await api.post("/api/models/v1/activate", headers=AUTH)).status_code == 501
