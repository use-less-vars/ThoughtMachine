# tests/tools/test_tool_schemas.py
"""Injected infrastructure fields must not be advertised to the model.

C68 / TASK 2.  ``exclude=True`` only affects ``model_dump`` -- it does NOT
remove a field from a generated JSON schema.  Measured before the fix, on
``Worker``:

    json_schema: [... "agent_config", "effective_permissions",
                  "is_worker_context", "session_permissions" ...]
    openai     : [... "agent_config", "effective_permissions",
                  "is_worker_context", "session_permissions" ...]

so the model was told it may supply them.  ``tools/utils.py``'s
``infra_fields`` set is an error-message strip list (it prunes the "Valid
fields:" hint), not a schema strip point -- instrumented and confirmed to leave
the generated schema untouched.  The mechanism that demonstrably works is
``pydantic.json_schema.SkipJsonSchema`` on the declaration itself, which the
five injected fields now use.  They remain real fields: validation accepts
them and ToolExecutor injection still lands.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.utils import model_to_openai_tool  # noqa: E402
from tools.workspace.worker_thread import Worker  # noqa: E402

#: Everything ToolExecutor injects into a tool's arguments.  None of it is the
#: model's to supply: the executor overwrites (or pops) every one of them.
_INJECTED = (
    "agent_config",
    "session_permissions",
    "effective_permissions",
    "is_worker_context",
    "session_id",
)


def test_worker_json_schema_advertises_no_injected_field():
    props = Worker.model_json_schema()["properties"]
    assert [f for f in _INJECTED if f in props] == [], sorted(props)


def test_openai_tool_definition_advertises_no_injected_field():
    props = model_to_openai_tool(Worker)["function"]["parameters"]["properties"]
    assert [f for f in _INJECTED if f in props] == [], sorted(props)


def test_injected_fields_still_validate_and_reach_the_model():
    """Hiding them from the schema must not disable them as fields."""
    cfg = {"model": "test-model", "max_turns": 3}
    perms = {"container": False}
    effective = {"container:false": True}

    worker = Worker.model_validate(
        {
            "action": "list",
            "agent_config": cfg,
            "session_permissions": perms,
            "effective_permissions": effective,
            "is_worker_context": True,
            "session_id": "sess-injected",
        }
    )

    assert worker.agent_config == cfg
    assert worker.session_permissions == perms
    assert worker.effective_permissions == effective
    assert worker.is_worker_context is True
    assert worker.session_id == "sess-injected"

    # The two fields that carry ``exclude=True`` stay out of a dump too, so an
    # args model can never be round-tripped back out with authority facts in it.
    dumped = worker.model_dump()
    assert "agent_config" not in dumped
    assert "session_id" not in dumped
