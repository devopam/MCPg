"""Audit trail verification utility.

Provides sequential verification of the HMAC-SHA256 signature chain of
persisted audit events to detect any modifications, deletions, or insertions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from os import environ
from typing import Any

from mcpg._vendor.sql import SqlDriver

AUDIT_SCHEMA = "mcpg_audit"
AUDIT_TABLE = "events"
_QUALIFIED = f"{AUDIT_SCHEMA}.{AUDIT_TABLE}"


async def verify_audit_chain(driver: SqlDriver) -> dict[str, Any]:
    """Verify the integrity of the audit events signature chain.

    Reads the audit events sequentially (ordered by id), computes the HMAC
    signatures, and checks that both `prev_hmac` and `event_hmac` are correct
    and untampered.

    Returns:
        A dict with 'status' (either 'ok' or 'tampered'), and details if tampered.
    """
    settings = getattr(driver, "settings", None)
    if settings is not None:
        key_str = settings.audit_hmac_key or ""
    else:
        key_str = environ.get("MCPG_AUDIT_HMAC_KEY", "").strip()

    if not key_str:
        return {
            "status": "error",
            "reason": "MCPG_AUDIT_HMAC_KEY environment variable is not configured.",
        }

    # Check if the audit table exists first
    table_exists_rows = await driver.execute_query(
        "SELECT 1 AS present FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        params=[AUDIT_SCHEMA, AUDIT_TABLE],
        force_readonly=True,
    )
    if not table_exists_rows:
        return {
            "status": "ok",
            "reason": "No audit events recorded (audit table does not exist).",
        }

    # Retrieve all rows sorted by ID ascending to verify sequentially
    rows = await driver.execute_query(
        f"SELECT id, occurred_at, tool, arguments, status, error, result, prev_hmac, event_hmac "
        f"FROM {_QUALIFIED} ORDER BY id ASC",
        force_readonly=True,
    )
    if not rows:
        return {
            "status": "ok",
            "reason": "No audit events recorded (table is empty).",
        }

    expected_prev_hmac = ""
    key_bytes = key_str.encode("utf-8")

    for row in rows:
        row_id = row.cells["id"]
        occurred_at = row.cells["occurred_at"]
        if hasattr(occurred_at, "astimezone"):
            from datetime import UTC

            occurred_at_str = occurred_at.astimezone(UTC).isoformat()
        elif hasattr(occurred_at, "isoformat"):
            occurred_at_str = occurred_at.isoformat()
        else:
            occurred_at_str = str(occurred_at)

        # Formulate payload exactly matches insertion
        payload_data = {
            "occurred_at": occurred_at_str,
            "tool": row.cells["tool"],
            "arguments": row.cells["arguments"],
            "status": row.cells["status"],
            "error": row.cells["error"],
            "result": row.cells["result"],
        }
        payload_bytes = json.dumps(payload_data, sort_keys=True, default=str).encode("utf-8")

        # Verify prev_hmac
        current_prev_hmac = row.cells.get("prev_hmac") or ""
        if current_prev_hmac != expected_prev_hmac:
            return {
                "status": "tampered",
                "broken_at_id": row_id,
                "reason": f"Mismatch in prev_hmac: expected {expected_prev_hmac!r}, got {current_prev_hmac!r}",
            }

        # Verify event_hmac signature
        current_event_hmac = row.cells.get("event_hmac") or ""
        if not current_event_hmac:
            return {
                "status": "tampered",
                "broken_at_id": row_id,
                "reason": "Missing event_hmac signature.",
            }

        # Compute computed_hmac
        data_to_sign = current_prev_hmac.encode("utf-8") + payload_bytes
        computed_hmac = hmac.new(key_bytes, data_to_sign, hashlib.sha256).hexdigest()

        if current_event_hmac != computed_hmac:
            return {
                "status": "tampered",
                "broken_at_id": row_id,
                "reason": f"Mismatch in event_hmac: expected {computed_hmac!r}, got {current_event_hmac!r}",
            }

        expected_prev_hmac = computed_hmac

    return {"status": "ok"}
