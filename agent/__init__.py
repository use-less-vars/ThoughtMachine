"""
Agent module - main entry point for ThoughtMachine agent.

Public API:
    Agent: Main agent class (from .core.agent)
"""
import importlib

from agent.logging import log
__all__ = ['Agent']


def __getattr__(name):
    if name == "Agent":
        from agent.core.agent import Agent
        return Agent
    # Any other public name is resolved as a submodule of this package.  This
    # is not mere convenience: a partially-initialised ``agent.core`` package
    # can be evicted from sys.modules by CPython's failed-import cleanup while
    # its already-imported children stay cached, so ``agent.core`` goes
    # missing from this package's namespace; ``import agent.core.x as y``
    # (IMPORT_FROM) then does getattr(agent, "core") and died.  Re-importing
    # the submodule here repairs the namespace instead.
    if name.startswith("_"):
        raise AttributeError(name)
    try:
        return importlib.import_module(f"agent.{name}")
    except ImportError:
        raise AttributeError(name) from None
