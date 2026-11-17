"""Настройки окружения с защитой от смешивания testnet и mainnet.

Сеть задаётся в нескольких местах (ENVIRONMENT, HYPERLIQUID_NETWORK, TRADING_MODE, имя БД).
Если они противоречат друг другу, приложение не запускается.
URL Hyperliquid не настраивается вручную, а выводится из сети, чтобы нельзя было
соединить testnet-конфигурацию с mainnet-адресом.
"""

import os
import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

MAINNET_CONFIRMATION_PHRASE = "I_UNDERSTAND_REAL_MONEY"
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class Network(StrEnum):
    TESTNET = "testnet"
    MAINNET = "mainnet"


class TradingMode(StrEnum):
    DISABLED = "DISABLED"
    TESTNET = "TESTNET"
    SHADOW = "SHADOW"
    LIVE_PAUSED = "LIVE_PAUSED"
    LIVE_ENABLED = "LIVE_ENABLED"


# Режимы, с которыми процесс может стартовать в каждой сети.
# LIVE_ENABLED отсутствует намеренно: включается только командой оператора.
ALLOWED_STARTUP_MODES: dict[Network, frozenset[TradingMode]] = {
    Network.TESTNET: frozenset({TradingMode.DISABLED, TradingMode.TESTNET}),
    Network.MAINNET: frozenset({TradingMode.DISABLED, TradingMode.SHADOW, TradingMode.LIVE_PAUSED}),
}

_API_URLS: dict[Network, str] = {
    Network.TESTNET: "https://api.hyperliquid-testnet.xyz",
    Network.MAINNET: "https://api.hyperliquid.xyz",
}
_WS_URLS: dict[Network, str] = {
    Network.TESTNET: "wss://api.hyperliquid-testnet.xyz/ws",
    Network.MAINNET: "wss://api.hyperliquid.xyz/ws",
}


class ConfigurationError(ValueError):
    """Противоречивая или опасная конфигурация."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Network
    hyperliquid_network: Network
    trading_mode: TradingMode = TradingMode.DISABLED
    mainnet_confirmation: str | None = None

    database_url: SecretStr

    # Адрес торгового аккаунта (или субаккаунта), не API-кошелька. Не секрет.
    hl_account_address: str | None = None
    # Ключ API/agent-кошелька. Нужен начиная с этапа 5.
    hl_api_wallet_private_key: SecretStr | None = None

    logfire_token: SecretStr | None = None

    # Токен оператора для API/панели (§13.1). Без него API не запускается.
    api_token: SecretStr | None = None

    # Уведомления оператору (§12.3, §12.5). Без них — только лог.
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    log_level: str = "info"

    strategy_config_path: Path = Path("config/strategy.toml")

    @field_validator("hl_account_address", mode="before")
    @classmethod
    def _check_address(cls, value: object) -> object:
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not _ADDRESS_RE.match(value):
            raise ValueError("HL_ACCOUNT_ADDRESS должен иметь вид 0x + 40 hex-символов")
        return value.lower()

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if self.environment != self.hyperliquid_network:
            raise ConfigurationError(
                f"ENVIRONMENT={self.environment} не совпадает с "
                f"HYPERLIQUID_NETWORK={self.hyperliquid_network}"
            )

        allowed = ALLOWED_STARTUP_MODES[self.environment]
        if self.trading_mode not in allowed:
            raise ConfigurationError(
                f"TRADING_MODE={self.trading_mode} недопустим при старте в сети "
                f"{self.environment}; разрешено: {sorted(m.value for m in allowed)}"
            )

        if (
            self.environment is Network.MAINNET
            and self.mainnet_confirmation != MAINNET_CONFIRMATION_PHRASE
        ):
            raise ConfigurationError(
                "Для mainnet нужно MAINNET_CONFIRMATION=" + MAINNET_CONFIRMATION_PHRASE
            )

        db_name = make_url(self.database_url.get_secret_value()).database or ""
        if not db_name.endswith(f"_{self.environment}"):
            raise ConfigurationError(
                f"Имя БД '{db_name}' должно оканчиваться на '_{self.environment}'"
            )

        return self

    @property
    def network(self) -> Network:
        return self.hyperliquid_network

    @property
    def hyperliquid_api_url(self) -> str:
        return _API_URLS[self.network]

    @property
    def hyperliquid_ws_url(self) -> str:
        return _WS_URLS[self.network]

    @property
    def is_mainnet(self) -> bool:
        return self.network is Network.MAINNET

    def safe_summary(self) -> dict[str, Any]:
        """Сводка для вывода в консоль и логи — без секретов."""
        url = make_url(self.database_url.get_secret_value())
        return {
            "environment": self.environment.value,
            "trading_mode": self.trading_mode.value,
            "hyperliquid_api_url": self.hyperliquid_api_url,
            "database": url.render_as_string(hide_password=True),
            "hl_account_address": self.hl_account_address,
            "api_wallet_key_configured": self.hl_api_wallet_private_key is not None,
            "logfire_token_configured": self.logfire_token is not None,
            "api_token_configured": self.api_token is not None,
            "telegram_configured": self.telegram_bot_token is not None
            and bool(self.telegram_chat_id),
            "strategy_config_path": str(self.strategy_config_path),
        }


def env_files_from_environment() -> tuple[str, ...]:
    """ERATRADE_ENV_FILE: один файл или несколько через запятую (последний имеет приоритет).

    Пример: .env.mainnet,.env.mainnet.secrets — общие настройки отдельно от ключа,
    чтобы процессы без права торговли (коллектор) не получали ключ API-кошелька.
    """
    raw = os.environ.get("ERATRADE_ENV_FILE", ".env")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Настройки процесса. Файлы окружения задаются через ERATRADE_ENV_FILE."""
    env_files = env_files_from_environment()
    try:
        return Settings(_env_file=env_files)
    except ValidationError as exc:
        # Сообщение без input_value: в нём могли бы оказаться DATABASE_URL или ключи.
        raise ConfigurationError(format_validation_error(exc)) from None


def format_validation_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        loc = ".".join(str(part) for part in err["loc"]) or "settings"
        lines.append(f"{loc}: {err['msg']}")
    return "Ошибка конфигурации:\n  " + "\n  ".join(lines)
