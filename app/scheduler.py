"""
APScheduler job that runs every Thursday to scrape Kobo deals and fetch ratings.
Also provides a manual trigger function.
"""
import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .scraper import scrape_weekly_deals
from .ratings import fetch_all_ratings
from .database import init_db, upsert_book, upsert_rating

logger = logging.getLogger(__name__)


def refresh_weekly_deals():
    logger.info("Starting weekly deal refresh...")
    init_db()
    week_date, books = scrape_weekly_deals()

    if not books:
        logger.warning("No books scraped for week %s", week_date)
        return {"week_date": week_date, "books_updated": 0}

    updated = 0
    for book in books:
        book_id = upsert_book(week_date, book)
        ratings = fetch_all_ratings(book["title"], book.get("author"))
        for rating in ratings:
            upsert_rating(
                book_id,
                rating["source"],
                rating.get("score"),
                rating.get("review_count", 0),
                rating.get("source_url", ""),
            )
        updated += 1

    logger.info("Refreshed %d books for week %s", updated, week_date)
    return {"week_date": week_date, "books_updated": updated}


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    # Run every Thursday at 10:00 AM Taipei time
    scheduler.add_job(
        refresh_weekly_deals,
        CronTrigger(day_of_week="thu", hour=10, minute=0),
        id="weekly_kobo_refresh",
        replace_existing=True,
    )
    return scheduler
