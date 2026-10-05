"""FastAPI web dashboard application."""

from __future__ import annotations

import hmac
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from bot.config import Config
from bot.state import BotState, RuntimeOverrides
from bot.store import TransactionStore
from bot.utils import APP_VERSION, format_crypto, format_currency, format_datetime, mask_secret, safe_load_json
from web.auth import auth_manager, csrf_protect, first_run_setup, login_post, logout, require_auth
from web.brute_force import brute_force_protector, check_login_allowed
from web.oidc import oidc_provider
from web.rate_limit import login_rate_limit
from web.routers import api_router, monitoring_router
from web.security import SecurityHeadersMiddleware, validate_production_security


def _strict_env() -> bool:
    return os.environ.get("APP_ENV", "production").lower() in ("production", "staging")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize shared state on startup."""
    # Production must have stable security configuration before handling traffic.
    validate_production_security()
    # If no admin password is configured anywhere, arm the one-time setup flow
    # and print the setup token to the container log.
    first_run_setup.ensure_token()

    # In production and staging, dependencies must be injected by the composition
    # root (e.g. bot.core or an explicit factory). Silently creating fallbacks here
    # could point different components at different files.
    if _strict_env():
        missing = []
        if not hasattr(app.state, "config") or app.state.config is None:
            missing.append("config")
        if not hasattr(app.state, "store") or app.state.store is None:
            missing.append("store")
        if not hasattr(app.state, "bot_state") or app.state.bot_state is None:
            missing.append("bot_state")
        if not hasattr(app.state, "overrides") or app.state.overrides is None:
            missing.append("overrides")
        if not hasattr(app.state, "attempt_store") or app.state.attempt_store is None:
            missing.append("attempt_store")
        if missing:
            raise RuntimeError(
                f"Missing required app.state dependencies in {os.environ.get('APP_ENV')}: {', '.join(missing)}"
            )
    else:
        if not hasattr(app.state, "config") or app.state.config is None:
            try:
                app.state.config = Config()
            except Exception:
                app.state.config = None
        if not hasattr(app.state, "store") or app.state.store is None:
            app.state.store = TransactionStore()
        if not hasattr(app.state, "bot_state") or app.state.bot_state is None:
            app.state.bot_state = BotState()
        if not hasattr(app.state, "overrides") or app.state.overrides is None:
            app.state.overrides = RuntimeOverrides()
    yield


app = FastAPI(title="DCA-Bot Dashboard", version=APP_VERSION, lifespan=lifespan)

# Security headers are the application's source of truth. Nginx should only add
# Strict-Transport-Security and should not duplicate the headers below.
app.add_middleware(SecurityHeadersMiddleware)

base_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")
templates = Jinja2Templates(directory=str(base_dir / "templates"))

# Template globals
templates.env.globals["format_currency"] = format_currency
templates.env.globals["format_crypto"] = format_crypto
templates.env.globals["format_datetime"] = format_datetime
templates.env.globals["mask_secret"] = mask_secret
templates.env.globals["app_version"] = APP_VERSION


def _static_version() -> str:
    """Short content hash of the main static assets, used for cache busting.

    Computed at startup, so every deploy that changes app.css/app.js yields a
    new query string and browsers fetch the fresh files instead of a stale
    cached copy.
    """
    import hashlib

    h = hashlib.sha256()
    for name in ("css/app.css", "js/app.js"):
        path = base_dir / "static" / name
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()[:12]


templates.env.globals["static_version"] = _static_version()

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "").strip() or secrets.token_hex(32),
    max_age=3600,
)

app.include_router(api_router, prefix="/api")
app.include_router(monitoring_router, prefix="/api")


@app.get("/", response_class=HTMLResponse)
def index(request: Request, username: str = Depends(require_auth)):
    return RedirectResponse(url="/dashboard")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    first_run_setup.ensure_token()
    # Generate the CSRF token BEFORE rendering so the form carries the same
    # value that is written to the response cookie. (Rendering first and then
    # setting a fresh cookie leaves first-time visitors with an empty form
    # token that fails validation on submit.)
    csrf_token = auth_manager.generate_csrf_token()
    response = templates.TemplateResponse(
        request, "login.html", {"request": request, "error": None, "oidc_enabled": auth_manager.oidc_enabled, "setup_required": first_run_setup.required(), "csrf_token": csrf_token}
    )
    # Issue a CSRF token for the login form. The token is rotated after a
    # successful login, so a pre-login token cannot be replayed post-auth.
    auth_manager.set_csrf_cookie(response, csrf_token)
    return response


@app.get("/login/oidc")
async def oidc_login(
    request: Request,
    _rate_limit=Depends(login_rate_limit),
):
    """Redirect the user to the OIDC provider (Authentik) for authentication."""
    if not oidc_provider.enabled:
        raise HTTPException(status_code=404, detail="OIDC authentication is not enabled")

    auth_url, state, nonce, code_verifier = await oidc_provider.build_authorization_url(request)
    request.session["oidc_state"] = state
    request.session["oidc_nonce"] = nonce
    request.session["oidc_code_verifier"] = code_verifier
    return RedirectResponse(url=auth_url, status_code=307)


@app.get("/auth/callback")
async def oidc_callback(
    request: Request,
    _rate_limit=Depends(login_rate_limit),
):
    """Handle the OIDC provider's authorization response."""
    if not oidc_provider.enabled:
        raise HTTPException(status_code=404, detail="OIDC authentication is not enabled")

    error = request.query_params.get("error")
    if error:
        detail = request.query_params.get("error_description", error)
        return templates.TemplateResponse(
            request, "login.html", {"request": request, "error": detail, "oidc_enabled": auth_manager.oidc_enabled, "setup_required": first_run_setup.required()}, status_code=400
        )

    state = request.query_params.get("state")
    code = request.query_params.get("code")
    if not state or not code:
        raise HTTPException(status_code=400, detail="Missing state or authorization code")

    expected_state = request.session.get("oidc_state")
    if not expected_state or not hmac.compare_digest(expected_state, state):
        raise HTTPException(status_code=403, detail="Invalid OIDC state")

    code_verifier = request.session.get("oidc_code_verifier")
    if not code_verifier:
        raise HTTPException(status_code=403, detail="OIDC login flow has expired")

    token_response = await oidc_provider.exchange_code(request, code, code_verifier)
    id_token = token_response.get("id_token")
    if not id_token:
        raise HTTPException(status_code=503, detail="OIDC provider did not return an ID token")

    claims = await oidc_provider.validate_id_token(id_token)
    nonce = claims.get("nonce")
    expected_nonce = request.session.get("oidc_nonce")
    if not expected_nonce or not hmac.compare_digest(expected_nonce, str(nonce)):
        raise HTTPException(status_code=403, detail="Invalid OIDC nonce")

    username = oidc_provider.extract_username(claims)
    response = RedirectResponse(url="/dashboard", status_code=303)
    auth_manager.create_session(response, username)
    auth_manager.set_csrf_cookie(response)

    # Clean up single-use flow state
    request.session.pop("oidc_state", None)
    request.session.pop("oidc_nonce", None)
    request.session.pop("oidc_code_verifier", None)

    return response


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    _rate_limit=Depends(login_rate_limit),
):
    form = await request.form()
    csrf_token = str(form.get("csrf_token", ""))
    try:
        auth_manager.validate_csrf(request, csrf_token)
    except HTTPException:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "Invalid or missing CSRF token", "oidc_enabled": auth_manager.oidc_enabled, "setup_required": first_run_setup.required()},
            status_code=403,
        )

    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    response = RedirectResponse(url="/dashboard", status_code=303)
    if login_post(request, username, password, response):
        # Rotate the CSRF token after successful authentication.
        auth_manager.set_csrf_cookie(response)
        return response
    return templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "error": "Invalid username or password", "oidc_enabled": auth_manager.oidc_enabled, "setup_required": first_run_setup.required()},
        status_code=401,
    )


@app.post("/setup")
@csrf_protect
async def setup_submit(
    request: Request,
    _rate_limit=Depends(login_rate_limit),
):
    """First-run setup: create the admin account using the setup token.

    Only available while no local admin password is configured anywhere
    (environment variable or secrets store). Afterwards the route is closed.
    """
    if not first_run_setup.required():
        raise HTTPException(status_code=400, detail="First-run setup has already been completed")

    form = await request.form()
    setup_token = str(form.get("setup_token", ""))
    username = str(form.get("username", "")).strip() or "admin"
    password = str(form.get("password", ""))
    confirm = str(form.get("password_confirm", ""))

    def render_error(message: str, status_code: int):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "error": message,
                "oidc_enabled": auth_manager.oidc_enabled,
                "setup_required": True,
                "setup_username": username,
            },
            status_code=status_code,
        )

    check_login_allowed(request, "first-run-setup")
    if not first_run_setup.verify(setup_token):
        brute_force_protector.record_failure(request, "first-run-setup")
        return render_error("Invalid setup token. Check the container logs (docker compose logs).", 400)
    if len(password) < 8:
        return render_error("Password must be at least 8 characters.", 400)
    if password != confirm:
        return render_error("Passwords do not match.", 400)
    if len(username) > 64:
        return render_error("Username must be 64 characters or fewer.", 400)

    import bcrypt as _bcrypt

    password_hash = _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
    from bot.secrets_store import SecretsStore

    SecretsStore().save_section("web", {"username": username, "password_hash": password_hash})
    first_run_setup.complete()

    brute_force_protector.record_success(request, "first-run-setup")
    from web.routers.api import _audit_log

    _audit_log("first_run_setup_completed", {"username": username}, request)

    response = RedirectResponse(url="/dashboard", status_code=303)
    auth_manager.create_session(response, username)
    auth_manager.set_csrf_cookie(response)
    return response


@app.post("/logout")
async def logout_route(request: Request):
    response = RedirectResponse(url="/login", status_code=303)
    # Clear any single-use OIDC flow state as well as session/CSRF cookies.
    request.session.pop("oidc_state", None)
    request.session.pop("oidc_nonce", None)
    request.session.pop("oidc_code_verifier", None)
    logout(response)
    return response


def _is_data_dir_writable(path: Path) -> bool:
    """Return True if the bot can write temporary data in ``path``."""
    import tempfile

    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path)
        os.close(fd)
        os.unlink(tmp)
        return True
    except OSError:
        return False


@app.get("/health/live")
def health_live():
    """Liveness probe: confirms the application process responds."""
    return {"status": "alive", "app": "ok"}


@app.get("/health/ready")
def health_ready():
    """Readiness probe: confirms the bot is configured and actively cycling.

    The response is machine-readable and intentionally omits secrets, balances,
    session data, and API keys. Kraken availability is **not** a readiness gate.
    """
    from datetime import datetime, timezone

    heartbeat_file = Path(os.environ.get("HEARTBEAT_FILE", "data/heartbeat.json"))
    max_age_seconds = int(os.environ.get("HEARTBEAT_MAX_AGE_SECONDS", "900"))
    now = datetime.now(timezone.utc)
    fatal_statuses = {"stopped", "error", "hold"}

    config = getattr(app.state, "config", None)
    store = getattr(app.state, "store", None)
    state = getattr(app.state, "bot_state", None)

    checks: dict[str, Any] = {}
    ready = True

    # Configuration
    if config is None:
        checks["configuration"] = "missing"
        ready = False
    elif not config.config_path.exists():
        checks["configuration"] = "config_not_readable"
        ready = False
    else:
        checks["configuration"] = "ok"

    # Storage
    if store is None:
        checks["storage"] = "missing"
        ready = False
    elif not _is_data_dir_writable(store.filepath.parent):
        checks["storage"] = "not_writable"
        ready = False
    else:
        checks["storage"] = "ok"

    # Trading-loop heartbeat. When the bot has no Kraken credentials the
    # trading loop intentionally idles and writes no heartbeat — that is a
    # valid first-run state, not a readiness failure.
    credentials_configured = bool(getattr(config, "configured", True))
    heartbeat_age: int | None = None
    heartbeat_status = "missing"
    if not credentials_configured:
        heartbeat_status = "not_required"
    elif heartbeat_file.exists():
        data = safe_load_json(heartbeat_file)
        if isinstance(data, dict) and data.get("timestamp"):
            try:
                heartbeat_ts = datetime.fromisoformat(data["timestamp"])
                heartbeat_age = int((now - heartbeat_ts).total_seconds())
                heartbeat_status = "ok" if heartbeat_age <= max_age_seconds else "stale"
                if heartbeat_status != "ok":
                    ready = False
            except ValueError:
                heartbeat_status = "invalid_timestamp"
                ready = False
        else:
            heartbeat_status = "invalid_format"
            ready = False
    else:
        heartbeat_status = "missing"
        ready = False
    checks["heartbeat"] = heartbeat_status
    checks["heartbeat_age_seconds"] = heartbeat_age

    # Bot state
    if state is None:
        checks["bot_state"] = "missing"
        ready = False
    elif not credentials_configured:
        # Idle-unconfigured bots intentionally have no trading status yet.
        checks["bot_state"] = "not_required"
    elif state.status in fatal_statuses:
        checks["bot_state"] = "fatal"
        ready = False
    else:
        checks["bot_state"] = state.status or "unknown"

    # Backup age (informational; does not make the service unready)
    backup_dir = Path(os.environ.get("BACKUP_DIR", "backups"))
    backup_status_file = backup_dir / "backup_status.json"
    backup_status = "unknown"
    backup_age: int | None = None
    if backup_status_file.exists():
        bs = safe_load_json(backup_status_file)
        if isinstance(bs, dict) and bs.get("last_successful_at"):
            try:
                backup_ts = datetime.fromisoformat(bs["last_successful_at"])
                backup_age = int((now - backup_ts).total_seconds())
                backup_status = bs.get("validation_status", "unknown")
            except ValueError:
                backup_status = "invalid_timestamp"
    else:
        backup_status = "no_status_file"
    checks["backup_status"] = backup_status
    checks["backup_age_seconds"] = backup_age

    status = "ready" if ready else "not_ready"
    response_code = 200 if ready else 503
    body = {
        "status": status,
        "app": "ok",
        "trading_loop": heartbeat_status if heartbeat_status != "ok" else "ok",
        "heartbeat_age_seconds": heartbeat_age,
        "storage": checks["storage"],
        "configuration": checks["configuration"],
        "checks": checks,
    }
    return JSONResponse(content=body, status_code=response_code)


def _page_response(request: Request, page_name: str, username: str):
    allowed = {
        "dashboard", "transactions", "performance",
        "settings", "logs", "alerts", "audit", "backups", "orders", "preflight",
    }
    if page_name not in allowed:
        return templates.TemplateResponse(request, "404.html", {"request": request, "page": page_name}, status_code=404)
    csrf_token = auth_manager.generate_csrf_token()
    response = templates.TemplateResponse(
        request,
        f"{page_name}.html",
        {
            "request": request,
            "username": username,
            "csrf_token": csrf_token,
            "page": page_name,
        },
    )
    auth_manager.set_csrf_cookie(response)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request, username: str = Depends(require_auth)):
    return _page_response(request, "dashboard", username)


@app.get("/telegram", response_class=HTMLResponse)
def telegram_page_redirect(request: Request, username: str = Depends(require_auth)):
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/health", response_class=HTMLResponse)
def health_page_redirect(request: Request, username: str = Depends(require_auth)):
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/preflight", response_class=HTMLResponse)
def preflight_page(request: Request, username: str = Depends(require_auth)):
    return _page_response(request, "preflight", username)


@app.get("/{page_name}", response_class=HTMLResponse)
def page(request: Request, page_name: str, username: str = Depends(require_auth)):
    return _page_response(request, page_name, username)
