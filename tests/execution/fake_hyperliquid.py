"""Симулятор Hyperliquid для тестов исполнения: /info и /exchange в форматах биржи.

Проверяет подпись каждого действия, исполняет IOC против лучшей цены с ограниченной
ликвидностью, держит trigger-ордера, считает позицию и fills. Умеет имитировать сбои:
connect (запрос не ушёл), timeout_before (потерян до обработки),
timeout_after (обработан, ответ потерян), crash_after (обработан, процесс «упал»).
"""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action

FIXTURES = Path(__file__).parents[1] / "hyperliquid" / "fixtures"
MISSING_ORDER = "Order was never placed, already canceled, or filled."


class SimulatedCrashError(Exception):
    """Процесс «упал» после отправки запроса."""


class FakeHyperliquid:
    def __init__(self, *, signer: str, account: str, now_ms: int) -> None:
        self.signer = signer.lower()
        self.account = account.lower()
        self.now_ms = now_ms
        self.meta = json.loads((FIXTURES / "meta_testnet.json").read_text())
        self.asset = next(i for i, m in enumerate(self.meta["universe"]) if m["name"] == "BTC")
        self.bid = Decimal("64999.5")
        self.ask = Decimal("65000.5")
        self.liquidity = Decimal("1")  # максимальный размер исполнения одного IOC
        self.position = Decimal(0)
        self.entry_px: Decimal | None = None
        self.leverage_type = "isolated"
        self.leverage = 12
        self.available = Decimal("98.45")  # availableToTrade (маржа)
        self.spot_usdc = Decimal("98.45")  # equity для расчёта риска
        self.orders: dict[int, dict[str, Any]] = {}  # все ордера по oid (история)
        self.fills: list[dict[str, Any]] = []
        self.next_oid = 1000
        self.next_tid = 1
        self.exchange_actions: list[dict[str, Any]] = []
        self.funding: list[dict[str, Any]] = []
        self.scheduled_cancel_ms: int | None = None
        self.unrealized_pnl = Decimal(0)
        self.requests_used = 1500
        self.requests_cap = 200_000
        self.ledger: list[dict[str, Any]] = []
        # Сбои и особые ответы.
        self.fail_next: str | None = None
        self.reject_next_order: str | None = None
        self.reject_trigger: str | None = None
        self.trigger_status_string = False
        self.weird_next_response: Any = None

    # --- транспорт ---

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path == "/info":
            return httpx.Response(200, json=self._info(payload))
        fail, self.fail_next = self.fail_next, None
        if fail == "connect":
            raise httpx.ConnectError("connection refused")
        if fail == "timeout_before":
            raise httpx.ReadTimeout("timeout")
        body = self._exchange(payload)
        if fail == "timeout_after":
            raise httpx.ReadTimeout("timeout after processing")
        if fail == "crash_after":
            raise SimulatedCrashError("process crashed after sending")
        if self.weird_next_response is not None:
            body, self.weird_next_response = self.weird_next_response, None
        return httpx.Response(200, json=body)

    # --- info ---

    def _info(self, p: dict[str, Any]) -> Any:
        t = p["type"]
        if t == "meta":
            return self.meta
        if t == "l2Book":
            return {
                "coin": "BTC",
                "time": self.now_ms,
                "levels": [
                    [{"px": str(self.bid), "sz": "5", "n": 3}],
                    [{"px": str(self.ask), "sz": "5", "n": 3}],
                ],
            }
        if t == "activeAssetData":
            return {
                "user": self.account,
                "coin": "BTC",
                "leverage": {"type": self.leverage_type, "value": self.leverage, "rawUsd": "0.0"},
                "maxTradeSzs": ["0.01", "0.01"],
                "availableToTrade": [str(self.available), str(self.available)],
                "markPx": str(self.mid),
            }
        if t == "clearinghouseState":
            return self._clearinghouse()
        if t == "spotClearinghouseState":
            return {
                "balances": [
                    {
                        "coin": "USDC",
                        "token": 0,
                        "total": str(self.spot_usdc),
                        "hold": "0.0",
                        "entryNtl": "0.0",
                    }
                ]
            }
        if t == "frontendOpenOrders":
            return [self._order_view(o) for o in self.orders.values() if o["status"] == "open"]
        if t == "orderStatus":
            order = self._find(p["oid"])
            if order is None:
                return {"status": "unknownOid"}
            return {
                "status": "order",
                "order": {
                    "order": self._order_view(order),
                    "status": order["status"],
                    "statusTimestamp": self.now_ms,
                },
            }
        if t == "userFillsByTime":
            return [f for f in self.fills if f["time"] >= p["startTime"]]
        if t == "userRateLimit":
            return {
                "cumVlm": "190000.0",
                "nRequestsUsed": self.requests_used,
                "nRequestsCap": self.requests_cap,
                "nRequestsSurplus": 0,
            }
        if t == "userFunding":
            return [e for e in self.funding if e["time"] >= p["startTime"]]
        if t == "userNonFundingLedgerUpdates":
            return [e for e in self.ledger if e["time"] >= p["startTime"]]
        if t == "userRole":
            if p["user"] == self.signer:
                return {"role": "agent", "data": {"user": self.account}}
            return {"role": "user"}
        if t == "extraAgents":
            return [
                {
                    "name": "test",
                    "address": self.signer,
                    "validUntil": self.now_ms + 90 * 86_400_000,
                }
            ]
        raise AssertionError(f"неожиданный info-запрос {t}")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    def _clearinghouse(self) -> dict[str, Any]:
        positions = []
        if self.position != 0:
            positions.append(
                {
                    "type": "oneWay",
                    "position": {
                        "coin": "BTC",
                        "szi": str(self.position),
                        "leverage": {"type": self.leverage_type, "value": self.leverage},
                        "entryPx": str(self.entry_px),
                        "positionValue": str(abs(self.position) * self.mid),
                        "unrealizedPnl": str(self.unrealized_pnl),
                        "returnOnEquity": "0.0",
                        "liquidationPx": None,
                        "marginUsed": str(abs(self.position) * self.mid / self.leverage),
                        "maxLeverage": 40,
                        "cumFunding": {"allTime": "0.0", "sinceOpen": "0.0", "sinceChange": "0.0"},
                    },
                }
            )
        return {
            "marginSummary": {
                "accountValue": "0.0",
                "totalNtlPos": "0.0",
                "totalRawUsd": "0.0",
                "totalMarginUsed": "0.0",
            },
            "crossMarginSummary": {
                "accountValue": "0.0",
                "totalNtlPos": "0.0",
                "totalRawUsd": "0.0",
                "totalMarginUsed": "0.0",
            },
            "crossMaintenanceMarginUsed": "0.0",
            "withdrawable": "0.0",
            "assetPositions": positions,
            "time": self.now_ms,
        }

    def _find(self, key: int | str) -> dict[str, Any] | None:
        if isinstance(key, int):
            return self.orders.get(key)
        return next((o for o in self.orders.values() if o.get("cloid") == key), None)

    def _order_view(self, o: dict[str, Any]) -> dict[str, Any]:
        return {
            "coin": "BTC",
            "side": "B" if o["is_buy"] else "A",
            "limitPx": o["px"],
            "sz": str(o["remaining"]),
            "oid": o["oid"],
            "timestamp": self.now_ms,
            "triggerCondition": o.get("trigger_condition", "N/A"),
            "isTrigger": o["is_trigger"],
            "triggerPx": o.get("trigger_px", "0.0"),
            "children": [],
            "isPositionTpsl": False,
            "reduceOnly": o["reduce_only"],
            "orderType": "Stop Market" if o["is_trigger"] else "Limit",
            "origSz": str(o["size"]),
            "tif": None if o["is_trigger"] else "Ioc",
            "cloid": o.get("cloid"),
        }

    # --- exchange ---

    def _exchange(self, payload: dict[str, Any]) -> Any:
        action = payload["action"]
        recovered = recover_agent_or_user_from_l1_action(
            action, payload["signature"], payload["vaultAddress"], payload["nonce"], None, False
        ).lower()
        if recovered != self.signer:
            return {"status": "err", "response": f"User or API Wallet {recovered} does not exist."}
        self.exchange_actions.append(action)
        t = action["type"]
        if t == "updateLeverage":
            self.leverage_type = "cross" if action["isCross"] else "isolated"
            self.leverage = action["leverage"]
            return {"status": "ok", "response": {"type": "default"}}
        if t == "scheduleCancel":
            when = action.get("time")
            if when is not None and when < self.now_ms + 5000:
                return {"status": "err", "response": "Scheduled cancel time too early"}
            self.scheduled_cancel_ms = when
            return {"status": "ok", "response": {"type": "default"}}
        if t == "cancelByCloid":
            order = self._find(action["cancels"][0]["cloid"])
            if order is None or order["status"] != "open":
                status: Any = {"error": MISSING_ORDER}
            else:
                order["status"] = "canceled"
                status = "success"
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [status]}}}
        if t == "cancel":
            order = self.orders.get(action["cancels"][0]["o"])
            status = {"error": MISSING_ORDER}
            if order is not None and order["status"] == "open":
                order["status"] = "canceled"
                status = "success"
            return {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [status]}}}
        if t == "order":
            statuses = [self._place(w) for w in action["orders"]]
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}
        raise AssertionError(f"неожиданное действие {t}")

    def _place(self, w: dict[str, Any]) -> Any:
        is_buy, px, sz, reduce_only = w["b"], Decimal(w["p"]), Decimal(w["s"]), w["r"]
        if "trigger" in w["t"]:
            if self.reject_trigger:
                message, self.reject_trigger = self.reject_trigger, None
                return {"error": message}
            if reduce_only and (self.position == 0 or (self.position > 0) == is_buy):
                return {"error": "Reduce only order would increase position."}
            oid = self._new_order(w, is_trigger=True, status="open")
            self.orders[oid]["trigger_px"] = w["t"]["trigger"]["triggerPx"]
            return "waitingForTrigger" if self.trigger_status_string else {"resting": {"oid": oid}}

        if self.reject_next_order:
            message, self.reject_next_order = self.reject_next_order, None
            return {"error": message}
        if sz * px < 10:
            return {"error": "Order must have minimum value of $10."}
        if reduce_only and (self.position == 0 or (self.position > 0) == is_buy):
            return {"error": "Reduce only order would increase position."}
        crosses = px >= self.ask if is_buy else px <= self.bid
        fill = min(sz, self.liquidity)
        if reduce_only:
            fill = min(fill, abs(self.position))
        if not crosses or fill <= 0:
            return {
                "error": "Order could not immediately match against any resting orders. asset=3"
            }
        fill_px = self.ask if is_buy else self.bid
        oid = self._new_order(w, is_trigger=False, status="filled" if fill == sz else "canceled")
        self.orders[oid]["remaining"] = sz - fill
        self._fill(oid, w.get("c"), is_buy, fill, fill_px)
        return {"filled": {"totalSz": str(fill), "avgPx": str(fill_px), "oid": oid}}

    def _new_order(self, w: dict[str, Any], *, is_trigger: bool, status: str) -> int:
        oid = self.next_oid
        self.next_oid += 1
        self.orders[oid] = {
            "oid": oid,
            "cloid": w.get("c"),
            "is_buy": w["b"],
            "px": w["p"],
            "size": Decimal(w["s"]),
            "remaining": Decimal(w["s"]),
            "reduce_only": w["r"],
            "is_trigger": is_trigger,
            "status": status,
        }
        return oid

    def _fill(self, oid: int, cloid: str | None, is_buy: bool, size: Decimal, px: Decimal) -> None:
        start = self.position
        self.position += size if is_buy else -size
        if self.position == 0:
            self.entry_px = None
            # Как на mainnet: при обнулении позиции биржа снимает reduce-only ордера.
            for other in self.orders.values():
                if other["status"] == "open" and other["reduce_only"]:
                    other["status"] = "reduceOnlyCanceled"
        elif start == 0:
            self.entry_px = px
        fill = {
            "coin": "BTC",
            "px": str(px),
            "sz": str(size),
            "side": "B" if is_buy else "A",
            "time": self.now_ms,
            "startPosition": str(start),
            "dir": ("Open " if start == 0 else "Close ")
            + ("Long" if (start > 0 or (start == 0 and is_buy)) else "Short"),
            "closedPnl": "0.0",
            "hash": "0x" + "0" * 64,
            "oid": oid,
            "crossed": True,
            "fee": "0.001",
            "tid": self.next_tid,
            "feeToken": "USDC",
        }
        if cloid:
            fill["cloid"] = cloid
        self.next_tid += 1
        self.fills.append(fill)

    def trigger_stop(self) -> None:
        """Имитация срабатывания stop: исполняется по рынку."""
        for order in self.orders.values():
            if order["is_trigger"] and order["status"] == "open":
                size = min(order["remaining"], abs(self.position))
                order["status"] = "filled"
                order["remaining"] = Decimal(0)
                self._fill(order["oid"], order["cloid"], order["is_buy"], size, self.bid)

    # --- события вне бота ---

    def manual_fill(self, *, is_buy: bool, size: Decimal) -> None:
        """Сделка через интерфейс биржи (без cloid бота)."""
        oid = self._new_order(
            {"b": is_buy, "p": str(self.mid), "s": str(size), "r": False},
            is_trigger=False,
            status="filled",
        )
        self.orders[oid]["remaining"] = Decimal(0)
        self._fill(oid, None, is_buy, size, self.ask if is_buy else self.bid)

    def external_stop(self, *, is_buy: bool, size: Decimal, trigger: str) -> int:
        """Стоп, поставленный не ботом (или чья локальная запись потеряна)."""
        oid = self._new_order(
            {"b": is_buy, "p": trigger, "s": str(size), "r": True}, is_trigger=True, status="open"
        )
        self.orders[oid]["trigger_px"] = trigger
        return oid

    def exchange_cancels(self, oid: int, status: str = "marginCanceled") -> None:
        self.orders[oid]["status"] = status

    def add_funding(self, usdc: str) -> None:
        self.funding.append(
            {
                "time": self.now_ms,
                "hash": "0x" + f"{len(self.funding) + 1:064x}",
                "delta": {
                    "type": "funding",
                    "coin": "BTC",
                    "usdc": usdc,
                    "szi": str(self.position),
                    "fundingRate": "0.0000125",
                    "nSamples": None,
                },
            }
        )

    def add_deposit(self, usdc: str) -> None:
        self.ledger.append(
            {
                "time": self.now_ms,
                "hash": "0x" + f"{len(self.ledger) + 100:064x}",
                "delta": {"type": "deposit", "usdc": usdc},
            }
        )

    def advance(self, ms: int) -> None:
        """Время биржи идёт вперёд; срабатывает scheduleCancel, если пора."""
        self.now_ms += ms
        if self.scheduled_cancel_ms is not None and self.now_ms >= self.scheduled_cancel_ms:
            for order in self.orders.values():
                if order["status"] == "open":
                    order["status"] = "scheduledCancel"
            self.scheduled_cancel_ms = None
