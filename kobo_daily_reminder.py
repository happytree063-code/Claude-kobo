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
JOANNE_URL = "https://joanneinhk.com/kobo-99/"
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

_debug_item_printed = False  # print raw keys once only


def _parse_book_item(item: dict) -> dict | None:
    """Parse one book item dict from Kobo's API. Returns None if not a real book."""
    global _debug_item_printed
    title = item.get("title") or item.get("Title")
    if not title:
        return None

    # Must have authors to be a real book
    contribs = (
        item.get("authors") or item.get("Authors")
        or item.get("Contributors") or item.get("contributors")
    )
    if not contribs:
        return None

    author = None
    if isinstance(contribs, list) and contribs:
        c = contribs[0]
        author = (c.get("name") or c.get("Name")) if isinstance(c, dict) else str(c)

    # URL — itemPageUrl is the correct field (slug alone is not a full path)
    url = None
    for field in ("itemPageUrl", "Uri", "uri", "Url", "url", "canonicalUrl", "CanonicalUrl", "ProductUrl"):
        val = item.get(field)
        if val and isinstance(val, str) and len(val) > 3:
            url = val if val.startswith("http") else f"https://www.kobo.com{val}"
            break
    if not url:
        slug = item.get("slug") or item.get("Slug")
        if slug:
            url = f"https://www.kobo.com/tw/zh/ebook/{slug}"

    # Price — field is "pricing" (dict), not "Price"
    price = None
    p = item.get("pricing") or item.get("CurrentPrice") or item.get("Price") or item.get("price")
    if isinstance(p, dict):
        amt = (
            p.get("currentPrice") or p.get("CurrentPrice")
            or p.get("salePrice") or p.get("SalePrice")
            or p.get("Amount") or p.get("amount")
            or p.get("TotalPrice") or p.get("totalPrice")
        )
        if amt is not None:
            price = f"NT${int(float(amt))}"
    elif isinstance(p, (int, float)):
        price = f"NT${int(p)}"

    # Rating — Kobo API includes it directly
    kobo_rating = None
    raw = item.get("rating")
    if isinstance(raw, dict):
        avg = raw.get("Average") or raw.get("average") or raw.get("Value") or raw.get("value")
        cnt = raw.get("Count") or raw.get("count") or raw.get("RatingCount") or 0
        if avg:
            kobo_rating = f"{float(avg):.1f}/5 ({int(cnt):,} 則評分)" if cnt else f"{float(avg):.1f}/5"
    elif isinstance(raw, (int, float)) and raw > 0:
        kobo_rating = f"{raw:.1f}/5"

    if not _debug_item_printed:
        _debug_item_printed = True
        print(f"[ITEM_KEYS] {sorted(item.keys())}")
        print(f"[ITEM] price raw={item.get('pricing')} → parsed={price}")
        print(f"[ITEM] rating raw={raw} → parsed={kobo_rating}")
        print(f"[ITEM] url={url}")

    return {
        "title": title,
        "author": author or "未知作者",
        "price": price,        # None means price unknown
        "url": url or KOBO_HOME_URL,
        "kobo_rating": kobo_rating,
    }


def _extract_books_from_featured_list(data: dict) -> tuple[list[dict], str]:
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
# joanneinhk.com scraper (simplest approach)
# ---------------------------------------------------------------------------

def _get_kobo_via_joanne() -> dict | None:
    """Scrape joanneinhk.com/kobo-99/ — a blog that tracks Kobo Taiwan daily deals."""
    try:
        resp = requests.get(
            JOANNE_URL,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-TW,zh;q=0.9",
                "Referer": "https://www.google.com/",
            },
            timeout=20,
        )
        print(f"[JOANNE] status={resp.status_code}")
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        print(f"[JOANNE] page title: {soup.title.string if soup.title else '?'}")
        print(f"[JOANNE] HTML length: {len(resp.text)}")

        # Try to find the most recent book entry
        # Common patterns for WordPress / blog sites
        for selector in (
            "article", ".post", ".entry", ".book-entry",
            "table tr", ".elementor-widget-container",
        ):
            items = soup.select(selector)
            if not items:
                continue
            print(f"[JOANNE] selector '{selector}' → {len(items)} elements")

            for item in items[:3]:
                text = item.get_text(" ", strip=True)
                # Look for a Kobo link
                link = item.select_one("a[href*='kobo.com']")
                # Heuristic: look for price 99 in text
                if "99" not in text and not link:
                    continue
                # Try to find title (first heading or strong text)
                title_el = item.select_one("h1, h2, h3, h4, strong, b")
                title = title_el.get_text(strip=True) if title_el else None
                if not title:
                    continue
                # Author might be in a second line or paragraph
                author_el = item.select_one("p, td")
                author = None
                if author_el:
                    lines = [l.strip() for l in author_el.get_text("\n").split("\n") if l.strip()]
                    if len(lines) >= 2:
                        author = lines[1]
                book_url = link["href"] if link else KOBO_HOME_URL
                print(f"[JOANNE] Found: title='{title}' author='{author}' url='{book_url}'")
                return {
                    "title": title,
                    "author": author or "未知作者",
                    "price": "NT$99",
                    "url": book_url,
                }

        # Show a snippet of the actual HTML to help debug selectors
        body = soup.find("body")
        snippet = body.get_text(" ", strip=True)[:500] if body else ""
        print(f"[JOANNE] body text snippet:\n{snippet}")

    except Exception as e:
        print(f"[JOANNE] error: {e}")
    return None


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
                print(f"[API] {url[:200]}")
                is_spotlight = "Spotlight" in url
                books, list_name = _extract_books_from_featured_list(data)
                for b in books:
                    found_books.append((is_spotlight, list_name, b))
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
            print(f"[DEBUG] Total books: {len(found_books)}")
            for is_sp, ln, b in found_books:
                tag = "SPOTLIGHT" if is_sp else "LIST"
                print(f"  [{tag}] price={b.get('price')} title={b.get('title','?')[:40]}")

            # Priority 1: Spotlight list + actual 99 NT price
            for is_sp, ln, book in found_books:
                if is_sp and book.get("price") == "NT$99":
                    print(f"[MATCH] Spotlight 99 NT: {book['title']}")
                    return book

            # Priority 2: Any list + actual 99 NT price
            for is_sp, ln, book in found_books:
                if book.get("price") == "NT$99":
                    print(f"[MATCH] 99 NT from list: {book['title']}")
                    return book

            # Priority 3: Spotlight book (even without confirmed price)
            for is_sp, ln, book in found_books:
                if is_sp:
                    book.setdefault("price", "NT$99")
                    print(f"[FALLBACK] Spotlight book: {book['title']}")
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
    # Try simplest approach first: third-party blog
    book = _get_kobo_via_joanne()
    if book and book.get("title"):
        return book

    # Try Kobo's internal API directly
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


def get_books_com_tw_rating(title: str) -> str | None:
    """Search 博客來 for book rating (Taiwan's largest book site)."""
    try:
        search_url = f"https://search.books.com.tw/search/query/key/{quote(title)}/cat/BKM"
        resp = requests.get(
            search_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "zh-TW,zh;q=0.9",
                "Referer": "https://www.books.com.tw/",
            },
            timeout=15,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        # 博客來 shows star ratings as "X.X 顆星" or in class "star-"
        for item in soup.select(".item"):
            rating_el = item.select_one(".rating-star, [class*='star']")
            count_el = item.select_one(".num, [class*='num']")
            if rating_el:
                match = re.search(r"(\d+\.?\d*)", rating_el.get("style", "") + rating_el.get_text())
                if match:
                    count = count_el.get_text(strip=True) if count_el else ""
                    count_str = f" ({count})" if count else ""
                    return f"{match.group(1)}/5{count_str}"
        # Fallback: look for rating text directly
        rating_text = soup.select_one(".evaluate, .rating")
        if rating_text:
            match = re.search(r"(\d+\.\d+)", rating_text.get_text())
            if match:
                return f"{match.group(1)}/5"
    except Exception as e:
        print(f"博客來 error: {e}")
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
        f"💰 價格：{book.get('price') or 'NT$99'}",
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
    if book.get("kobo_rating"):
        ratings["Kobo"] = book["kobo_rating"]
    ratings["博客來"] = get_books_com_tw_rating(book["title"])
    time.sleep(1)
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
