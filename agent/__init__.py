"""
Agent module - main entry point for ThoughtMachine agent.

Public API:
    Agent: Main agent class (from .core.agent)
"""
from agent.logging import log
__all__ = ['Agent']


def __getattr__(name):
    if name == "Agent":
        from agent.core.agent import Agent
        return Agent
    raise AttributeError(name)
