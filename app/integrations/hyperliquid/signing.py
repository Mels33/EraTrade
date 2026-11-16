"""Подпись L1-действий ключом API-кошелька (через официальный SDK).

Белый список (§9.2 плана): подписываются только торговые действия. Вывод средств,
переводы и управление кошельками в торговом сервисе невозможны даже при ошибке в коде.
"""

from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from hyperliquid.utils.signing import sign_l1_action
from pydantic import SecretStr

ALLOWED_ACTIONS = frozenset(
    {"order", "cancel", "cancelByCloid", "updateLeverage", "scheduleCancel"}
)


class ForbiddenActionError(PermissionError):
    """Попытка подписать действие вне белого списка."""


class ActionSigner:
    def __init__(self, private_key: SecretStr, *, is_mainnet: bool) -> None:
        try:
            self._wallet: LocalAccount = Account.from_key(private_key.get_secret_value())
        except Exception:  # текст исключения мог бы содержать фрагменты ключа
            raise ValueError("некорректный приватный ключ API-кошелька") from None
        self.is_mainnet = is_mainnet
        self.address = self._wallet.address.lower()

    def __repr__(self) -> str:
        network = "mainnet" if self.is_mainnet else "testnet"
        return f"ActionSigner(address={self.address}, network={network})"

    def sign(
        self,
        action: dict[str, Any],
        nonce: int,
        vault_address: str | None,
        expires_after: int | None = None,
    ) -> dict[str, Any]:
        """Готовый payload для POST /exchange."""
        action_type = action.get("type")
        if action_type not in ALLOWED_ACTIONS:
            raise ForbiddenActionError(f"действие {action_type!r} запрещено для торгового сервиса")
        signature = sign_l1_action(
            self._wallet, action, vault_address, nonce, expires_after, self.is_mainnet
        )
        return {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": vault_address,
            "expiresAfter": expires_after,
        }
