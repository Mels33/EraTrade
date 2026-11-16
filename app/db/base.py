from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, MetaData, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

# Предсказуемые имена ограничений, чтобы Alembic генерировал стабильные миграции.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Время — только timestamptz; деньги — NUMERIC без ограничения точности;
    # сырые ответы биржи — JSONB.
    type_annotation_map = {  # noqa: RUF012 — так принято в SQLAlchemy
        datetime: DateTime(timezone=True),
        Decimal: Numeric(asdecimal=True),
        str: Text(),
        dict[str, Any]: JSONB(),
    }
