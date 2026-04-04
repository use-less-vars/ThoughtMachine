"""
Agent module - main entry point for ThoughtMachine agent.

Public API:
    Agent: Main agent class (from .core.agent)
"""

from .core.agent import Agent
from agent.logging.debug_log import debug_log

__all__ = ["Agent"]