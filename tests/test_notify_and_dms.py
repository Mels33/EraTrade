"""Уведомления (Telegram) и dead man's switch (scheduleCancel)."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.config.settings import Settings
from app.config.strategy import DeadMansSwitchConfig
from app.execution.dead_mans_switch import DeadMansSwitch, DeadMansSwitchError
from app.integrations.hyperliquid.exchange_client import ExchangeResponse
from app.observability.notify import LogNotifier, TelegramNotifier, build_notifier

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


class FakeSession:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.actions: list[dict[str, Any]] = []

    async def submit(self, action: dict[str, Any]) -> ExchangeResponse:
        self.actions.append(action)
        error = None if self.ok else "Cannot set scheduled cancel time until enough volume traded"
        return ExchangeResponse(self.ok, "default" if self.ok else None, None, error, {})


async def test_dead_mans_switch_arm_and_disarm() -> None:
    session = FakeSession()
    config = DeadMansSwitchConfig(enabled=True, horizon_seconds=60, refresh_seconds=20)
    dms = DeadMansSwitch(session, config, clock=lambda: NOW)  # type: ignore[arg-type]
    assert await dms.arm() == NOW + timedelta(seconds=60)
    assert session.actions[-1] == {"type": "scheduleCancel", "time": 1793534460000}
    await dms.disarm()
    assert session.actions[-1] == {"type": "scheduleCancel"}


async def test_dead_mans_switch_rejection_is_reported() -> None:
    # Биржа требует оборот $1 млн — маркет-мейкер ловит эту ошибку и работает без switch.
    config = DeadMansSwitchConfig(enabled=True, horizon_seconds=60, refresh_seconds=20)
    dms = DeadMansSwitch(FakeSession(ok=False), config, clock=lambda: NOW)  # type: ignore[arg-type]
    with pytest.raises(DeadMansSwitchError, match="volume"):
        await dms.arm()


async def test_disabled_dead_mans_switch_does_nothing() -> None:
    session = FakeSession()
    config = DeadMansSwitchConfig(enabled=False, horizon_seconds=60, refresh_seconds=20)
    await DeadMansSwitch(session, config).run()  # type: ignore[arg-type]
    assert session.actions == []


async def test_telegram_notifier_sends_without_leaking_token() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        "123:SECRET", "42", prefix="🔴 EraTrade MAINNET:", transport=httpx.MockTransport(handler)
    )
    assert await notifier.send("дневной лимит")
    assert sent[0]["body"] == {"chat_id": "42", "text": "🔴 EraTrade MAINNET: дневной лимит"}
    assert "SECRET" not in repr(notifier)


async def test_telegram_failure_does_not_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    notifier = TelegramNotifier("t", "1", prefix="x", transport=httpx.MockTransport(handler))
    assert not await notifier.send("ok")  # не бросает, сообщает о неудаче


def test_build_notifier_falls_back_to_log() -> None:
    base: dict[str, Any] = {
        "environment": "testnet",
        "hyperliquid_network": "testnet",
        "database_url": "postgresql+asyncpg://u:p@h/eratrade_testnet",
    }
    assert isinstance(build_notifier(Settings(_env_file=None, **base)), LogNotifier)
    configured = Settings(_env_file=None, telegram_bot_token="1:x", telegram_chat_id="5", **base)
    assert isinstance(build_notifier(configured), TelegramNotifier)


@pytest.mark.parametrize("bad", [{"horizon_seconds": 10, "refresh_seconds": 8}])
def test_dead_mans_switch_config_validation(bad: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        DeadMansSwitchConfig(enabled=True, **bad)
