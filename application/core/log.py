"""Structured logging: one JSON line per business event.

Never pass tokens, passwords, request bodies or emails to these helpers.
Lambda's JSON log format turns the `extra` fields into top-level JSON keys, so
CloudWatch Logs Insights can filter on action, outcome, tenant_id and user_id.
"""
import logging
from typing import Any

logger = logging.getLogger("saas")
logger.setLevel(logging.INFO)


def log_event(action: str, org_id: str, user_id: str, **fields: Any) -> None:
    logger.info(action, extra={"action": action, "outcome": "success",
                               "tenant_id": org_id, "user_id": user_id, **fields})


def log_denied(action: str, org_id: str | None, user_id: str | None, **fields: Any) -> None:
    logger.warning("access_denied", extra={"action": action, "outcome": "denied",
                                           "tenant_id": org_id, "user_id": user_id, **fields})