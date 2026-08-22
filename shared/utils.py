"""
datetime.utcnow() is deprecated (Python 3.12+) in favor of
datetime.now(timezone.utc) — but that returns a timezone-AWARE datetime,
while everything already stored in the database (created before this fix,
and via SQLModel's default naive-datetime columns) is timezone-NAIVE.
Mixing the two raises "can't compare offset-naive and offset-aware
datetimes" the moment anything compares a fresh "now" against a stored
date_sent/created_at.

This helper gets the current UTC time without the deprecation warning,
while staying naive — matching everything already in the database. Use
this instead of datetime.utcnow() everywhere in this project.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)