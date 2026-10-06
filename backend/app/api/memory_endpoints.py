"""Compatibility import; all memory endpoints require authenticated ownership."""

from app.api.memory import router

__all__ = ["router"]
