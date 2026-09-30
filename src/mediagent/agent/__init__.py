"""LLM-guided agent core.

The package facade is deliberately lazy.  Tool modules use the lower-level
``mediagent.agent.llm`` and ``mediagent.agent.metadata_tagger`` modules while
the agent runner itself imports the tool registry.  Importing the runner here
eagerly would therefore create a registry -> tagging -> agent -> registry
cycle in a fresh Python process.
"""

from __future__ import annotations

from typing import Any

__all__ = ["AgentRunResult", "AgentRunner"]


def __getattr__(name: str) -> Any:
    if name == "AgentRunner":
        from mediagent.agent.core import AgentRunner

        return AgentRunner
    if name == "AgentRunResult":
        from mediagent.agent.schema import AgentRunResult

        return AgentRunResult
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
