"""Торговая сессия: всё, что нужно для отправки подписанного действия.

При открытии, по порядку:
1. захват блокировки единственного торгового процесса на этот API-кошелёк;
2. проверка API-кошелька (роль agent, принадлежность аккаунту, срок действия);
3. проверка расхождения часов с биржей.

Каждое действие: проверка блокировки → новый nonce → подпись (белый список) → отправка.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import logfire
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config.settings import Settings
from app.execution.lock import TraderLock
from app.execution.nonce import NonceManager
from app.execution.wallet import WalletStatus, check_api_wallet, check_clock
from app.integrations.hyperliquid.exchange_client import ExchangeClient, ExchangeResponse
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.integrations.hyperliquid.signing import ActionSigner


class TradingNotConfiguredError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class TradingSession:
    signer: ActionSigner
    nonces: NonceManager
    exchange: ExchangeClient
    lock: TraderLock
    wallet: WalletStatus
    clock_skew: timedelta

    async def prepare(self, action: dict[str, Any]) -> dict[str, Any]:
        """Проверка блокировки → новый nonce → подпись. Возвращает payload, ещё не отправленный:
        вызывающий успевает сохранить nonce и действие до отправки."""
        await self.lock.ensure_held()
        nonce = await self.nonces.next()
        return self.signer.sign(action, nonce, self.wallet.vault_address)

    async def send(self, payload: dict[str, Any]) -> ExchangeResponse:
        return await self.exchange.post(payload)

    async def submit(self, action: dict[str, Any]) -> ExchangeResponse:
        return await self.send(await self.prepare(action))


@asynccontextmanager
async def open_trading_session(
    settings: Settings,
    engine: AsyncEngine,
    gateway: HyperliquidGateway,
    *,
    exchange: ExchangeClient | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> AsyncIterator[TradingSession]:
    if settings.hl_api_wallet_private_key is None:
        raise TradingNotConfiguredError(
            "нет ключа API-кошелька: запустите с "
            "ERATRADE_ENV_FILE=.env.mainnet,.env.mainnet.secrets"
        )
    if settings.hl_account_address is None:
        raise TradingNotConfiguredError("не задан HL_ACCOUNT_ADDRESS")

    signer = ActionSigner(settings.hl_api_wallet_private_key, is_mainnet=settings.is_mainnet)
    async with TraderLock(engine, signer.address) as lock:
        wallet = await check_api_wallet(
            gateway, signer.address, settings.hl_account_address, now=clock()
        )
        for warning in wallet.warnings:
            logfire.warn("trading: {warning}", warning=warning)
        skew = await check_clock(gateway, settings.hl_account_address, clock=clock)
        nonces = NonceManager(async_sessionmaker(engine, expire_on_commit=False), signer.address)
        client = exchange or ExchangeClient(settings.hyperliquid_api_url)
        async with client:
            yield TradingSession(
                signer=signer,
                nonces=nonces,
                exchange=client,
                lock=lock,
                wallet=wallet,
                clock_skew=skew,
            )
