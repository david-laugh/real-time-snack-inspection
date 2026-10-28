"""ASGI entrypoint: `uvicorn src.main:app` (settings from environment)."""

from .app import create_app

app = create_app()
