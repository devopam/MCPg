"""The Tier-B task set — database questions with **known-correct answers**.

Two kinds of task, both graded deterministically (no human or LLM judge):

**Planted-flaw audit tasks** (``missing_index``, ``pii_columns``,
``naming_violation``) — the demo dataset (``mcpg --demo``) plants these on
purpose:

- ``orders.customer_id`` is a foreign key with **no covering index** (the other
  FKs are indexed, so this one is a real finding).
- ``customers.email`` / ``customers.phone`` are **PII**.
- ``reviews."reviewSource"`` is a deliberate **camelCase naming** violation.

MCPg's purpose-built advisors (index / sensitive-column / naming) answer these
in roughly one tool call, while a bare ``run_select`` agent must run many
exploratory queries and interpret raw rows itself.

**Analytical NL→SQL tasks** (``top_revenue_category``,
``top_customer_lifetime_spend``) — plain-English business questions requiring
a real multi-table join + aggregation to answer correctly, not a lookup.
Ground truth was computed directly against the live demo dataset (not
asserted from memory) and each has a clear, unambiguous margin over the
runner-up so an approximately-right answer still passes and a wrong one still
fails. These exercise MCPg's actual NL→SQL tool (``translate_nl_to_sql``) and
are more likely than the audit tasks to provoke a wrong first SQL attempt —
the baseline arm has no tool for this beyond raw ``run_select``.

Across both kinds, the two arms diverge on tokens, tool-calls, and turns while
(ideally) both reaching the same correct answer. Graders are pure and
case-insensitive, matching on the identifying tokens of the correct answer.
They are intentionally lenient about prose and strict about the identifiers
that constitute a correct answer.
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
        Task(
            id="top_revenue_category",
            prompt="Which product category generated the most revenue overall? Name the category.",
            # Verified against the live demo dataset: "Home" leads at 95.6M
            # cents vs. the runner-up Computing's 80.4M — a clear margin, not
            # a close call sensitive to rounding.
            grade=lambda a: "home" in _norm(a),
            ideal_tools=("translate_nl_to_sql", "run_select"),
        ),
        Task(
            id="top_customer_lifetime_spend",
            prompt="Who is our single highest-spending customer by total lifetime order value? Give their name.",
            # Verified against the live demo dataset: Liam Okafor
            # (customer_id 1) leads at 12.85M cents vs. the runner-up's
            # 6.25M — more than double, not a close call.
            grade=lambda a: "liam" in _norm(a) and "okafor" in _norm(a),
            ideal_tools=("translate_nl_to_sql", "run_select"),
        ),
    ]
