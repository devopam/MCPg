"""Audit trail verification utility.

Provides sequential verification of the HMAC-SHA256 signature chain of
persisted audit events to detect any modifications, deletions, or insertions.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC
from os import environ
from typing import Any

from mcpg._vendor.sql import SqlDriver

AUDIT_SCHEMA = "mcpg_audit"
AUDIT_TABLE = "events"
CHAIN_TIP_TABLE = "chain_tip"
_QUALIFIED = f"{AUDIT_SCHEMA}.{AUDIT_TABLE}"
_QUALIFIED_CHAIN_TIP = f"{AUDIT_SCHEMA}.{CHAIN_TIP_TABLE}"

# Batch size for the keyset-paginated walk in :func:`verify_audit_chain`.
# Each batch carries one (id, arguments jsonb, result jsonb) round-trip;
# 1000 keeps memory bounded (~ a few MB at typical row sizes) and keeps
# the round-trip count reasonable (1k calls per million rows). Tuned to
# match list_audit_events' default page size for cross-tool consistency.
_VERIFY_BATCH_SIZE = 1_000


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

    # The chain_tip row anchors the highest (id, event_hmac) the writer
    # has signed. Reading it BEFORE the walk lets us detect tail-
    # truncation: an attacker with table-write access DELETEs the most
    # recent rows; the per-row chain still verifies for whatever
    # remains, but the highest id we walk no longer matches what the
    # writer recorded. Tolerate a pre-anchor DB (no chain_tip table)
    # by returning a warning instead of false-positive tampered.
    tip_present, tip_event_id, tip_event_hmac = await _read_chain_tip(driver)

    # Walk events in keyset-paginated batches. The previous shape pulled
    # every row into the process in one SELECT — fine for the toy case,
    # but on a million-row audit table that's gigabytes of jsonb in
    # memory before the chain check even starts. Keyset pagination
    # (``WHERE id > last_id``) is index-friendly (the PRIMARY KEY does
    # the work), order-stable (id is monotonic for an append-only audit
    # surface), and bounds peak memory at one batch.
    expected_prev_hmac = ""
    key_bytes = key_str.encode("utf-8")
    last_seen_id: int = 0
    walked_anything = False

    while True:
        rows = await driver.execute_query(
            f"SELECT id, occurred_at, tool, arguments, status, error, result, prev_hmac, event_hmac "
            f"FROM {_QUALIFIED} WHERE id > %s ORDER BY id ASC LIMIT %s",
            params=[last_seen_id, _VERIFY_BATCH_SIZE],
            force_readonly=True,
        )
        if not rows:
            break
        for row in rows:
            walked_anything = True
            row_id = row.cells["id"]
            occurred_at = row.cells["occurred_at"]
            if hasattr(occurred_at, "astimezone"):
                # ``UTC`` imported at module scope — keeping it out of
                # the inner row loop saves the import-lookup cost on
                # large audit tables (sourcery review on #99).
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
            # constant-time HMAC compare — verification-side timing
            # isn't a sensitive oracle here (verify is operator-
            # initiated, not a per-request hot path), but inconsistency
            # with the rest of the codebase's hmac.compare_digest usage
            # was flagged in deep-review P2 #7 and gemini review on #99.
            if not hmac.compare_digest(current_prev_hmac, expected_prev_hmac):
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

            # constant-time HMAC compare — see comment above.
            if not hmac.compare_digest(current_event_hmac, computed_hmac):
                return {
                    "status": "tampered",
                    "broken_at_id": row_id,
                    "reason": f"Mismatch in event_hmac: expected {computed_hmac!r}, got {current_event_hmac!r}",
                }

            expected_prev_hmac = computed_hmac
            last_seen_id = row_id

        # Short batch → we reached the tail; don't issue an extra empty
        # SELECT just to discover that.
        if len(rows) < _VERIFY_BATCH_SIZE:
            break

    if not walked_anything:
        # Empty events but a populated chain_tip → every signed row was
        # DELETEd. That's exactly the truncation attack the tip exists
        # to catch.
        if tip_present and tip_event_id is not None:
            return {
                "status": "tampered",
                "broken_at_id": tip_event_id,
                "reason": (
                    f"truncation_detected: chain_tip records last_event_id={tip_event_id!r} "
                    f"but the events table is empty"
                ),
            }
        return {
            "status": "ok",
            "reason": "No audit events recorded (table is empty).",
        }
    # Truncation cross-check: when the writer recorded a chain_tip, the
    # highest row we walked MUST match it. Per-row HMAC is fine for
    # whatever survived but can't see what the attacker deleted past it.
    if tip_present and tip_event_id is not None:
        if last_seen_id != tip_event_id or not hmac.compare_digest(expected_prev_hmac, tip_event_hmac or ""):
            return {
                "status": "tampered",
                "broken_at_id": last_seen_id,
                "reason": (
                    f"truncation_detected: chain_tip records last_event_id={tip_event_id!r} "
                    f"with hmac matching writer state, but the highest event walked is id={last_seen_id!r}"
                ),
            }
        return {"status": "ok"}
    # Pre-anchor DB: per-row chain verified but truncation can't be
    # detected on this database. Operator-visible warning so they know
    # the upgrade gap without a false-positive alarm.
    return {
        "status": "ok",
        "warning": (
            "no_chain_tip: mcpg_audit.chain_tip is missing or empty. "
            "Per-row HMAC chain verified, but tail-truncation cannot be "
            "detected on this database. Run any audited write to populate."
        ),
    }


async def _read_chain_tip(driver: SqlDriver) -> tuple[bool, int | None, str | None]:
    """Read the chain_tip row, tolerating a pre-anchor DB.

    Returns ``(present, last_event_id, last_event_hmac)``:
    ``present=False`` when the table doesn't exist or holds no row,
    in which case :func:`verify_audit_chain` falls back to the
    pre-anchor contract (per-row chain only, with a warning).
    """
    tip_table_exists = await driver.execute_query(
        "SELECT 1 AS present FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = %s AND c.relname = %s",
        params=[AUDIT_SCHEMA, CHAIN_TIP_TABLE],
        force_readonly=True,
    )
    if not tip_table_exists:
        return False, None, None
    rows = await driver.execute_query(
        f"SELECT last_event_id, last_event_hmac FROM {_QUALIFIED_CHAIN_TIP} WHERE id = 1",
        force_readonly=True,
    )
    if not rows:
        return False, None, None
    cells = rows[0].cells
    return True, cells.get("last_event_id"), cells.get("last_event_hmac")
