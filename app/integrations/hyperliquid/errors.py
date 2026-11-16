class HyperliquidError(Exception):
    """Базовая ошибка интеграции с Hyperliquid."""


class HyperliquidRequestError(HyperliquidError):
    """Биржа отклонила запрос (4xx, кроме 429) или вернула ошибку по существу."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HyperliquidRateLimitError(HyperliquidError):
    """HTTP 429 после всех повторов."""


class HyperliquidUnavailableError(HyperliquidError):
    """Сеть, таймаут или 5xx после всех повторов."""


class HyperliquidFormatError(HyperliquidError):
    """Ответ неожиданного формата. Нельзя игнорировать: это сигнал остановить новые действия."""


class UnknownMarketError(HyperliquidRequestError):
    """Инструмента нет в метаданных биржи."""
