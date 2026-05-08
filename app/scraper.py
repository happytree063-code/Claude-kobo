"""
Scrapes the Kobo Taiwan blog for weekly 99-dollar book deals.
The blog posts every Thursday with titles like "本週 99 元電子書特賣".
"""
import re
import logging
from datetime import date, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BLOG_BASE = "https://www.kobo.com/zh/blog"
# Keywords that identify the weekly 99-deal posts
DEAL_KEYWORDS = ["99", "特賣", "優惠", "特價", "週週"]


def get_this_thursday() -> str:
    today = date.today()
    days_since_thursday = (today.weekday() - 3) % 7
    thursday = today - timedelta(days=days_since_thursday)
    return thursday.isoformat()


def fetch_blog_listing(page: int = 1) -> Optional[BeautifulSoup]:
    url = BLOG_BASE if page == 1 else f"{BLOG_BASE}?page={page}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        logger.error("Failed to fetch blog listing page %d: %s", page, e)
        return None


def find_deal_article_url(soup: BeautifulSoup) -> Optional[str]:
    """Find the most recent 99-deal article link from the blog listing."""
    # Kobo blog article cards typically have <a> tags with blog post URLs
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)
        if not ("/blog/" in href):
            continue
        if any(kw in text for kw in DEAL_KEYWORDS) or any(kw in href for kw in ["99", "deal", "special"]):
            full_url = href if href.startswith("http") else f"https://www.kobo.com{href}"
            return full_url

    # Fallback: look inside article/card titles
    for tag in soup.find_all(["h2", "h3", "h4"]):
        text = tag.get_text(strip=True)
        if any(kw in text for kw in DEAL_KEYWORDS):
            parent_a = tag.find_parent("a") or tag.find("a")
            if parent_a and parent_a.get("href"):
                href = parent_a["href"]
                return href if href.startswith("http") else f"https://www.kobo.com{href}"
    return None


def fetch_article(url: str) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        logger.error("Failed to fetch article %s: %s", url, e)
        return None


def parse_books_from_article(soup: BeautifulSoup, article_url: str) -> list[dict]:
    """
    Parse book entries from a Kobo blog deal article.
    Returns list of dicts with title, author, kobo_url, cover_url, description.
    """
    books = []

    # Strategy 1: look for product/book cards with links to /zh/ebook/
    for a in soup.find_all("a", href=re.compile(r"/zh/ebook/")):
        href = a["href"]
        full_url = href if href.startswith("http") else f"https://www.kobo.com{href}"

        # Try to find cover image near this link
        img = a.find("img") or (a.parent and a.parent.find("img"))
        cover_url = img["src"] if img and img.get("src") else None

        # Title from alt text, aria-label, or text
        title = (
            a.get("aria-label")
            or (img and img.get("alt"))
            or a.get_text(strip=True)
        )
        if not title:
            continue

        # Author – often in a sibling/nearby element
        author = None
        parent = a.parent
        if parent:
            for sibling in parent.find_next_siblings():
                sibling_text = sibling.get_text(strip=True)
                if sibling_text and len(sibling_text) < 60:
                    author = sibling_text
                    break

        books.append({
            "title": title.strip(),
            "author": author,
            "isbn": _extract_isbn_from_url(full_url),
            "kobo_url": full_url,
            "cover_url": cover_url,
            "description": None,
            "original_price": None,
        })

    if books:
        return books

    # Strategy 2: look for structured list items in the article body
    article_body = (
        soup.find("article")
        or soup.find(class_=re.compile(r"blog|post|content|article", re.I))
        or soup.find("main")
    )
    if not article_body:
        article_body = soup

    for li in article_body.find_all("li"):
        text = li.get_text(" ", strip=True)
        a_tag = li.find("a", href=True)
        href = a_tag["href"] if a_tag else None
        img = li.find("img")

        if not text or len(text) < 3:
            continue

        title = a_tag.get_text(strip=True) if a_tag else text.split("：")[0].split("|")[0].strip()
        if len(title) < 2:
            continue

        author = None
        if "作者" in text or "/" in text:
            parts = re.split(r"作者[：:]|/", text, maxsplit=1)
            if len(parts) > 1:
                author = parts[1].split()[0]

        kobo_url = None
        if href:
            kobo_url = href if href.startswith("http") else f"https://www.kobo.com{href}"

        books.append({
            "title": title,
            "author": author,
            "isbn": _extract_isbn_from_url(kobo_url) if kobo_url else None,
            "kobo_url": kobo_url,
            "cover_url": img["src"] if img and img.get("src") else None,
            "description": None,
            "original_price": None,
        })

    return books


def _extract_isbn_from_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/ebook/[^/]+-(\d{10,13})$", url)
    return m.group(1) if m else None


def fetch_book_metadata_from_kobo(kobo_url: str) -> dict:
    """
    Given a Kobo ebook page URL, return title/author/cover/description.
    Uses Open Graph meta tags which are usually accessible.
    """
    try:
        resp = requests.get(kobo_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        def og(prop):
            tag = soup.find("meta", property=f"og:{prop}") or soup.find("meta", attrs={"name": prop})
            return tag["content"].strip() if tag and tag.get("content") else None

        title = og("title") or soup.title and soup.title.get_text(strip=True)
        # Strip " | Kobo" suffix
        if title and " | " in title:
            title = title.split(" | ")[0].strip()

        description = og("description")
        cover_url = og("image")

        # Author from JSON-LD or meta
        author = None
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json
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

        isbn = _extract_isbn_from_url(kobo_url)
        return {
            "title": title,
            "author": author,
            "isbn": isbn,
            "kobo_url": kobo_url,
            "cover_url": cover_url,
            "description": description,
            "original_price": None,
        }
    except Exception as e:
        logger.error("fetch_book_metadata_from_kobo failed for %s: %s", kobo_url, e)
        return {}


def fetch_book_metadata_from_google(title: str) -> dict:
    """Fallback: look up book info from Google Books API by title."""
    from urllib.parse import quote_plus
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
        logger.error("fetch_book_metadata_from_google failed: %s", e)
        return {}


def scrape_weekly_deals() -> tuple[str, list[dict]]:
    """
    Main entry point. Returns (week_date, list_of_books).
    week_date is the ISO date of the most recent Thursday.
    """
    week_date = get_this_thursday()
    logger.info("Scraping Kobo blog for week of %s", week_date)

    for page in range(1, 4):
        soup = fetch_blog_listing(page)
        if not soup:
            break
        article_url = find_deal_article_url(soup)
        if article_url:
            logger.info("Found deal article: %s", article_url)
            article_soup = fetch_article(article_url)
            if article_soup:
                books = parse_books_from_article(article_soup, article_url)
                logger.info("Parsed %d books from article", len(books))
                return week_date, books
            break

    logger.warning("Could not find deal article; returning empty list")
    return week_date, []
