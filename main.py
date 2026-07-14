from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import app.config as cfg
from app.api import auth as auth_api
from app.api import market as market_api
from app.api import trading as trading_api
from app.services.database import DatabaseService
from app.services.market_data import MarketDataService
from app.services.scheduler import SchedulerService
from app.services.settings import load_and_apply as load_settings
from app.ws.account_ws import account_ws_manager
from app.ws.market_ws import market_ws_manager

# ── Global service instances ──────────────────────────────────────────────────

db_service  = DatabaseService()
mkt_service = MarketDataService()


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db_service.init()
    app.state.db = db_service
    # Apply persisted runtime settings BEFORE the scheduler reads any timing.
    await load_settings(db_service)

    scheduler = SchedulerService(
        db          = db_service,
        market_data = mkt_service,
        market_ws   = market_ws_manager,
        account_ws  = account_ws_manager,
    )
    await scheduler.start()

    market_api.set_db(db_service)
    trading_api.set_db(db_service)

    yield

    await scheduler.stop()
    await db_service.close()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Paper Trading Terminal", lifespan=lifespan)

app.add_middleware(SessionMiddleware, secret_key=cfg.SESSION_SECRET)

app.include_router(auth_api.router)
app.include_router(market_api.router)
app.include_router(trading_api.router)

app.mount("/css", StaticFiles(directory="static/css"), name="css")
app.mount("/js",  StaticFiles(directory="static/js"),  name="js")


@app.get("/")
def index() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse("static/login.html")


@app.get("/holdings")
def holdings_page() -> FileResponse:
    return FileResponse("static/holdings.html")


@app.get("/positions")
def positions_page() -> FileResponse:
    return FileResponse("static/positions.html")


@app.get("/orders")
def orders_page() -> FileResponse:
    return FileResponse("static/orders.html")


@app.get("/console")
def console_page() -> FileResponse:
    return FileResponse("static/console.html")


@app.get("/settings")
def settings_page() -> FileResponse:
    return FileResponse("static/settings.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
