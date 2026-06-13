"""
استخراج پیام‌های جدید از کانال‌های تلگرام
از طریق صفحه preview: t.me/s/channelname
(بدون نیاز به API یا RSSHub)
"""
import re
import html as htmlmod
import logging

import aiohttp

logger = logging.getLogger(__name__)

PREVIEW_URL = "https://t.me/s/{channel}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _clean_text(text: str) -> str:
    """پاکسازی HTML و فاصله‌های اضافی از متن پیام."""
    # تبدیل <br> به خط جدید
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # استخراج لینک‌ها — href رو نگه دار
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r"\2 (\1)", text, flags=re.DOTALL)
    # حذف بقیه تگ‌ها
    text = re.sub(r"<[^>]+>", "", text)
    # unescape HTML entities
    text = htmlmod.unescape(text)
    # پاکسازی فاصله‌ها ولی حفظ خطوط جدید
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    return text.strip()


async def fetch_channel_messages(channel: str, timeout: int = 25) -> list[dict]:
    """
    دریافت آخرین پیام‌های یک کانال تلگرام.

    خروجی: لیستی از دیکشنری‌ها:
        [{ "id": "airdropfind/12345",
           "channel": "airdropfind",
           "message_id": "12345",
           "text": "متن پیام...",
           "link": "https://t.me/airdropfind/12345",
           "timestamp": "14:31:00" (در صورت وجود)
        }, ...]

    در صورت خطا exception پرتاب می‌کند.
    """
    url = PREVIEW_URL.format(channel=channel)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(url, headers=HEADERS) as resp:
            if resp.status != 200:
                raise Exception(f"HTTP {resp.status}")
            page = await resp.text()

    messages = []

    # الگوی استخراج بلوک پیام
    # data-post="channel/msgid" ... <div class="tgme_widget_message_text...">text</div>
    # همچنین datetime و data-post در همان بلوک
    # روی کل بلوک‌های tgme_widget_message کار می‌کنیم
    blocks = re.split(r'class="tgme_widget_message ', page)

    for block in blocks[1:]:  # اولین split قبل از اولین message است
        # استخراج data-post
        post_match = re.search(r'data-post="([^"]+)"', block)
        if not post_match:
            continue
        post_id = post_match.group(1)

        # استخراج متن
        text_match = re.search(
            r'tgme_widget_message_text[^"]*"[^>]*>(.*?)(?:</div>)',
            block,
            re.DOTALL,
        )
        if not text_match:
            continue
        raw_text = text_match.group(1)
        text = _clean_text(raw_text)

        if len(text.strip()) < 5:
            continue

        # تقسیم channel/message_id
        parts = post_id.split("/")
        channel_name = parts[0]
        msg_id = parts[1] if len(parts) > 1 else ""

        # استخراج زمان (در صورت وجود)
        time_match = re.search(r'datetime="([^"]+)"', block)
        timestamp = time_match.group(1) if time_match else ""

        messages.append({
            "id": post_id,
            "channel": channel_name,
            "message_id": msg_id,
            "text": text,
            "link": f"https://t.me/{channel_name}/{msg_id}",
            "timestamp": timestamp,
        })

    # حذف پیام‌های تکراری (بر اساس id)
    seen_ids = set()
    unique = []
    for msg in messages:
        if msg["id"] not in seen_ids:
            seen_ids.add(msg["id"])
            unique.append(msg)

    return unique


async def test_channel(channel: str) -> bool:
    """تست اینکه آیا یک کانال قابل دسترس است یا نه."""
    try:
        msgs = await fetch_channel_messages(channel)
        return len(msgs) >= 0  # فقط نباید exception بده
    except Exception:
        return False
