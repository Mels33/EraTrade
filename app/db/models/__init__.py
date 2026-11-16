"""ORM-модели. Импорт этого пакета регистрирует все таблицы в Base.metadata (нужно Alembic).

Таблицы добавляются по этапам, см. §6.6 плана.
"""

from app.db.models.control import (
    AuditLogRecord,
    BotStateRecord,
    HeartbeatRecord,
    NonceStateRecord,
)
from app.db.models.market import CandleRecord, MarketContextSnapshotRecord, MarketRecord
from app.db.models.trading import (
    AccountSnapshotRecord,
    FillRecord,
    OrderEventRecord,
    OrderRecord,
    PositionSnapshotRecord,
)

__all__ = [
    "AccountSnapshotRecord",
    "AuditLogRecord",
    "BotStateRecord",
    "CandleRecord",
    "FillRecord",
    "HeartbeatRecord",
    "MarketContextSnapshotRecord",
    "MarketRecord",
    "NonceStateRecord",
    "OrderEventRecord",
    "OrderRecord",
    "PositionSnapshotRecord",
]
