"""Entrypoint for the poptonium service.

The implementation lives in the ``app`` package (one module per responsibility);
this module just re-exports the FastAPI instance so ``uvicorn main:app`` keeps
working unchanged.
"""

from app.server import app

__all__ = ["app"]
