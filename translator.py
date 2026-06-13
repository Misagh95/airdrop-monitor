"""ترجمه متن به فارسی با استفاده از Google Translate"""
import re
import logging

logger = logging.getLogger(__name__)


def _strip_html(text):
    """حذف تگ‌های HTML"""
    return re.sub(r"<[^>]+>", "", text)


def _truncate(text, max_length=4500):
    if len(text) <= max_length:
        return text
    return text[:max_length] + " …"


def translate_to_farsi(text, max_length=4500):
    """
    ترجمه متن به فارسی.
    از Google Translate استفاده می‌کند (رایگان، غیررسمی).
    """
    if not text or not text.strip():
        return ""

    try:
        from deep_translator import GoogleTranslator

        cleaned = _strip_html(text)
        cleaned = _truncate(cleaned, max_length)
        translator = GoogleTranslator(source="auto", target="fa")
        result = translator.translate(cleaned)
        return result or ""
    except Exception as e:
        logger.error(f"خطا در ترجمه: {e}")
        return f"❌ ترجمه ناموفق بود ({type(e).__name__})"
