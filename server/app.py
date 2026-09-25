"""Compatibility import: legacy callers now receive the safe editorial API."""

from free_router.app import app, build_app

__all__ = ["app", "build_app"]
