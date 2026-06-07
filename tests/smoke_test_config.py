#!/usr/bin/env python3
"""
Smoke test for the config pipeline using a real provider file.

This script creates a temporary ``providers.json`` file (no real API keys),
runs the full config load → resolve → validate → save pipeline,
and reports results.  It is meant to be run standalone (not via pytest).

Usage:
    python tests/smoke_test_config.py

Exit code:
    0  → all checks passed
    1  → one or more checks failed
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Dict, Any, Optional, List

# Add project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ══════════════════════════════════════════════════════════════════════════════
# Test helpers
# ══════════════════════════════════════════════════════════════════════════════

class SmokeResult:
    """Collects smoke test results."""
    def __init__(self):
        self.checks: List[Dict[str, Any]] = []
        self.failures = 0

    def check(self, name: str, passed: bool, detail: str = ""):
        self.checks.append({"name": name, "passed": passed, "detail": detail})
        if not passed:
            self.failures += 1
        status = "✅" if passed else "❌"
        print(f"  {status}  {name}")
        if detail and not passed:
            print(f"        {detail}")

    def summary(self) -> str:
        total = len(self.checks)
        passed = total - self.failures
        return f"\n{'='*50}\nResults: {passed}/{total} passed, {self.failures} failed\n"


# ══════════════════════════════════════════════════════════════════════════════
# Smoke test
# ══════════════════════════════════════════════════════════════════════════════

def run_smoke_test() -> bool:
    """Run all smoke test checks. Returns True if all passed."""
    result = SmokeResult()
    print("=" * 50)
    print("Config Pipeline Smoke Test")
    print("=" * 50)

    # ── Create temp workspace ────────────────────────────────────────────────
    tmp_dir = tempfile.mkdtemp(prefix="smoke_test_")
    thoughtmachine_dir = Path(tmp_dir) / ".thoughtmachine"
    thoughtmachine_dir.mkdir(parents=True, exist_ok=True)

    old_home = os.environ.get("HOME")
    os.environ["HOME"] = tmp_dir

    try:
        # ── 1. Create a providers.json file ─────────────────────────────────
        providers_file = thoughtmachine_dir / "providers.json"
        providers_data = {
            "profiles": [
                {
                    "id": "smoke-openai",
                    "label": "Smoke Test OpenAI",
                    "provider_type": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-smoke-test-key",
                    "default_model": "gpt-4",
                },
                {
                    "id": "smoke-deepseek",
                    "label": "Smoke Test DeepSeek",
                    "provider_type": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-smoke-deepseek-key",
                    "default_model": "deepseek-chat",
                },
            ],
            "active_profile_id": "smoke-openai",
        }
        providers_file.write_text(json.dumps(providers_data, indent=2))
        result.check(
            "Create providers.json",
            providers_file.exists(),
            f"File not found: {providers_file}",
        )

        # ── 2. Import modules ──────────────────────────────────────────────
        try:
            from agent.config.provider_profile import ProviderManager, ProviderProfile
            from agent.config.models import AgentConfig
            from agent.config.loader import validate_config, load_config, save_config
            from agent.utils import deep_merge
            result.check("Import modules", True)
        except ImportError as e:
            result.check("Import modules", False, str(e))
            # Cannot continue without imports
            return False

        # ── 3. Load provider manager ───────────────────────────────────────
        try:
            manager = ProviderManager()
            result.check("ProviderManager created", True)
        except Exception as e:
            result.check("ProviderManager created", False, str(e))
            return False

        # ── 4. List profiles ──────────────────────────────────────────────
        profiles = manager.list_profiles()
        result.check(
            "List profiles",
            len(profiles) == 2,
            f"Expected 2 profiles, got {len(profiles)}: {[p.id for p in profiles]}",
        )

        # ── 5. Check active profile ────────────────────────────────────────
        active = manager.get_active_profile()
        result.check(
            "Active profile loaded",
            active is not None and active.id == "smoke-openai",
            f"Expected smoke-openai, got {active.id if active else None}",
        )

        # ── 6. Resolve config with model_override ──────────────────────────
        resolved = manager.resolve_config({
            "provider_id": "smoke-openai",
            "model": "gpt-3.5-turbo",
            "model_override": "gpt-4-turbo",
        })
        result.check(
            "resolve_config: model_override wins",
            resolved.get("model") == "gpt-4-turbo",
            f"Expected gpt-4-turbo, got {resolved.get('model')!r}",
        )

        # ── 7. Resolve config — user model preserved ──────────────────────
        resolved2 = manager.resolve_config({
            "provider_id": "smoke-openai",
            "model": "gpt-3.5-turbo",
        })
        result.check(
            "resolve_config: user model preserved",
            resolved2.get("model") == "gpt-3.5-turbo",
            f"Expected gpt-3.5-turbo, got {resolved2.get('model')!r}",
        )

        # ── 8. Resolve config — default model fallback ────────────────────
        resolved3 = manager.resolve_config({
            "provider_id": "smoke-openai",
        })
        result.check(
            "resolve_config: default model fallback",
            resolved3.get("model") == "gpt-4",
            f"Expected gpt-4 (default_model), got {resolved3.get('model')!r}",
        )

        # ── 9. Resolve config — provider fields overwritten ────────────────
        resolved4 = manager.resolve_config({
            "provider_id": "smoke-openai",
            "base_url": "https://stale.url",
            "api_key": "stale-key",
        })
        result.check(
            "resolve_config: provider fields overwritten",
            resolved4.get("base_url") == "https://api.openai.com/v1"
            and resolved4.get("api_key") == "sk-smoke-test-key",
            f"Expected openai values, got base_url={resolved4.get('base_url')!r}, "
            f"api_key={resolved4.get('api_key')!r}",
        )

        # ── 10. Switch provider — stale values cleared ────────────────────
        resolved5 = manager.resolve_config({
            "provider_id": "smoke-deepseek",
            "base_url": "https://api.openai.com/v1",  # stale
            "api_key": "sk-smoke-test-key",            # stale
        })
        result.check(
            "resolve_config: provider switch clears stale values",
            resolved5.get("base_url") == "https://api.deepseek.com/v1"
            and resolved5.get("api_key") == "sk-smoke-deepseek-key"
            and resolved5.get("provider_type") == "deepseek",
            f"Expected deepseek values, got base_url={resolved5.get('base_url')!r}, "
            f"api_key={resolved5.get('api_key')!r}",
        )

        # ── 11. validate_config ────────────────────────────────────────────
        valid = validate_config({
            "enabled_tools": ["FilePreviewTool"],
            "model": "gpt-4",
            "session_permissions": {"filesystem": "read"},
        })
        result.check(
            "validate_config: valid config returns AgentConfig",
            valid is not None and valid.model == "gpt-4",
            f"Expected AgentConfig, got {type(valid).__name__ if valid else None}",
        )

        invalid = validate_config({"enabled_tools": "not_a_list"})
        result.check(
            "validate_config: invalid config returns None",
            invalid is None,
            f"Expected None for invalid config, got {type(invalid).__name__}",
        )

        # ── 12. deep_merge ─────────────────────────────────────────────────
        merged = deep_merge(
            {"a": {"x": 1, "y": 2}},
            {"a": {"x": 99}},
        )
        result.check(
            "deep_merge: nested dict merge preserves unmentioned keys",
            merged == {"a": {"x": 99, "y": 2}},
            f"Expected preserved 'y', got {merged!r}",
        )

        merged_none = deep_merge(
            {"keep": "me", "remove": {"nested": "value"}},
            {"remove": None},
        )
        result.check(
            "deep_merge: None overlay removes key",
            "remove" not in merged_none and merged_none.get("keep") == "me",
            f"Expected 'remove' gone, got {merged_none!r}",
        )

        # ── 13. Save config to file ────────────────────────────────────────
        config_path = str(thoughtmachine_dir / "agent_config.json")
        test_config = AgentConfig(
            provider_id="smoke-openai",
            provider_type="openai",
            model="gpt-4",
            enabled_tools=["FilePreviewTool", "GlobTool"],
            session_permissions={"filesystem": "read", "network": False},
            system_prompt="You are a smoke test agent.",
        )
        save_config(test_config.model_dump(), config_path)
        result.check(
            "save_config: file written",
            os.path.exists(config_path),
            f"Config file not found at {config_path}",
        )

        # ── 14. Load config from file ─────────────────────────────────────
        loaded = load_config(str(config_path) if not isinstance(config_path, str) else config_path)
        result.check(
            "load_config: returns AgentConfig",
            loaded is not None,
            "load_config returned None",
        )
        if loaded is not None:
            result.check(
                "load_config: model preserved",
                loaded["model"] == "gpt-4",
                f"Expected gpt-4, got {loaded['model']!r}",
            )
            result.check(
                "load_config: session_permissions preserved",
                loaded.get("session_permissions", {}).get("filesystem") == "read"
                and loaded.get("session_permissions", {}).get("network") == False,
                f"session_permissions mismatch: {loaded.get('session_permissions')}",
            )

        # ── 15. AgentConfig.resolve_from_profile ──────────────────────────
        if loaded is not None:
            cfg = AgentConfig(**loaded)
            resolved = cfg.resolve_from_profile(manager)
            result.check(
                "resolve_from_profile: model preserved",
                resolved.model == "gpt-4",
                f"Expected gpt-4 (user model), got {resolved.model!r}",
            )

            # Simulate model_override
            # Remove model/model_override from loaded dict so we can override them
            loaded_clean = {k: v for k, v in loaded.items()
                           if k not in ("model", "model_override")}
            cfg2 = AgentConfig(**loaded_clean, model="gpt-3.5-turbo", model_override="gpt-4-turbo")
            resolved2 = cfg2.resolve_from_profile(manager)
            result.check(
                "resolve_from_profile: model_override wins",
                resolved2.model == "gpt-4-turbo",
                f"Expected gpt-4-turbo, got {resolved2.model!r}",
            )

        # ── 16. Provider file integrity ───────────────────────────────────
        raw = json.loads(providers_file.read_text())
        result.check(
            "providers.json: valid JSON and has profiles",
            isinstance(raw.get("profiles"), list) and len(raw["profiles"]) == 2,
            f"providers.json structure: {list(raw.keys())}",
        )

    except Exception as e:
        result.check("UNEXPECTED ERROR", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally:
        # Restore HOME
        if old_home is not None:
            os.environ["HOME"] = old_home
        else:
            os.environ.pop("HOME", None)

        # Cleanup
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # ── Summary ──────────────────────────────────────────────────────────────
    print(result.summary())
    return result.failures == 0


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    success = run_smoke_test()
    sys.exit(0 if success else 1)
