"""Идемпотентность управляющих запросов API (§13.1): повтор с тем же ключом не выполняет
команду второй раз, а возвращает сохранённый ответ."""

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ApiRequestRecord(Base):
    __tablename__ = "api_requests"

    idempotency_key: Mapped[str] = mapped_column(primary_key=True)
    command: Mapped[str]
    actor: Mapped[str]
    status_code: Mapped[int | None]
    response: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
