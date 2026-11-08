"""
Timezone-aware UTC helpers. SQLModel requires timezone-aware datetime
objects (datetime.now(timezone.utc)) when binding to TIMESTAMP WITH TIME ZONE
columns in PostgreSQL.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)