"""
Push notifications via ntfy.sh (free, no account needed).
User installs the ntfy app on their phone and subscribes to their topic.
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

NTFY_BASE = "https://ntfy.sh"


def get_topic() -> str | None:
    return os.environ.get("NTFY_TOPIC", "").strip() or None


def send_weekly_notification(books: list[dict], week_date: str):
    topic = get_topic()
    if not topic:
        logger.info("NTFY_TOPIC not set, skipping notification")
        return

    count = len(books)
    if count == 0:
        return

    titles = "\n".join(f"📖 {b['title']}" for b in books[:8])
    if count > 8:
        titles += f"\n… 還有 {count - 8} 本"

    message = f"本週共 {count} 本 99 元特賣電子書！\n\n{titles}"

    try:
        requests.post(
            f"{NTFY_BASE}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": f"📚 Kobo 99元週特賣 ({week_date})",
                "Priority": "default",
                "Tags": "books,tada",
                "Click": "https://claude-kobo.onrender.com",
            },
            timeout=10,
        )
        logger.info("Push notification sent to topic '%s'", topic)
    except Exception as e:
        logger.error("Failed to send ntfy notification: %s", e)
