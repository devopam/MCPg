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
import random
import re
from collections.abc import Callable
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


# pgvector distance operators, shared across the analytics tools so the
# choice stays consistent with :mod:`mcpg.textsearch`.
_VECTOR_METRICS = {"l2": "<->", "cosine": "<=>", "inner_product": "<#>"}


async def _vector_column_dimension(driver: SqlDriver, schema: str, table: str, column: str) -> int | None:
    """Return the declared ``N`` of a ``vector(N)`` column, or ``None``.

    ``None`` means either the column doesn't exist or it isn't a
    pgvector ``vector`` type; the caller decides whether that's an error.
    Same shape as the helper :mod:`mcpg.data_movement` uses for
    ``import_vectors`` — duplicated here rather than imported to avoid
    cross-module coupling for what is a single 8-line query.
    """
    rows = await driver.execute_query(
        "SELECT t.typname AS type_name, a.atttypmod AS type_mod "
        "FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_type t ON t.oid = a.atttypid "
        "WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s AND a.attnum > 0",
        params=[schema, table, column],
        force_readonly=True,
    )
    if not rows:
        return None
    cell = rows[0].cells
    if cell.get("type_name") != "vector":
        return None
    type_mod = cell.get("type_mod")
    return int(type_mod) if isinstance(type_mod, int) and type_mod > 0 else None


def _vector_literal(vec: list[float]) -> str:
    """Format a Python embedding as a pgvector text literal (``"[v1,v2,...]"``)."""
    return "[" + ",".join(str(float(v)) for v in vec) + "]"


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


# --- cross_table_similarity -----------------------------------------------


DEFAULT_K = 10


@dataclass(frozen=True, slots=True)
class CrossTableMatch:
    """One hit from :func:`cross_table_similarity`.

    ``distance`` follows the pgvector metric semantics: smaller = closer
    for ``l2`` / ``cosine`` (where ``cosine`` is ``1 - cos(theta)``) and
    smaller (more negative) = closer for ``inner_product`` (it returns
    the negated dot product). ``row`` is the target row minus its
    embedding column.
    """

    distance: float
    row: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CrossTableSimilarityResult:
    """The outcome of a :func:`cross_table_similarity` call.

    ``available`` is ``False`` when the pgvector extension is not
    installed. ``source_embedding_found`` is ``False`` when the
    identifier value matched no row in the source table (callers can
    distinguish "not found" from "table is empty of matches").
    """

    available: bool
    source_embedding_found: bool
    source_dimension: int
    matches: list[CrossTableMatch]


async def cross_table_similarity(
    driver: SqlDriver,
    *,
    source_schema: str,
    source_table: str,
    source_embedding_column: str,
    source_id_column: str,
    source_id_value: Any,
    target_schema: str,
    target_table: str,
    target_embedding_column: str,
    k: int = DEFAULT_K,
    metric: str = "l2",
) -> CrossTableSimilarityResult:
    """Find the ``k`` rows in B most similar to a given row in A.

    Locates ``source_id_value`` in ``source_schema.source_table`` via
    ``source_id_column``, reads its embedding from
    ``source_embedding_column``, then issues a k-NN query against
    ``target_schema.target_table.target_embedding_column``.

    The source and target columns must be pgvector ``vector(N)`` of the
    same ``N`` — checked from the catalog up front so a dimension
    mismatch fails with a clear error rather than a pgvector cast error
    on the inner query.

    Useful for entity resolution / linking ("which posts most resemble
    this comment?") across tables whose embeddings come from different
    models so long as they share a dimension.

    Raises:
        VectorOpsError: For invalid identifiers, an unknown ``metric``,
            non-positive ``k``, a missing column, dimension mismatch
            between source and target, or an unreadable source row.
    """
    if metric not in _VECTOR_METRICS:
        raise VectorOpsError(f"unknown vector metric: {metric!r}")
    if k < 1:
        raise VectorOpsError("k must be at least 1")
    # Validate identifiers up front — a bad schema/table/column name
    # would otherwise route through the catalog lookup as "column
    # missing" first, hiding the real "invalid identifier" failure.
    for name, kind in (
        (source_schema, "schema"),
        (source_table, "table"),
        (source_embedding_column, "column"),
        (source_id_column, "column"),
        (target_schema, "schema"),
        (target_table, "table"),
        (target_embedding_column, "column"),
    ):
        _checked(name, kind)

    if not await extension_installed(driver, "vector"):
        return CrossTableSimilarityResult(available=False, source_embedding_found=False, source_dimension=0, matches=[])

    source_dim = await _vector_column_dimension(driver, source_schema, source_table, source_embedding_column)
    if source_dim is None:
        raise VectorOpsError(
            f"{source_schema}.{source_table}.{source_embedding_column} is not a pgvector "
            "vector(N) column (column missing or wrong type)"
        )
    target_dim = await _vector_column_dimension(driver, target_schema, target_table, target_embedding_column)
    if target_dim is None:
        raise VectorOpsError(
            f"{target_schema}.{target_table}.{target_embedding_column} is not a pgvector "
            "vector(N) column (column missing or wrong type)"
        )
    if source_dim != target_dim:
        raise VectorOpsError(
            f"vector dimension mismatch: source {source_schema}.{source_table}."
            f"{source_embedding_column} is vector({source_dim}) but target "
            f"{target_schema}.{target_table}.{target_embedding_column} is "
            f"vector({target_dim}); cross_table_similarity needs equal dimensions"
        )

    # Source-row fetch — bound id value; column names identifier-validated.
    src_relation = f"{_quoted(source_schema, 'schema')}.{_quoted(source_table, 'table')}"
    src_emb_col = _quoted(source_embedding_column, "column")
    src_id_col = _quoted(source_id_column, "column")
    src_rows = await driver.execute_query(
        f"SELECT {src_emb_col} AS embedding FROM {src_relation} WHERE {src_id_col} = %s LIMIT 1",
        params=[source_id_value],
        force_readonly=True,
    )
    if not src_rows:
        return CrossTableSimilarityResult(
            available=True, source_embedding_found=False, source_dimension=source_dim, matches=[]
        )
    embedding = _parse_embedding(src_rows[0].cells.get("embedding"))
    if embedding is None:
        raise VectorOpsError(
            f"source row {source_id_value!r} has a NULL or unparseable embedding in "
            f"{source_schema}.{source_table}.{source_embedding_column}"
        )

    # Target k-NN — bind the literal as a vector parameter, ORDER BY the
    # configured metric. SELECT * carries through whatever columns the
    # target table has; we strip the embedding column from each result
    # row so the caller doesn't get a wall of floats.
    operator = _VECTOR_METRICS[metric]
    tgt_relation = f"{_quoted(target_schema, 'schema')}.{_quoted(target_table, 'table')}"
    tgt_col = _quoted(target_embedding_column, "column")
    literal = _vector_literal(embedding)
    tgt_rows = await driver.execute_query(
        f"SELECT *, {tgt_col} {operator} %s::vector AS mcpg_distance "
        f"FROM {tgt_relation} ORDER BY {tgt_col} {operator} %s::vector LIMIT %s",
        params=[literal, literal, k],
        force_readonly=True,
    )
    matches: list[CrossTableMatch] = []
    for row in tgt_rows or []:
        cells = dict(row.cells)
        distance = cells.pop("mcpg_distance")
        cells.pop(target_embedding_column, None)  # strip the embedding column
        matches.append(CrossTableMatch(distance=distance, row=cells))
    return CrossTableSimilarityResult(
        available=True, source_embedding_found=True, source_dimension=source_dim, matches=matches
    )


# --- cluster_vectors (k-means) --------------------------------------------


DEFAULT_CLUSTER_SAMPLE_SIZE = 5000
DEFAULT_MAX_ITERATIONS = 20

# Tolerance for convergence: when the largest centroid drift between
# iterations falls below this fraction of the mean centroid norm, stop
# iterating. Cheap and accurate enough for the analytics use case.
_KMEANS_TOLERANCE = 1e-4


@dataclass(frozen=True, slots=True)
class ClusterCentroid:
    """One k-means centroid + the number of rows assigned to it."""

    cluster: int
    centroid: list[float]
    size: int


@dataclass(frozen=True, slots=True)
class ClusterAssignment:
    """The cluster a single sampled row was assigned to.

    ``id`` is the row's ``id_column`` value when one was supplied,
    otherwise the row's positional index in the sample. ``distance``
    follows the metric used for clustering (squared L2 by default,
    or 1 - cos(theta) for cosine).
    """

    id: Any
    cluster: int
    distance: float


@dataclass(frozen=True, slots=True)
class ClusterVectorsResult:
    """The outcome of a :func:`cluster_vectors` call.

    ``inertia`` is the sum of squared distances from each assigned row to
    its centroid (the classical k-means objective). ``converged`` is
    ``True`` if the centroids stopped moving before ``max_iterations``.
    ``metric`` echoes back which metric was used so a result inspected
    out-of-context is self-describing.
    """

    available: bool
    sampled_rows: int
    dimension: int
    metric: str
    iterations: int
    converged: bool
    inertia: float
    centroids: list[ClusterCentroid]
    assignments: list[ClusterAssignment]


def _squared_distance(a: list[float], b: list[float]) -> float:
    """Squared Euclidean distance. Skips the sqrt — both k-means
    assignment and convergence checks use the squared form."""
    total = 0.0
    for x, y in zip(a, b, strict=True):
        d = x - y
        total += d * d
    return total


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """``1 - cos(theta)``; 1.0 for either vector being zero (max distance).

    Cosine distance has the property that the cluster-mean centroid
    minimises it locally for normalised inputs — we normalise vectors
    up front when ``metric="cosine"``, so the standard Lloyd update
    still converges.
    """
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0
    return 1.0 - dot / math.sqrt(norm_a * norm_b)


def _normalize_in_place(vec: list[float]) -> list[float]:
    """Scale ``vec`` to unit norm in place; zero vectors pass through."""
    n = _l2_norm(vec)
    if n == 0.0:
        return vec
    inv = 1.0 / n
    for i in range(len(vec)):
        vec[i] *= inv
    return vec


def _kmeans_plus_plus_init(
    vectors: list[list[float]],
    k: int,
    rng: random.Random,
    distance: Callable[[list[float], list[float]], float],
) -> list[list[float]]:
    """k-means++ centroid seeding: pick centroids weighted by distance²."""
    centroids: list[list[float]] = [list(vectors[rng.randrange(len(vectors))])]
    while len(centroids) < k:
        # Squared distance from each point to its nearest existing centroid.
        weights = []
        total = 0.0
        for v in vectors:
            best = min(distance(v, c) for c in centroids)
            # Standard k-means++ weights points by D(x)² of their nearest
            # centroid. ``_squared_distance`` is already squared and
            # ``_cosine_distance`` is non-negative; using ``best`` directly
            # gives D² in the L2 case and a sensible cosine analogue.
            # (Earlier `best * best` was D⁴ for L2 — over-weighted outliers;
            # gemini PR #52 caught this.)
            weights.append(best)
            total += best
        if total == 0.0:
            # All remaining points coincide with existing centroids — just
            # pick uniformly to keep going.
            centroids.append(list(vectors[rng.randrange(len(vectors))]))
            continue
        # Roulette-wheel selection.
        target = rng.random() * total
        cum = 0.0
        pick = 0
        for i, w in enumerate(weights):
            cum += w
            if cum >= target:
                pick = i
                break
        centroids.append(list(vectors[pick]))
    return centroids


def _kmeans(
    vectors: list[list[float]],
    k: int,
    *,
    max_iterations: int,
    metric: str,
    seed: int,
) -> tuple[list[list[float]], list[int], list[float], int, bool, float]:
    """Run Lloyd's algorithm.

    Returns ``(centroids, labels, distances, iterations, converged, inertia)``.
    ``labels[i]`` is the cluster index for ``vectors[i]``; ``distances[i]`` is
    the distance from ``vectors[i]`` to its centroid under ``metric``.
    """
    rng = random.Random(seed)

    # Inside the hot loop, every vector and centroid is unit-norm under
    # cosine (the caller pre-normalises inputs and we re-normalise
    # centroids after each iteration), so ``1 - dot(a, b)`` matches
    # ``_cosine_distance`` at ~3x the speed by dropping the per-call
    # norm + sqrt. Gemini PR #52 flagged the original full computation
    # as redundant in this path. ``_cosine_distance`` itself stays as
    # the general-purpose helper for code outside the hot loop.
    def _cosine_distance_unit(a: list[float], b: list[float]) -> float:
        dot = 0.0
        for x, y in zip(a, b, strict=True):
            dot += x * y
        return 1.0 - dot

    distance = _cosine_distance_unit if metric == "cosine" else _squared_distance
    centroids = _kmeans_plus_plus_init(vectors, k, rng, distance)
    labels = [0] * len(vectors)
    distances = [0.0] * len(vectors)
    dim = len(vectors[0])
    converged = False
    iteration = 0
    while iteration < max_iterations:
        iteration += 1
        # Assignment step: every point goes to its closest centroid.
        max_centroid_drift = 0.0
        for i, v in enumerate(vectors):
            best_dist = math.inf
            best_label = 0
            for j, c in enumerate(centroids):
                d = distance(v, c)
                if d < best_dist:
                    best_dist = d
                    best_label = j
            labels[i] = best_label
            distances[i] = best_dist
        # Update step: each centroid becomes the mean of its members.
        new_centroids: list[list[float]] = [[0.0] * dim for _ in range(k)]
        counts = [0] * k
        for v, lbl in zip(vectors, labels, strict=True):
            counts[lbl] += 1
            row = new_centroids[lbl]
            for d in range(dim):
                row[d] += v[d]
        for j in range(k):
            if counts[j] == 0:
                # Re-seed an empty cluster on the point currently farthest
                # from its assigned centroid — a standard safety valve.
                # Zero the picked point's distance so that subsequent
                # empty clusters in the same iteration pick the next
                # farthest point rather than all collapsing onto the
                # same row (gemini PR #52 caught this).
                worst = max(range(len(vectors)), key=lambda i: distances[i])
                new_centroids[j] = list(vectors[worst])
                counts[j] = 1
                distances[worst] = 0.0
                continue
            inv = 1.0 / counts[j]
            for d in range(dim):
                new_centroids[j][d] *= inv
        if metric == "cosine":
            for c in new_centroids:
                _normalize_in_place(c)
        # Convergence check: largest per-dim drift in any centroid.
        for old, new in zip(centroids, new_centroids, strict=True):
            for od, nd in zip(old, new, strict=True):
                diff = abs(od - nd)
                if diff > max_centroid_drift:
                    max_centroid_drift = diff
        centroids = new_centroids
        if max_centroid_drift < _KMEANS_TOLERANCE:
            converged = True
            break
    inertia = sum(distances)
    return centroids, labels, distances, iteration, converged, inertia


async def cluster_vectors(
    driver: SqlDriver,
    schema: str,
    table: str,
    embedding_column: str,
    *,
    k: int,
    id_column: str | None = None,
    sample_size: int = DEFAULT_CLUSTER_SAMPLE_SIZE,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    metric: str = "l2",
    seed: int = 42,
) -> ClusterVectorsResult:
    """k-means cluster a pgvector column; return centroids + per-row labels.

    Samples up to ``sample_size`` non-NULL rows of
    ``schema.table.embedding_column``, runs k-means with ``k`` clusters
    in-process (k-means++ seeding, Lloyd updates, deterministic via
    ``seed``), and returns centroids + assignments. When ``id_column``
    is supplied each assignment carries that column's value; otherwise
    each carries its positional sample index.

    Args:
        k: Number of clusters. Must be at least 2 and at most
            ``sample_size`` / 2 (otherwise clustering is degenerate).
        metric: ``l2`` (default — squared Euclidean) or ``cosine``
            (vectors are normalised before clustering and centroids are
            re-normalised after each iteration, so the standard Lloyd
            update still converges).
        sample_size: Cap on rows examined. Default 5000.
        max_iterations: Lloyd-iteration cap (default 20). The algorithm
            stops earlier when centroids stop moving.
        seed: Deterministic random seed for k-means++ init.

    Raises:
        VectorOpsError: On invalid identifiers, unknown ``metric``,
            non-positive ``sample_size`` / ``max_iterations`` /
            ``seed`` arguments, ``k < 2``, ``k`` too large for the
            sample, or a target column that isn't pgvector.
    """
    if metric not in {"l2", "cosine"}:
        raise VectorOpsError(f"unknown metric for clustering: {metric!r}; expected 'l2' or 'cosine'")
    if sample_size < 1:
        raise VectorOpsError("sample_size must be at least 1")
    if max_iterations < 1:
        raise VectorOpsError("max_iterations must be at least 1")
    if k < 2:
        raise VectorOpsError("k must be at least 2")
    # Identifier validation before any DB contact.
    _checked(schema, "schema")
    _checked(table, "table")
    _checked(embedding_column, "column")
    if id_column is not None:
        _checked(id_column, "column")

    if not await extension_installed(driver, "vector"):
        return ClusterVectorsResult(
            available=False,
            sampled_rows=0,
            dimension=0,
            metric=metric,
            iterations=0,
            converged=False,
            inertia=0.0,
            centroids=[],
            assignments=[],
        )

    dimension = await _vector_column_dimension(driver, schema, table, embedding_column)
    if dimension is None:
        raise VectorOpsError(
            f"{schema}.{table}.{embedding_column} is not a pgvector vector(N) column (column missing or wrong type)"
        )

    relation = f"{_quoted(schema, 'schema')}.{_quoted(table, 'table')}"
    emb_col = _quoted(embedding_column, "column")
    select_clause = f"{emb_col} AS embedding"
    if id_column is not None:
        id_col = _quoted(id_column, "column")
        select_clause = f"{id_col} AS row_id, {select_clause}"
    rows = await driver.execute_query(
        f"SELECT {select_clause} FROM {relation} WHERE {emb_col} IS NOT NULL LIMIT %s",
        params=[sample_size],
        force_readonly=True,
    )

    vectors: list[list[float]] = []
    ids: list[Any] = []
    for idx, row in enumerate(rows or []):
        vec = _parse_embedding(row.cells.get("embedding"))
        if vec is None or len(vec) != dimension:
            continue
        if metric == "cosine":
            _normalize_in_place(vec)
        vectors.append(vec)
        ids.append(row.cells.get("row_id") if id_column is not None else idx)

    if len(vectors) < k * 2:
        raise VectorOpsError(
            f"not enough rows to cluster: sampled {len(vectors)} parseable embeddings "
            f"but k={k} requires at least {k * 2} (and ideally many more)"
        )

    centroids, labels, distances, iterations, converged, inertia = _kmeans(
        vectors,
        k,
        max_iterations=max_iterations,
        metric=metric,
        seed=seed,
    )

    sizes = [0] * k
    for lbl in labels:
        sizes[lbl] += 1
    centroid_objs = [ClusterCentroid(cluster=j, centroid=centroids[j], size=sizes[j]) for j in range(k)]
    assignment_objs = [
        ClusterAssignment(id=ids[i], cluster=labels[i], distance=distances[i]) for i in range(len(vectors))
    ]

    return ClusterVectorsResult(
        available=True,
        sampled_rows=len(vectors),
        dimension=dimension,
        metric=metric,
        iterations=iterations,
        converged=converged,
        inertia=inertia,
        centroids=centroid_objs,
        assignments=assignment_objs,
    )
