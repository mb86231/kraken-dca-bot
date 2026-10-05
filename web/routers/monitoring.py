"""Monitoring and metrics endpoints for external observability tooling."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import PlainTextResponse

from web.auth import require_auth
from web.monitoring import get_operational_status, get_prometheus_metrics, require_monitoring_token
from web.rate_limit import api_rate_limit

router = APIRouter()


@router.get("/operations/status")
def api_operations_status(
    request: Request,
    username: str = Depends(require_auth),
    _rate_limit=Depends(api_rate_limit),
):
    """Return a non-sensitive operational status summary.

    Requires dashboard authentication. Use this endpoint with Uptime Kuma or
    similar tools that can authenticate as a dashboard user.
    """
    return get_operational_status()


@router.get("/metrics")
def api_metrics(
    request: Request,
    _token=Depends(require_monitoring_token),
):
    """Return Prometheus-compatible metrics.

    Protected by a dedicated ``MONITORING_TOKEN`` Bearer token. The token must be
    configured via the environment; if it is unset, this endpoint returns 401.
    """
    return PlainTextResponse(
        content=get_prometheus_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
