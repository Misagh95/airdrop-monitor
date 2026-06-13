"""
ترجمه هوشمند به فارسی با دیکشنری کریپتو و موتورهای متعدد
"""
import re
import logging

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  دیکشنری کلمات تخصصی کریپتو
#  کلمات انگلیسی → معادل رایج فارسی
# ──────────────────────────────────────────────
CRYPTO_TERMS = {
    # اعمال‌ها
    "claim": "کلیم",
    "claiming": "کلیم کردن",
    "claimed": "کلیم کرد",
    "connect wallet": "اتصال کیف پول",
    "snapshot": "اسنپ‌شات",
    "airdrop": "ایردراپ",
    "airdrops": "ایردراپ",
    "whitelist": "وایت‌لیست",
    "whitelisted": "وایت‌لیست شده",
    "mint": "مینت",
    "minting": "مینت کردن",
    "staking": "استیک",
    "bridge": "بریج",
    "bridging": "بریج کردن",
    "swap": "سواپ",
    "testnet": "تست‌نت",
    "mainnet": "مین‌نت",
    "token": "توکن",
    "tokens": "توکن",
    "wallet": "کیف پول",
    "reward": "ریوارد",
    "rewards": "ریوارد",
    "allocation": "تخصیص",
    "eligibility": "واجد شرایط بودن",
    "eligible": "واجد شرایط",
    "retroactive": "رترواکتیو",
    "presale": "پری‌سیل",
    "launchpad": "لانچ‌پد",
    "gas fee": "گس فی",
    "gas fees": "گس فی",
    "liquidity": "نقدینگی",
    "yield": "بازده",
    "farming": "فارمینگ",
    "deFi": "دی‌فای",
    "blockchain": "بلاکچین",
    "smart contract": "قرارداد هوشمند",
    # اصطلاحات
    "GM": "صبح بخیر",
    "WAGMI": "همه با هم موفق می‌شیم",
    "NGMI": "موفق نمی‌شه",
    "DYOR": "تحقیق خودتون رو بکنید",
    "NFA": "توصیه مالی نیست",
    "FUD": "ترس و تردید",
    "moon": "مون شد (سود بالا)",
    "HODL": "هول دادن (نگه داشتن)",
    "pump": "پامپ (رشد شدید)",
    "dump": "دامپ (ریزش)",
    "bullish": "بولیش (رو به رشد)",
    "bearish": "بیریش (رو به افت)",
    # پروژه‌ها و مفاهیم
    "TGE": "تولید توکن",
    "ICO": "پیش‌فروش اولیه",
    "IDO": "عرضه اولیه غیرمتمرکز",
    "DAO": "سازمان خودمختار غیرمتمرکز",
    "DEX": "صرافی غیرمتمرکز",
    "CEX": "صرافی متمرکز",
    "LP": "ارائه‌دهنده نقدینگی",
    "TVL": "مقدار کل قفل‌شده",
    "APR": "نرخ بازدهی سالانه",
    "APY": "بازدهی سالانه ترکیبی",
    # تسک‌ها
    "task": "تسک",
    "tasks": "تسک‌ها",
    "quest": "کوئست",
    "quests": "کوئست‌ها",
    "complete the task": "تسک رو کامل کنید",
    "follow": "فالو",
    "retweet": "ریتوییت",
    "like": "لایک",
    "join": "جوین",
    "register": "ثبت‌نام",
    "early access": "دسترسی زودهنگام",
    "phase": "فاز",
    "season": "سیزن",
    "campaign": "کمپین",
    # اعداد و مبالغ
    "free": "رایگان",
    "winners": "برندگان",
    "win": "برنده",
    "worth": "به ارزش",
    "usdt": "تتر",
    "total supply": "عرضه کل",
    "max supply": "حداکثر عرضه",
    "circulating supply": "عرضه در گردش",
}

# ──────────────────────────────────────────────
#  پاکسازی متن قبل از ترجمه
# ──────────────────────────────────────────────
def _preprocess_text(text: str) -> str:
    """پاکسازی و نرمال‌سازی متن قبل از ترجمه."""
    # حذف ایموجی‌های زیاد پشت سر هم
    text = re.sub(r"([\U0001F000-\U0001FFFF\u2600-\u27BF]){3,}", r"\1", text)
    # حذف کاراکترهای کنترلی
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    # تبدیل چندین خط خالی به یک خط
    text = re.sub(r"\n{3,}", "\n\n", text)
    # حذف تگ‌های HTML باقی‌مانده
    text = re.sub(r"<[^>]+>", " ", text)
    # نرمال‌سازی فاصله‌ها
    text = re.sub(r"[ \t]{2,}", " ", text)
    # حذف لینک‌های t.co و مشابه (ولی نگه داشتن متن لینک)
    text = re.sub(r"https?://t\.co/\S+", "", text)
    return text.strip()


# ──────────────────────────────────────────────
#  اعمال دیکشنری کریپتو
# ──────────────────────────────────────────────
def _apply_crypto_dict(text: str) -> str:
    """جایگزینی ترجمه‌های کتابی با معادل‌های رایج فارسی کریپتو."""
    replacements = {
        # claim
        "مطالبه کنید": "کلیم کنید",
        "مطالبه": "کلیم",
        "ادعا کنید": "کلیم کنید",
        "ادعای": "کلیم",
        "دریافت کنید": "کلیم کنید",
        # snapshot
        "عکس فوری": "اسنپ‌شات",
        "تصویر فوری": "اسنپ‌شات",
        # whitelist
        "نشان سفید": "وایت‌لیست",
        "فهرست سفید": "وایت‌لیست",
        # wallet
        "کیف پول خود را وصل کنید": "کیف پولتون رو وصل کنید",
        "کیف پول خود را": "کیف پولتون رو",
        # reward
        "پاداش": "ریوارد",
        "جایزه": "ریوارد",
        # tasks
        "انجام وظایف": "کامل کردن تسک‌ها",
        "تکمیل وظایف": "کامل کردن تسک‌ها",
        "وظایف": "تسک‌ها",
        "وظیفه": "تسک",
        # testnet / mainnet
        "شبکه آزمایشی": "تست‌نت",
        "شبکه اصلی": "مین‌نت",
        "شبکه تست": "تست‌نت",
        # bridge
        "پل زدن": "بریج کردن",
        "پل": "بریج",
        # swap
        "تعویض": "سواپ",
        # mint
        "نعناع": "مینت",
        "ضرب نهایی": "مینت",
        "ضرب": "مینت",
        "استخراج": "مینت",
        # whitelist
        "لیست سفید": "وایت‌لیست",
        "در لیست سفید قرار بگیرید": "وایت‌لیست بشید",
        "در لیست سفید قرار گرفته": "وایت‌لیست شده",
        # bridge
        "متصل کنید": "بریج کنید",
        "پل زدن": "بریج کردن",
        "پل": "بریج",
        # token launch
        "راه‌اندازی توکن": "تولید توکن (TGE)",
        "تولید توکن": "تولید توکن (TGE)",
        #その他
        "ارز دیجیتال رایگان": "کریپتو رایگان",
        "به ماه بروید": "مون شد",
        "نگه دارید": "هول کنید",
        "نقدینگی کل قفل": "TVL",
    }

    for wrong, correct in replacements.items():
        text = text.replace(wrong, correct)

    return text


def _strip_html(text: str) -> str:
    """حذف تگ‌های HTML."""
    return re.sub(r"<[^>]+>", "", text)


def _truncate(text: str, max_length: int) -> str:
    """کوتاه‌کردن متن به حد مجاز."""
    if len(text) <= max_length:
        return text
    # در حد ممکن توی یه جمله کوتاه کن
    truncated = text[:max_length]
    last_space = truncated.rfind(" ")
    if last_space > max_length * 0.7:
        truncated = truncated[:last_space]
    return truncated + " …"


# ──────────────────────────────────────────────
#  موتورهای ترجمه
# ──────────────────────────────────────────────
def _try_google(text: str) -> str | None:
    """ترجمه با Google Translate."""
    try:
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target="fa").translate(text)
    except Exception as e:
        logger.warning(f"Google Translate ناموفق: {e}")
        return None


def _try_mymemory(text: str) -> str | None:
    """ترجمه با MyMemory (به‌عنوان fallback)."""
    try:
        from deep_translator import MyMemoryTranslator
        return MyMemoryTranslator(source="auto", target="fa").translate(text)
    except Exception as e:
        logger.warning(f"MyMemory ناموفق: {e}")
        return None


def _try_libre(text: str) -> str | None:
    """ترجمه با LibreTranslate (به‌عنوان fallback)."""
    try:
        from deep_translator import LibreTranslator
        return LibreTranslator(
            source="auto", target="fa",
            api_key="",
            base_url="https://libretranslate.com",
        ).translate(text)
    except Exception as e:
        logger.warning(f"LibreTranslate ناموفق: {e}")
        return None


# ──────────────────────────────────────────────
#  تابع اصلی ترجمه
# ──────────────────────────────────────────────
def translate_to_farsi(text: str, max_length: int = 4500) -> str:
    """
    ترجمه هوشمند به فارسی.

    ۱. متن پاک‌سازی می‌شود
    ۲. با Google Translate ترجمه می‌شود
    ۳. اگر ناموفق بود، موتورهای جایگزین امتحان می‌شوند
    ۴. دیکشنری کریپتو اعمال می‌شود (جایگزینی ترجمه‌های کتابی)
    """
    if not text or not text.strip():
        return ""

    # پاک‌سازی
    cleaned = _preprocess_text(text)
    cleaned = _strip_html(cleaned)
    cleaned = _truncate(cleaned, max_length)

    if not cleaned.strip():
        return ""

    # ترجمه با موتورهای مختلف
    translated = _try_google(cleaned)
    if not translated or not translated.strip():
        translated = _try_mymemory(cleaned)
    if not translated or not translated.strip():
        translated = _try_libre(cleaned)
    if not translated or not translated.strip():
        return "❌ ترجمه در دسترس نیست"

    # اعمال دیکشنری کریپتو
    translated = _apply_crypto_dict(translated)

    return translated
