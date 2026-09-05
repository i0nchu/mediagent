"""Default tool registry."""

from __future__ import annotations

from mediagent.core.tooling import ToolRegistry
from mediagent.tools import (
    asset_tools,
    auth_tools,
    cleanup_tools,
    comic_tools,
    core_tools,
    download_tools,
    instagram_tools,
    local_import_tools,
    library_content_tools,
    library_tools,
    link_tools,
    media_tools,
    metadata_tools,
    pixiv_tools,
    pixiv_library_tools,
    reddit_tools,
    storage_tools,
    telegram_tools,
    x_tools,
)


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for module in (
        asset_tools,
        core_tools,
        cleanup_tools,
        comic_tools,
        auth_tools,
        media_tools,
        storage_tools,
        download_tools,
        library_content_tools,
        library_tools,
        link_tools,
        local_import_tools,
        metadata_tools,
        instagram_tools,
        x_tools,
        pixiv_tools,
        pixiv_library_tools,
        reddit_tools,
        telegram_tools,
    ):
        for definition in module.definitions():
            registry.register(definition)
    return registry
