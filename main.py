from dotenv import load_dotenv
load_dotenv()

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.dashboard import router, set_services
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
    # The market-data connection loops are separate tasks the scheduler does
    # not own — stop them before the DB closes so nothing is left running
    # against a torn-down process (no-op when the WS never started).
    await mkt_service.stop()
    await db_service.close()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="NSE Equity Paper Trader", lifespan=lifespan)

app.include_router(router)

app.mount("/css", StaticFiles(directory="static/css"), name="css")
app.mount("/js",  StaticFiles(directory="static/js"),  name="js")


@app.get("/")
def index() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/indicators")
def indicators_page() -> FileResponse:
    return FileResponse("static/indicators.html")


@app.get("/settings")
def settings_page() -> FileResponse:
    return FileResponse("static/settings.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
