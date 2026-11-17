"""FastAPI: мониторинг и управление ботом (§13 плана). Только 127.0.0.1, токен оператора.

Деньги и размеры отдаются строками (Decimal без потери точности).
"""

import hmac
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from app.api.context import ApiContext
from app.config.settings import MAINNET_CONFIRMATION_PHRASE, TradingMode
from app.db.models import ApiRequestRecord, AuditLogRecord, FillRecord
from app.db.repositories import control
from app.db.repositories import market as market_repo
from app.db.repositories import models as models_repo
from app.db.repositories import reconciliation as rec_repo
from app.db.repositories import trading as trading_repo
from app.domain.control import Actor, ActorKind, TransitionError
from app.execution import emergency
from app.execution.engine import OrderPreview
from app.execution.lock import AnotherTraderRunningError
from app.integrations.hyperliquid.gateway import INTERVALS
from app.market_data.collector import COMPONENT as COLLECTOR

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
    confirm: str | None = None  # для опасных команд — имя команды (CANCEL_ALL, FLATTEN, KILL)


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


@read.get("/status")
async def status(ctx: Ctx) -> dict[str, Any]:
    now = ctx.clock()
    step = INTERVALS[ctx.strategy.market.interval]
    async with ctx.sm() as db:
        state = await control.get_bot_state(db)
        heartbeats = await control.get_heartbeats(db)
        _, _, last_closed = await market_repo.candle_stats(
            db, ctx.market_id, ctx.strategy.market.interval
        )
        context = await market_repo.get_latest_context(db, ctx.market_id)
        run = await rec_repo.latest_run(db, ctx.market_id, finished_only=True)
        differences = await rec_repo.list_differences(db, run.id) if run else []
    return {
        "network": ctx.settings.network.value,
        "coin": ctx.coin,
        "interval": ctx.strategy.market.interval,
        "mode": state.trading_mode if state else TradingMode.DISABLED.value,
        "entries_blocked": state.entries_blocked if state else False,
        "block_reason": state.block_reason if state else None,
        "mode_updated_at": _t(state.updated_at) if state else None,
        "mode_updated_by": state.updated_by if state else None,
        "trading_commands_available": ctx.engine_factory is not None,
        "components": [
            {
                "component": hb.component,
                "last_beat_age_s": round((now - hb.last_beat_at).total_seconds(), 1),
                "details": hb.details,
            }
            for hb in heartbeats
        ],
        "data": {
            "last_closed_candle": _t(last_closed),
            "last_closed_age_min": (
                round((now - (last_closed + step)).total_seconds() / 60, 1) if last_closed else None
            ),
            "context_age_s": (
                round((now - context.received_at).total_seconds(), 1) if context else None
            ),
            "funding": _s(context.funding) if context else None,
            "open_interest": _s(context.open_interest) if context else None,
            "mark_price": _s(context.mark_price) if context else None,
        },
        "reconciliation": (
            {
                "run_id": run.id,
                "status": run.status,
                "trigger": run.trigger,
                "finished_at": _t(run.finished_at),
                "summary": run.summary,
                "differences": [
                    {
                        "kind": d.kind,
                        "severity": d.severity,
                        "message": d.message,
                        "repaired": d.repaired,
                    }
                    for d in differences
                ],
            }
            if run
            else None
        ),
        "collector_component": COLLECTOR,
    }


async def _live(ctx: ApiContext) -> dict[str, Any]:
    async def load() -> dict[str, Any]:
        state = await ctx.gateway.get_account_state(ctx.user)
        orders = [o for o in await ctx.gateway.get_open_orders(ctx.user) if o.coin == ctx.coin]
        snap = await ctx.risk.snapshot()
        limits = ctx.strategy.limits
        position = state.position(ctx.coin)
        return {
            "exchange_time": _t(state.exchange_time),
            "position": (
                {
                    "coin": position.coin,
                    "size": _s(position.size),
                    "entry_price": _s(position.entry_price),
                    "unrealized_pnl": _s(position.unrealized_pnl),
                    "liquidation_price": _s(position.liquidation_price),
                    "margin_used": _s(position.margin_used),
                    "leverage": f"{position.leverage_type} {position.leverage}x",
                }
                if position
                else None
            ),
            "open_orders": [
                {
                    "oid": o.oid,
                    "cloid": o.cloid,
                    "side": o.side.value,
                    "size": _s(o.size),
                    "limit_price": _s(o.limit_price),
                    "trigger_price": _s(o.trigger_price),
                    "order_type": o.order_type,
                    "reduce_only": o.reduce_only,
                }
                for o in orders
            ],
            "risk": {
                "equity": _s(snap.equity),
                "spot_usdc": _s(snap.spot_usdc),
                "perp_account_value": _s(snap.perp_account_value),
                "unrealized_pnl": _s(snap.unrealized_pnl),
                "daily_pnl": _s(snap.daily_pnl),
                "daily_limit": _s(snap.daily_base * limits.max_daily_loss_fraction),
                "weekly_pnl": _s(snap.weekly_pnl),
                "weekly_limit": _s(snap.weekly_base * limits.max_weekly_loss_fraction),
                "realized_today": _s(snap.realized_today),
                "fees_today": _s(snap.fees_today),
                "funding_today": _s(snap.funding_today),
                "trades_today": snap.trades_today,
                "max_trades_per_day": limits.max_trades_per_day,
                "blocks": ctx.risk.entry_blocks(snap),
            },
        }

    result: dict[str, Any] = await ctx.cache.get("live", load)
    return result


@read.get("/account")
async def account(ctx: Ctx) -> dict[str, Any]:
    return await _live(ctx)


@read.get("/positions")
async def positions(ctx: Ctx) -> dict[str, Any]:
    live = await _live(ctx)
    return {"position": live["position"], "open_orders": live["open_orders"]}


@read.get("/risk")
async def risk(ctx: Ctx) -> dict[str, Any]:
    return (await _live(ctx))["risk"]  # type: ignore[no-any-return]


@read.get("/orders")
async def orders(ctx: Ctx, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict[str, Any]]:
    async with ctx.sm() as db:
        records = await trading_repo.list_orders(db, market_id=ctx.market_id, limit=limit)
    return [
        {
            "id": str(o.id),
            "cloid": o.cloid,
            "oid": o.oid,
            "purpose": o.purpose,
            "kind": o.order_kind,
            "side": o.side,
            "size": _s(o.size),
            "price": _s(o.price),
            "trigger_price": _s(o.trigger_price),
            "status": o.status,
            "filled_size": _s(o.filled_size),
            "avg_fill_price": _s(o.avg_fill_price),
            "error": o.error,
            "created_at": _t(o.created_at),
        }
        for o in records
    ]


@read.get("/fills")
async def fills(ctx: Ctx, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict[str, Any]]:
    async with ctx.sm() as db:
        stmt = (
            select(FillRecord)
            .where(FillRecord.market_id == ctx.market_id)
            .order_by(FillRecord.time.desc())
            .limit(limit)
        )
        records = list((await db.execute(stmt)).scalars())
    return [
        {
            "tid": f.tid,
            "time": _t(f.time),
            "side": f.side,
            "size": _s(f.size),
            "price": _s(f.price),
            "fee": _s(f.fee),
            "closed_pnl": _s(f.closed_pnl),
            "direction": f.direction,
            "bot_order": f.order_id is not None,
        }
        for f in records
    ]


@read.get("/reconciliation")
async def reconciliation(ctx: Ctx) -> dict[str, Any]:
    return {"latest": (await status(ctx))["reconciliation"]}


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


@read.get("/signals")
async def signals(ctx: Ctx, limit: Annotated[int, Query(ge=1, le=500)] = 50) -> dict[str, Any]:
    async with ctx.sm() as db:
        records = await models_repo.list_signals(db, limit=limit)
    return {
        "items": [
            {
                "id": r.id,
                "decision_time": _t(r.decision_time),
                "model_version": r.model_version,
                "model_role": r.model_role,
                "policy_version": r.policy_version,
                "decision": r.decision,
                "reason": r.reason,
                "probabilities": r.probabilities,
                "expected_return_bps": r.expected_return_bps,
                "position_side": r.position_side,
                "outcome_return": r.outcome_return,
                "outcome_label": r.outcome_label,
            }
            for r in records
        ]
    }


@read.get("/shadow/report")
async def shadow_report_endpoint(
    ctx: Ctx, days: Annotated[float, Query(gt=0, le=90)] = 7
) -> dict[str, Any]:
    from datetime import timedelta

    from app.signals.report import shadow_report

    now = ctx.clock()
    async with ctx.sm() as db:
        return await shadow_report(
            db,
            ctx.market_id,
            since=now - timedelta(days=days),
            now=now,
            step=INTERVALS[ctx.strategy.market.interval],
            cost_bps=float(ctx.strategy.target.cost_bps),
        )


def _test_metrics(artifact: dict[str, Any]) -> dict[str, Any]:
    test = artifact.get("metrics", {}).get("test", {})
    return {
        "log_loss": test.get("log_loss"),
        "baseline_log_loss": test.get("baseline_log_loss"),
        "brier": test.get("brier"),
        "ece": test.get("ece"),
        "policy": test.get("policy"),
    }


@read.get("/models")
async def models(ctx: Ctx) -> dict[str, Any]:
    async with ctx.sm() as db:
        records = await models_repo.list_models(db)
    return {
        "items": [
            {
                "version": r.version,
                "status": r.status,
                "feature_set_version": r.feature_set_version,
                "created_at": _t(r.created_at),
                "shadow_at": _t(r.shadow_at),
                "activated_at": _t(r.activated_at),
                "retired_at": _t(r.retired_at),
                "training_period": r.artifact.get("training_period"),
                "test": _test_metrics(r.artifact),
            }
            for r in records
        ]
    }


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
    ctx.cache.clear()
    return JSONResponse(body, status_code=status_code)


def _require_confirm(body: CommandBody, command: str) -> None:
    if body.confirm != command:
        raise HTTPException(status_code=422, detail=f"подтвердите команду: confirm = {command}")


def _engine(ctx: ApiContext) -> Callable[[], AbstractAsyncContextManager[Any]]:
    if ctx.engine_factory is None:
        raise HTTPException(status_code=503, detail="ключ API-кошелька не подключён к API")
    return ctx.engine_factory


async def _yes(preview: OrderPreview) -> bool:
    return True  # подтверждение — сам запрос оператора с confirm=<команда>


IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


@control_router.post("/bot/pause")
async def bot_pause(
    body: CommandBody, ctx: Ctx, actor: Operator, key: IdemKey = None
) -> JSONResponse:
    async def run() -> dict[str, Any]:
        steps = await emergency.pause(
            ctx.sm, ctx.settings.network, actor=actor, reason=body.reason, notifier=ctx.notifier
        )
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


def _emergency_body(result: emergency.EmergencyResult) -> dict[str, Any]:
    return {"steps": result.steps, "warnings": result.warnings}


@control_router.post("/bot/cancel-all")
async def bot_cancel_all(
    body: CommandBody, ctx: Ctx, actor: Operator, key: IdemKey = None
) -> JSONResponse:
    async def run() -> dict[str, Any]:
        _require_confirm(body, "CANCEL_ALL")
        async with _engine(ctx)() as engine:
            return _emergency_body(
                await emergency.cancel_all(engine, actor=actor, reason=body.reason)
            )

    return await _idempotent(ctx, key, "cancel-all", actor, run)


@control_router.post("/bot/flatten")
async def bot_flatten(
    body: CommandBody, ctx: Ctx, actor: Operator, key: IdemKey = None
) -> JSONResponse:
    async def run() -> dict[str, Any]:
        _require_confirm(body, "FLATTEN")
        async with _engine(ctx)() as engine:
            return _emergency_body(await emergency.flatten(engine, confirm=_yes))

    return await _idempotent(ctx, key, "flatten", actor, run)


@control_router.post("/bot/kill-switch")
async def bot_kill(
    body: CommandBody, ctx: Ctx, actor: Operator, key: IdemKey = None
) -> JSONResponse:
    async def run() -> dict[str, Any]:
        _require_confirm(body, "KILL")
        async with _engine(ctx)() as engine:
            return _emergency_body(
                await emergency.full_kill(engine, actor=actor, reason=body.reason, confirm=_yes)
            )

    return await _idempotent(ctx, key, "kill-switch", actor, run)


@control_router.post("/models/{version}/activate")
async def activate_model(
    version: str, body: CommandBody, ctx: Ctx, actor: Operator, key: IdemKey = None
) -> JSONResponse:
    async def run() -> dict[str, Any]:
        _require_confirm(body, "ACTIVATE")
        async with ctx.sm.begin() as db:
            record = await models_repo.activate(db, version, actor=actor, reason=body.reason)
        return {"steps": [f"{record.version}: {record.status}"]}

    return await _idempotent(ctx, key, f"model-activate:{version}", actor, run)


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
