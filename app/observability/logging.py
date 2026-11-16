"""Настройка Logfire и correlation_id.

correlation_id связывает цепочку свеча → признаки → прогноз → решение → риск → ордер → fills.
Он кладётся в baggage Logfire и автоматически попадает во все вложенные спаны.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import logfire

from app.config.settings import Settings

# Дополнительно к встроенным шаблонам Logfire (password, secret, private key, api key, ...).
EXTRA_SCRUB_PATTERNS = [
    "mnemonic",
    "seed[._ -]?phrase",
    "signature",
    "wallet[._ -]?key",
    "database[._ -]?url",
]

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def configure_logging(settings: Settings, *, service: str) -> None:
    logfire.configure(
        service_name=f"eratrade-{service}",
        environment=settings.environment.value,
        token=settings.logfire_token.get_secret_value() if settings.logfire_token else None,
        send_to_logfire="if-token-present",
        min_level=settings.log_level,  # type: ignore[arg-type]
        scrubbing=logfire.ScrubbingOptions(extra_patterns=EXTRA_SCRUB_PATTERNS),
        inspect_arguments=False,
    )


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def current_correlation_id() -> str | None:
    return _correlation_id.get()


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """Задаёт correlation_id для всего, что выполняется внутри блока (включая async-задачи)."""
    cid = correlation_id or new_correlation_id()
    token = _correlation_id.set(cid)
    try:
        with logfire.set_baggage(correlation_id=cid):
            yield cid
    finally:
        _correlation_id.reset(token)
