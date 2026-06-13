"""Kagi web search + extract plugin — bundled, auto-loaded."""

from __future__ import annotations

from plugins.web.kagi.provider import KagiWebSearchProvider


def register(ctx) -> None:
    """Register the Kagi provider with the plugin context."""
    ctx.register_web_search_provider(KagiWebSearchProvider())
