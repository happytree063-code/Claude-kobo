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


KOBO_DAILY_DEAL_URLS = [
    "https://www.kobo.com/tw/zh/p/daily-deal",
    "https://www.kobo.com/tw/zh",
]
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def get_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def _parse_next_data_book(data: dict) -> dict:
    """Extract book fields from a Next.js pageProps or product dict."""
    title = data.get("title") or data.get("name") or data.get("Title")

    author = None
    contributors = (
        data.get("contributors")
        or data.get("authors")
        or data.get("Contributors")
    )
    if isinstance(contributors, list) and contributors:
        c = contributors[0]
        author = c.get("name") or c.get("Name") if isinstance(c, dict) else str(c)
    elif isinstance(contributors, dict):
        author = contributors.get("name")

    price = None
    pricing = data.get("pricing") or data.get("CurrentPrice") or data.get("price")
    if isinstance(pricing, dict):
        raw = pricing.get("regularPrice") or pricing.get("listPrice") or pricing.get("price")
        price = f"NT${raw}" if raw else None
    elif isinstance(pricing, (int, float)):
        price = f"NT${int(pricing)}"
    elif isinstance(pricing, str) and pricing:
        price = pricing if "$" in pricing else f"NT${pricing}"

    url = data.get("canonicalUrl") or data.get("url") or data.get("ProductUrl")
    if url and not url.startswith("http"):
        url = urljoin("https://www.kobo.com", url)

    return {"title": title, "author": author, "price": price or "NT$99", "url": url}


def _try_next_data(soup: BeautifulSoup) -> dict | None:
    """Parse book info from Next.js __NEXT_DATA__ embedded JSON."""
    script = soup.find("script", {"id": "__NEXT_DATA__"})
    if not script:
        return None
    try:
        data = json.loads(script.string or "")
        page_props = data.get("props", {}).get("pageProps", {})
        for key in ("book", "product", "featuredBook", "dailyDeal", "promotion"):
            item = page_props.get(key)
            if isinstance(item, dict):
                parsed = _parse_next_data_book(item)
                if parsed.get("title"):
                    return parsed
        for key in ("products", "books", "items"):
            collection = page_props.get(key)
            if isinstance(collection, list) and collection:
                parsed = _parse_next_data_book(collection[0])
                if parsed.get("title"):
                    return parsed
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


def _try_json_ld(soup: BeautifulSoup, page_url: str) -> dict | None:
    """Parse book info from JSON-LD structured data."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") in ("Book", "Product"):
                    author_data = item.get("author", {})
                    if isinstance(author_data, list):
                        author_data = author_data[0] if author_data else {}
                    author = author_data.get("name") if isinstance(author_data, dict) else str(author_data)
                    raw_url = item.get("url", page_url)
                    url = raw_url if raw_url.startswith("http") else urljoin("https://www.kobo.com", raw_url)
                    return {"title": item.get("name"), "author": author, "price": "NT$99", "url": url}
        except (json.JSONDecodeError, AttributeError):
            continue
    return None


def _try_html(soup: BeautifulSoup, page_url: str) -> dict | None:
    """Fallback: parse book info from HTML element selectors."""
    selectors = [
        "[class*='BookCard']",
        "[class*='book-card']",
        "[class*='product-item']",
        "article[class*='book']",
        ".item-detail",
    ]
    for selector in selectors:
        for item in soup.select(selector):
            title_el = item.select_one("[class*='title'], h1, h2, h3")
            author_el = item.select_one("[class*='author'], [class*='contributor']")
            link_el = item.select_one("a[href*='/ebook/']")
            title = title_el.get_text(strip=True) if title_el else None
            if not title:
                continue
            author = author_el.get_text(strip=True) if author_el else "未知作者"
            url = urljoin("https://www.kobo.com", link_el["href"]) if link_el else page_url
            return {"title": title, "author": author, "price": "NT$99", "url": url}
    return None


def get_kobo_daily_deal() -> dict | None:
    """Fetch today's Kobo Taiwan daily 99 NT deal book info."""
    for url in KOBO_DAILY_DEAL_URLS:
        try:
            resp = requests.get(url, headers=get_headers(), timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            book = _try_next_data(soup) or _try_json_ld(soup, url) or _try_html(soup, url)
            if book and book.get("title"):
                book.setdefault("url", url)
                return book
        except requests.RequestException as e:
            print(f"Request error for {url}: {e}")
    return None


def get_goodreads_rating(title: str, author: str | None = None) -> str | None:
    """Search Goodreads for book rating (scraping)."""
    try:
        query = f"{title} {author}" if author else title
        url = f"https://www.goodreads.com/search?q={quote(query)}&search_type=books"
        resp = requests.get(url, headers=get_headers(), timeout=20)
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
    """Get rating from Google Books API (free, no auth needed)."""
    try:
        query = f"intitle:{title}"
        if author:
            query += f"+inauthor:{author}"
        url = f"https://www.googleapis.com/books/v1/volumes?q={quote(query)}&maxResults=5"
        resp = requests.get(url, timeout=20)
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
    """Get rating from Open Library (Internet Archive)."""
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


def build_message(book: dict, ratings: dict[str, str | None]) -> str:
    title = book.get("title", "未知書名")
    author = book.get("author", "未知作者")
    price = book.get("price", "NT$99")
    url = book.get("url", "https://www.kobo.com/tw/zh/p/daily-deal")

    lines = [
        "📚 今日 Kobo 每日 99 元好書",
        "─────────────────",
        f"📖 書名：{title}",
        f"✍️  作者：{author}",
        f"💰 價格：{price}",
        "",
        "⭐ 各平台評分",
    ]

    has_rating = any(v for v in ratings.values())
    if has_rating:
        for platform, rating in ratings.items():
            if rating:
                lines.append(f"• {platform}：{rating}")
    else:
        lines.append("（暫無評分資料）")

    lines += ["", f"🔗 {url}"]
    return "\n".join(lines)


def send_line_message(user_id: str, message: str, token: str) -> None:
    """Send push message via LINE Messaging API."""
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
            "⚠️ 無法自動取得今日 Kobo 每日特惠書籍\n請手動查看：\nhttps://www.kobo.com/tw/zh/p/daily-deal",
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
