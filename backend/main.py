"""Compatibility API entrypoint; research execution lives in the worker."""
from app.main import app

__all__ = ["app"]
