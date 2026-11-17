"""Совместимость с официальным SDK: одинаковый msgpack-хеш действий и проверяемая подпись."""

from decimal import Decimal
from typing import Any

import pytest
from eth_account import Account
from hyperliquid.utils.signing import (
    action_hash,
    order_request_to_order_wire,
    order_wires_to_order_action,
    recover_agent_or_user_from_l1_action,
)
from hyperliquid.utils.types import Cloid
from pydantic import SecretStr

from app.integrations.hyperliquid import actions
from app.integrations.hyperliquid.actions import (
    LimitOrderType,
    OrderRequest,
    Tif,
    TriggerOrderType,
)
from app.integrations.hyperliquid.signing import ActionSigner, ForbiddenActionError

TEST_KEY = "0x" + "11" * 32  # тестовый ключ, не используется ни в одной сети
CLOID = "0x" + "ab" * 16
NONCE = 1794866400123


def same_hash(ours: dict[str, Any], sdk: dict[str, Any], vault: str | None = None) -> bool:
    return bool(action_hash(ours, vault, NONCE, None) == action_hash(sdk, vault, NONCE, None))


def test_ioc_order_matches_sdk() -> None:
    ours = actions.order_action(
        [
            OrderRequest(
                asset=0,
                is_buy=True,
                price=Decimal("65074"),
                size=Decimal("0.00016"),
                reduce_only=False,
                order_type=LimitOrderType(Tif.IOC),
                cloid=CLOID,
            )
        ]
    )
    sdk_wire = order_request_to_order_wire(
        {
            "coin": "BTC",
            "is_buy": True,
            "sz": 0.00016,
            "limit_px": 65074.0,
            "order_type": {"limit": {"tif": "Ioc"}},
            "reduce_only": False,
            "cloid": Cloid(CLOID),
        },
        0,
    )
    sdk = order_wires_to_order_action([sdk_wire])
    assert ours == sdk
    assert same_hash(ours, sdk)


def test_reduce_only_stop_loss_matches_sdk() -> None:
    ours = actions.order_action(
        [
            OrderRequest(
                asset=3,
                is_buy=False,
                price=Decimal("63500.5"),
                size=Decimal("0.0002"),
                reduce_only=True,
                order_type=TriggerOrderType(Decimal("63800"), is_market=True, tpsl="sl"),
            )
        ]
    )
    sdk_wire = order_request_to_order_wire(
        {
            "coin": "BTC",
            "is_buy": False,
            "sz": 0.0002,
            "limit_px": 63500.5,
            "order_type": {"trigger": {"triggerPx": 63800.0, "isMarket": True, "tpsl": "sl"}},
            "reduce_only": True,
        },
        3,
    )
    sdk = order_wires_to_order_action([sdk_wire])
    assert ours == sdk
    assert same_hash(ours, sdk)


@pytest.mark.parametrize(
    ("ours", "sdk"),
    [
        (actions.cancel_action(0, 12345), {"type": "cancel", "cancels": [{"a": 0, "o": 12345}]}),
        (
            actions.cancel_by_cloid_action(0, CLOID),
            {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": CLOID}]},
        ),
        (
            actions.update_leverage_action(0, is_cross=False, leverage=2),
            {"type": "updateLeverage", "asset": 0, "isCross": False, "leverage": 2},
        ),
        (
            actions.schedule_cancel_action(1794866460000),
            {"type": "scheduleCancel", "time": 1794866460000},
        ),
        (actions.schedule_cancel_action(None), {"type": "scheduleCancel"}),
    ],
)
def test_other_actions_match_sdk_format(ours: dict[str, Any], sdk: dict[str, Any]) -> None:
    # Форматы из hyperliquid/exchange.py (bulk_cancel, bulk_cancel_by_cloid, update_leverage,
    # schedule_cancel) SDK 0.24.0 — с тем же порядком ключей.
    assert ours == sdk
    assert list(ours) == list(sdk)
    assert same_hash(ours, sdk)


def test_signature_recovers_to_signer() -> None:
    signer = ActionSigner(SecretStr(TEST_KEY), is_mainnet=True)
    assert signer.address == Account.from_key(TEST_KEY).address.lower()
    action = actions.cancel_action(0, 1)
    payload = signer.sign(action, NONCE, vault_address=None)

    assert payload["action"] == action and payload["nonce"] == NONCE
    assert payload["vaultAddress"] is None and payload["expiresAfter"] is None
    recovered = recover_agent_or_user_from_l1_action(
        action, payload["signature"], None, NONCE, None, True
    )
    assert recovered.lower() == signer.address


def test_signature_binds_network_vault_and_nonce() -> None:
    action = actions.cancel_action(0, 1)
    mainnet = ActionSigner(SecretStr(TEST_KEY), is_mainnet=True)
    testnet = ActionSigner(SecretStr(TEST_KEY), is_mainnet=False)
    vault = "0x" + "cd" * 20
    sigs = {
        str(mainnet.sign(action, NONCE, None)["signature"]),
        str(testnet.sign(action, NONCE, None)["signature"]),
        str(mainnet.sign(action, NONCE, vault)["signature"]),
        str(mainnet.sign(action, NONCE + 1, None)["signature"]),
    }
    assert (
        len(sigs) == 4
    )  # подпись нельзя переиспользовать в другой сети, аккаунте или с другим nonce


@pytest.mark.parametrize(
    "action_type",
    [
        "withdraw3",
        "usdSend",
        "usdClassTransfer",
        "spotSend",
        "approveAgent",
        "sendAsset",
        "vaultTransfer",
        None,
    ],
)
def test_forbidden_actions_are_never_signed(action_type: str | None) -> None:
    signer = ActionSigner(SecretStr(TEST_KEY), is_mainnet=True)
    with pytest.raises(ForbiddenActionError):
        signer.sign({"type": action_type, "amount": "1"}, NONCE, None)


def test_key_never_exposed() -> None:
    signer = ActionSigner(SecretStr(TEST_KEY), is_mainnet=True)
    assert TEST_KEY[2:] not in repr(signer)
    assert TEST_KEY[2:] not in str(vars(signer).get("address"))
    with pytest.raises(ValueError, match="некорректный") as exc_info:
        ActionSigner(SecretStr("0xnot-a-key-" + "9" * 20), is_mainnet=True)
    assert "9" * 20 not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_order_request_validation() -> None:
    with pytest.raises(ValueError):
        OrderRequest(0, True, Decimal("0"), Decimal("1"), False, LimitOrderType(Tif.IOC))
    with pytest.raises(ValueError, match="cloid"):
        OrderRequest(
            0, True, Decimal("1"), Decimal("1"), False, LimitOrderType(Tif.IOC), cloid="0x1"
        )
    with pytest.raises(ValueError, match="пустой"):
        actions.order_action([])
    cloid = actions.new_cloid()
    assert actions.validate_cloid(cloid) == cloid
    assert actions.action_weight({"type": "order", "orders": [{}] * 80}) == 3


def test_cloids_are_unique_and_valid() -> None:
    cloids = {actions.new_cloid() for _ in range(10_000)}
    assert len(cloids) == 10_000
    assert all(actions.validate_cloid(c) == c for c in cloids)
