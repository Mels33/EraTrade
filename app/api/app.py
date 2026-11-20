"""FastAPI: мониторинг и управление ботом (§13 плана). Только 127.0.0.1, токен оператора.

Деньги и размеры отдаются строками (Decimal без потери точности).
"""

import asyncio
import hmac
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from app.api.context import ApiContext
from app.api.jobs import COMMANDS, JobError, catalog
from app.config.settings import MAINNET_CONFIRMATION_PHRASE, TradingMode
from app.db.models import ApiRequestRecord, AuditLogRecord
from app.db.repositories import control
from app.db.repositories import mm as mm_repo
from app.domain.control import Actor, ActorKind, TransitionError
from app.execution.lock import AnotherTraderRunningError
from app.integrations.hyperliquid.gateway import open_gateway

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
API_ACTOR = Actor(ActorKind.OPERATOR, "api")


def _s(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _t(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _ctx(request: Request) -> ApiContext:
    ctx: ApiContext = request.app.state.ctx
    return ctx


Ctx = Annotated[ApiContext, Depends(_ctx)]


def require_operator(ctx: Ctx, authorization: Annotated[str | None, Header()] = None) -> Actor:
    token = ctx.settings.api_token
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if (
        token is None
        or not supplied
        or not hmac.compare_digest(supplied.encode(), token.get_secret_value().encode())
    ):
        raise HTTPException(status_code=401, detail="нужен токен оператора")
    return API_ACTOR


Operator = Annotated[Actor, Depends(require_operator)]


class CommandBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ModeBody(CommandBody):
    mode: TradingMode
    confirmation_phrase: str | None = None


# --- чтение ---

read = APIRouter(prefix="/api", dependencies=[Depends(require_operator)])
public = APIRouter(prefix="/api")


@public.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@public.get("/health/ready")
async def health_ready(ctx: Ctx) -> JSONResponse:
    try:
        async with ctx.sm() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "db unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


@read.get("/account")
async def account_overview(ctx: Ctx) -> dict[str, Any]:
    """Актуальные позиции и ордера с биржи, независимо от heartbeat стратегии."""
    user = ctx.settings.hl_account_address
    if user is None:
        raise HTTPException(status_code=503, detail="не задан HL_ACCOUNT_ADDRESS")
    try:
        async with open_gateway(ctx.settings, service="trading") as gw:
            account, orders, mids = await asyncio.gather(
                gw.get_account_state(user), gw.get_open_orders(user), gw.get_all_mids()
            )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="биржа временно недоступна") from exc
    return {
        "time": _t(account.exchange_time),
        "account_value": str(account.account_value),
        "withdrawable": str(account.withdrawable),
        "positions": [
            {
                "coin": p.coin,
                "size": str(p.size),
                "entry_price": _s(p.entry_price),
                "position_value": str(p.position_value),
                "unrealized_pnl": str(p.unrealized_pnl),
                "mid": _s(mids.get(p.coin)),
            }
            for p in account.positions
        ],
        "orders": [
            {
                "coin": o.coin,
                "side": o.side.value,
                "price": _s(o.trigger_price if o.is_trigger else o.limit_price),
                "size": str(o.size),
                "order_type": o.order_type,
                "reduce_only": o.reduce_only,
                "is_trigger": o.is_trigger,
                "oid": o.oid,
            }
            for o in orders
        ],
    }


@read.get("/mm")
async def mm_overview(ctx: Ctx, days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    """Маркет-мейкер: живое состояние (heartbeat), последние исполнения, итоги по дням."""
    now = ctx.clock()
    async with ctx.sm() as db:
        beat = next((h for h in await control.get_heartbeats(db) if h.component == "mm"), None)
        fills = await mm_repo.recent_fills(db, 50)
        daily = await mm_repo.daily_summary(db, now - timedelta(days=days))
        minutes = await mm_repo.pnl_by_minute(db, now - timedelta(days=days))
    curve, total = [], Decimal(0)
    for t, net in minutes:
        total += net
        curve.append({"t": _t(t), "pnl": str(total)})
    return {
        "curve": curve,
        "config": {
            "coins": list(ctx.strategy.mm.coins),
            "order_usd": str(ctx.strategy.mm.order_usd),
            "max_inventory_usd": str(ctx.strategy.mm.max_inventory_usd),
            "daily_loss_usd": str(ctx.strategy.mm.daily_loss_usd),
        },
        "live": (
            {
                "last_beat_age_s": round((now - beat.last_beat_at).total_seconds(), 1),
                "started_at": _t(beat.started_at),
                **beat.details,
            }
            if beat
            else None
        ),
        "fills": [
            {
                "time": _t(f.time),
                "coin": f.coin,
                "side": f.side,
                "price": str(f.price),
                "size": str(f.size),
                "notional": str((f.price * f.size).quantize(Decimal("0.01"))),
                "fee": str(f.fee),
                "closed_pnl": str(f.closed_pnl),
                "crossed": f.crossed,
            }
            for f in fills
        ],
        "daily": [
            {k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()} for row in daily
        ],
    }


@read.get("/wick")
async def wick_overview(ctx: Ctx, days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
    """Ловля проколов: живое состояние (heartbeat wick), исполнения, итоги по дням, кривая."""
    data = await mm_overview(ctx, days)
    async with ctx.sm() as db:
        beat = next((h for h in await control.get_heartbeats(db) if h.component == "wick"), None)
    now = ctx.clock()
    cfg = ctx.strategy.wick
    data["config"] = {
        "max_coins": cfg.max_coins,
        "levels": cfg.levels,
        "levels_usd": [str(round(cfg.base_usd * cfg.size_mult**k, 2)) for k in range(cfg.levels)],
        "daily_loss_usd": str(cfg.daily_loss_usd),
    }
    data["live"] = (
        {
            "last_beat_age_s": round((now - beat.last_beat_at).total_seconds(), 1),
            "started_at": _t(beat.started_at),
            **beat.details,
        }
        if beat
        else None
    )
    return data


class ScanBody(BaseModel):
    top: int = Field(default=80, ge=10, le=200)


def _scan_result(ctx: ApiContext) -> dict[str, Any] | None:
    path = Path(ctx.strategy.wick.scan_path)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return {
        "at": data.get("at"),
        "train": data.get("train"),
        "test": data.get("test"),
        "universe": data.get("universe"),
        "coins": [
            {
                "coin": c["coin"],
                "passed": c.get("passed", False),
                "test_total": c.get("test_total"),
                "test_drawdown": c.get("test_drawdown"),
                "test_cycles": c.get("test_cycles"),
                "test_wins": c.get("test_wins"),
                "step": (c.get("params") or {}).get("step"),
                "tp": (c.get("params") or {}).get("tp"),
            }
            for c in data.get("coins", [])
        ],
    }


def _scan_job(ctx: ApiContext) -> dict[str, Any]:
    """Ход поиска монет — если последней запускалась именно она."""
    st = ctx.jobs.status(lines=12)
    if st["name"] != "wick-scan":
        return {**st, "state": "idle", "log": []}
    return st


@read.get("/wick/scan")
async def wick_scan_status(ctx: Ctx) -> dict[str, Any]:
    """Поиск монет: ход последнего запуска и результат отбора."""
    async with ctx.sm() as db:
        beat = next((h for h in await control.get_heartbeats(db) if h.component == "wick"), None)
    return {
        "job": _scan_job(ctx),
        "result": _scan_result(ctx),
        "trading": list((beat.details.get("coins") or {}).keys()) if beat else [],
        "max_coins": ctx.strategy.wick.max_coins,
    }


@read.get("/status")
async def status(ctx: Ctx) -> dict[str, Any]:
    now = ctx.clock()
    async with ctx.sm() as db:
        state = await control.get_bot_state(db)
        heartbeats = await control.get_heartbeats(db)
    return {
        "network": ctx.settings.network.value,
        "mode": state.trading_mode if state else TradingMode.DISABLED.value,
        "entries_blocked": state.entries_blocked if state else False,
        "block_reason": state.block_reason if state else None,
        "mode_updated_at": _t(state.updated_at) if state else None,
        "mode_updated_by": state.updated_by if state else None,
        "components": [
            {
                "component": hb.component,
                "last_beat_age_s": round((now - hb.last_beat_at).total_seconds(), 1),
                "details": hb.details,
            }
            for hb in heartbeats
        ],
    }


@read.get("/audit")
async def audit(ctx: Ctx, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict[str, Any]]:
    async with ctx.sm() as db:
        stmt = select(AuditLogRecord).order_by(AuditLogRecord.id.desc()).limit(limit)
        records = list((await db.execute(stmt)).scalars())
    return [
        {
            "id": a.id,
            "occurred_at": _t(a.occurred_at),
            "actor": a.actor,
            "action": a.action,
            "reason": a.reason,
            "details": a.details,
        }
        for a in records
    ]


# --- управление ---

control_router = APIRouter(prefix="/api")


async def _idempotent(
    ctx: ApiContext,
    key: str | None,
    command: str,
    actor: Actor,
    action: Callable[[], Awaitable[dict[str, Any]]],
) -> JSONResponse:
    if not key or len(key) > 200:
        raise HTTPException(status_code=400, detail="нужен заголовок Idempotency-Key")
    async with ctx.sm.begin() as db:
        existing = await db.get(ApiRequestRecord, key)
        if existing is not None:
            if existing.command != command:
                raise HTTPException(
                    status_code=422, detail="ключ уже использован для другой команды"
                )
            if existing.response is None:
                raise HTTPException(status_code=409, detail="команда с этим ключом ещё выполняется")
            return JSONResponse(
                existing.response,
                status_code=existing.status_code or 200,
                headers={"Idempotent-Replay": "true"},
            )
        db.add(ApiRequestRecord(idempotency_key=key, command=command, actor=str(actor)))
    try:
        status_code, body = 200, await action()
    except HTTPException as exc:
        status_code, body = exc.status_code, {"detail": exc.detail}
    except AnotherTraderRunningError as exc:
        status_code, body = 409, {"detail": str(exc)}
    except (TransitionError, PermissionError, ValueError) as exc:
        status_code, body = 422, {"detail": str(exc)}
    async with ctx.sm.begin() as db:
        record = await db.get(ApiRequestRecord, key)
        if record is not None:
            record.status_code = status_code
            record.response = body
            record.finished_at = ctx.clock()
    return JSONResponse(body, status_code=status_code)


IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


@control_router.post("/wick/scan")
async def wick_scan_start(body: ScanBody, ctx: Ctx, actor: Operator) -> dict[str, Any]:
    """Запустить поиск новых монет (eratrade wick-scan --top N). Бот подхватит отбор сам."""
    return await _start_job(ctx, actor, "wick-scan", {"top": str(body.top)})


class JobBody(BaseModel):
    args: dict[str, str] = Field(default_factory=dict)


@read.get("/jobs")
async def jobs(ctx: Ctx) -> dict[str, Any]:
    """Команды CLI, доступные из панели, и ход последней."""
    return {"commands": catalog(), "job": ctx.jobs.status()}


@control_router.post("/jobs/{name}")
async def job_start(name: str, body: JobBody, ctx: Ctx, actor: Operator) -> dict[str, Any]:
    return await _start_job(ctx, actor, name, body.args)


async def _start_job(
    ctx: ApiContext, actor: Actor, name: str, args: dict[str, str]
) -> dict[str, Any]:
    if name not in COMMANDS:
        raise HTTPException(status_code=404, detail=f"нет команды {name}")
    try:
        ctx.jobs.start(name, args)
    except JobError as exc:
        status = 409 if ctx.jobs.running else 422
        raise HTTPException(status_code=status, detail=str(exc)) from None
    async with ctx.sm.begin() as db:
        await control.record_audit(
            db, actor=actor, action="panel.command", reason=name, details=ctx.jobs.args
        )
    return {"steps": [f"запущено: {COMMANDS[name].label}"], "job": ctx.jobs.status()}


@control_router.post("/bot/pause")
async def bot_pause(
    body: CommandBody, ctx: Ctx, actor: Operator, key: IdemKey = None
) -> JSONResponse:
    async def run() -> dict[str, Any]:
        async with ctx.sm.begin() as db:
            steps = await control.pause(db, ctx.settings.network, actor=actor, reason=body.reason)
        await ctx.notifier.send(f"PAUSE ({actor}): {body.reason}")
        return {"steps": steps}

    return await _idempotent(ctx, key, "pause", actor, run)


@control_router.post("/bot/resume")
async def bot_resume(
    body: CommandBody, ctx: Ctx, actor: Operator, key: IdemKey = None
) -> JSONResponse:
    async def run() -> dict[str, Any]:
        async with ctx.sm.begin() as db:
            changed = await control.unblock_entries(
                db, ctx.settings.network, actor=actor, reason=body.reason
            )
        return {"steps": ["запрет входов снят" if changed else "запрета не было"]}

    return await _idempotent(ctx, key, "resume", actor, run)


class StartBody(CommandBody):
    confirmation_phrase: str


@control_router.post("/bot/start")
async def bot_start(
    body: StartBody, ctx: Ctx, actor: Operator, key: IdemKey = None
) -> JSONResponse:
    """Запустить торговлю: снять запрет входов и включить LIVE_ENABLED (нужна фраза mainnet)."""

    async def run() -> dict[str, Any]:
        if body.confirmation_phrase != MAINNET_CONFIRMATION_PHRASE:
            raise HTTPException(
                status_code=422, detail=f"введите фразу {MAINNET_CONFIRMATION_PHRASE}"
            )
        steps = []
        async with ctx.sm.begin() as db:
            if await control.unblock_entries(
                db, ctx.settings.network, actor=actor, reason=body.reason
            ):
                steps.append("запрет входов снят")
            if await control.set_trading_mode(
                db, ctx.settings.network, TradingMode.LIVE_ENABLED, actor=actor, reason=body.reason
            ):
                steps.append("режим LIVE_ENABLED")
        await ctx.notifier.send(f"START ({actor}): {body.reason}")
        return {"steps": steps or ["уже запущен"]}

    return await _idempotent(ctx, key, "start", actor, run)


@control_router.post("/bot/stop")
async def bot_stop(
    body: CommandBody, ctx: Ctx, actor: Operator, key: IdemKey = None
) -> JSONResponse:
    """Выключить: режим DISABLED. Маркет-мейкер снимает заявки и закрывает позиции своих монет."""

    async def run() -> dict[str, Any]:
        async with ctx.sm.begin() as db:
            changed = await control.set_trading_mode(
                db, ctx.settings.network, TradingMode.DISABLED, actor=actor, reason=body.reason
            )
        await ctx.notifier.send(f"STOP ({actor}): {body.reason}")
        return {
            "steps": ["режим DISABLED: заявки снимаются, позиции закрываются"]
            if changed
            else ["уже выключен"]
        }

    return await _idempotent(ctx, key, "stop", actor, run)


@control_router.post("/bot/mode")
async def bot_mode(body: ModeBody, ctx: Ctx, actor: Operator, key: IdemKey = None) -> JSONResponse:
    async def run() -> dict[str, Any]:
        if (
            body.mode is TradingMode.LIVE_ENABLED
            and body.confirmation_phrase != MAINNET_CONFIRMATION_PHRASE
        ):
            raise HTTPException(
                status_code=422,
                detail=f"для LIVE_ENABLED нужна фраза {MAINNET_CONFIRMATION_PHRASE}",
            )
        async with ctx.sm.begin() as db:
            changed = await control.set_trading_mode(
                db, ctx.settings.network, body.mode, actor=actor, reason=body.reason
            )
        return {"steps": [f"режим {body.mode.value}" if changed else "режим не изменился"]}

    return await _idempotent(ctx, key, f"mode:{body.mode.value}", actor, run)


def create_api(
    context_factory: Callable[[], AbstractAsyncContextManager[ApiContext]],
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with context_factory() as ctx:
            app.state.ctx = ctx
            yield

    app = FastAPI(
        title="EraTrade", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json"
    )
    app.include_router(public)
    app.include_router(read)
    app.include_router(control_router)
    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


def new_idempotency_key() -> str:
    return uuid.uuid4().hex
