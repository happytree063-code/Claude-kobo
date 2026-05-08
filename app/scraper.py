"""
Scrapes joanneinhk.com/kobo-99/ for weekly Kobo 99-dollar book deals.
This fan-made aggregator page is far more scraper-friendly than Kobo's own blog.
"""
import re
import json
import logging
from datetime import date, timedelta
from typing import Optional
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-TW,zh-HK;q=0.9,zh;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

SOURCE_URL = "https://joanneinhk.com/kobo-99/"


def get_this_thursday() -> str:
    today = date.today()
    days_since_thursday = (today.weekday() - 3) % 7
    thursday = today - timedelta(days=days_since_thursday)
    return thursday.isoformat()


def _fetch(url: str) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        logger.error("fetch failed %s: %s", url, e)
        return None


def _extract_isbn_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/ebook/[^/]+-(\d{10,13})$", url)
    return m.group(1) if m else None


# ── Main scraper ─────────────────────────────────────────────────────────────

def scrape_weekly_deals() -> tuple[str, list[dict]]:
    """
    Scrape joanneinhk.com/kobo-99/ for this week's 99-dollar books.
    Returns (week_date, list_of_books).
    """
    week_date = get_this_thursday()
    logger.info("Scraping %s for week %s", SOURCE_URL, week_date)

    soup = _fetch(SOURCE_URL)
    if not soup:
        logger.warning("Could not fetch source page")
        return week_date, []

    books = _parse_joanne_page(soup)
    logger.info("Parsed %d books", len(books))
    return week_date, books


def _parse_joanne_page(soup: BeautifulSoup) -> list[dict]:
    books = []
    seen_titles = set()

    # Strategy 1: find all links pointing to kobo.com ebooks
    kobo_links = soup.find_all("a", href=re.compile(r"kobo\.com/zh/ebook/|kobo\.com/tw/ebook/", re.I))

    for a in kobo_links:
        href = a["href"].strip()
        if not href.startswith("http"):
            href = "https:" + href if href.startswith("//") else "https://www.kobo.com" + href

        # Title: from link text, aria-label, or nearby heading
        title = (
            a.get("aria-label", "").strip()
            or a.get("title", "").strip()
            or a.get_text(" ", strip=True)
        )

        # Walk up to find a better title from a heading
        for ancestor in a.parents:
            if ancestor.name in ("div", "li", "article", "section", "p"):
                heading = ancestor.find(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
                if heading:
                    candidate = heading.get_text(" ", strip=True)
                    if candidate and len(candidate) > len(title):
                        title = candidate
                break

        if not title or len(title) < 2:
            continue
        # Clean up: remove price tags like "NT$99" or "(99元)" appended to title
        title = re.sub(r"\s*(NT\$?\d+|[\(（]\d+[元円][\)）])\s*$", "", title).strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)

        # Cover: nearest img tag
        cover_url = None
        for ancestor in a.parents:
            img = ancestor.find("img")
            if img:
                src = img.get("src") or img.get("data-src") or img.get("data-lazy-src", "")
                if src and not src.endswith(".svg") and "logo" not in src.lower():
                    cover_url = src if src.startswith("http") else "https:" + src
                    break
            if ancestor.name in ("article", "section", "main"):
                break

        # Author: text near the link that's short and isn't the title
        author = None
        container = a.find_parent(["li", "div", "article"])
        if container:
            texts = [t.strip() for t in container.stripped_strings
                     if t.strip() and t.strip() != title and len(t.strip()) < 40]
            author_candidates = [t for t in texts if "作者" in t or "著" in t or "/" in t]
            if author_candidates:
                raw = author_candidates[0]
                raw = re.sub(r"作者[：:：]?\s*", "", raw).strip()
                author = raw.split("/")[0].strip() or None

        books.append({
            "title": title,
            "author": author,
            "isbn": _extract_isbn_from_url(href),
            "kobo_url": href,
            "cover_url": cover_url,
            "description": None,
            "original_price": None,
        })

    if books:
        return books

    # Strategy 2 fallback: look for headings followed by Kobo-looking content
    logger.info("Strategy 1 found 0 books, trying heading-based strategy")
    content = soup.find("main") or soup.find("article") or soup.find("div", class_=re.compile(r"content|entry|post"))
    if not content:
        content = soup

    for heading in content.find_all(["h2", "h3", "h4"]):
        title = heading.get_text(" ", strip=True)
        title = re.sub(r"\s*(NT\$?\d+|[\(（]\d+[元円][\)）])\s*$", "", title).strip()
        if not title or len(title) < 2 or title in seen_titles:
            continue

        # Look for a Kobo link in the next few siblings
        kobo_url = None
        cover_url = None
        for sib in heading.find_next_siblings()[:6]:
            a = sib.find("a", href=re.compile(r"kobo\.com", re.I))
            if a:
                kobo_url = a["href"]
            img = sib.find("img")
            if img:
                src = img.get("src") or img.get("data-src", "")
                if src and "logo" not in src.lower():
                    cover_url = src if src.startswith("http") else "https:" + src
            if kobo_url:
                break

        seen_titles.add(title)
        books.append({
            "title": title,
            "author": None,
            "isbn": _extract_isbn_from_url(kobo_url),
            "kobo_url": kobo_url,
            "cover_url": cover_url,
            "description": None,
            "original_price": None,
        })

    return books


# ── Single-book metadata helpers ─────────────────────────────────────────────

def fetch_book_metadata_from_kobo(kobo_url: str) -> dict:
    """Parse OG/JSON-LD tags from a Kobo ebook page."""
    try:
        resp = requests.get(kobo_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        def og(prop):
            tag = soup.find("meta", property=f"og:{prop}") or soup.find("meta", attrs={"name": prop})
            return tag["content"].strip() if tag and tag.get("content") else None

        title = og("title") or (soup.title and soup.title.get_text(strip=True) or "")
        if " | " in title:
            title = title.split(" | ")[0].strip()

        author = None
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
                if isinstance(data, dict) and data.get("author"):
                    a = data["author"]
                    if isinstance(a, list):
                        author = a[0].get("name")
                    elif isinstance(a, dict):
                        author = a.get("name")
                    if author:
                        break
            except Exception:
                pass

        return {
            "title": title,
            "author": author,
            "isbn": _extract_isbn_from_url(kobo_url),
            "kobo_url": kobo_url,
            "cover_url": og("image"),
            "description": (og("description") or "")[:300] or None,
            "original_price": None,
        }
    except Exception as e:
        logger.error("fetch_book_metadata_from_kobo %s: %s", kobo_url, e)
        return {}


def fetch_book_metadata_from_google(title: str) -> dict:
    url = f"https://www.googleapis.com/books/v1/volumes?q={quote_plus(title)}&langRestrict=zh&maxResults=1"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            return {}
        info = items[0].get("volumeInfo", {})
        authors = info.get("authors", [])
        return {
            "title": info.get("title", title),
            "author": authors[0] if authors else None,
            "isbn": None,
            "kobo_url": None,
            "cover_url": info.get("imageLinks", {}).get("thumbnail"),
            "description": (info.get("description") or "")[:300] or None,
            "original_price": None,
        }
    except Exception as e:
        logger.error("fetch_book_metadata_from_google: %s", e)
        return {}
