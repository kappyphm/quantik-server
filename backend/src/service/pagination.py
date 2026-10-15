"""Stable newest-first cursor pagination for operational history."""
import base64
import json
from sqlalchemy import and_, or_
from service.jobs import JobError


def encode(timestamp: int, identity: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([timestamp, identity], separators=(",", ":")).encode()).decode().rstrip("=")


def newest(query, model, field: str, cursor: str | None):
    column = getattr(model, field)
    query = query.order_by(None).order_by(column.desc(), model.id.desc())
    if cursor:
        try:
            if len(cursor) > 512:
                raise ValueError()
            timestamp, identity = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if type(timestamp) is not int or not 0 <= timestamp <= 9223372036854775807 or not isinstance(identity, str) or not 1 <= len(identity) <= 128:
                raise ValueError()
        except (ValueError, TypeError, UnicodeError):
            raise JobError("INVALID_CURSOR", 422) from None
        query = query.where(or_(column < timestamp, and_(column == timestamp, model.id < identity)))
    return query
