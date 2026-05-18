#!/usr/bin/env python3
"""
Kobo Daily 99 Book Reminder
每天自動抓取 Kobo 台灣每日 99 元特惠書，查詢各平台評分，透過 LINE 傳送通知。
"""

import json
import csv
import io
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
# Public Google Sheet maintained by joanneinhk.com — Kobo 99 books with Goodreads/Amazon ratings
JOANNEINHK_SHEET_CSV = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vSzYRJXRm07AGxb15c7SNnMX9gup7HmodrJIyvGaa2eTpZE8n7Mo4FvnvxGKN9h6aWrwW-rDD1tC5zK"
    "/pub?gid=722977077&single=true&output=csv"
)

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
    """Return today's Kobo 99 NT deal books.

    iCal identifies the correct books by title; Playwright provides full
    metadata (author, specific URL, Kobo rating).  Both run every time and
    results are merged by title so we always get complete data.
    """
    ical_books = _get_kobo_via_calendar()
    pw_books = _get_kobo_via_playwright()

    if not ical_books and not pw_books:
        return []

    # Playwright only (iCal failed) — keep NT$99 spotlight books
    if not ical_books:
        books_99 = [b for b in pw_books if b.get("price") == "NT$99"]
        return books_99 or pw_books[:2]

    # iCal only (Playwright failed) — titles correct but no URL/author
    if not pw_books:
        return ical_books

    # Both succeeded — enrich iCal books with Playwright metadata
    result = []
    for ical in ical_books:
        key = _clean_title(ical["title"])[:8]
        match = next(
            (b for b in pw_books if _clean_title(b.get("title", ""))[:8] == key),
            None,
        )
        if match:
            print(f"  [ENRICH] '{ical['title'][:40]}' ← URL+author from Playwright")
            result.append(match)
        else:
            print(f"  [ICAL_ONLY] '{ical['title'][:40]}' — no Playwright match")
            result.append(ical)

    return result


# ---------------------------------------------------------------------------
# Ratings
# ---------------------------------------------------------------------------

def _clean_title(title: str) -> str:
    """Simplify title for search: strip edition notes, annotations, and subtitles."""
    title = re.sub(r"（[^）]{0,30}）", "", title)
    title = re.sub(r"\([^)]{0,30}\)", "", title)
    title = re.sub(r"【[^】]{0,30}】", "", title)
    # Keep only the main title before ：or : (subtitle separator)
    title = re.split(r"[：:]", title)[0]
    return title.strip()


def _get_sheet_ratings(title: str) -> dict[str, str | None]:
    """Fetch pre-compiled ratings from joanneinhk.com's public Google Sheet.

    Sheet columns (confirmed): 日期 | 圖片 | 書名 | 原文名 | Kobo原價 | Kobo Plus |
    (empty) | Good Reads | (count) | (empty) | Amazon | (count) | (empty) |
    讀墨 | (count) | (empty) | 博客來 | (count)
    """
    try:
        resp = requests.get(
            JOANNEINHK_SHEET_CSV,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        print(f"[Sheet] status={resp.status_code}  size={len(resp.content)}")
        if resp.status_code != 200:
            return {}

        # Must decode as UTF-8 explicitly — requests may default to Latin-1
        rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8"))))

        # Find header row containing 書名 and Good
        header_idx = next(
            (i for i, row in enumerate(rows) if any("書名" in c for c in row) and any("Good" in c for c in row)),
            None,
        )
        if header_idx is None:
            print(f"[Sheet] header row not found; row[0]={rows[0][:5] if rows else '[]'}")
            return {}

        headers = rows[header_idx]
        title_col = gr_col = amz_col = readmoo_col = books_col = price_col = None
        for i, h in enumerate(headers):
            h = h.strip()
            if "書名" in h:
                title_col = i
            elif "Good" in h or "Reads" in h:
                gr_col = i
            elif "Amazon" in h or "Amz" in h:
                amz_col = i
            elif "讀" in h or "誠" in h:    # 讀墨 or 誠墾
                readmoo_col = i
            elif "博客" in h:
                books_col = i
            elif "原價" in h or "Kobo" in h and price_col is None:
                price_col = i

        print(f"[Sheet] cols → title={title_col} GR={gr_col} Amz={amz_col} 讀墨={readmoo_col} 博客來={books_col} 原價={price_col}")

        # Match book by first 6 chars of cleaned title
        key = _clean_title(title)[:6]
        for row in rows[header_idx + 1:]:
            if title_col is None or len(row) <= title_col:
                continue
            if _clean_title(row[title_col])[:6] != key:
                continue

            def _cell(col: int | None) -> str:
                if col is None or len(row) <= col:
                    return ""
                return row[col].strip()

            def _fmt(val_col: int | None, suffix: str = "/5") -> str | None:
                val = _cell(val_col)
                if not val or val.lower() in ("n/a", "-", ""):
                    return None
                # Next column is usually the review count
                cnt = _cell(val_col + 1) if val_col is not None else ""
                cnt = cnt.strip("() ")
                count = f" ({cnt} 則評分)" if cnt and cnt.isdigit() and int(cnt) > 0 else ""
                if "星" in val:
                    return f"{val}{count}"
                return f"{val}{suffix}{count}"

            result: dict[str, str | None] = {}
            gr = _fmt(gr_col)
            if gr:
                result["Goodreads"] = gr
            amz = _fmt(amz_col)
            if amz:
                result["Amazon"] = amz
            rm = _fmt(readmoo_col, suffix="")
            if rm:
                result["讀墨"] = rm
            bc = _fmt(books_col, suffix="")
            if bc:
                result["博客來"] = bc
            # Original price — used as fallback when Playwright fails
            p = _cell(price_col)
            if p and p.isdigit():
                result["_kobo_price"] = f"NT${p}"

            print(f"[Sheet] ratings for '{title[:30]}': {result}")
            return result

        print(f"[Sheet] '{key}' not found in sheet")
        return {}

    except Exception as e:
        print(f"[Sheet] error: {e}")
        return {}


def _extract_english_author(author: str) -> str | None:
    """Extract English name from bilingual Kobo author strings.

    Handles:
      '[美]悉尼·霍默（Sidney Homer）、理查德·西拉（Richard Sylla）' → 'Sidney Homer'
      '珍妮佛‧高曼－威茲勒博士 Jennifer Goldman-Wetzler'           → 'Jennifer Goldman-Wetzler'
    """
    if not author or author == "未知作者":
        return None
    # English name in full-width parentheses: （Sidney Homer）
    m = re.search(r"[（(]([A-Za-z][A-Za-z\s\-\.]+)[）)]", author)
    if m and len(m.group(1).strip()) > 3:
        return m.group(1).strip()
    # English name inline (two or more capitalized words)
    m = re.search(r"\b([A-Z][a-z]+(?:[\s\-][A-Z][a-z]+)+)\b", author)
    if m:
        return m.group(1).strip()
    return None


def get_goodreads_rating(title: str, author: str | None = None) -> str | None:
    try:
        en_author = _extract_english_author(author) if author else None
        clean = _clean_title(title)
        # Use English-only query — Goodreads blocks mixed Chinese/English searches
        query = en_author if en_author else clean
        resp = requests.get(
            f"https://www.goodreads.com/search?q={quote(query)}&search_type=books",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
            },
            timeout=20,
        )
        print(f"[Goodreads] status={resp.status_code}  query={query[:60]}")
        soup = BeautifulSoup(resp.text, "html.parser")
        ratings_els = soup.select(".minirating")
        print(f"[Goodreads] found {len(ratings_els)} .minirating elements")
        for el in ratings_els:
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
        clean = _clean_title(title)
        en_author = _extract_english_author(author) if author else None
        # Plain text search (no inauthor: qualifier) matches more results than field-qualified
        q = en_author if en_author else clean
        resp = requests.get(
            f"https://www.googleapis.com/books/v1/volumes?q={quote(q)}&maxResults=5",
            timeout=20,
        )
        items = resp.json().get("items", [])
        print(f"[Google Books] query={q[:60]}  results={len(items)}")
        for item in items:
            vi = item.get("volumeInfo", {})
            rating = vi.get("averageRating")
            count = vi.get("ratingsCount", 0)
            print(f"[Google Books]   '{vi.get('title','?')[:40]}'  rating={rating}")
            if rating:
                return f"{rating}/5 ({count:,} 則評分)"
    except Exception as e:
        print(f"Google Books error: {e}")
    return None


def get_open_library_rating(title: str, author: str | None = None) -> str | None:
    """Open Library is public, no auth required, good for English books."""
    try:
        en_author = _extract_english_author(author) if author else None
        clean = _clean_title(title)
        q = en_author if en_author else clean
        resp = requests.get(
            "https://openlibrary.org/search.json",
            params={"q": q, "fields": "title,ratings_average,ratings_count", "limit": 5},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        docs = resp.json().get("docs", [])
        print(f"[Open Library] q={q[:50]}  results={len(docs)}")
        for doc in docs:
            avg = doc.get("ratings_average")
            count = doc.get("ratings_count", 0)
            if avg and float(avg) > 0 and count and int(count) > 0:
                return f"{float(avg):.2f}/5 ({int(count):,} 則評分)"
    except Exception as e:
        print(f"Open Library error: {e}")
    return None


def get_douban_rating(title: str) -> str | None:
    """Search 豆瓣 for book rating — most comprehensive for Chinese/translated books."""
    try:
        clean = _clean_title(title)
        resp = requests.get(
            f"https://search.douban.com/book/subject_search?search_text={quote(clean)}&cat=1001",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-TW,zh;q=0.9,zh-CN;q=0.8",
                "Referer": "https://book.douban.com/",
            },
            timeout=15,
        )
        print(f"[豆瓣] status={resp.status_code}  q={clean[:20]}")
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for sel in (".rating_nums", ".rating_num", "[class*='rating_nums']"):
            for el in soup.select(sel):
                text = el.get_text(strip=True)
                if re.match(r"^\d+\.\d+$", text):
                    return f"{text}/10"
        # JSON-LD fallback
        m = re.search(r'"averageRating"\s*:\s*"?(\d+\.\d+)"?', resp.text)
        if m:
            return f"{m.group(1)}/10"
        print(f"[豆瓣] no rating found in page")
    except Exception as e:
        print(f"豆瓣 error: {e}")
    return None


def get_books_com_tw_rating(title: str) -> str | None:
    """Search 博客來 for book rating."""
    try:
        clean = _clean_title(title)
        search_url = f"https://search.books.com.tw/search/query/key/{quote(clean)}/cat/BKM"
        resp = requests.get(
            search_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-TW,zh;q=0.9",
                "Referer": "https://www.books.com.tw/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=15,
        )
        print(f"[博客來] status={resp.status_code}  q={clean[:20]}")
        html = resp.text

        # CSS-selector approach (works when structure is known)
        soup = BeautifulSoup(html, "html.parser")
        for star_el in soup.find_all(style=re.compile(r"width:\s*\d")):
            style = star_el.get("style", "")
            m = re.search(r"width:\s*(\d+(?:\.\d+)?)%", style)
            if m:
                pct = float(m.group(1))
                # Star ratings are multiples of 20 (1★=20%…5★=100%)
                if 20 <= pct <= 100 and pct % 10 == 0:
                    return f"{round(pct / 20, 1)}/5"

        # Regex fallback directly on raw HTML (handles obfuscated class names)
        m = re.search(
            r'(?:star|grade|rating)[^<]{0,200}?width:\s*(\d+(?:\.\d+)?)%',
            html, re.IGNORECASE | re.DOTALL,
        )
        if m:
            pct = float(m.group(1))
            if 10 <= pct <= 100:
                return f"{round(pct / 20, 1)}/5"

        print(f"[博客來] no star rating found in page")
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

        # Primary: joanneinhk.com Google Sheet (pre-compiled Goodreads + Amazon ratings)
        sheet = _get_sheet_ratings(title)
        # Use sheet's original price as fallback when Playwright didn't provide one
        sheet_price = sheet.pop("_kobo_price", None)
        if sheet_price and not book.get("price"):
            book["price"] = sheet_price
        ratings.update(sheet)

        # Fallback scrapers for books not yet in the sheet
        if not sheet:
            ratings["豆瓣"] = get_douban_rating(title)
            time.sleep(0.5)
            ratings["Google Books"] = get_google_books_rating(title, author)
            time.sleep(0.5)
            ratings["Open Library"] = get_open_library_rating(title, author)
            time.sleep(0.5)
            ratings["Goodreads"] = get_goodreads_rating(title, author)

        print(f"     ratings: {ratings}")
        ratings_list.append(ratings)

    message = build_message(books, ratings_list)
    print(f"Sending:\n{message}")
    send_line_message(user_id, message, token)


if __name__ == "__main__":
    main()
