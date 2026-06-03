"""pgvector analytics — heuristics on top of stored embeddings.

This module hosts vector-analytics tools that aren't search and aren't
storage tuning — they reason about the *distribution* of embeddings
already in a column:

- :func:`analyze_distance_metric` — sample rows and recommend cosine,
  L2, or inner-product based on the magnitude distribution.

Future siblings (cluster_vectors, detect_vector_outliers,
monitor_embedding_drift, cross_table_similarity) land here so the
search surface (:mod:`mcpg.textsearch`) and the storage advisors
(:mod:`mcpg.vector_tuning`, :mod:`mcpg.vector_tuner_advanced`) stay
focused.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from mcpg._vendor.sql import SqlDriver
from mcpg.extensions import extension_installed

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# Default sample size when the caller doesn't pass one. Chosen so the
# tool is cheap on big tables (a few MB of vectors) while still giving
# the magnitude-distribution heuristic a stable signal.
DEFAULT_SAMPLE_SIZE = 1000


# Below this coefficient of variation we treat the magnitudes as
# "essentially constant" — in that regime cosine / L2 / inner-product
# give the same ranking, and inner-product is the cheapest to compute.
# Above it the metric choice matters; we default to cosine because it
# normalises out magnitude (the usual right answer for embeddings
# coming from heterogeneous sources or models that don't pre-normalise).
_CV_FLAT_THRESHOLD = 0.05

# A vector with norm within this band of 1.0 is considered
# pre-normalised. Combined with a low coefficient of variation, this
# triggers the "inner-product is equivalent to cosine and cheaper"
# recommendation. Two non-trivial decimals so a near-1.0 hand-rolled
# normaliser still trips it.
_NORM_TOLERANCE = 0.02


class VectorOpsError(Exception):
    """Raised when a vector-analytics request is rejected."""


@dataclass(frozen=True, slots=True)
class DistanceMetricRecommendation:
    """The outcome of an :func:`analyze_distance_metric` call.

    ``available`` is ``False`` when the pgvector extension isn't
    installed. ``sampled_rows`` is the number of non-NULL embeddings
    actually examined (may be less than the requested ``sample_size``).
    ``mean_magnitude`` / ``magnitude_std`` describe the L2-norm
    distribution, ``magnitude_cv`` is the scale-free coefficient of
    variation (``std / mean``). ``pre_normalised`` flags the
    "everything looks like a unit vector" case where inner-product
    is preferable.
    """

    available: bool
    sampled_rows: int
    mean_magnitude: float
    magnitude_std: float
    magnitude_cv: float
    pre_normalised: bool
    recommended_metric: str
    rationale: str


def _checked(name: str, kind: str) -> str:
    if not _IDENTIFIER.match(name):
        raise VectorOpsError(f"invalid {kind} name: {name!r}")
    return name


def _quoted(name: str, kind: str) -> str:
    return f'"{_checked(name, kind)}"'


def _parse_embedding(value: Any) -> list[float] | None:
    """Coerce a pgvector cell into a list of floats, or ``None``.

    Same shape pgvector cells arrive in as :func:`mcpg.textsearch._parse_embedding`,
    but tolerant: any unparseable / unexpected value returns ``None``
    so a single bad row doesn't sink the whole analysis (the caller
    counts only the rows that parsed).
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        try:
            return [float(v) for v in value]
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        inner = value.strip().lstrip("[").rstrip("]").strip()
        if not inner:
            return None
        try:
            return [float(part) for part in inner.split(",")]
        except ValueError:
            return None
    return None


def _l2_norm(vec: list[float]) -> float:
    """Plain Euclidean norm; 0.0 for a zero vector."""
    total = 0.0
    for v in vec:
        total += v * v
    return math.sqrt(total)


def _pick_metric(mean_mag: float, cv: float) -> tuple[str, bool, str]:
    """Translate distribution stats into (metric, pre_normalised, rationale).

    The three branches are documented in the module's threshold comments.
    """
    if mean_mag == 0.0:
        return (
            "cosine",
            False,
            "All sampled embeddings have zero magnitude — column likely uninitialised; defaulting to cosine.",
        )
    pre_normalised = cv < _CV_FLAT_THRESHOLD and abs(mean_mag - 1.0) < _NORM_TOLERANCE
    if pre_normalised:
        return (
            "inner_product",
            True,
            f"Embeddings look pre-normalised (mean magnitude {mean_mag:.3f}, "
            f"CV {cv:.4f} < {_CV_FLAT_THRESHOLD}); inner-product is equivalent "
            "to cosine and cheaper.",
        )
    if cv < _CV_FLAT_THRESHOLD:
        return (
            "cosine",
            False,
            f"Magnitudes are nearly constant (CV {cv:.4f} < {_CV_FLAT_THRESHOLD}) "
            f"but not unit-norm (mean {mean_mag:.3f}); cosine and L2 give the same "
            "ranking — cosine is the safe choice.",
        )
    return (
        "cosine",
        False,
        f"Magnitudes vary substantially (CV {cv:.4f}); cosine normalises out "
        "magnitude differences from heterogeneous sources — the safe default "
        "for mixed embedding pipelines.",
    )


async def analyze_distance_metric(
    driver: SqlDriver,
    schema: str,
    table: str,
    column: str,
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> DistanceMetricRecommendation:
    """Recommend a pgvector distance metric from the embedding-magnitude distribution.

    Samples up to ``sample_size`` non-NULL rows of ``schema.table.column``,
    computes each embedding's L2 norm, and applies a small heuristic:

    - **Pre-normalised** (CV < 5%, mean ≈ 1.0): every vector is
      effectively a unit vector → inner-product is the cheapest and
      gives the same ranking as cosine.
    - **Nearly constant magnitude** but **not unit-norm**: cosine and
      L2 give the same ranking; cosine is the safer default.
    - **Variable magnitude** (CV ≥ 5%): cosine normalises out
      magnitude differences from heterogeneous sources.

    Args:
        sample_size: Cap on rows examined. Defaults to 1000 — enough
            to stabilise the CV signal without scanning huge tables.

    Raises:
        VectorOpsError: On an invalid identifier or a non-positive
            ``sample_size``.
    """
    if sample_size < 1:
        raise VectorOpsError("sample_size must be at least 1")
    if not await extension_installed(driver, "vector"):
        return DistanceMetricRecommendation(
            available=False,
            sampled_rows=0,
            mean_magnitude=0.0,
            magnitude_std=0.0,
            magnitude_cv=0.0,
            pre_normalised=False,
            recommended_metric="cosine",
            rationale="pgvector extension is not installed",
        )

    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    col = _quoted(column, "column")
    rows = await driver.execute_query(
        f"SELECT {col} AS embedding FROM {relation} WHERE {col} IS NOT NULL LIMIT %s",
        params=[sample_size],
        force_readonly=True,
    )
    magnitudes: list[float] = []
    for row in rows or []:
        vec = _parse_embedding(row.cells.get("embedding"))
        if vec is None:
            continue
        magnitudes.append(_l2_norm(vec))

    if not magnitudes:
        return DistanceMetricRecommendation(
            available=True,
            sampled_rows=0,
            mean_magnitude=0.0,
            magnitude_std=0.0,
            magnitude_cv=0.0,
            pre_normalised=False,
            recommended_metric="cosine",
            rationale=(
                f"No non-NULL embeddings found in {schema}.{table}.{column} (within the "
                f"first {sample_size} rows); defaulting to cosine. Backfill embeddings "
                "and re-run for a real recommendation."
            ),
        )

    n = len(magnitudes)
    mean_mag = sum(magnitudes) / n
    # Population variance is sufficient — this is a heuristic, not
    # statistical inference, so the Bessel correction would just be
    # noise. Guard against a flat sample (variance == 0 → CV == 0).
    variance = sum((m - mean_mag) ** 2 for m in magnitudes) / n
    std = math.sqrt(variance)
    cv = std / mean_mag if mean_mag > 0 else 0.0

    metric, pre_normalised, rationale = _pick_metric(mean_mag, cv)
    return DistanceMetricRecommendation(
        available=True,
        sampled_rows=n,
        mean_magnitude=mean_mag,
        magnitude_std=std,
        magnitude_cv=cv,
        pre_normalised=pre_normalised,
        recommended_metric=metric,
        rationale=rationale,
    )
