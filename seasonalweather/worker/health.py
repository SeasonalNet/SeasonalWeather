"""Worker-owned compatibility names for the shared health record contract."""

from ..health_records import (
    DEFAULT_HEALTH_PATH,
    MAX_HEALTH_AGE_SECONDS,
    MAX_HEALTH_BYTES,
    HealthRecordStore,
    health_path,
    read_health,
)

WorkerHealthStore = HealthRecordStore

__all__ = [
    "DEFAULT_HEALTH_PATH",
    "MAX_HEALTH_AGE_SECONDS",
    "MAX_HEALTH_BYTES",
    "WorkerHealthStore",
    "health_path",
    "read_health",
]
