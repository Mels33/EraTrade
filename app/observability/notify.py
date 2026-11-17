"""Уведомления оператору. Telegram, если настроен; иначе — только лог.

Ошибка отправки никогда не прерывает работу бота: уведомление — вспомогательный канал,
основной аудит — в PostgreSQL.
"""

from typing import Protocol

import httpx
import logfire

from app.config.settings import Settings


class Notifier(Protocol):
    async def send(self, text: str) -> bool: ...


class LogNotifier:
    async def send(self, text: str) -> bool:
        logfire.warn("уведомление: {text}", text=text)
        return True


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        prefix: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._prefix = prefix
        self._transport = transport

    def __repr__(self) -> str:
        return f"TelegramNotifier(chat_id={self._chat_id})"  # без токена

    async def send(self, text: str) -> bool:
        message = f"{self._prefix} {text}"[:4000]
        logfire.warn("уведомление: {text}", text=text)
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                response = await client.post(
                    self._url, json={"chat_id": self._chat_id, "text": message}
                )
        except httpx.HTTPError as exc:
            logfire.error("telegram: не отправлено ({error})", error=type(exc).__name__)
            return False
        if response.status_code != 200:
            # Тело ответа и URL не логируем: в URL запроса токен бота.
            logfire.error("telegram: HTTP {status}", status=response.status_code)
            return False
        return True


def build_notifier(settings: Settings) -> Notifier:
    if settings.telegram_bot_token is not None and settings.telegram_chat_id:
        prefix = "🔴 EraTrade MAINNET:" if settings.is_mainnet else "🟡 EraTrade testnet:"
        return TelegramNotifier(
            settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id, prefix=prefix
        )
    return LogNotifier()
