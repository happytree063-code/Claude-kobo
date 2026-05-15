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
from datetime import date
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup


KOBO_HOME_URL = "https://www.kobo.com/tw/zh"
KOBO_FEATURED_API = "https://www.kobo.com/api/kobo-ui/storeapi/v2/products/list/featured"
# Public Google Calendar (iCal) maintained by joanneinhk.com — lists daily Kobo 99 books
KOBO_ICAL_URL = (
    "https://calendar.google.com/calendar/ical/"
    "52ddd966361e1aafe52f5ef8c3b19a4d1538e64fdd3ccef1a0acc2bb83bb971d"
    "%40group.calendar.google.com/public/basic.ics"
)
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
# Source 1: Google Calendar iCal (simplest, most reliable)
# ---------------------------------------------------------------------------

def _get_kobo_via_calendar() -> list[dict]:
    """Fetch today's Kobo 99 books from a public Google Calendar iCal feed."""
    try:
        resp = requests.get(KOBO_ICAL_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        print(f"[ICAL] status={resp.status_code}  size={len(resp.text)}")
        if resp.status_code != 200:
            return []

        today = date.today()
        books: list[dict] = []

        for raw_event in re.split(r"BEGIN:VEVENT", resp.text)[1:]:
            end = raw_event.find("END:VEVENT")
            ev = raw_event[:end] if end >= 0 else raw_event

            # Unfold iCal line continuations (lines starting with space/tab)
            ev = re.sub(r"\r?\n[ \t]", "", ev)

            # Date
            m = re.search(r"DTSTART[^:]*:(\d{8})", ev)
            if not m:
                continue
            try:
                ev_date = date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:8]))
            except ValueError:
                continue
            if ev_date != today:
                continue

            # Summary (book title)
            sm = re.search(r"SUMMARY:(.+?)(?:\r?\n[A-Z]|\Z)", ev, re.DOTALL)
            title = sm.group(1).strip().replace("\\n", " ").replace("\\,", ",") if sm else ""

            # URL
            um = re.search(r"URL:(.+?)(?:\r?\n[A-Z]|\Z)", ev, re.DOTALL)
            url = um.group(1).strip() if um else ""
            if not url.startswith("http"):
                url = KOBO_HOME_URL

            print(f"[ICAL] TODAY: '{title[:60]}' → {url[:80]}")
            if title:
                books.append({"title": title, "author": "未知作者", "price": "NT$99", "url": url})

        return books
    except Exception as e:
        print(f"[ICAL] error: {e}")
        return []


# ---------------------------------------------------------------------------
# Source 2: Kobo API via Playwright (intercept featured list calls)
# ---------------------------------------------------------------------------

def _parse_book_item(item: dict) -> dict | None:
    title = item.get("title") or item.get("Title")
    if not title:
        return None

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

    # URL — itemPageUrl is the correct field
    url = None
    for field in ("itemPageUrl", "Uri", "uri", "Url", "url", "canonicalUrl", "ProductUrl"):
        val = item.get(field)
        if val and isinstance(val, str) and len(val) > 3:
            url = val if val.startswith("http") else f"https://www.kobo.com{val}"
            break
    if not url:
        slug = item.get("slug") or item.get("Slug")
        if slug:
            url = f"https://www.kobo.com/tw/zh/ebook/{slug}"

    # Price — field is "pricing" → dict with "ourPrice" → dict with "price"
    price = None
    p = item.get("pricing")
    if isinstance(p, dict):
        for pk in ("ourPrice", "vipPrice", "salePrice", "regularPrice"):
            nested = p.get(pk)
            if isinstance(nested, dict):
                v = nested.get("price") or nested.get("amount") or nested.get("Amount")
                if v is not None:
                    price = f"NT${int(float(v))}"; break
        if not price:
            v = p.get("currentPrice") or p.get("price") or p.get("amount")
            if v is not None:
                price = f"NT${int(float(v))}"
    elif isinstance(p, (int, float)):
        price = f"NT${int(p)}"

    # Rating — field is "rating" → dict with "averageRating" + "numberOfRatings"
    kobo_rating = None
    raw = item.get("rating")
    if isinstance(raw, dict):
        avg = raw.get("averageRating") or raw.get("Average") or raw.get("average")
        cnt = raw.get("numberOfRatings") or raw.get("Count") or raw.get("count") or 0
        if avg and float(avg) > 0:
            kobo_rating = f"{float(avg):.1f}/5 ({int(cnt):,} 則評分)" if cnt else f"{float(avg):.1f}/5"

    return {
        "title": title,
        "author": author or "未知作者",
        "price": price,
        "url": url or KOBO_HOME_URL,
        "kobo_rating": kobo_rating,
    }


def _extract_books_from_featured_list(data: dict) -> tuple[list[dict], str]:
    list_name = data.get("Name") or data.get("name") or data.get("Title") or ""
    items = data.get("Items") or data.get("items") or data.get("Books") or data.get("books") or []
    books = [b for item in items if isinstance(item, dict) for b in [_parse_book_item(item)] if b]
    return books, list_name


def _get_kobo_via_playwright() -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []

    spotlight_books: list[dict] = []
    all_books: list[dict] = []

    def on_response(resp):
        try:
            if resp.status != 200 or "json" not in resp.headers.get("content-type", ""):
                return
            if "products/list/featured" not in resp.url:
                return
            data = resp.json()
            is_spotlight = "Spotlight" in resp.url
            books, _ = _extract_books_from_featured_list(data)
            for b in books:
                if is_spotlight:
                    spotlight_books.append(b)
                all_books.append(b)
        except Exception:
            pass

    print("Launching Playwright...")
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
        except Exception as e:
            print(f"Playwright error: {e}")
        finally:
            browser.close()

    print(f"[PW] spotlight={len(spotlight_books)}  all={len(all_books)}")
    for b in spotlight_books:
        print(f"  [SPOT] price={b.get('price')} title={b.get('title','?')[:50]}")

    # Return spotlight books (most likely daily deals), fall back to 99-NT books
    if spotlight_books:
        return spotlight_books
    return [b for b in all_books if b.get("price") == "NT$99"]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def get_kobo_daily_deal() -> list[dict]:
    """Return today's Kobo 99 NT deal books. Tries multiple sources."""
    # 1. Google Calendar (simplest)
    books = _get_kobo_via_calendar()
    if books:
        return books

    # 2. Playwright API intercept
    print("Calendar failed, trying Playwright...")
    return _get_kobo_via_playwright()


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

def _clean_title(title: str) -> str:
    """Remove parenthetical notes and edition info for better search results."""
    title = re.sub(r"（[^）]{0,30}）", "", title)
    title = re.sub(r"\([^)]{0,30}\)", "", title)
    title = re.sub(r"【[^】]{0,30}】", "", title)
    return title.strip()


def get_goodreads_rating(title: str, author: str | None = None) -> str | None:
    try:
        query = f"{_clean_title(title)} {author}" if author and author != "未知作者" else _clean_title(title)
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
        q = f"intitle:{_clean_title(title)}"
        if author and author != "未知作者":
            # Strip country prefix like [美] [日]
            clean_author = re.sub(r"^\[[^\]]+\]", "", author).strip()
            q += f"+inauthor:{clean_author}"
        resp = requests.get(
            f"https://www.googleapis.com/books/v1/volumes?q={quote(q)}&maxResults=5",
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


def get_books_com_tw_rating(title: str) -> str | None:
    """Search 博客來 for book rating."""
    try:
        search_url = f"https://search.books.com.tw/search/query/key/{quote(_clean_title(title))}/cat/BKM"
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
        for item in soup.select(".item"):
            rating_el = item.select_one("[class*='star']")
            if rating_el:
                style = rating_el.get("style", "")
                m = re.search(r"width:\s*(\d+(?:\.\d+)?)%", style)
                if m:
                    pct = float(m.group(1))
                    stars = round(pct / 20, 1)
                    return f"{stars}/5"
    except Exception as e:
        print(f"博客來 error: {e}")
    return None


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

NUMBER_ICONS = ["①", "②", "③", "④", "⑤"]


def build_message(books: list[dict], ratings_list: list[dict[str, str | None]]) -> str:
    lines = ["📚 今日 Kobo 每日 99 元好書", "─────────────────"]

    for i, (book, ratings) in enumerate(zip(books, ratings_list)):
        if len(books) > 1:
            lines.append(f"\n{NUMBER_ICONS[i] if i < len(NUMBER_ICONS) else str(i+1)} {book.get('title', '未知書名')}")
        else:
            lines.append(f"📖 書名：{book.get('title', '未知書名')}")

        lines.append(f"✍️  作者：{book.get('author', '未知作者')}")
        lines.append(f"💰 價格：{book.get('price') or 'NT$99'}")

        rated = {k: v for k, v in ratings.items() if v}
        if rated:
            lines.append("⭐ 評分：" + " | ".join(f"{p} {r}" for p, r in rated.items()))
        else:
            lines.append("⭐ 評分：（暫無資料）")

        lines.append(f"🔗 {book.get('url', KOBO_HOME_URL)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LINE
# ---------------------------------------------------------------------------

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
    books = get_kobo_daily_deal()

    if not books:
        send_line_message(
            user_id,
            "⚠️ 無法自動取得今日 Kobo 每日特惠書籍\n請手動查看：\nhttps://www.kobo.com/tw/zh",
            token,
        )
        return

    print(f"Found {len(books)} book(s)")
    ratings_list: list[dict[str, str | None]] = []

    for book in books:
        title = book["title"]
        author = book.get("author")
        print(f"  → {title}")

        ratings: dict[str, str | None] = {}
        if book.get("kobo_rating"):
            ratings["Kobo"] = book["kobo_rating"]
        ratings["博客來"] = get_books_com_tw_rating(title)
        time.sleep(0.5)
        ratings["Goodreads"] = get_goodreads_rating(title, author)
        time.sleep(0.5)
        ratings["Google Books"] = get_google_books_rating(title, author)

        print(f"     ratings: {ratings}")
        ratings_list.append(ratings)

    message = build_message(books, ratings_list)
    print(f"Sending:\n{message}")
    send_line_message(user_id, message, token)


if __name__ == "__main__":
    main()
