"""
Fetches book ratings from multiple sources:
- Google Books API (free, no key needed for basic search)
- 博客來 (books.com.tw) - Taiwan's largest bookstore
- Momo Books (momoshop.com.tw)
"""
import re
import logging
from typing import Optional
from urllib.parse import quote_plus

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
}


# ── Google Books ────────────────────────────────────────────────────────────

def fetch_google_books(title: str, author: Optional[str] = None) -> dict:
    query = title
    if author:
        query += f" {author}"
    url = f"https://www.googleapis.com/books/v1/volumes?q={quote_plus(query)}&langRestrict=zh&maxResults=3"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items:
            return {}
        # Pick best match
        vol = _best_google_match(items, title)
        info = vol.get("volumeInfo", {})
        rating = info.get("averageRating")
        count = info.get("ratingsCount", 0)
        book_id = vol.get("id", "")
        return {
            "source": "Google Books",
            "score": f"{rating}/5" if rating else None,
            "review_count": count,
            "source_url": f"https://books.google.com/books?id={book_id}",
            "cover_url": info.get("imageLinks", {}).get("thumbnail"),
            "description": info.get("description", "")[:300] if info.get("description") else None,
        }
    except Exception as e:
        logger.error("Google Books error for '%s': %s", title, e)
        return {}


def _best_google_match(items: list, title: str) -> dict:
    title_lower = title.lower()
    for item in items:
        info_title = item.get("volumeInfo", {}).get("title", "").lower()
        if title_lower in info_title or info_title in title_lower:
            return item
    return items[0]


# ── 博客來 (books.com.tw) ───────────────────────────────────────────────────

def fetch_books_com_tw(title: str) -> dict:
    search_url = f"https://search.books.com.tw/search/query/key/{quote_plus(title)}/cat/BKA"
    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Find first result
        result = soup.find("li", class_=re.compile(r"item"))
        if not result:
            # Try alternative selector
            result = soup.find("div", class_=re.compile(r"item|result"))

        if not result:
            return {}

        # Rating – 博客來 uses star ratings in span elements
        rating_tag = result.find("span", class_=re.compile(r"star|rating|score", re.I))
        score = None
        if rating_tag:
            score_text = rating_tag.get_text(strip=True)
            m = re.search(r"[\d.]+", score_text)
            score = f"{m.group()}/5" if m else score_text

        # Review count
        review_tag = result.find(string=re.compile(r"\d+\s*則"))
        review_count = 0
        if review_tag:
            m = re.search(r"\d+", review_tag)
            review_count = int(m.group()) if m else 0

        # Product URL
        a_tag = result.find("a", href=re.compile(r"books.com.tw"))
        book_url = a_tag["href"] if a_tag else search_url

        if score or review_count:
            return {
                "source": "博客來",
                "score": score,
                "review_count": review_count,
                "source_url": book_url,
            }
        return {}
    except Exception as e:
        logger.error("博客來 error for '%s': %s", title, e)
        return {}


# ── 讀冊生活 (taaze.tw) ─────────────────────────────────────────────────────

def fetch_taaze(title: str) -> dict:
    search_url = f"https://www.taaze.tw/search.html?keyword={quote_plus(title)}&category_id=0"
    try:
        resp = requests.get(search_url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Find rating elements
        rating_el = soup.find(class_=re.compile(r"star|score|rating", re.I))
        score = None
        if rating_el:
            score_text = rating_el.get_text(strip=True)
            m = re.search(r"[\d.]+", score_text)
            if m:
                score = f"{m.group()}/5"

        product_link = soup.find("a", href=re.compile(r"taaze.tw/sell"))
        book_url = product_link["href"] if product_link else search_url

        if score:
            return {
                "source": "讀冊生活",
                "score": score,
                "review_count": 0,
                "source_url": book_url,
            }
        return {}
    except Exception as e:
        logger.error("讀冊 error for '%s': %s", title, e)
        return {}


# ── Main entry point ────────────────────────────────────────────────────────

def fetch_all_ratings(title: str, author: Optional[str] = None) -> list[dict]:
    """
    Returns a list of rating dicts from all available sources.
    Each dict: {source, score, review_count, source_url}
    """
    results = []

    google = fetch_google_books(title, author)
    if google.get("score") or google.get("review_count"):
        results.append(google)

    books_tw = fetch_books_com_tw(title)
    if books_tw.get("score") or books_tw.get("review_count"):
        results.append(books_tw)

    taaze = fetch_taaze(title)
    if taaze.get("score"):
        results.append(taaze)

    return results
