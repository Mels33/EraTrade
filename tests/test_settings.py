from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config.settings import (
    MAINNET_CONFIRMATION_PHRASE,
    ConfigurationError,
    Network,
    Settings,
    TradingMode,
    get_settings,
)

TESTNET_DB = "postgresql+asyncpg://u:db-pass-123@localhost:5435/eratrade_testnet"
MAINNET_DB = "postgresql+asyncpg://u:db-pass-123@localhost:5435/eratrade_mainnet"
FAKE_KEY = "0x" + "ab" * 32


def make(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "testnet",
        "hyperliquid_network": "testnet",
        "trading_mode": "TESTNET",
        "database_url": TESTNET_DB,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_mainnet(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "mainnet",
        "hyperliquid_network": "mainnet",
        "trading_mode": "SHADOW",
        "database_url": MAINNET_DB,
        "mainnet_confirmation": MAINNET_CONFIRMATION_PHRASE,
    }
    values.update(overrides)
    return make(**values)


def test_valid_testnet() -> None:
    s = make()
    assert s.network is Network.TESTNET
    assert s.hyperliquid_api_url == "https://api.hyperliquid-testnet.xyz"
    assert s.hyperliquid_ws_url == "wss://api.hyperliquid-testnet.xyz/ws"
    assert not s.is_mainnet


def test_valid_mainnet_with_confirmation() -> None:
    s = make_mainnet()
    assert s.is_mainnet
    assert s.hyperliquid_api_url == "https://api.hyperliquid.xyz"


def test_default_mode_is_disabled() -> None:
    values: dict[str, Any] = {
        "environment": "testnet",
        "hyperliquid_network": "testnet",
        "database_url": TESTNET_DB,
    }
    s = Settings(_env_file=None, **values)
    assert s.trading_mode is TradingMode.DISABLED


def test_environment_network_mismatch_fails() -> None:
    with pytest.raises(ValidationError, match="не совпадает"):
        make(hyperliquid_network="mainnet")


@pytest.mark.parametrize("mode", ["SHADOW", "LIVE_PAUSED", "LIVE_ENABLED"])
def test_testnet_rejects_mainnet_modes(mode: str) -> None:
    with pytest.raises(ValidationError, match="недопустим"):
        make(trading_mode=mode)


@pytest.mark.parametrize("mode", ["TESTNET", "LIVE_ENABLED"])
def test_mainnet_rejects_modes(mode: str) -> None:
    with pytest.raises(ValidationError, match="недопустим"):
        make_mainnet(trading_mode=mode)


@pytest.mark.parametrize("confirmation", [None, "", "yes", "true"])
def test_mainnet_requires_confirmation_phrase(confirmation: str | None) -> None:
    with pytest.raises(ValidationError, match="MAINNET_CONFIRMATION"):
        make_mainnet(mainnet_confirmation=confirmation)


def test_testnet_config_with_mainnet_database_fails() -> None:
    with pytest.raises(ValidationError, match="Имя БД"):
        make(database_url=MAINNET_DB)


def test_mainnet_config_with_testnet_database_fails() -> None:
    with pytest.raises(ValidationError, match="Имя БД"):
        make_mainnet(database_url=TESTNET_DB)


def test_secrets_not_exposed() -> None:
    s = make(hl_api_wallet_private_key=FAKE_KEY, logfire_token="pylf_v1_secret")
    rendered = [repr(s), str(s), s.model_dump_json(), str(s.safe_summary())]
    for text in rendered:
        assert FAKE_KEY not in text
        assert "pylf_v1_secret" not in text
        assert "db-pass-123" not in text
    assert s.safe_summary()["api_wallet_key_configured"] is True


def test_settings_are_immutable() -> None:
    s = make()
    with pytest.raises(ValidationError):
        s.trading_mode = TradingMode.LIVE_ENABLED  # type: ignore[misc]


def test_get_settings_error_hides_input(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "ENVIRONMENT=testnet\n"
        "HYPERLIQUID_NETWORK=mainnet\n"
        f"DATABASE_URL={TESTNET_DB}\n"
        f"HL_API_WALLET_PRIVATE_KEY={FAKE_KEY}\n"
    )
    for var in ("ENVIRONMENT", "HYPERLIQUID_NETWORK", "DATABASE_URL", "TRADING_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ERATRADE_ENV_FILE", str(env))
    get_settings.cache_clear()
    try:
        with pytest.raises(ConfigurationError) as exc_info:
            get_settings()
    finally:
        get_settings.cache_clear()
    message = str(exc_info.value)
    assert "не совпадает" in message
    assert FAKE_KEY not in message
    assert "db-pass-123" not in message


def test_account_address_normalized() -> None:
    assert make(hl_account_address="").hl_account_address is None
    addr = "0x" + "AB" * 20
    assert make(hl_account_address=addr).hl_account_address == addr.lower()


@pytest.mark.parametrize("bad", ["0x123", "ab" * 20, "0x" + "zz" * 20])
def test_invalid_account_address_rejected(bad: str) -> None:
    with pytest.raises(ValidationError, match="HL_ACCOUNT_ADDRESS"):
        make(hl_account_address=bad)


def test_multiple_env_files_secrets_separate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    common = tmp_path / ".env.common"
    common.write_text(
        f"ENVIRONMENT=testnet\nHYPERLIQUID_NETWORK=testnet\nDATABASE_URL={TESTNET_DB}\n"
    )
    secrets = tmp_path / ".env.secrets"
    secrets.write_text(f"HL_API_WALLET_PRIVATE_KEY={FAKE_KEY}\n")
    for var in ("ENVIRONMENT", "HYPERLIQUID_NETWORK", "DATABASE_URL", "HL_API_WALLET_PRIVATE_KEY"):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("ERATRADE_ENV_FILE", f"{common}, {secrets}")
    get_settings.cache_clear()
    try:
        with_key = get_settings()
        monkeypatch.setenv("ERATRADE_ENV_FILE", str(common))
        get_settings.cache_clear()
        without_key = get_settings()
    finally:
        get_settings.cache_clear()
    assert with_key.hl_api_wallet_private_key is not None
    assert with_key.hl_api_wallet_private_key.get_secret_value() == FAKE_KEY
    assert without_key.hl_api_wallet_private_key is None
