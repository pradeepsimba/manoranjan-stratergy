from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.dashboard import router, set_services, ws_router
from app.auth import (
    COOKIE_NAME, SESSION_MAX_AGE_SEC, check_credentials, is_logged_in,
    limiter as login_limiter, make_session_token, require_login_page,
)
from app.services.database import DatabaseService
from app.services.market_data import MarketDataService
from app.services.scheduler import SchedulerService
from app.services.settings import load_and_apply as load_settings
from app.ws.dashboard_ws import ws_manager

# ── Global service instances ──────────────────────────────────────────────────

db_service  = DatabaseService()
mkt_service = MarketDataService()


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db_service.init()
    # Apply persisted runtime settings BEFORE the scheduler reads any timing.
    await load_settings(db_service)

    scheduler = SchedulerService(
        db          = db_service,
        market_data = mkt_service,
        ws_manager  = ws_manager,
    )
    await scheduler.start()

    set_services(db_service, scheduler)

    yield

    await scheduler.stop()
    await db_service.close()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Bank Nifty Options Paper Trader", lifespan=lifespan)

app.include_router(router)
app.include_router(ws_router)   # /ws/dashboard - deliberately NOT behind login


@app.middleware("http")
async def _gate_static_assets(request: Request, call_next):
    # /css and /js below are Starlette StaticFiles mounts, not APIRouter
    # routes - they can't take a `dependencies=[Depends(require_login_page)]`
    # the way every other page/route in this app does, so they were a
    # silent 4th exception to the login gate beyond the 3 CLAUDE.md documents
    # (/ws/dashboard, /login, /healthz) until this fix (2026-09-24, found in
    # review). No secrets live in these files, but serving the full
    # client-side trading-dashboard/manual-order JS to a caller with no
    # session cookie contradicts "every page and API route is login-gated"
    # and is cheap to close with a request-level check here.
    path = request.url.path
    if (path.startswith("/css/") or path.startswith("/js/")) \
            and not is_logged_in(request.cookies.get(COOKIE_NAME)):
        return Response(status_code=401)
    return await call_next(request)


app.mount("/css", StaticFiles(directory="static/css"), name="css")
app.mount("/js",  StaticFiles(directory="static/js"),  name="js")

# ── PWA installability (2026-09-24) — manifest/service-worker/icons ──────────
# Deliberately NOT behind login, unlike /css and /js above: these carry no
# business logic or secrets (just icon images and app metadata), and the
# browser needs to be able to fetch the manifest/icons to offer "Install
# app" even from /login before a session exists. app.mount's own path
# (/icons) can't collide with the gated /css or /js prefixes above, so the
# _gate_static_assets middleware's startswith() checks never touch it.
app.mount("/icons", StaticFiles(directory="static/icons"), name="icons")


@app.get("/manifest.json")
def manifest() -> FileResponse:
    return FileResponse("static/manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> Response:
    # no-store (not just a short max-age) — this file is small and rarely
    # changes, but a stale cached copy of the ONE file responsible for
    # picking up every future update is the one thing in a PWA setup worth
    # being deliberately paranoid about; most browsers already special-case
    # service-worker fetches to bypass HTTP cache, but not all versions do.
    with open("static/sw.js", "rb") as f:
        body = f.read()
    return Response(body, media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    return FileResponse("static/favicon.ico")


@app.get("/healthz")
def healthz() -> dict:
    # Unauthenticated on purpose - the Dockerfile's HEALTHCHECK curls this
    # from inside the container with no credentials; /api/status itself is
    # behind login now, same as everything else in this app.
    return {"ok": True}


@app.get("/", dependencies=[Depends(require_login_page)])
def index() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/settings", dependencies=[Depends(require_login_page)])
def settings_page() -> FileResponse:
    return FileResponse("static/settings.html")


# ── Login / logout (see app/auth.py) ────────────────────────────────────────────

@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse("static/login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    if not login_limiter.allow(ip):
        return RedirectResponse("/login?error=locked", status_code=302)

    if not check_credentials(username, password):
        # allow() above already reserved (counted) this attempt atomically -
        # see LoginAttemptLimiter's own docstring for why a separate
        # record-failure call here would double count.
        return RedirectResponse("/login?error=1", status_code=302)
    login_limiter.record_success(ip)

    token = make_session_token()
    response = RedirectResponse("/", status_code=303)
    # secure=True whenever the request arrived over HTTPS. This app itself is
    # plain HTTP only - nginx terminates TLS in front of it and this reads
    # that via X-Forwarded-Proto (see default.conf and the Dockerfile's
    # --forwarded-allow-ips). A hardcoded secure=True would break login if
    # this is ever run directly (no nginx) for local dev instead.
    # samesite="strict" is this app's only CSRF defense (no CSRF token
    # anywhere) - safe since the login flow never needs this cookie sent
    # cross-site.
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_MAX_AGE_SEC, httponly=True,
        samesite="strict", secure=request.url.scheme == "https",
    )
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
