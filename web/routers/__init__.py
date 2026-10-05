"""Web dashboard API routers."""

from web.routers.api import router as api_router
from web.routers.monitoring import router as monitoring_router

__all__ = ["api_router", "monitoring_router"]
