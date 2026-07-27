#!/usr/bin/env python3
"""Static analysis of test suite — no pytest run needed."""
import ast
import os
import re
from collections import defaultdict
from pathlib import Path

TESTS_DIR = Path("/workspace/tests")
REPORT_DIR = Path("/workspace/docs/testing")
REPORT_DIR.mkdir(parents=True, exist_ok=True)

def analyze_test_file(path):
    """Analyze a single test file and return its metadata."""
    rel_path = str(path.relative_to(TESTS_DIR))
    content = path.read_text(encoding="utf-8", errors="replace")
    
    # Parse AST
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return {"path": rel_path, "error": "syntax_error", "test_count": 0, "imports": [], "classes": [], "functions": []}
    
    # Extract imports
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports.append(f"{module}.{alias.name}" if module else alias.name)
    
    # Determine type
    rel_str = rel_path.replace("\\", "/")
    if "integration" in rel_str:
        file_type = "integration"
    elif "docker_integration" in rel_str:
        file_type = "docker_integration"
    elif "presenter" in rel_str:
        file_type = "presenter"
    elif "security" in rel_str:
        file_type = "security"
    elif "web_ui" in rel_str:
        file_type = "web_ui"
    elif "workspace" in rel_str:
        file_type = "workspace"
    else:
        # Classify by imports
        integration_indicators = ["TestClient", "httpx", "WebSocket", "websocket"]
        if any(ind in " ".join(imports) for ind in integration_indicators):
            file_type = "integration"
        else:
            file_type = "unit"
    
    # Check for mock usage
    uses_mocks = any("mock" in imp.lower() for imp in imports)
    
    # Check for async
    has_async = any(isinstance(n, (ast.AsyncFunctionDef, ast.AsyncFor, ast.AsyncWith)) for n in ast.walk(tree))
    
    # Count test functions
    test_functions = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            func_body = ast.get_source_segment(content, node) or ""
            has_await = "await " in func_body
            test_functions.append({
                "name": node.name,
                "async": has_await,
                "assert_count": func_body.count("assert"),
            })
    
    # Extract class names
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    
    return {
        "path": rel_path,
        "type": file_type,
        "test_count": len(test_functions),
        "imports": imports,
        "classes": classes,
        "functions": test_functions,
        "uses_mocks": uses_mocks,
        "has_async": has_async,
        "error": None,
    }

def main():
    all_files = sorted(TESTS_DIR.rglob("*.py"))
    results = []
    errors = []
    
    for f in all_files:
        if f.name.startswith("__"):
            continue
        r = analyze_test_file(f)
        if r["error"]:
            errors.append(r)
        results.append(r)
    
    # Compute stats
    by_type = defaultdict(list)
    test_functions_total = 0
    for r in results:
        by_type[r["type"]].append(r)
        test_functions_total += r["test_count"]
    
    # Key module coverage
    modules_to_check = [
        ("agent.core.agent", "Main Agent loop"),
        ("agent.core.tool_executor", "Tool execution engine"),
        ("tools.workspace.worker", "Worker sub-agent"),
        ("tools.respond", "Respond tool"),
        ("WebAgentBridge", "Web UI bridge"),
        ("EventBus", "Event bus"),
        ("SessionConfig", "Session config"),
        ("ToolPreset", "Tool presets"),
        ("Vault", "Vault"),
        ("Permission", "Permissions"),
    ]
    
    coverage = []
    for module, label in modules_to_check:
        found = [r for r in results if any(module in imp for imp in r["imports"])]
        coverage.append({"module": label, "pattern": module, "files": [r["path"] for r in found]})
    
    # --- Build Report ---
    lines = []
    lines.append("# Test Inventory Report")
    lines.append(f"""
Generated: Static analysis
Total test files: {len(results)}
Total test functions: {test_functions_total}
Errors: {len(errors)}
""")
    
    lines.append("## 1. Summary by Type")
    lines.append("")
    lines.append(f"| Type | Files | Tests |")
    lines.append(f"|------|-------|-------|")
    for t in sorted(by_type.keys()):
        files = by_type[t]
        total = sum(f["test_count"] for f in files)
        lines.append(f"| {t} | {len(files)} | {total} |")
    lines.append("")
    
    lines.append("## 2. Complete Test File Inventory")
    lines.append("")
    lines.append("| # | File | Type | Tests | Classes | Mocks | Async |")
    lines.append("|---|------|------|-------|---------|-------|-------|")
    for i, r in enumerate(results, 1):
        classes_str = ", ".join(r["classes"][:3])
        lines.append(f"| {i} | {r['path']} | {r['type']} | {r['test_count']} | {classes_str} | {'Y' if r['uses_mocks'] else 'N'} | {'Y' if r['has_async'] else 'N'} |")
    lines.append("")
    
    lines.append("## 3. Coverage Gaps")
    lines.append("")
    lines.append("| Module | Pattern | Test Files | Status |")
    lines.append("|--------|---------|------------|--------|")
    for c in coverage:
        count = len(c["files"])
        status = "Covered" if count > 0 else "NOT COVERED"
        files_str = ", ".join(c["files"][:5]) if c["files"] else "—"
        lines.append(f"| {c['module']} | `{c['pattern']}` | {files_str} | {status} |")
    lines.append("")
    
    lines.append("## 4. Errors / Files that couldn't be parsed")
    lines.append("")
    if errors:
        for e in errors:
            lines.append(f"- {e['path']}: {e['error']}")
    else:
        lines.append("None — all files parsed successfully.")
    lines.append("")
    
    lines.append("## 5. Known Issues (from earlier pytest run)")
    lines.append("""
### Failing test: `test_token_warning_duplication.py`
- **Root cause**: Test expects `token_warning` event at index 4 (tokens=64000) but gets `token_recovery` instead.
  The `update_token_state()` method emits `token_recovery` when tokens drop below the warning threshold.
  The test's oscillating sequence [50000, 68000, 72000, 85000, 64000, 68000] triggers recovery at index 4.
  The assertion `events[0]["type"] == "token_warning"` fails.
- **Recommendation**: Update the test to accept `token_recovery` events in the recovery phase of the sequence.

### Missing dependencies blocking test collection:
- `tiktoken` — needed by `agent/core/agent.py` → `session/context_builder.py`. Missing from requirements.txt.
- `httpx2` — needed by starlette TestClient. Not installed in base environment.
- `fast-json-repair` — needed at import time by agent modules. Not in requirements.txt.

All 23 collection errors stem from these three missing deps.
""")
    
    lines.append("## 6. Test Configuration")
    lines.append("""
- `pyproject.toml`: No `[tool.pytest.ini_options]` section
- Root `conftest.py`: Not present
- Subdirectory conftest files: `tests/docker_integration/conftest.py`, `tests/security/conftest.py`
- Pytest markers: None defined
- Asyncio mode: Not configured
""")
    
    lines.append("## 7. Priority Action List")
    lines.append("""
### Immediate (unblocks suite):
1. Add `tiktoken`, `fast-json-repair`, `httpx2` to requirements.txt
2. Add `[tool.pytest.ini_options]` to pyproject.toml with `testpaths = ["tests"]`, `asyncio_mode = "auto"`, and marker definitions
3. Create root `conftest.py` for shared fixtures

### Quick wins:
4. Fix `test_token_warning_duplication.py` — update assertion to accept `token_recovery`
5. Add basic tests for `tools.respond` (the only tool module with zero coverage)
6. Add tests for `tools.workspace.worker` telemetry/metadata fields

### Medium term:
7. Add `agent.core.agent` integration test for the main agent loop
8. Add `agent.core.tool_executor` unit tests for edge cases
9. Migrate Pydantic V1 `@validator` to V2 `@field_validator`
10. Set up CI pipeline with test categorization (unit/integration/slow)
""")
    
    report = "\n".join(lines)
    
    # Write report
    report_path = REPORT_DIR / "test_inventory.md"
    report_path.write_text(report)
    print(f"Report written to {report_path}")
    print(f"Total files: {len(results)}")
    print(f"Total tests: {test_functions_total}")
    print(f"Coverage gaps: {sum(1 for c in coverage if len(c['files']) == 0)}")
    
    # Print the report to stdout
    print("\n" + "="*60)
    print(report)

if __name__ == "__main__":
    main()
