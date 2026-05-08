import logging
import asyncio
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel
from typing import Optional

from .database import init_db, get_this_week_books, get_all_weeks, get_books_by_week, upsert_book, upsert_rating
from .scheduler import create_scheduler, refresh_weekly_deals
from .scraper import get_this_thursday, fetch_book_metadata_from_kobo, fetch_book_metadata_from_google
from .ratings import fetch_all_ratings
from .notify import get_topic, send_weekly_notification

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

scheduler = create_scheduler()


async def _auto_refresh_if_empty():
    """On cold start, fetch data if DB is empty."""
    await asyncio.sleep(3)
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


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    books, week_date = get_this_week_books()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "week_date": week_date,
        "books": books,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "ntfy_topic": get_topic() or "",
    })


@app.post("/api/test_notify")
async def api_test_notify():
    topic = get_topic()
    if not topic:
        raise HTTPException(status_code=400, detail="尚未設定 NTFY_TOPIC")
    import requests as req
    try:
        req.post(
            f"https://ntfy.sh/{topic}",
            data="這是測試通知，代表設定成功！📚".encode("utf-8"),
            headers={"Title": "Kobo 99元 · 測試通知", "Tags": "white_check_mark"},
            timeout=8,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True}


# ── API ──────────────────────────────────────────────────────────────────────

@app.post("/api/refresh")
async def api_refresh():
    result = refresh_weekly_deals()
    return JSONResponse(result)


@app.get("/api/books")
async def api_books():
    books, week_date = get_this_week_books()
    return {"week_date": week_date, "books": books}


class LookupRequest(BaseModel):
    kobo_url: Optional[str] = None
    title: Optional[str] = None


@app.post("/api/lookup")
async def api_lookup(body: LookupRequest):
    """Given a Kobo URL or title, return book metadata for preview."""
    meta = {}
    if body.kobo_url and "kobo.com" in body.kobo_url:
        meta = fetch_book_metadata_from_kobo(body.kobo_url)
    if not meta.get("title") and body.title:
        meta = fetch_book_metadata_from_google(body.title)
    if not meta:
        raise HTTPException(status_code=404, detail="找不到書籍資訊")
    return meta


class AddBookRequest(BaseModel):
    kobo_url: Optional[str] = None
    title: str
    author: Optional[str] = None
    cover_url: Optional[str] = None
    description: Optional[str] = None
    original_price: Optional[int] = None


@app.post("/api/add_book")
async def api_add_book(body: AddBookRequest):
    """Manually add a book to this week's list."""
    week_date = get_this_thursday()
    book = {
        "title": body.title.strip(),
        "author": body.author,
        "isbn": None,
        "kobo_url": body.kobo_url,
        "cover_url": body.cover_url,
        "description": body.description,
        "original_price": body.original_price,
    }
    book_id = upsert_book(week_date, book)

    # Fetch ratings in background
    ratings = fetch_all_ratings(body.title, body.author)
    for r in ratings:
        upsert_rating(book_id, r["source"], r.get("score"), r.get("review_count", 0), r.get("source_url", ""))

    return {"ok": True, "book_id": book_id, "week_date": week_date, "ratings_found": len(ratings)}


class DeleteBookRequest(BaseModel):
    book_id: int


@app.post("/api/delete_book")
async def api_delete_book(body: DeleteBookRequest):
    from .database import get_conn
    with get_conn() as conn:
        conn.execute("DELETE FROM book_ratings WHERE book_id=?", (body.book_id,))
        conn.execute("DELETE FROM weekly_deals WHERE id=?", (body.book_id,))
    return {"ok": True}
