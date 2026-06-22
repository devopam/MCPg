"""Tool return-shape contract test — guards against silent dataclass drift.

This test closes a gap the `test_tool_surface_snapshot` doesn't cover: that
snapshot pins ``name`` / ``description`` / ``inputSchema``, but a developer
could still rename or remove a dataclass field on the underlying helper
module and the tool wrapper would happily ``asdict(...)`` the new shape
into the wire response — silently breaking the documented "Returns an
object with `field_a`, `field_b`…" contract.

The guard works by AST-walking ``src/mcpg/tools.py``:

1. For each ``@server.tool(name=...)``-decorated handler, find the body's
   ``await <module>.<helper>(...)`` call (the common pattern is
   ``result = await <module>.<helper>(...); return asdict(result)`` for
   scalars or ``items = await <module>.<helper>(...); return [asdict(i)
   for i in items]`` for lists).
2. Import the helper, inspect its return annotation, and if it's a
   ``@dataclass`` (or a ``list[Dataclass]``), capture ``(kind,
   sorted-field-names)``.
3. Diff against the checked-in snapshot
   ``tests/contract/tool_return_shapes.snapshot.json``.

Tools whose return annotation is *not* a dataclass (ad-hoc ``dict[str,
Any]`` returns, primitives, etc.) are skipped — they're tracked separately
in the snapshot as ``"opaque"`` so the snapshot still grows / shrinks
predictably when new tools land. The expected-coverage assertion below
keeps the auto-derived set from silently regressing.

**Regenerating the snapshot**: same escape hatch as the sibling snapshot
test. Set ``MCPG_REGENERATE_TOOL_RETURN_SHAPES=1`` and commit the diff
alongside the source change. The diff is the review surface — a reviewer
sees "this PR added field ``X`` to ``RepackResult``" or "this PR renamed
``rows`` to ``items`` on ``PgqRunResult``" before the change lands.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import json
import os
import typing
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TOOLS_PATH = _REPO_ROOT / "src" / "mcpg" / "tools.py"
_SNAPSHOT_PATH = Path(__file__).parent / "tool_return_shapes.snapshot.json"

# Modules whose dataclass fields are part of the public contract. Anything
# imported in ``src/mcpg/tools.py`` qualifies; the AST walk only looks at
# helper-module calls in tool bodies, so listing the namespace explicitly
# isn't strictly needed — kept here only as documentation for the reviewer.
_KNOWN_HELPER_MODULES = (
    "advisors",
    "aio",
    "audit",
    "audit_trail",
    "composite",
    "cron",
    "cursors",
    "cypher",
    "data_movement",
    "diagrams",
    "extensions",
    "graph",
    "graph_diagram",
    "graph_mgmt",
    "health",
    "indexing",
    "introspection",
    "io_stats",
    "liveops",
    "locks",
    "maintenance",
    "migration_history",
    "migrations",
    "naming",
    "nl2sql",
    "partman",
    "pg19_ddl",
    "pg19_runtime",
    "pg19_stats",
    "pg_prewarm",
    "pg_search",
    "pgq",
    "rag_efficiency",
    "rag_telemetry",
    "redis_fdw",
    "repack",
    "rls",
    "schema_diff",
    "schema_docs",
    "shell",
    "test_data",
    "textsearch",
    "timescaledb",
    "turboquant",
    "vector_ops",
    "vector_tuning",
    "walinspect",
    "workload",
    "write",
)


def _find_tool_decorator_name(decorator: ast.expr) -> str | None:
    """Return the ``name=`` kwarg of an ``@server.tool(...)`` call, if any."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    # Match both ``@server.tool(...)`` and ``@some.server.tool(...)``.
    if not (isinstance(func, ast.Attribute) and func.attr == "tool"):
        return None
    for kw in decorator.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _find_helper_call(body: list[ast.stmt]) -> tuple[str, str] | None:
    """Return ``(module_name, helper_name)`` for the first awaited
    ``<module>.<helper>(...)`` call in a tool body, or ``None`` if the
    handler doesn't follow the asdict-of-helper pattern.

    Walks the whole function body — handlers wrapped in a nested
    ``_run`` (the cached-call pattern) are found too.
    """
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if not isinstance(node, ast.Await):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        # ``module.helper(...)``
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module_name = func.value.id
            helper_name = func.attr
            if module_name in _KNOWN_HELPER_MODULES:
                return module_name, helper_name
    return None


def _classify_annotation(annotation: Any) -> tuple[str, type[Any] | None]:
    """Reduce a return annotation to ``("scalar" | "list" | "opaque", dataclass_cls)``.

    ``scalar`` means ``Dataclass``. ``list`` means ``list[Dataclass]`` /
    ``List[Dataclass]``. Everything else (``dict``, ``int``, ``bool``,
    plain ``list``) is ``opaque``.
    """
    if annotation is None or annotation is type(None):
        return "opaque", None
    origin = typing.get_origin(annotation)
    if origin is list:
        args = typing.get_args(annotation)
        if len(args) == 1 and isinstance(args[0], type) and dataclasses.is_dataclass(args[0]):
            return "list", args[0]
        return "opaque", None
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        return "scalar", annotation
    return "opaque", None


def _derive_shape(module_name: str, helper_name: str) -> dict[str, Any] | None:
    """Import the helper and reduce its return annotation to a shape record.

    Returns ``None`` when the helper is missing or has no usable
    annotation, so the caller can record it as ``opaque`` without
    crashing.
    """
    try:
        module = importlib.import_module(f"mcpg.{module_name}")
    except Exception:
        return None
    helper = getattr(module, helper_name, None)
    if helper is None:
        return None
    try:
        hints = typing.get_type_hints(helper)
    except Exception:
        return None
    ret = hints.get("return")
    kind, dataclass_cls = _classify_annotation(ret)
    if dataclass_cls is None:
        return {"kind": "opaque"}
    field_names = sorted(f.name for f in dataclasses.fields(dataclass_cls))
    return {"kind": kind, "dataclass": dataclass_cls.__name__, "fields": field_names}


def _capture_return_shapes() -> dict[str, Any]:
    """Walk ``tools.py`` and produce ``{tool_name: shape_record}`` for every
    ``@server.tool(name=...)``-decorated handler.

    Records are sorted by tool name so the snapshot diff is stable.
    """
    tree = ast.parse(_TOOLS_PATH.read_text(encoding="utf-8"))
    shapes: dict[str, Any] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            tool_name = _find_tool_decorator_name(dec)
            if tool_name is None:
                continue
            call = _find_helper_call(list(node.body))
            if call is None:
                shapes[tool_name] = {"kind": "opaque"}
                break
            module_name, helper_name = call
            shape = _derive_shape(module_name, helper_name)
            shapes[tool_name] = shape or {"kind": "opaque"}
            break
    return {
        "_meta": {
            "tool_count": len(shapes),
            "schema_version": 1,
        },
        "tools": dict(sorted(shapes.items())),
    }


def _format_canonical(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_tool_return_shapes_match_snapshot() -> None:
    """Every tool's underlying dataclass field set must match the snapshot.

    Auto-derived from ``src/mcpg/tools.py`` via AST walk. Tools whose
    handler doesn't follow the ``asdict(<module>.<helper>(...))`` pattern
    are recorded as ``{"kind": "opaque"}`` — they're tracked for census
    coverage but not field-by-field.

    Regenerate when adding / changing a tool::

        MCPG_REGENERATE_TOOL_RETURN_SHAPES=1 \\
            uv run pytest tests/contract/test_tool_return_shapes.py
    """
    captured = _capture_return_shapes()
    captured_text = _format_canonical(captured)

    if os.environ.get("MCPG_REGENERATE_TOOL_RETURN_SHAPES") == "1":
        _SNAPSHOT_PATH.write_text(captured_text, encoding="utf-8")
        pytest.skip(
            f"Regenerated {_SNAPSHOT_PATH.name} ({captured['_meta']['tool_count']} tools). "
            "Commit the diff alongside the source change."
        )

    if not _SNAPSHOT_PATH.exists():
        pytest.fail(
            f"{_SNAPSHOT_PATH} is missing. Generate it with: "
            "MCPG_REGENERATE_TOOL_RETURN_SHAPES=1 uv run pytest "
            "tests/contract/test_tool_return_shapes.py"
        )

    expected_text = _SNAPSHOT_PATH.read_text(encoding="utf-8")
    if captured_text == expected_text:
        return

    expected = json.loads(expected_text)
    expected_names = set(expected["tools"].keys())
    captured_names = set(captured["tools"].keys())
    added = sorted(captured_names - expected_names)
    removed = sorted(expected_names - captured_names)
    changed = sorted(
        name for name in expected_names & captured_names if expected["tools"][name] != captured["tools"][name]
    )

    lines = ["mcpg tool return shapes drifted from snapshot."]
    if added:
        lines.append(f"  added ({len(added)}): {', '.join(added)}")
    if removed:
        lines.append(f"  removed ({len(removed)}): {', '.join(removed)}")
    if changed:
        lines.append(f"  changed ({len(changed)}): {', '.join(changed)}")
        for name in changed[:10]:
            lines.append(f"    {name}: {expected['tools'][name]} -> {captured['tools'][name]}")
    lines.append("")
    lines.append("If this is intentional, regenerate the snapshot:")
    lines.append("  MCPG_REGENERATE_TOOL_RETURN_SHAPES=1 uv run pytest tests/contract/test_tool_return_shapes.py")
    lines.append("…and commit the resulting tool_return_shapes.snapshot.json diff.")
    pytest.fail("\n".join(lines))


_MIN_COVERAGE_RATIO = 0.75


def test_auto_derived_coverage_stays_high() -> None:
    """Sanity gate — the auto-derivation should classify the majority of
    tools' return shapes.

    The AST walk skips tool handlers that don't follow the
    ``asdict(<module>.<helper>(...))`` pattern (e.g. generators that
    return ad-hoc ``dict`` payloads, code-emitting tools like the ORM
    generators, etc.). The threshold is currently **75%** — at the time
    this test landed, real coverage was ~80%, so 75% gives normal-PR
    breathing room while still flagging if the trend reverses.

    When a follow-up sweep refactors more handlers onto the
    asdict-of-helper pattern, lift this threshold to lock the win in.
    """
    captured = _capture_return_shapes()
    tools = captured["tools"]
    if not tools:
        pytest.fail("no tools captured — AST walk likely broken")
    typed = sum(1 for shape in tools.values() if shape.get("kind") in {"scalar", "list"})
    ratio = typed / len(tools)
    assert ratio >= _MIN_COVERAGE_RATIO, (
        f"return-shape derivation covers only {typed}/{len(tools)} tools ({ratio:.0%}); "
        f"the snapshot's contract value is shrinking below the {_MIN_COVERAGE_RATIO:.0%} "
        "floor. Either refactor the offending tools to the asdict-of-helper pattern, "
        "or lower this threshold deliberately."
    )
