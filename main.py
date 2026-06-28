from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.dashboard import router, set_db
from app.services.database import DatabaseService
from app.services.scheduler import SchedulerService
from app.services.tick_feed import TickFeedService
from app.ws.dashboard_ws import ws_manager

# ── Global service instances ──────────────────────────────────────────────────

db_service:   DatabaseService  = DatabaseService()
tick_service: TickFeedService  | None = None
scheduler:    SchedulerService | None = None


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global tick_service, scheduler

    await db_service.init()
    set_db(db_service)

    tick_service = TickFeedService(db=db_service)
    tick_service.start()

    scheduler = SchedulerService(ws_manager=ws_manager, db=db_service)
    scheduler.start()

    yield

    if tick_service:
        await tick_service.stop()
    if scheduler:
        await scheduler.stop()
    await db_service.close()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="BankNifty Trading Dashboard", lifespan=lifespan)

app.include_router(router)

# Serve CSS and JS at the paths the HTML references (/css/…, /js/…)
app.mount("/css", StaticFiles(directory="static/css"), name="css")
app.mount("/js",  StaticFiles(directory="static/js"),  name="js")


@app.get("/")
def index() -> FileResponse:
    return FileResponse("static/index.html")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
