"""Tool registration & naming cleanup: canonical ``tool_name()`` identities.

Verifies that the git/host tools expose stable snake_case tool names
(``git_read`` / ``git_write`` / ``host_bash``) via the ``name`` ClassVar +
``ToolBase.tool_name()`` mechanism, that the legacy ``GitInfoTool`` class
name keeps working through the module-level alias, and that registries /
schema builders / ``AgentConfig`` consume the stable names.
"""

from tools import SIMPLIFIED_TOOL_CLASSES, TOOL_CLASSES
from tools.base import ToolBase
from tools.git_info_tool import GitInfoTool, GitReadTool
from tools.git_write_tool import GitWriteTool
from tools.host_bash_tool import HostBashTool
from tools.utils import model_to_openai_tool
from agent.config.models import AgentConfig


class TestToolNames:
    """The ``name`` ClassVar drives ``tool_name()``."""

    def test_git_read_tool_name(self):
        assert GitReadTool.tool_name() == "git_read"

    def test_git_write_tool_name(self):
        assert GitWriteTool.tool_name() == "git_write"

    def test_host_bash_tool_name(self):
        assert HostBashTool.tool_name() == "host_bash"

    def test_name_classvars_set(self):
        assert GitReadTool.name == "git_read"
        assert GitWriteTool.name == "git_write"
        assert HostBashTool.name == "host_bash"

    def test_legacy_alias(self):
        # Backward-compat alias: old class name still resolves.
        assert GitInfoTool is GitReadTool
        assert GitInfoTool.tool_name() == "git_read"

    def test_tool_base_defaults_to_class_name(self):
        assert ToolBase.name == ""
        assert ToolBase.tool_name() == "ToolBase"


class TestRegistration:
    """Stable names are what the registries expose."""

    def test_git_read_registered(self):
        assert GitReadTool in TOOL_CLASSES
        assert GitReadTool in SIMPLIFIED_TOOL_CLASSES

    def test_git_write_registered(self):
        assert GitWriteTool in TOOL_CLASSES
        assert GitWriteTool in SIMPLIFIED_TOOL_CLASSES

    def test_host_bash_registered(self):
        assert HostBashTool in TOOL_CLASSES
        assert HostBashTool in SIMPLIFIED_TOOL_CLASSES

    def test_registered_names_are_stable(self):
        names = {cls.tool_name() for cls in SIMPLIFIED_TOOL_CLASSES}
        assert "git_read" in names
        assert "git_write" in names
        assert "host_bash" in names
        assert "GitInfoTool" not in names


class TestOpenAIToolSchema:
    """model_to_openai_tool uses tool_name() for the function name."""

    def test_git_read_schema_name(self):
        schema = model_to_openai_tool(GitReadTool)
        assert schema["function"]["name"] == "git_read"

    def test_git_write_schema_name(self):
        schema = model_to_openai_tool(GitWriteTool)
        assert schema["function"]["name"] == "git_write"

    def test_host_bash_schema_name(self):
        schema = model_to_openai_tool(HostBashTool)
        assert schema["function"]["name"] == "host_bash"


class TestAgentConfigTools:
    """AgentConfig defaults and filtering use stable tool names."""

    def test_default_enabled_tools_contain_stable_names(self):
        cfg = AgentConfig()  # mode='agent' -> AGENT_TOOLS preset
        enabled = set(cfg.enabled_tools)
        assert "git_read" in enabled
        assert "git_write" in enabled
        assert "host_bash" in enabled

    def test_get_filtered_tool_classes_by_stable_name(self):
        cfg = AgentConfig(mode="custom", enabled_tools=["git_read"])
        classes = cfg.get_filtered_tool_classes()
        assert GitReadTool in classes
        assert GitWriteTool not in classes
        assert HostBashTool not in classes

    def test_get_filtered_tool_classes_accepts_legacy_class_name(self):
        cfg = AgentConfig(mode="custom", enabled_tools=["GitInfoTool"])
        classes = cfg.get_filtered_tool_classes()
        assert GitReadTool in classes
