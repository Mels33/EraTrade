"""Проверка API-кошелька, часов и торговой сессии целиком (фейковая биржа + PostgreSQL)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_account import Account
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import Network, Settings
from app.execution.lock import AnotherTraderRunningError, TraderLock
from app.execution.session import TradingNotConfiguredError, open_trading_session
from app.execution.wallet import ClockSkewError, WalletCheckError, check_api_wallet, check_clock
from app.integrations.hyperliquid import actions
from app.integrations.hyperliquid.exchange_client import ExchangeClient
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.integrations.hyperliquid.info_client import InfoClient
from app.integrations.hyperliquid.parsing import datetime_to_ms
from tests.db.constants import TEST_DB_URL

FIXTURES = Path(__file__).parents[1] / "hyperliquid" / "fixtures"
TEST_KEY = "0x" + "22" * 32
SIGNER = Account.from_key(TEST_KEY).address.lower()
ACCOUNT = "0x" + "a1" * 20
SUBACCOUNT = "0x" + "5b" * 20
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


class FakeInfo:
    """Info-эндпоинт: роли, API-кошельки и время биржи настраиваются в тесте."""

    def __init__(self) -> None:
        self.roles: dict[str, Any] = {
            SIGNER: {"role": "agent", "data": {"user": ACCOUNT}},
            ACCOUNT: {"role": "user"},
            SUBACCOUNT: {"role": "subAccount", "data": {"master": ACCOUNT}},
        }
        self.agents: list[dict[str, Any]] = [
            {
                "name": "my_era_api",
                "address": SIGNER,
                "validUntil": datetime_to_ms(NOW + timedelta(days=90)),
            }
        ]
        self.exchange_time = NOW

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = json.loads(request.content)
        if p["type"] == "userRole":
            return httpx.Response(200, json=self.roles.get(p["user"], {"role": "missing"}))
        if p["type"] == "extraAgents":
            return httpx.Response(200, json=self.agents)
        if p["type"] == "clearinghouseState":
            state = json.loads((FIXTURES / "clearinghouse_state.json").read_text())
            state["time"] = datetime_to_ms(self.exchange_time)
            return httpx.Response(200, json=state)
        return httpx.Response(422)


def gateway(info: FakeInfo) -> HyperliquidGateway:
    client = InfoClient("https://example.invalid", transport=httpx.MockTransport(info))
    return HyperliquidGateway(client, Network.TESTNET)


async def test_valid_agent_of_account() -> None:
    status = await check_api_wallet(gateway(FakeInfo()), SIGNER, ACCOUNT, now=NOW)
    assert status.owner == ACCOUNT and status.vault_address is None
    assert status.agent_name == "my_era_api" and status.warnings == ()


async def test_subaccount_trading_uses_vault_address() -> None:
    status = await check_api_wallet(gateway(FakeInfo()), SIGNER, SUBACCOUNT, now=NOW)
    assert status.vault_address == SUBACCOUNT and status.owner == ACCOUNT


async def test_main_wallet_key_refused() -> None:
    with pytest.raises(WalletCheckError, match="основного кошелька"):
        await check_api_wallet(gateway(FakeInfo()), ACCOUNT, ACCOUNT, now=NOW)


async def test_key_that_is_not_an_agent_refused() -> None:
    info = FakeInfo()
    info.roles[SIGNER] = {"role": "missing"}
    with pytest.raises(WalletCheckError, match="не является API-кошельком"):
        await check_api_wallet(gateway(info), SIGNER, ACCOUNT, now=NOW)


async def test_agent_of_other_account_refused() -> None:
    other = "0x" + "99" * 20
    with pytest.raises(WalletCheckError, match="одобрен аккаунтом"):
        await check_api_wallet(gateway(FakeInfo()), SIGNER, other, now=NOW)


async def test_expiry_warning_and_refusal() -> None:
    info = FakeInfo()
    info.agents[0]["validUntil"] = datetime_to_ms(NOW + timedelta(days=5))
    status = await check_api_wallet(gateway(info), SIGNER, ACCOUNT, now=NOW)
    assert any("истекает через 5 дн." in w for w in status.warnings)

    info.agents[0]["validUntil"] = datetime_to_ms(NOW + timedelta(hours=12))
    with pytest.raises(WalletCheckError, match="срок действия"):
        await check_api_wallet(gateway(info), SIGNER, ACCOUNT, now=NOW)


async def test_unnamed_agent_gives_warning() -> None:
    info = FakeInfo()
    info.agents = []
    status = await check_api_wallet(gateway(info), SIGNER, ACCOUNT, now=NOW)
    assert any("срок действия неизвестен" in w for w in status.warnings)


async def test_clock_skew() -> None:
    info = FakeInfo()
    assert abs(await check_clock(gateway(info), ACCOUNT, clock=lambda: NOW)) < timedelta(seconds=1)
    info.exchange_time = NOW + timedelta(seconds=5)
    with pytest.raises(ClockSkewError, match=r"\+5"):
        await check_clock(gateway(info), ACCOUNT, clock=lambda: NOW)


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "testnet",
        "hyperliquid_network": "testnet",
        "database_url": TEST_DB_URL,
        "hl_account_address": ACCOUNT,
        "hl_api_wallet_private_key": TEST_KEY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def test_session_signs_and_sends_with_unique_nonces(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sent: list[dict[str, Any]] = []

    def exchange_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok", "response": {"type": "default"}})

    engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    exchange = ExchangeClient(
        "https://example.invalid", transport=httpx.MockTransport(exchange_handler)
    )
    try:
        async with open_trading_session(
            settings(), engine, gateway(FakeInfo()), exchange=exchange, clock=lambda: NOW
        ) as session:
            # Пока сессия открыта, второй торговый процесс с тем же ключом не запустится.
            with pytest.raises(AnotherTraderRunningError):
                async with TraderLock(engine, SIGNER):
                    pass
            action = actions.update_leverage_action(3, is_cross=False, leverage=2)
            first = await session.submit(action)
            await session.submit(actions.cancel_action(3, 1))
    finally:
        await engine.dispose()

    assert first.ok
    assert len(sent) == 2 and sent[0]["nonce"] < sent[1]["nonce"]
    recovered = recover_agent_or_user_from_l1_action(
        sent[0]["action"], sent[0]["signature"], None, sent[0]["nonce"], None, False
    )
    assert recovered.lower() == SIGNER


async def test_session_requires_key_and_valid_wallet(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    try:
        with pytest.raises(TradingNotConfiguredError, match="нет ключа"):
            async with open_trading_session(
                settings(hl_api_wallet_private_key=None), engine, gateway(FakeInfo())
            ):
                pass
        info = FakeInfo()
        info.roles[SIGNER] = {"role": "missing"}
        with pytest.raises(WalletCheckError):
            async with open_trading_session(settings(), engine, gateway(info), clock=lambda: NOW):
                pass
        # После неудачной проверки блокировка освобождена.
        async with TraderLock(engine, SIGNER):
            pass
    finally:
        await engine.dispose()
