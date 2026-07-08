"""
Tests for WorkerDefinition Pydantic model.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.models.worker_definition import WorkerDefinition


# ---------------------------------------------------------------------------
# Template file validation
# ---------------------------------------------------------------------------

TEMPLATE_DIR = Path("resources/worker_templates")
TEMPLATE_NAMES = ["coder.json", "reviewer.json", "researcher.json"]


class TestTemplates:
    def test_all_templates_load_from_disk(self):
        """Every template in resources/worker_templates/ validates."""
        for name in TEMPLATE_NAMES:
            path = TEMPLATE_DIR / name
            assert path.exists(), f"Missing template: {path}"
            data = json.loads(path.read_text())
            wd = WorkerDefinition.model_validate(data)
            assert wd.name in path.stem, f"name mismatch in {name}"

    def test_coder_has_write_permissions(self):
        """Coder template has filesystem write + docker execution."""
        data = json.loads((TEMPLATE_DIR / "coder.json").read_text())
        wd = WorkerDefinition.model_validate(data)
        assert wd.worker_permissions.get("filesystem") == "write"
        assert wd.worker_permissions.get("execution") == "docker"

    def test_reviewer_read_only(self):
        """Reviewer template has filesystem read only."""
        data = json.loads((TEMPLATE_DIR / "reviewer.json").read_text())
        wd = WorkerDefinition.model_validate(data)
        assert wd.worker_permissions.get("filesystem") == "read"
        assert "write" not in wd.worker_permissions.values()

    def test_researcher_read_only(self):
        """Researcher template has filesystem read only."""
        data = json.loads((TEMPLATE_DIR / "researcher.json").read_text())
        wd = WorkerDefinition.model_validate(data)
        assert wd.worker_permissions.get("filesystem") == "read"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def valid_kwargs() -> dict:
    """Minimal valid arguments for WorkerDefinition."""
    return {
        "name": "code-reviewer",
        "description": "Reviews pull requests for style and correctness.",
        "system_prompt": "You are a code reviewer. Be concise.\n",
        "tools": ["FileEditor", "GlobTool", "Respond"],
        "worker_permissions": {"filesystem": "read"},
    }


# ---------------------------------------------------------------------------
# Valid instantiation
# ---------------------------------------------------------------------------

class TestValidInstantiation:
    def test_minimal(self, valid_kwargs):
        """Minimal required fields only."""
        wd = WorkerDefinition(**valid_kwargs)
        assert wd.name == "code-reviewer"
        assert wd.description == "Reviews pull requests for style and correctness."
        assert wd.system_prompt == "You are a code reviewer. Be concise.\n"
        assert wd.tools == ["FileEditor", "GlobTool", "Respond"]
        assert wd.worker_permissions == {"filesystem": "read"}

    def test_all_optional_fields_default_to_none(self, valid_kwargs):
        """Optional fields not provided default to None."""
        wd = WorkerDefinition(**valid_kwargs)
        assert wd.timeout_seconds is None
        assert wd.max_turns is None
        assert wd.temperature is None

    def test_critical_threshold_default(self, valid_kwargs):
        """critical_threshold_tokens defaults to 80000."""
        wd = WorkerDefinition(**valid_kwargs)
        assert wd.critical_threshold_tokens == 80000

    def test_critical_threshold_override(self, valid_kwargs):
        """critical_threshold_tokens accepts custom values."""
        wd = WorkerDefinition(**valid_kwargs, critical_threshold_tokens=120000)
        assert wd.critical_threshold_tokens == 120000

    def test_all_optional_fields_set(self, valid_kwargs):
        """All optional fields accept valid values."""
        kwargs = dict(valid_kwargs)
        kwargs.update({
            "timeout_seconds": 60,
            "max_turns": 10,
            "temperature": 0.3,
            "critical_threshold_tokens": 160000,
        })
        wd = WorkerDefinition(**kwargs)
        assert wd.timeout_seconds == 60
        assert wd.max_turns == 10
        assert wd.temperature == 0.3
        assert wd.critical_threshold_tokens == 160000


# ---------------------------------------------------------------------------
# JSON schema generation
# ---------------------------------------------------------------------------

class TestJsonSchema:
    def test_schema_includes_critical_threshold_tokens(self):
        """critical_threshold_tokens is in the schema properties."""
        schema = WorkerDefinition.model_json_schema()
        props = schema.get("properties", {})
        assert "critical_threshold_tokens" in props
        ct = props["critical_threshold_tokens"]
        assert ct.get("default") == 80000
        assert ct.get("title") == "Critical Threshold Tokens"

    def test_schema_has_required_fields(self):
        """The generated JSON Schema marks the right fields as required."""
        schema = WorkerDefinition.model_json_schema()
        required = set(schema.get("required", []))
        assert "name" in required
        assert "description" in required
        assert "system_prompt" in required
        assert "tools" in required
        assert "worker_permissions" in required
        # Optional fields should NOT be in required
        assert "timeout_seconds" not in required
        assert "temperature" not in required

    def test_schema_file_matches(self):
        """The on-disk schema file matches ``WorkerDefinition.model_json_schema()``."""
        schema_path = Path("resources/worker_definition_schema.json")
        assert schema_path.exists(), (
            f"Schema file not found at {schema_path}. "
            "Run the schema generator to create it."
        )
        on_disk = json.loads(schema_path.read_text())
        in_memory = WorkerDefinition.model_json_schema()
        assert on_disk == in_memory, (
            "Schema file is out of date — regenerate it."
        )


# ---------------------------------------------------------------------------
# Model serialisation / deserialisation round-trip
# ---------------------------------------------------------------------------

class TestSerialisation:
    def test_json_round_trip(self, valid_kwargs):
        """Serialise to JSON and back produces an identical model."""
        kwargs = dict(valid_kwargs)
        kwargs["temperature"] = 0.7
        wd = WorkerDefinition(**kwargs)
        data = wd.model_dump()
        restored = WorkerDefinition(**data)
        assert restored == wd
        assert restored.temperature == 0.7
        assert restored.critical_threshold_tokens == 80000
