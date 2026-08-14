"""FastAPI dependency. Split out of `__init__` so `routes` can import it without a cycle
(design §3): `__init__` -> `routes` -> `deps` -> `service`, and nothing points back."""

from __future__ import annotations

from fastapi import Request

from .service import MarketDataService


def get_service(request: Request) -> MarketDataService:
    """The service is stored on app.state during lifespan startup."""
    return request.app.state.market
