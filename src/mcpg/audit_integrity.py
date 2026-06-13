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
CHAIN_TIP_TABLE = "chain_tip"
_QUALIFIED = f"{AUDIT_SCHEMA}.{AUDIT_TABLE}"
_QUALIFIED_CHAIN_TIP = f"{AUDIT_SCHEMA}.{CHAIN_TIP_TABLE}"


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

    # The chain_tip row anchors the highest (id, event_hmac) pair the
    # writer has ever signed off on. Reading it BEFORE walking events
    # avoids a race where verify reads the table mid-write and judges
    # an in-flight chain as truncated; after the walk we'll re-read to
    # confirm. The table may not exist on an old DB that pre-dates the
    # truncation-anchor work — None in that case means "verify under
    # the old contract" (no truncation detection, but no false positives).
    tip_present, tip_event_id, tip_event_hmac = await _read_chain_tip(driver)

    # Retrieve all rows sorted by ID ascending to verify sequentially
    rows = await driver.execute_query(
        f"SELECT id, occurred_at, tool, arguments, status, error, result, prev_hmac, event_hmac "
        f"FROM {_QUALIFIED} ORDER BY id ASC",
        force_readonly=True,
    )
    if not rows:
        # Empty events but a populated chain_tip → every signed row
        # was DELETEd. That's exactly the truncation attack the tip
        # exists to catch.
        if tip_present and tip_event_id is not None:
            return {
                "status": "tampered",
                "reason": (
                    f"truncation_detected: chain_tip records last_event_id={tip_event_id!r} "
                    f"but the events table is empty"
                ),
                "broken_at_id": tip_event_id,
            }
        return {
            "status": "ok",
            "reason": "No audit events recorded (table is empty).",
        }

    expected_prev_hmac = ""
    key_bytes = key_str.encode("utf-8")
    last_row_id: int | None = None
    last_event_hmac: str = ""

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
        last_row_id = row_id
        last_event_hmac = computed_hmac

    # Truncation check: if the writer recorded a chain_tip, the highest
    # row we just walked MUST match it. Any mismatch means the tail
    # was DELETEd (and no further per-row HMAC check would catch it —
    # the chain still verifies up to whatever rows remain).
    if tip_present and tip_event_id is not None:
        if last_row_id != tip_event_id or last_event_hmac != tip_event_hmac:
            return {
                "status": "tampered",
                "broken_at_id": last_row_id,
                "reason": (
                    f"truncation_detected: chain_tip records last_event_id={tip_event_id!r} "
                    f"with hmac matching writer state, but the highest event walked is "
                    f"id={last_row_id!r}"
                ),
            }
        return {"status": "ok"}
    # No tip row — typically an old DB that pre-dates this work. The
    # per-row chain still verified; surface a warning so operators know
    # they're running without the truncation-anchor.
    return {
        "status": "ok",
        "warning": (
            "no_chain_tip: mcpg_audit.chain_tip is missing or empty. "
            "Per-row HMAC chain verified, but tail-truncation cannot be "
            "detected on this database. Run any audited write to populate."
        ),
    }


async def _read_chain_tip(driver: SqlDriver) -> tuple[bool, int | None, str | None]:
    """Read the chain_tip row, tolerating an old DB that lacks the table.

    Returns ``(present, last_event_id, last_event_hmac)``.

    * ``present=False`` when the table doesn't exist or holds no row —
      :func:`verify_audit_chain` falls back to the pre-anchor contract
      with a warning so operators see the upgrade gap.
    * ``last_event_id is None`` when the row exists but no integrity
      write has happened yet (first record_audit will fill it).
    """
    tip_table_exists_rows = await driver.execute_query(
        "SELECT 1 AS present FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        params=[AUDIT_SCHEMA, CHAIN_TIP_TABLE],
        force_readonly=True,
    )
    if not tip_table_exists_rows:
        return False, None, None
    rows = await driver.execute_query(
        f"SELECT last_event_id, last_event_hmac FROM {_QUALIFIED_CHAIN_TIP} WHERE id = 1",
        force_readonly=True,
    )
    if not rows:
        return False, None, None
    cells = rows[0].cells
    return True, cells.get("last_event_id"), cells.get("last_event_hmac")
