#!/usr/bin/env python3
"""
Kobo Daily 99 Book Reminder
每天自動抓取 Kobo 台灣每日 99 元特惠書，查詢各平台評分，透過 LINE 傳送通知。
"""

import json
import os
import re
import sys
import time
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup


KOBO_HOME_URL = "https://www.kobo.com/tw/zh"
KOBO_FEATURED_API = "https://www.kobo.com/api/kobo-ui/storeapi/v2/products/list/featured"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

_API_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": "https://www.kobo.com/tw/zh",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


# ---------------------------------------------------------------------------
# Book extraction helpers
# ---------------------------------------------------------------------------

def _parse_book_item(item: dict, fallback_price: str = "NT$99") -> dict | None:
    """Parse one book item dict from Kobo's API. Returns None if no real book data."""
    title = item.get("Title") or item.get("title")
    if not title:
        return None

    # Must have contributors to be a real book (filters out promo banners)
    contribs = (
        item.get("Contributors") or item.get("contributors")
        or item.get("Authors") or item.get("authors")
    )
    if not contribs:
        return None

    author = None
    if isinstance(contribs, list) and contribs:
        c = contribs[0]
        author = (c.get("Name") or c.get("name")) if isinstance(c, dict) else str(c)

    url = (
        item.get("Uri") or item.get("uri")
        or item.get("Url") or item.get("url")
        or item.get("canonicalUrl")
    )
    if url and not url.startswith("http"):
        url = f"https://www.kobo.com{url}"

    price = None
    for pk in ("CurrentPrice", "SalePrice", "Price", "price"):
        p = item.get(pk)
        if isinstance(p, dict):
            amt = p.get("Amount") or p.get("amount")
            if amt is not None:
                price = f"NT${int(float(amt))}"; break
        elif isinstance(p, (int, float)):
            price = f"NT${int(p)}"; break

    return {
        "title": title,
        "author": author or "未知作者",
        "price": price or fallback_price,
        "url": url or KOBO_HOME_URL,
    }


def _extract_books_from_featured_list(data: dict) -> list[dict]:
    """Return all parseable books from a featured list API response."""
    list_name = data.get("Name") or data.get("name") or data.get("Title") or ""
    items = (
        data.get("Items") or data.get("items")
        or data.get("Books") or data.get("books") or []
    )
    print(f"[LIST] '{list_name[:60]}' — {len(items)} items")
    books = []
    for item in items:
        if isinstance(item, dict):
            b = _parse_book_item(item)
            if b:
                books.append(b)
    return books, list_name


# ---------------------------------------------------------------------------
# Direct API call (try without Playwright)
# ---------------------------------------------------------------------------

def _get_kobo_via_direct_api() -> dict | None:
    """Call Kobo's featured-list API directly using requests."""
    groups = ["Home.Spotlight", "Home.DailyDeal", "Home.Featured", "DailyDeal"]
    for group in groups:
        try:
            resp = requests.get(
                KOBO_FEATURED_API,
                params={"country": "tw", "language": "zh", "featuredListGroup": group},
                headers=_API_HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"[DIRECT] group={group} → {resp.status_code}")
                continue
            data = resp.json()
            books, list_name = _extract_books_from_featured_list(data)
            print(f"[DIRECT] group={group} got {len(books)} books")
            for b in books:
                if "99" in str(b.get("price", "")):
                    print(f"[DIRECT] 99 NT book: {b['title']}")
                    return b
        except Exception as e:
            print(f"[DIRECT] group={group} error: {e}")
    return None


# ---------------------------------------------------------------------------
# Playwright scraper (API intercept)
# ---------------------------------------------------------------------------

def _get_kobo_via_playwright() -> dict | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed.")
        return None

    # Store all books found, keyed by list name
    found_books: list[tuple[str, dict]] = []  # (list_name, book)
    captured_list_ids: list[str] = []

    def on_response(resp):
        try:
            if resp.status != 200:
                return
            if "json" not in resp.headers.get("content-type", ""):
                return
            url = resp.url
            data = resp.json()

            if "products/list/featured" in url:
                # Capture full URL for later direct API use
                print(f"[API] {url[:200]}")
                books, list_name = _extract_books_from_featured_list(data)
                for b in books:
                    found_books.append((list_name, b))
        except Exception:
            pass

    print("Launching Playwright (API intercept)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="zh-TW",
            extra_http_headers={"Accept-Language": "zh-TW,zh;q=0.9"},
            user_agent=_API_HEADERS["User-Agent"],
        )
        page = ctx.new_page()
        page.on("response", on_response)

        try:
            page.goto(KOBO_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(10000)
            print(f"[DEBUG] Total (list_name, book) pairs: {len(found_books)}")

            # Priority 1: list name contains daily-deal keywords + 99 NT book
            keywords = ["每日", "daily", "今日", "99", "book of the day"]
            for list_name, book in found_books:
                name_lower = list_name.lower()
                if any(k in name_lower for k in keywords) and "99" in str(book.get("price", "")):
                    print(f"[MATCH] Keyword match in '{list_name}': {book['title']}")
                    return book

            # Priority 2: any 99 NT book with real author
            for list_name, book in found_books:
                if "99" in str(book.get("price", "")) and book.get("author") != "未知作者":
                    print(f"[MATCH] 99 NT book from '{list_name}': {book['title']}")
                    return book

            # Priority 3: first book with real author from any list
            for list_name, book in found_books:
                if book.get("author") and book["author"] != "未知作者":
                    print(f"[FALLBACK] First real book from '{list_name}': {book['title']}")
                    return book

            return None

        except Exception as e:
            print(f"Playwright error: {e}")
            return None
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Entry point for Kobo data
# ---------------------------------------------------------------------------

def get_kobo_daily_deal() -> dict | None:
    # Try direct API first (faster, no browser overhead)
    book = _get_kobo_via_direct_api()
    if book and book.get("title"):
        return book

    # Fall back to Playwright with API interception
    print("Direct API failed, trying Playwright...")
    return _get_kobo_via_playwright()


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

def get_goodreads_rating(title: str, author: str | None = None) -> str | None:
    try:
        query = f"{title} {author}" if author else title
        resp = requests.get(
            f"https://www.goodreads.com/search?q={quote(query)}&search_type=books",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        for el in soup.select(".minirating"):
            match = re.search(r"(\d+\.\d+)", el.get_text())
            if match:
                count_match = re.search(r"([\d,]+)\s*rating", el.get_text())
                count = f" ({count_match.group(1)} 則評分)" if count_match else ""
                return f"{match.group(1)}/5{count}"
    except Exception as e:
        print(f"Goodreads error: {e}")
    return None


def get_google_books_rating(title: str, author: str | None = None) -> str | None:
    try:
        query = f"intitle:{title}"
        if author:
            query += f"+inauthor:{author}"
        resp = requests.get(
            f"https://www.googleapis.com/books/v1/volumes?q={quote(query)}&maxResults=5",
            timeout=20,
        )
        for item in resp.json().get("items", []):
            vi = item.get("volumeInfo", {})
            rating = vi.get("averageRating")
            count = vi.get("ratingsCount", 0)
            if rating:
                return f"{rating}/5 ({count:,} 則評分)"
    except Exception as e:
        print(f"Google Books error: {e}")
    return None


def get_open_library_rating(title: str, author: str | None = None) -> str | None:
    try:
        params: dict = {"title": title, "limit": 5}
        if author:
            params["author"] = author
        resp = requests.get("https://openlibrary.org/search.json", params=params, timeout=20)
        for doc in resp.json().get("docs", []):
            avg = doc.get("ratings_average")
            count = doc.get("ratings_count", 0)
            if avg and count > 0:
                return f"{avg:.2f}/5 ({count:,} 則評分)"
    except Exception as e:
        print(f"Open Library error: {e}")
    return None


# ---------------------------------------------------------------------------
# Message & LINE
# ---------------------------------------------------------------------------

def build_message(book: dict, ratings: dict[str, str | None]) -> str:
    lines = [
        "📚 今日 Kobo 每日 99 元好書",
        "─────────────────",
        f"📖 書名：{book.get('title', '未知書名')}",
        f"✍️  作者：{book.get('author', '未知作者')}",
        f"💰 價格：{book.get('price', 'NT$99')}",
        "",
        "⭐ 各平台評分",
    ]
    rated = {k: v for k, v in ratings.items() if v}
    lines += [f"• {p}：{r}" for p, r in rated.items()] if rated else ["（暫無評分資料）"]
    lines += ["", f"🔗 {book.get('url', KOBO_HOME_URL)}"]
    return "\n".join(lines)


def send_line_message(user_id: str, message: str, token: str) -> None:
    resp = requests.post(
        LINE_PUSH_URL,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        json={"to": user_id, "messages": [{"type": "text", "text": message}]},
        timeout=15,
    )
    if resp.status_code == 200:
        print("LINE message sent successfully.")
    else:
        print(f"LINE API error {resp.status_code}: {resp.text}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if not token or not user_id:
        print("ERROR: LINE_CHANNEL_ACCESS_TOKEN and LINE_USER_ID must be set.")
        sys.exit(1)

    print("Fetching Kobo daily deal...")
    book = get_kobo_daily_deal()

    if not book:
        send_line_message(
            user_id,
            "⚠️ 無法自動取得今日 Kobo 每日特惠書籍\n請手動查看：\nhttps://www.kobo.com/tw/zh",
            token,
        )
        return

    print(f"Book: {book['title']} by {book.get('author', '?')}")
    print("Fetching ratings...")

    ratings: dict[str, str | None] = {}
    ratings["Goodreads"] = get_goodreads_rating(book["title"], book.get("author"))
    time.sleep(1)
    ratings["Google Books"] = get_google_books_rating(book["title"], book.get("author"))
    time.sleep(1)
    ratings["Open Library"] = get_open_library_rating(book["title"], book.get("author"))

    print(f"Ratings: {ratings}")
    message = build_message(book, ratings)
    print(f"Sending:\n{message}")
    send_line_message(user_id, message, token)


if __name__ == "__main__":
    main()
