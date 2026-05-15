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
KOBO_SEARCH_99_URL = (
    "https://www.kobo.com/tw/zh/search"
    "?query=&f=Price%3A99&f=Language%3AZh&sort=NewlyAdded"
)
KOBO_DAILY_DEAL_URL = "https://www.kobo.com/tw/zh/p/daily-deal"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


# ---------------------------------------------------------------------------
# Kobo JSON / API helpers
# ---------------------------------------------------------------------------

def _extract_book_from_json(data) -> dict | None:
    """Try to pull one book out of any JSON shape Kobo's API returns."""
    candidates = []
    if isinstance(data, dict):
        if data.get("Title") or data.get("title") or data.get("name"):
            candidates.append(data)
        for key in ("Items", "Books", "Results", "Products", "books", "items", "results"):
            val = data.get(key)
            if isinstance(val, list) and val:
                candidates.append(val[0])
    elif isinstance(data, list) and data:
        candidates.append(data[0])

    for c in candidates:
        if not isinstance(c, dict):
            continue
        title = c.get("Title") or c.get("title") or c.get("Name") or c.get("name")
        if not title:
            continue

        author = None
        for k in ("Contributors", "contributors", "Authors", "authors"):
            raw = c.get(k)
            if isinstance(raw, list) and raw:
                first = raw[0]
                author = first.get("Name") or first.get("name") if isinstance(first, dict) else str(first)
                break

        url = c.get("Uri") or c.get("uri") or c.get("Url") or c.get("url") or c.get("canonicalUrl")
        if url and not url.startswith("http"):
            url = f"https://www.kobo.com{url}"

        price = None
        for pk in ("CurrentPrice", "SalePrice", "Price", "price"):
            p = c.get(pk)
            if isinstance(p, dict):
                amt = p.get("Amount") or p.get("amount") or p.get("ListPrice")
                if amt is not None:
                    price = f"NT${int(float(amt))}"; break
            elif isinstance(p, (int, float)):
                price = f"NT${int(p)}"; break

        return {
            "title": title,
            "author": author or "未知作者",
            "price": price or "NT$99",
            "url": url or KOBO_HOME_URL,
        }
    return None


def _extract_book_from_dom(page) -> dict | None:
    """Use page.evaluate to pull the first book card from the rendered DOM."""
    try:
        page.wait_for_selector(
            "a[href*='/ebook/'], [data-testid='book-card'], [class*='BookCard']",
            timeout=6000,
        )
    except Exception:
        pass

    return page.evaluate("""() => {
        // Try product cards with ebook links
        const cards = document.querySelectorAll('a[href*="/ebook/"]');
        for (const card of cards) {
            const root = card.closest('[class*="BookCard"], [data-testid="book-card"], article, li') || card;
            const title = (
                root.querySelector('[class*="title"], [class*="Title"], h2, h3, h4')
                || card.querySelector('span, div')
            )?.textContent?.trim();
            const author = root.querySelector(
                '[class*="author"], [class*="Author"], [class*="contributor"]'
            )?.textContent?.trim();
            if (title && title.length > 1) {
                const href = card.href || card.getAttribute('href') || '';
                return {
                    title,
                    author: author || null,
                    url: href.startsWith('http') ? href : 'https://www.kobo.com' + href,
                };
            }
        }
        return null;
    }""")


# ---------------------------------------------------------------------------
# Playwright scraper (primary)
# ---------------------------------------------------------------------------

def _get_kobo_via_playwright() -> dict | None:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("Playwright not installed.")
        return None

    api_books: list[dict] = []

    def on_response(resp):
        try:
            if resp.status != 200:
                return
            if "json" not in resp.headers.get("content-type", ""):
                return
            data = resp.json()
            print(f"[API] {resp.url[:120]}")
            book = _extract_book_from_json(data)
            if book and book.get("title"):
                api_books.append(book)
        except Exception:
            pass

    print("Launching Playwright (API intercept mode)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="zh-TW",
            extra_http_headers={"Accept-Language": "zh-TW,zh;q=0.9"},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.on("response", on_response)

        try:
            # --- Step 1: homepage, intercept all API calls (10 s) ---
            print(f"[DEBUG] Loading homepage...")
            page.goto(KOBO_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(10000)
            print(f"[DEBUG] Homepage done. Captured {len(api_books)} API books.")

            # Prefer 99-NT books from API
            for b in api_books:
                if "99" in str(b.get("price", "")):
                    print(f"[API] 99 NT book found: {b['title']}")
                    return b

            # --- Step 2: search page, DOM extraction ---
            print(f"[DEBUG] Loading search page...")
            api_books.clear()
            page.goto(KOBO_SEARCH_99_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(8000)
            print(f"[DEBUG] Search page title: {page.title()}")
            print(f"[DEBUG] Search API books: {len(api_books)}")

            # Try DOM first
            dom_book = _extract_book_from_dom(page)
            if dom_book and dom_book.get("title"):
                print(f"[DOM] Found: {dom_book['title']}")
                dom_book.setdefault("price", "NT$99")
                dom_book.setdefault("author", "未知作者")
                return dom_book

            # Fallback to intercepted API
            for b in api_books:
                if b.get("title"):
                    return b

            return None

        except Exception as e:
            print(f"Playwright error: {e}")
            return None
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Requests fallback (may 403 from cloud IPs)
# ---------------------------------------------------------------------------

def _get_kobo_via_requests() -> dict | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9",
    }
    try:
        resp = requests.get(KOBO_SEARCH_99_URL, headers=headers, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select("a[href*='/ebook/']"):
            title_el = a.select_one("[class*='title'], h2, h3, span")
            if title_el and title_el.get_text(strip=True):
                return {
                    "title": title_el.get_text(strip=True),
                    "author": "未知作者",
                    "price": "NT$99",
                    "url": urljoin("https://www.kobo.com", a["href"]),
                }
    except requests.RequestException as e:
        print(f"Requests error: {e}")
    return None


def get_kobo_daily_deal() -> dict | None:
    book = _get_kobo_via_playwright()
    if book and book.get("title"):
        return book
    print("Playwright returned nothing, trying requests fallback...")
    return _get_kobo_via_requests()


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

def get_goodreads_rating(title: str, author: str | None = None) -> str | None:
    try:
        query = f"{title} {author}" if author else title
        url = f"https://www.goodreads.com/search?q={quote(query)}&search_type=books"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
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
    if rated:
        for platform, rating in rated.items():
            lines.append(f"• {platform}：{rating}")
    else:
        lines.append("（暫無評分資料）")
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
