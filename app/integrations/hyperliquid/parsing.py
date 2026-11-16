"""Преобразование JSON-ответов Hyperliquid в доменные типы.

Строгий режим: отсутствующее поле, неверный тип или нечисловая строка -> HyperliquidFormatError.
Числа приходят строками и переводятся в Decimal без потери точности.
"""

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.config.settings import Network
from app.domain.account import (
    AccountState,
    AgentInfo,
    Fill,
    OpenOrder,
    OrderStatusResult,
    Position,
    RateLimitStatus,
    Side,
    UserRole,
)
from app.domain.market import AssetContext, Candle, DataSource, MarketMeta
from app.integrations.hyperliquid.errors import HyperliquidFormatError

ISOLATED_ONLY_MARGIN_MODES = frozenset({"strictIsolated", "noCross"})


# --- примитивы ---


def _obj(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HyperliquidFormatError(f"{what}: ожидался объект, получено {type(value).__name__}")
    return value


def _list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise HyperliquidFormatError(f"{what}: ожидался список, получено {type(value).__name__}")
    return value


def _get(obj: Mapping[str, Any], key: str, what: str) -> Any:
    if key not in obj:
        raise HyperliquidFormatError(f"{what}: нет поля '{key}'")
    return obj[key]


def _str(obj: Mapping[str, Any], key: str, what: str) -> str:
    value = _get(obj, key, what)
    if not isinstance(value, str):
        raise HyperliquidFormatError(f"{what}.{key}: ожидалась строка")
    return value


def _int(obj: Mapping[str, Any], key: str, what: str) -> int:
    value = _get(obj, key, what)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HyperliquidFormatError(f"{what}.{key}: ожидалось целое")
    return value


def _bool(obj: Mapping[str, Any], key: str, what: str, default: bool | None = None) -> bool:
    if key not in obj and default is not None:
        return default
    value = _get(obj, key, what)
    if not isinstance(value, bool):
        raise HyperliquidFormatError(f"{what}.{key}: ожидалось bool")
    return value


def _to_decimal(value: Any, what: str) -> Decimal:
    if not isinstance(value, str | int) or isinstance(value, bool):
        raise HyperliquidFormatError(f"{what}: ожидалось число строкой, получено {value!r}")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise HyperliquidFormatError(f"{what}: не число: {value!r}") from exc
    if not result.is_finite():
        raise HyperliquidFormatError(f"{what}: не конечное число: {value!r}")
    return result


def _dec(obj: Mapping[str, Any], key: str, what: str) -> Decimal:
    return _to_decimal(_get(obj, key, what), f"{what}.{key}")


def _opt_dec(obj: Mapping[str, Any], key: str, what: str) -> Decimal | None:
    value = obj.get(key)
    return None if value is None else _to_decimal(value, f"{what}.{key}")


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def datetime_to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("ожидалось timezone-aware время")
    return int(value.timestamp() * 1000)


def _time(obj: Mapping[str, Any], key: str, what: str) -> datetime:
    return ms_to_datetime(_int(obj, key, what))


def _side(obj: Mapping[str, Any], what: str) -> Side:
    raw = _str(obj, "side", what)
    if raw == "B":
        return Side.BUY
    if raw == "A":
        return Side.SELL
    raise HyperliquidFormatError(f"{what}.side: неизвестное значение {raw!r}")


def _opt_cloid(obj: Mapping[str, Any], what: str) -> str | None:
    value = obj.get("cloid")
    if value is not None and not isinstance(value, str):
        raise HyperliquidFormatError(f"{what}.cloid: ожидалась строка")
    return value


# --- метаданные ---


def parse_meta(data: Any, network: Network, updated_at: datetime) -> list[MarketMeta]:
    """meta → список perp-рынков основного DEX. asset_id = индекс в universe."""
    universe = _list(_get(_obj(data, "meta"), "universe", "meta"), "meta.universe")
    markets = []
    for index, item in enumerate(universe):
        what = f"meta.universe[{index}]"
        entry = _obj(item, what)
        margin_mode = entry.get("marginMode")
        markets.append(
            MarketMeta(
                network=network,
                coin=_str(entry, "name", what),
                asset_id=index,
                sz_decimals=_int(entry, "szDecimals", what),
                max_leverage=_int(entry, "maxLeverage", what),
                only_isolated=_bool(entry, "onlyIsolated", what, default=False)
                or margin_mode in ISOLATED_ONLY_MARGIN_MODES,
                is_delisted=_bool(entry, "isDelisted", what, default=False),
                margin_table_id=entry.get("marginTableId"),
                updated_at=updated_at,
                raw=dict(entry),
            )
        )
    return markets


def parse_meta_and_contexts(
    data: Any, network: Network, received_at: datetime
) -> tuple[list[MarketMeta], dict[str, AssetContext]]:
    pair = _list(data, "metaAndAssetCtxs")
    if len(pair) != 2:
        raise HyperliquidFormatError("metaAndAssetCtxs: ожидалось [meta, contexts]")
    markets = parse_meta(pair[0], network, received_at)
    contexts = _list(pair[1], "metaAndAssetCtxs.contexts")
    if len(contexts) != len(markets):
        raise HyperliquidFormatError("metaAndAssetCtxs: число контекстов не совпадает с universe")
    result = {
        market.coin: parse_asset_context(ctx, received_at, DataSource.REST, what=market.coin)
        for market, ctx in zip(markets, contexts, strict=True)
    }
    return markets, result


def parse_asset_context(
    data: Any, received_at: datetime, source: DataSource, *, what: str = "ctx"
) -> AssetContext:
    ctx = _obj(data, what)
    impact = ctx.get("impactPxs")
    impact_bid = impact_ask = None
    if impact is not None:
        impact_list = _list(impact, f"{what}.impactPxs")
        if len(impact_list) != 2:
            raise HyperliquidFormatError(f"{what}.impactPxs: ожидалось [bid, ask]")
        impact_bid = _to_decimal(impact_list[0], f"{what}.impactPxs[0]")
        impact_ask = _to_decimal(impact_list[1], f"{what}.impactPxs[1]")
    return AssetContext(
        mark_price=_dec(ctx, "markPx", what),
        oracle_price=_dec(ctx, "oraclePx", what),
        funding=_dec(ctx, "funding", what),
        open_interest=_dec(ctx, "openInterest", what),
        mid_price=_opt_dec(ctx, "midPx", what),
        premium=_opt_dec(ctx, "premium", what),
        day_notional_volume=_opt_dec(ctx, "dayNtlVlm", what),
        impact_bid_price=impact_bid,
        impact_ask_price=impact_ask,
        received_at=received_at,
        source=source,
        raw=dict(ctx),
    )


def parse_all_mids(data: Any) -> dict[str, Decimal]:
    mids = _obj(data, "allMids")
    return {coin: _to_decimal(px, f"allMids.{coin}") for coin, px in mids.items()}


# --- свечи ---


def parse_candle(
    data: Any,
    *,
    received_at: datetime,
    source: DataSource,
    is_closed: bool,
) -> Candle:
    c = _obj(data, "candle")
    return Candle(
        interval=_str(c, "i", "candle"),
        open_time=_time(c, "t", "candle"),
        # T — последняя миллисекунда свечи включительно; храним как конец интервала.
        close_time=_time(c, "T", "candle") + timedelta(milliseconds=1),
        open=_dec(c, "o", "candle"),
        high=_dec(c, "h", "candle"),
        low=_dec(c, "l", "candle"),
        close=_dec(c, "c", "candle"),
        volume=_dec(c, "v", "candle"),
        trades_count=_int(c, "n", "candle"),
        is_closed=is_closed,
        source=source,
        received_at=received_at,
        raw=dict(c),
    )


def parse_candle_snapshot(
    data: Any,
    *,
    coin: str,
    interval: str,
    received_at: datetime,
    close_grace: timedelta,
) -> list[Candle]:
    """Свеча считается закрытой, если после неё в ответе есть следующая свеча,
    или если с момента её окончания прошло не меньше close_grace (защита от сдвига часов)."""
    if data is None:
        raise HyperliquidFormatError(f"candleSnapshot {coin}: null (неизвестный инструмент?)")
    items = sorted(
        _list(data, "candleSnapshot"), key=lambda x: _int(_obj(x, "candle"), "t", "candle")
    )
    candles = []
    for i, item in enumerate(items):
        entry = _obj(item, "candle")
        if _str(entry, "s", "candle") != coin or _str(entry, "i", "candle") != interval:
            raise HyperliquidFormatError(
                f"candleSnapshot: чужая свеча {entry.get('s')} {entry.get('i')}"
            )
        end = _time(entry, "T", "candle") + timedelta(milliseconds=1)
        is_closed = i < len(items) - 1 or received_at >= end + close_grace
        candles.append(
            parse_candle(
                entry, received_at=received_at, source=DataSource.REST, is_closed=is_closed
            )
        )
    return candles


# --- аккаунт ---


def parse_position(data: Any) -> Position:
    wrapper = _obj(data, "assetPosition")
    p = _obj(_get(wrapper, "position", "assetPosition"), "position")
    leverage = _obj(_get(p, "leverage", "position"), "position.leverage")
    cum_funding = _obj(_get(p, "cumFunding", "position"), "position.cumFunding")
    return Position(
        coin=_str(p, "coin", "position"),
        size=_dec(p, "szi", "position"),
        entry_price=_opt_dec(p, "entryPx", "position"),
        position_value=_dec(p, "positionValue", "position"),
        unrealized_pnl=_dec(p, "unrealizedPnl", "position"),
        liquidation_price=_opt_dec(p, "liquidationPx", "position"),
        margin_used=_dec(p, "marginUsed", "position"),
        leverage_type=_str(leverage, "type", "position.leverage"),
        leverage=_int(leverage, "value", "position.leverage"),
        max_leverage=_int(p, "maxLeverage", "position"),
        funding_since_open=_dec(cum_funding, "sinceOpen", "position.cumFunding"),
        raw=dict(p),
    )


def parse_clearinghouse_state(data: Any) -> AccountState:
    s = _obj(data, "clearinghouseState")
    summary = _obj(_get(s, "marginSummary", "clearinghouseState"), "marginSummary")
    positions = _list(_get(s, "assetPositions", "clearinghouseState"), "assetPositions")
    return AccountState(
        account_value=_dec(summary, "accountValue", "marginSummary"),
        total_notional=_dec(summary, "totalNtlPos", "marginSummary"),
        total_margin_used=_dec(summary, "totalMarginUsed", "marginSummary"),
        maintenance_margin_used=_dec(s, "crossMaintenanceMarginUsed", "clearinghouseState"),
        withdrawable=_dec(s, "withdrawable", "clearinghouseState"),
        positions=tuple(parse_position(p) for p in positions),
        exchange_time=_time(s, "time", "clearinghouseState"),
        raw=dict(s),
    )


def parse_open_order(data: Any, *, what: str = "order") -> OpenOrder:
    """Формат frontendOpenOrders (и вложенного order в orderStatus)."""
    o = _obj(data, what)
    is_trigger = _bool(o, "isTrigger", what)
    return OpenOrder(
        coin=_str(o, "coin", what),
        side=_side(o, what),
        limit_price=_dec(o, "limitPx", what),
        size=_dec(o, "sz", what),
        original_size=_dec(o, "origSz", what),
        oid=_int(o, "oid", what),
        cloid=_opt_cloid(o, what),
        created_at=_time(o, "timestamp", what),
        order_type=_str(o, "orderType", what),
        tif=o.get("tif"),
        reduce_only=_bool(o, "reduceOnly", what),
        is_trigger=is_trigger,
        trigger_price=_dec(o, "triggerPx", what) if is_trigger else None,
        trigger_condition=_str(o, "triggerCondition", what),
        is_position_tpsl=_bool(o, "isPositionTpsl", what),
        raw=dict(o),
    )


def parse_open_orders(data: Any) -> list[OpenOrder]:
    return [parse_open_order(o) for o in _list(data, "frontendOpenOrders")]


def parse_fill(data: Any) -> Fill:
    f = _obj(data, "fill")
    return Fill(
        coin=_str(f, "coin", "fill"),
        side=_side(f, "fill"),
        price=_dec(f, "px", "fill"),
        size=_dec(f, "sz", "fill"),
        time=_time(f, "time", "fill"),
        start_position=_dec(f, "startPosition", "fill"),
        direction=_str(f, "dir", "fill"),
        closed_pnl=_dec(f, "closedPnl", "fill"),
        fee=_dec(f, "fee", "fill"),
        fee_token=_str(f, "feeToken", "fill"),
        oid=_int(f, "oid", "fill"),
        tid=_int(f, "tid", "fill"),
        hash=_str(f, "hash", "fill"),
        crossed=_bool(f, "crossed", "fill"),
        cloid=_opt_cloid(f, "fill"),
        raw=dict(f),
    )


def parse_fills(data: Any) -> list[Fill]:
    return [parse_fill(f) for f in _list(data, "userFills")]


def parse_order_status(data: Any) -> OrderStatusResult:
    s = _obj(data, "orderStatus")
    status = _str(s, "status", "orderStatus")
    if status == "unknownOid":
        return OrderStatusResult(found=False)
    if status != "order":
        raise HyperliquidFormatError(f"orderStatus: неизвестный status {status!r}")
    wrapper = _obj(_get(s, "order", "orderStatus"), "orderStatus.order")
    return OrderStatusResult(
        found=True,
        status=_str(wrapper, "status", "orderStatus.order"),
        order=parse_open_order(_get(wrapper, "order", "orderStatus.order")),
        status_time=_time(wrapper, "statusTimestamp", "orderStatus.order"),
    )


def parse_user_rate_limit(data: Any) -> RateLimitStatus:
    r = _obj(data, "userRateLimit")
    return RateLimitStatus(
        cumulative_volume=_dec(r, "cumVlm", "userRateLimit"),
        requests_used=_int(r, "nRequestsUsed", "userRateLimit"),
        requests_cap=_int(r, "nRequestsCap", "userRateLimit"),
        requests_surplus=_int(r, "nRequestsSurplus", "userRateLimit")
        if "nRequestsSurplus" in r
        else 0,
    )


# --- WebSocket ---


def parse_ws_message(raw: str | bytes) -> tuple[str, Any]:
    try:
        message = json.loads(raw)
    except ValueError as exc:
        raise HyperliquidFormatError(f"ws: сообщение не JSON: {str(raw)[:200]!r}") from exc
    obj = _obj(message, "ws")
    return _str(obj, "channel", "ws"), obj.get("data")


def parse_subscription_ack(data: Any) -> str:
    """Тип подписки из subscriptionResponse."""
    ack = _obj(data, "subscriptionResponse")
    subscription = _obj(_get(ack, "subscription", "subscriptionResponse"), "subscription")
    return _str(subscription, "type", "subscriptionResponse.subscription")


def parse_ws_candle(data: Any, *, coin: str, interval: str, received_at: datetime) -> Candle:
    """Свеча из WebSocket всегда сохраняется как незакрытая: финальные значения берутся из REST."""
    entry = _obj(data, "ws.candle")
    if _str(entry, "s", "ws.candle") != coin or _str(entry, "i", "ws.candle") != interval:
        raise HyperliquidFormatError(f"ws.candle: чужая свеча {entry.get('s')} {entry.get('i')}")
    return parse_candle(entry, received_at=received_at, source=DataSource.WS, is_closed=False)


def parse_ws_asset_context(data: Any, *, coin: str, received_at: datetime) -> AssetContext:
    entry = _obj(data, "ws.activeAssetCtx")
    if _str(entry, "coin", "ws.activeAssetCtx") != coin:
        raise HyperliquidFormatError(f"ws.activeAssetCtx: чужой инструмент {entry.get('coin')}")
    return parse_asset_context(
        _get(entry, "ctx", "ws.activeAssetCtx"), received_at, DataSource.WS, what="ws.ctx"
    )


# --- роли и API-кошельки ---


def _address(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        raise HyperliquidFormatError(f"{what}: ожидался адрес, получено {value!r}")
    return value.lower()


def parse_user_role(data: Any) -> UserRole:
    r = _obj(data, "userRole")
    role = _str(r, "role", "userRole")
    details = r.get("data")
    if role == "agent":
        d = _obj(details, "userRole.data")
        return UserRole(role=role, agent_owner=_address(_get(d, "user", "userRole.data"), "user"))
    if role == "subAccount":
        d = _obj(details, "userRole.data")
        return UserRole(role=role, master=_address(_get(d, "master", "userRole.data"), "master"))
    return UserRole(role=role)


def parse_extra_agents(data: Any) -> list[AgentInfo]:
    agents = []
    for i, item in enumerate(_list(data, "extraAgents")):
        a = _obj(item, f"extraAgents[{i}]")
        valid_until = a.get("validUntil")
        agents.append(
            AgentInfo(
                address=_address(_get(a, "address", "extraAgents"), "extraAgents.address"),
                name=_str(a, "name", "extraAgents"),
                valid_until=ms_to_datetime(valid_until) if isinstance(valid_until, int) else None,
            )
        )
    return agents
