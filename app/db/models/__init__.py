"""ORM-модели. Импорт этого пакета регистрирует все таблицы в Base.metadata (нужно Alembic).

Таблицы добавляются по этапам, см. §6.6 плана.
"""

from app.db.models.api import ApiRequestRecord
from app.db.models.control import (
    AuditLogRecord,
    BotStateRecord,
    HeartbeatRecord,
    NonceStateRecord,
)
from app.db.models.market import (
    BookStatsRecord,
    CandleRecord,
    FundingHistoryRecord,
    MarketContextSnapshotRecord,
    MarketRecord,
    TradeFlowRecord,
)
from app.db.models.models import FeatureSnapshotRecord, ModelVersionRecord, SignalRecord
from app.db.models.reconciliation import (
    FundingEventRecord,
    LedgerEventRecord,
    PositionBaselineRecord,
    ReconciliationDifferenceRecord,
    ReconciliationRunRecord,
)
from app.db.models.trading import (
    AccountSnapshotRecord,
    FillRecord,
    OrderEventRecord,
    OrderRecord,
    PositionSnapshotRecord,
)

__all__ = [
    "AccountSnapshotRecord",
    "ApiRequestRecord",
    "AuditLogRecord",
    "BookStatsRecord",
    "BotStateRecord",
    "CandleRecord",
    "FeatureSnapshotRecord",
    "FillRecord",
    "FundingEventRecord",
    "FundingHistoryRecord",
    "HeartbeatRecord",
    "LedgerEventRecord",
    "MarketContextSnapshotRecord",
    "MarketRecord",
    "ModelVersionRecord",
    "NonceStateRecord",
    "OrderEventRecord",
    "OrderRecord",
    "PositionBaselineRecord",
    "PositionSnapshotRecord",
    "ReconciliationDifferenceRecord",
    "ReconciliationRunRecord",
    "SignalRecord",
    "TradeFlowRecord",
]
