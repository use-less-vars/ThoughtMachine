"""
Tests for WorkerDefinition Pydantic model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest
from pydantic import ValidationError

from agent.models.worker_definition import WorkerDefinition


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
        "permission_footprint": {"filesystem": "read"},
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
        assert wd.permission_footprint == {"filesystem": "read"}

    def test_all_optional_fields_default_to_none(self, valid_kwargs):
        """Optional fields not provided default to None."""
        wd = WorkerDefinition(**valid_kwargs)
        assert wd.timeout_seconds is None
        assert wd.max_context_tokens is None
        assert wd.warning_threshold_tokens is None
        assert wd.turn_limit is None
        assert wd.temperature is None

    def test_all_optional_fields_set(self, valid_kwargs):
        """All optional fields accept valid values."""
        kwargs = dict(valid_kwargs)
        kwargs.update({
            "timeout_seconds": 60,
            "max_context_tokens": 4096,
            "warning_threshold_tokens": 3072,
            "turn_limit": 10,
            "temperature": 0.3,
        })
        wd = WorkerDefinition(**kwargs)
        assert wd.timeout_seconds == 60
        assert wd.max_context_tokens == 4096
        assert wd.warning_threshold_tokens == 3072
        assert wd.turn_limit == 10
        assert wd.temperature == 0.3


# ---------------------------------------------------------------------------
# Validation: tool names
# ---------------------------------------------------------------------------

class TestToolValidation:
    def test_unknown_tool_raises(self, valid_kwargs):
        """An unknown tool name in the tools list raises ValidationError."""
        kwargs = dict(valid_kwargs)
        kwargs["tools"] = ["FileEditor", "NonExistentTool"]
        with pytest.raises(ValidationError, match="Unknown tool"):
            WorkerDefinition(**kwargs)

    def test_all_unknown_tools_reported(self, valid_kwargs):
        """The error message lists every unknown tool."""
        kwargs = dict(valid_kwargs)
        kwargs["tools"] = ["Foo", "Bar", "FileEditor"]
        with pytest.raises(ValidationError) as exc:
            WorkerDefinition(**kwargs)
        err_msg = str(exc.value)
        assert "Foo" in err_msg
        assert "Bar" in err_msg

    def test_empty_tools_list_accepted(self, valid_kwargs):
        """An empty tools list is valid (no unknown tools to reject)."""
        kwargs = dict(valid_kwargs)
        kwargs["tools"] = []
        wd = WorkerDefinition(**kwargs)
        assert wd.tools == []


# ---------------------------------------------------------------------------
# JSON schema generation
# ---------------------------------------------------------------------------

class TestJsonSchema:
    def test_schema_has_required_fields(self):
        """The generated JSON Schema marks the right fields as required."""
        schema = WorkerDefinition.model_json_schema()
        required = set(schema.get("required", []))
        assert "name" in required
        assert "description" in required
        assert "system_prompt" in required
        assert "tools" in required
        assert "permission_footprint" in required
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
