"""The Tier-B task set — database questions with **known-correct answers**.

Each task is a question an agent answers by exploring the database, graded
deterministically against the flaws the demo dataset (``mcpg --demo``) plants on
purpose. That makes correctness checkable without a human or an LLM judge:

- ``orders.customer_id`` is a foreign key with **no covering index** (the other
  FKs are indexed, so this one is a real finding).
- ``customers.email`` / ``customers.phone`` are **PII**.
- ``reviews."reviewSource"`` is a deliberate **camelCase naming** violation.

The point of the study: MCPg's purpose-built advisors (index / sensitive-column
/ naming) answer these in roughly one tool call, while a bare ``run_select``
agent must run many exploratory queries and interpret raw rows itself — so the
two arms diverge on tokens, tool-calls, and turns while (ideally) both reaching
the same correct answer.

Graders are pure and case-insensitive, matching on the identifying tokens of the
planted finding. They are intentionally lenient about prose and strict about the
identifiers that constitute a correct answer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field


def _norm(text: str) -> str:
    return re.sub(r'["`\']', "", text).lower()


def _mentions_all(text: str, needles: list[str]) -> bool:
    t = _norm(text)
    return all(n.lower() in t for n in needles)


@dataclass(frozen=True)
class Task:
    """One benchmark task: a prompt + a deterministic grader over the final answer.

    ``grade`` returns True when the agent's final free-text answer names the
    planted finding. ``ideal_tools`` documents the MCPg tool(s) that answer it
    directly (for the writeup / sanity, not enforced).
    """

    id: str
    prompt: str
    grade: Callable[[str], bool]
    ideal_tools: tuple[str, ...] = field(default_factory=tuple)


def default_tasks() -> list[Task]:
    """The seed task set, grounded in the demo dataset's planted flaws."""
    return [
        Task(
            id="missing_index",
            prompt=(
                "This database has a performance problem: one foreign key column is missing an index "
                "that it should have. Identify the single table and column that most needs a new index, "
                "and state them explicitly."
            ),
            # Correct answer names orders + customer_id.
            grade=lambda a: _mentions_all(a, ["orders", "customer_id"]),
            ideal_tools=("recommend_indexes", "analyze_query_plan"),
        ),
        Task(
            id="pii_columns",
            prompt=(
                "Identify every column in this database that stores personally identifiable "
                "information (PII). List each as table.column."
            ),
            # Correct answer names both PII columns on customers.
            grade=lambda a: _mentions_all(a, ["email"]) and _mentions_all(a, ["phone"]),
            ideal_tools=("audit_database",),
        ),
        Task(
            id="naming_violation",
            prompt=(
                "This database mostly follows snake_case naming, but one column breaks the convention. "
                "Name the offending column."
            ),
            # Correct answer names the camelCase column.
            grade=lambda a: "reviewsource" in _norm(a),
            ideal_tools=("audit_database",),
        ),
    ]
