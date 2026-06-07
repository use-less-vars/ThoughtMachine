"""
Startup health check for ThoughtMachine agent.

Performs a series of pre-flight checks before the agent starts to ensure:
  1. Config files exist and are readable / valid JSON
  2. Required directories (logs, workspace, etc.) are reachable or creatable
  3. API key environment variables or config values are present
  4. Provider configuration is coherent

Usage:
    from agent.startup_health_check import run_health_check, HealthReport

    report = run_health_check()
    if not report.ok:
        print(report.summary())
        # optionally abort startup
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from agent.logging import log
from agent.config.loader import (
    load_config,
    load_default_config,
    migrate_config,
    get_config_paths,
)


# ══════════════════════════════════════════════════════════════════════════════
#  Health check primitives
# ══════════════════════════════════════════════════════════════════════════════


class CheckResult:
    """Result of a single health check item."""

    def __init__(self, name: str, passed: bool, message: str = ''):
        self.name = name
        self.passed = passed
        self.message = message

    def __repr__(self) -> str:
        return f'[{"PASS" if self.passed else "FAIL"}] {self.name}: {self.message}'


class HealthReport:
    """Aggregated health check report."""

    def __init__(self):
        self.checks: List[CheckResult] = []

    @property
    def ok(self) -> bool:
        """True if *all* checks passed."""
        return all(c.passed for c in self.checks)

    @property
    def passed(self) -> List[CheckResult]:
        return [c for c in self.checks if c.passed]

    @property
    def failed(self) -> List[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def add(self, name: str, passed: bool, message: str = '') -> None:
        self.checks.append(CheckResult(name, passed, message))

    def summary(self, verbose: bool = False) -> str:
        """Human-readable summary."""
        lines = [
            f'Health check: {"ALL PASSED" if self.ok else f"{len(self.failed)} FAILED"} '
            f'({len(self.checks)} checks)',
        ]
        for c in self.checks:
            if not c.passed or verbose:
                lines.append(f'  {"✓" if c.passed else "✗"} {c.name}: {c.message}')
        return '\n'.join(lines)

    def __bool__(self) -> bool:
        return self.ok


# ══════════════════════════════════════════════════════════════════════════════
#  Individual checks
# ══════════════════════════════════════════════════════════════════════════════


def _check_config_paths(report: HealthReport) -> None:
    """Verify config file paths are accessible."""
    paths = get_config_paths()
    for label, path in paths.items():
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    raw = f.read()
                if not raw.strip():
                    report.add(
                        f'Config file exists but is empty: {label}',
                        False, f'{path} is empty (will use defaults)',
                    )
                else:
                    json.loads(raw)  # validate JSON
                    report.add(
                        f'Config file readable: {label}',
                        True, f'{path} ({len(raw)} bytes)',
                    )
            except json.JSONDecodeError as e:
                report.add(
                    f'Config file corrupted: {label}',
                    False,
                    f'{path}: {e}',
                )
            except (IOError, OSError) as e:
                report.add(
                    f'Config file unreadable: {label}',
                    False, f'{path}: {e}',
                )
        else:
            report.add(
                f'Config file missing: {label}',
                True,  # Not a hard failure — defaults will be used
                f'{path} not found (defaults will be used)',
            )


def _check_config_valid(report: HealthReport) -> None:
    """Verify config can be loaded and migrated successfully."""
    paths = get_config_paths()
    for label, path in paths.items():
        if not os.path.exists(path):
            continue
        try:
            cfg = load_config(path)
            if not isinstance(cfg, dict):
                report.add(
                    f'Config validates: {label}',
                    False, f'load_config returned {type(cfg).__name__}',
                )
                continue
            migrated = migrate_config(cfg)
            report.add(
                f'Config migration: {label}',
                True,
                f'{len(migrated)} fields, {len(cfg)} after migration',
            )
        except Exception as e:
            report.add(
                f'Config load failed: {label}',
                False, f'{path}: {e}',
            )


def _check_api_key(report: HealthReport) -> None:
    """Check for at least one configured API key (env var or config)."""
    keys_found = []
    for var in ('OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'ANTHROPIC_API_KEY'):
        if os.getenv(var):
            keys_found.append(var)

    paths = get_config_paths()
    config_key = None
    for path in paths.values():
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                if data.get('api_key'):
                    config_key = '<present in config>'
            except Exception:
                pass

    all_keys = keys_found + ([config_key] if config_key else [])
    if all_keys:
        report.add(
            'API key present',
            True,
            f'Found: {", ".join(all_keys)}',
        )
    else:
        report.add(
            'API key missing',
            False,
            'No API key found in environment or config files. '
            'Set OPENAI_API_KEY, DEEPSEEK_API_KEY, or ANTHROPIC_API_KEY.',
        )


def _check_directories(report: HealthReport) -> None:
    """Verify required directories exist or can be created."""
    dirs_to_check = {
        'Log directory': './logs',
        'Config backups': '.config_backups',
        'Knowledge base': '.thoughtmachine/knowledge',
    }

    # Also check workspace_path from config if available
    try:
        cfg = load_default_config()
        if cfg.get('workspace_path'):
            dirs_to_check['Workspace'] = cfg['workspace_path']
    except Exception:
        pass

    for label, path in dirs_to_check.items():
        try:
            os.makedirs(path, exist_ok=True)
            report.add(
                f'Directory accessible: {label}',
                True, path,
            )
        except (IOError, OSError, PermissionError) as e:
            report.add(
                f'Directory inaccessible: {label}',
                False, f'{path}: {e}',
            )


def _check_python_version(report: HealthReport) -> None:
    """Verify Python version meets minimum requirements."""
    min_version = (3, 9)
    current = sys.version_info[:2]
    if current >= min_version:
        report.add(
            'Python version',
            True,
            f'{sys.version.split()[0]} (>= {min_version[0]}.{min_version[1]})',
        )
    else:
        report.add(
            'Python version',
            False,
            f'{sys.version.split()[0]} (< {min_version[0]}.{min_version[1]})',
        )


def _check_imports(report: HealthReport) -> None:
    """Verify critical imports resolve without error."""
    critical_modules = [
        ('pydantic', 'pydantic.BaseModel'),
    ]
    for mod_name, test_import in critical_modules:
        try:
            __import__(mod_name)
            report.add(
                f'Import: {mod_name}',
                True, f'{test_import} resolved',
            )
        except ImportError as e:
            report.add(
                f'Import: {mod_name}',
                False, str(e),
            )


# ══════════════════════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════════════════════


def run_health_check(
    checks: Optional[List[str]] = None,
    verbose: bool = False,
    exit_on_fail: bool = False,
) -> HealthReport:
    """Run the full startup health check suite.

    Args:
        checks: Optional list of check names to run (default: all).
                Known names: ``config_paths``, ``config_valid``, ``api_key``,
                ``directories``, ``python_version``, ``imports``.
        verbose: If True, include passing checks in log output.
        exit_on_fail: If True, call ``sys.exit(1)`` on failure.

    Returns:
        ``HealthReport`` with per-check results.
    """
    ALL_CHECKS = {
        'config_paths': _check_config_paths,
        'config_valid': _check_config_valid,
        'api_key': _check_api_key,
        'directories': _check_directories,
        'python_version': _check_python_version,
        'imports': _check_imports,
    }

    if checks is None:
        checks = list(ALL_CHECKS.keys())

    report = HealthReport()

    for name in checks:
        func = ALL_CHECKS.get(name)
        if func is None:
            report.add(name, False, f'Unknown check name — skipped')
            continue
        try:
            func(report)
        except Exception as e:
            report.add(name, False, f'Check raised exception: {e}')

    # Log results
    for c in report.checks:
        if not c.passed:
            log('WARNING', 'startup_health_check', c.message)
        elif verbose:
            log('INFO', 'startup_health_check', c.message)

    if not report.ok:
        log('WARNING', 'startup_health_check',
            f'{len(report.failed)} health check(s) FAILED — review warnings above')

    if exit_on_fail and not report.ok:
        log('ERROR', 'startup_health_check', 'Aborting startup due to health check failure')
        sys.exit(1)

    return report


if __name__ == '__main__':
    """CLI usage: python -m agent.startup_health_check"""
    report = run_health_check(verbose=True)
    print(report.summary(verbose=True))
    if not report.ok:
        sys.exit(1)
