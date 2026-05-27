#!/usr/bin/env python3
"""MCPg Expert AI Local PR Review System.

This script parses git changes against the 'main' branch, performs AST-driven
semantic reviews on modified code, and prints a detailed quality and compliance
report. It can be run at any time via:
    uv run scratch/pr_review.py
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from typing import NamedTuple

# Reconfigure stdout to use UTF-8 on Windows consoles to prevent UnicodeEncodeError
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass


class Finding(NamedTuple):
    file_path: str
    line_number: int
    rule_id: str
    severity: str
    message: str
    code_snippet: str


def get_modified_files() -> list[str]:
    """Get all modified and untracked files compared to 'main'."""
    try:
        # Run git diff against main
        result = subprocess.run(
            ["git", "diff", "main", "--name-only"],
            capture_output=True,
            text=True,
            check=True,
        )
        files = [f.strip() for f in result.stdout.splitlines() if f.strip()]

        # Add untracked files as well
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            check=True,
        )
        for f in untracked.stdout.splitlines():
            f_strip = f.strip()
            if f_strip and f_strip not in files:
                files.append(f_strip)

        return files
    except Exception as exc:
        print(f"[!] Failed to get git diff: {exc}")
        return []


def analyze_file(file_path: str) -> list[Finding]:
    """Perform deep static analysis and AST checks on a single Python file."""
    findings: list[Finding] = []

    if not file_path.endswith(".py") or not os.path.exists(file_path):
        return findings

    with open(file_path, encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()

    # Rule 1: Line length limit (120 chars)
    for idx, line in enumerate(lines):
        line_num = idx + 1
        if len(line) > 120 and "http" not in line and "SELECT" not in line:
            findings.append(
                Finding(
                    file_path=file_path,
                    line_number=line_num,
                    rule_id="line-too-long",
                    severity="info",
                    message=f"Line is too long ({len(line)} > 120 chars)",
                    code_snippet=line.strip(),
                )
            )

    # AST-driven Semantic Checks
    try:
        tree = ast.parse(content, filename=file_path)
    except SyntaxError as err:
        findings.append(
            Finding(
                file_path=file_path,
                line_number=err.lineno or 1,
                rule_id="syntax-error",
                severity="critical",
                message=f"Python syntax error: {err.msg}",
                code_snippet="",
            )
        )
        return findings

    # Walk AST
    for node in ast.walk(tree):
        # Rule 2: psycopg3 execute_query parameter type check
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute_query":
            if len(node.args) >= 2:
                param_node = node.args[1]
                # Check if parameters are passed as a tuple
                if isinstance(param_node, ast.Tuple):
                    findings.append(
                        Finding(
                            file_path=file_path,
                            line_number=node.lineno,
                            rule_id="psycopg3-tuple-params",
                            severity="error",
                            message="psycopg3 driver expects params to be a list or None, not a tuple",
                            code_snippet=f"execute_query(..., {ast.unparse(param_node)})",
                        )
                    )

        # Rule 3: Safe iteration over potentially None execute_query results
        if isinstance(node, ast.For):
            # Check if we are iterating over a variable that doesn't safeguard None
            iter_unparsed = ast.unparse(node.iter)
            # If the loop iterates over a standard variable without 'or []' or 'or ()'
            if (
                any(x in iter_unparsed for x in ("rows", "label_rows", "graph_rows", "v_rows", "e_rows"))
                and "or" not in iter_unparsed
                and "COALESCE" not in iter_unparsed
            ):
                findings.append(
                    Finding(
                        file_path=file_path,
                        line_number=node.lineno,
                        rule_id="unsafe-none-iteration",
                        severity="warning",
                        message="iterating over a DB query result that can be None; append 'or []' for safety",
                        code_snippet=f"for ... in {iter_unparsed}:",
                    )
                )

        # Rule 4: Gated DDL check
        if isinstance(node, ast.FunctionDef) and node.name in ("create_graph", "drop_graph"):
            body_src = ast.unparse(node)
            if "check_permission" not in body_src and "is_permitted" not in body_src:
                findings.append(
                    Finding(
                        file_path=file_path,
                        line_number=node.lineno,
                        rule_id="missing-ddl-gate",
                        severity="error",
                        message="graph management DDL function is missing standard permission capability gates",
                        code_snippet=f"def {node.name}(...):",
                    )
                )

    return findings


def check_test_coverage(modified_files: list[str]) -> list[Finding]:
    """Verify that corresponding unit tests exist for all new or modified source files."""
    findings: list[Finding] = []

    for f in modified_files:
        if f.startswith("src/mcpg/") and f.endswith(".py") and "__init__" not in f and "__main__" not in f:
            basename = os.path.basename(f)
            test_name = f"test_{basename}"
            test_path = os.path.join("tests/unit", test_name)

            if not os.path.exists(test_path):
                findings.append(
                    Finding(
                        file_path=f,
                        line_number=1,
                        rule_id="missing-unit-test",
                        severity="warning",
                        message=f"Corresponding unit test {test_path} does not exist",
                        code_snippet="",
                    )
                )

    return findings


def generate_report(files: list[str], findings: list[Finding]) -> str:
    """Format review results into a gorgeous Markdown review report."""
    severity_icons = {
        "critical": "[🚨 CRITICAL]",
        "error": "[❌ ERROR]",
        "warning": "[⚠️ WARNING]",
        "info": "[ℹ️ INFO]",  # noqa: RUF001
    }

    report = []
    report.append("# [Review] MCPg Local Expert AI PR Review Report\n")
    report.append(
        "This automated review checks proposed modifications against strict "
        "PostgreSQL, psycopg3, asyncio, and capability-gating architectural patterns.\n"
    )
    # Metrics Summary
    by_severity = {"critical": 0, "error": 0, "warning": 0, "info": 0}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1

    status_icon = "[🟢]"
    status_msg = "CLEAN & COMPLIANT"
    if by_severity["critical"] > 0 or by_severity["error"] > 0:
        status_icon = "[🔴]"
        status_msg = "ACTION REQUIRED: ISSUES FOUND"
    elif by_severity["warning"] > 0:
        status_icon = "[🟡]"
        status_msg = "POTENTIAL ISSUES FOR REVIEW"

    report.append("## Summary Metrics\n")
    report.append(f"* **Review Status:** {status_icon} **{status_msg}**")
    report.append(f"* **Files Analyzed:** {len(files)}")
    report.append(f"* **Total Findings:** {len(findings)}")
    report.append(f"  * **Critical:** {by_severity['critical']}")
    report.append(f"  * **Errors:** {by_severity['error']}")
    report.append(f"  * **Warnings:** {by_severity['warning']}")
    report.append(f"  * **Infos:** {by_severity['info']}\n")

    # Files List
    report.append("### Files Checked:\n")
    for f in sorted(files):
        report.append(f"- `{f}`")
    report.append("\n---\n")

    # Findings Details
    report.append("## Detailed Findings\n")
    if not findings:
        report.append(
            "[*] Perfect! No issues were identified in any of the modified files. "
            "All architectural gates, psycopg3 patterns, and safety iteration rules are fully compliant."
        )
    else:
        # Group by file
        grouped: dict[str, list[Finding]] = {}
        for f in findings:
            grouped.setdefault(f.file_path, []).append(f)

        for path, file_findings in sorted(grouped.items()):
            report.append(f"### [{os.path.basename(path)}](file:///{os.path.abspath(path)})\n")
            for f in sorted(file_findings, key=lambda x: x.line_number):
                icon = severity_icons.get(f.severity, "[INFO]")
                report.append(f"#### {icon} | Line {f.line_number} (`{f.rule_id}`)\n")
                report.append(f"> **Message:** {f.message}")
                if f.code_snippet:
                    report.append(f">\n> ```python\n> {f.code_snippet}\n> ```")
                report.append("")

    return "\n".join(report)


def main() -> None:
    print("[*] Initializing Local PR Review Analysis...")
    files = get_modified_files()
    if not files:
        print("[*] No modified files found in this branch compared to 'main'.")
        sys.exit(0)

    findings: list[Finding] = []

    # 1. Perform static analysis on files
    for f in files:
        findings.extend(analyze_file(f))

    # 2. Check test coverage
    findings.extend(check_test_coverage(files))

    # 3. Generate report
    report = generate_report(files, findings)

    # Print to console safely
    print("\n" + "=" * 60)
    try:
        print(report)
    except Exception:
        # Fallback to ascii replacement print if terminal is completely hostile
        print(report.encode("ascii", errors="replace").decode("ascii"))
    print("=" * 60 + "\n")

    # Write report artifact to scratch / workspace
    os.makedirs("scratch", exist_ok=True)
    report_path = "scratch/pr_review_report.md"
    with open(report_path, "w", encoding="utf-8") as rf:
        rf.write(report)
    print("[+] Report saved successfully to: scratch/pr_review_report.md")

    # Return exit code based on critical or error findings
    has_errors = any(f.severity in ("critical", "error") for f in findings)
    sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main()
