import logging
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from .database import init_db, get_this_week_books, get_all_weeks, get_books_by_week
from .scheduler import create_scheduler, refresh_weekly_deals

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

scheduler = create_scheduler()


async def _auto_refresh_if_empty():
    """On cold start, fetch data if DB is empty."""
    await asyncio.sleep(3)  # let the server finish starting
    books, _ = get_this_week_books()
    if not books:
        logger.info("DB is empty on startup — auto-fetching this week's deals")
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, refresh_weekly_deals)
        except Exception as e:
            logger.warning("Auto-fetch on startup failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    logger.info("Scheduler started")
    asyncio.create_task(_auto_refresh_if_empty())
    yield
    scheduler.shutdown()
    logger.info("Scheduler stopped")


app = FastAPI(title="Kobo 99元週特賣書單", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    books, week_date = get_this_week_books()
    all_weeks = get_all_weeks()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "books": books,
        "week_date": week_date,
        "all_weeks": all_weeks,
        "selected_week": week_date,
    })


@app.get("/week/{week_date}", response_class=HTMLResponse)
async def week_view(request: Request, week_date: str):
    import re
    if not re.match(r"\d{4}-\d{2}-\d{2}", week_date):
        raise HTTPException(status_code=400, detail="Invalid date format")
    books = get_books_by_week(week_date)
    all_weeks = get_all_weeks()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "books": books,
        "week_date": week_date,
        "all_weeks": all_weeks,
        "selected_week": week_date,
    })


@app.post("/api/refresh")
async def api_refresh():
    """Manually trigger a scrape + rating refresh."""
    result = refresh_weekly_deals()
    return JSONResponse(result)


@app.get("/api/books")
async def api_books():
    books, week_date = get_this_week_books()
    return {"week_date": week_date, "books": books}
