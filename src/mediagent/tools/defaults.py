"""Default tool registry."""

from __future__ import annotations

from mediagent.core.tooling import ToolRegistry
from mediagent.tools import (
    auth_tools,
    cleanup_tools,
    core_tools,
    download_tools,
    library_tools,
    link_tools,
    media_tools,
    metadata_tools,
    pixiv_tools,
    reddit_tools,
    storage_tools,
    telegram_tools,
    x_tools,
)


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for module in (
        core_tools,
        cleanup_tools,
        auth_tools,
        media_tools,
        storage_tools,
        download_tools,
        library_tools,
        link_tools,
        metadata_tools,
        x_tools,
        pixiv_tools,
        reddit_tools,
        telegram_tools,
    ):
        for definition in module.definitions():
            registry.register(definition)
    return registry
