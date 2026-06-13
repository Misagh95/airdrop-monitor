"""
╔══════════════════════════════════════════════════╗
║     🪂  Airdrop Monitor Bot  -  ربات مانیتور     ║
║                                                  ║
║  رصد کانال‌های تلگرام و اکانت‌های توییتر           ║
║  فقط با Bot Token — بدون API_ID / my.telegram.org ║
║  ترجمه فارسی خودکار هر پست جدید                   ║
╚══════════════════════════════════════════════════╝
"""
import asyncio
import logging
import re
from datetime import datetime

import aiohttp
import feedparser
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import BOT_TOKEN, RSSHUB_URL, POLL_INTERVAL, MAX_TRANSLATE_LENGTH, DATABASE_PATH, check_config
from database import Database
from translator import translate_to_farsi
from telegram_scraper import fetch_channel_messages
from twitter_scraper import fetch_tweets

# ──────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("airdrop")

# ──────────────────────────────────────────────
#  Globals
# ──────────────────────────────────────────────
db = Database(DATABASE_PATH)

owner_chat_id: int | None = None
notifications_paused = False
user_states: dict[int, str] = {}       # chat_id → conversation state
feed_health: dict[str, dict] = {}       # name → {last_check, ok, error}
admin_states: dict[int, str] = {}       # chat_id → admin conversation state (e.g. "awaiting_admin_id")

# ── ضد-اسپم: کش پیام‌های اخیر برای جلوگیری از تکرار ──
recent_content_hashes: dict[str, float] = {}  # hash → timestamp
DEDUP_WINDOW = 3600  # ۱ ساعت

# ── کلمات کلیدی مهم برای تشخیص اهمیت پست ──
HIGH_PRIORITY_KEYWORDS = [
    "airdrop", "claim", "snapshot", "tge", "listing", "token launch",
    "free mint", "reward", "whitelist", "wl", "early access",
    "connect wallet", "mainnet", "genesis", "drop", "retroactive",
    "free", "allocation", "eligib",
]
MEDIUM_PRIORITY_KEYWORDS = [
    "update", "announcement", "launch", "bridge", "swap", "testnet",
    "mainnet", "phase", "season", "task", "quest", "complete",
]

# منابع پیش‌فرض کاربر
DEFAULT_SOURCES = {
    "telegram": [
        "shadatofficialYT",
        "airdropfind",
        "dutacryptoairdrop",
        "airdropsultanindonesia",
        "tomketairdrop",
        "cryptpublic",
    ],
    "twitter": [
        "rajib9336",
        "Airdropinsider_",
        "Crypto_Pranjal",
    ],
}


# ════════════════════════════════════════════════════
#  INIT
# ════════════════════════════════════════════════════
def init_sources():
    """اگر دیتابیس خالی است، منابع پیش‌فرض را اضافه کن."""
    if not db.get_sources():
        for name in DEFAULT_SOURCES["telegram"]:
            db.add_source("telegram", name, f"https://t.me/{name}")
        for name in DEFAULT_SOURCES["twitter"]:
            db.add_source("twitter", name, f"https://x.com/{name}")
        logger.info("منابع پیش‌فرض اضافه شدند.")


def load_state():
    """بارگذاری وضعیت ذخیره‌شده از دیتابیس."""
    global owner_chat_id, notifications_paused
    stored_owner = db.get_meta("owner_chat_id")
    if stored_owner:
        owner_chat_id = int(stored_owner)
    if db.get_meta("notifications_paused") == "1":
        notifications_paused = True


# ════════════════════════════════════════════════════
#  RSS FETCHING & MONITORING
# ════════════════════════════════════════════════════
async def fetch_rss(url: str):
    """فید RSS را به‌صورت async دریافت و parse کن."""
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            body = await resp.text()
            return feedparser.parse(body)


def clean_html(text: str) -> str:
    """حذف تگ‌های HTML و فاصله‌های اضافی."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    return text.strip()


def _content_hash(text: str) -> str:
    """هش محتوا برای تشخیص پیام‌های تکراری."""
    import hashlib
    cleaned = re.sub(r"https?://\S+", "", text.lower())
    cleaned = re.sub(r"[^\w]", "", cleaned)
    return hashlib.md5(cleaned.encode()).hexdigest()


def _is_duplicate(text: str) -> bool:
    """بررسی اینکه آیا این محتوا اخیراً ارسال شده است."""
    now = datetime.now().timestamp()
    h = _content_hash(text)

    expired = [k for k, v in recent_content_hashes.items() if now - v > DEDUP_WINDOW]
    for k in expired:
        del recent_content_hashes[k]

    if h in recent_content_hashes:
        return True

    recent_content_hashes[h] = now
    return False


def _detect_priority(text: str) -> str:
    """تشخیص اهمیت پست. خروجی: '🔴' (بالا)، '🟡' (متوسط)، '⚪' (عادی)"""
    text_lower = text.lower()
    for kw in HIGH_PRIORITY_KEYWORDS:
        if kw in text_lower:
            return "🔴"
    for kw in MEDIUM_PRIORITY_KEYWORDS:
        if kw in text_lower:
            return "🟡"
    return "⚪"


async def check_feed(bot, src_type: str, name: str, first_run: bool = False):
    """چک کردن یک منبع برای محتوای جدید."""
    label = f"📢 کانال تلگرام @{name}" if src_type == "telegram" else f"🐦 توییتر @{name}"
    new_count = 0

    try:
        if src_type == "telegram":
            # ── اسکرپ مستقیم صفحه preview تلگرام ──
            messages = await fetch_channel_messages(name)
            if messages is None:
                raise Exception("خطا در دریافت صفحه")

            for msg in messages:
                msg_key = f"tg:{msg['id']}"
                if db.is_seen(msg_key):
                    continue
                db.mark_seen(msg_key)

                # در اولین اجرا، پیام‌های قدیمی رو فقط علامت بزن، نفرست
                if first_run:
                    new_count += 1
                    continue

                if notifications_paused:
                    new_count += 1
                    continue

                await send_notification(bot, label, msg["text"], msg["link"])
                await asyncio.sleep(2)

            feed_health[name] = {
                "last_check": datetime.now().strftime("%H:%M"),
                "ok": True,
                "error": "",
                "new": new_count,
            }

        else:
            # ── توییتر از طریق GraphQL API (رایگان، بدون کلید) ──
            tweets = await fetch_tweets(name)
            if tweets is None:
                raise Exception("خطا در دریافت توییت‌ها")

            for tweet in tweets[:5]:
                msg_key = f"tw:{name}:{tweet['id']}"
                if db.is_seen(msg_key):
                    continue
                db.mark_seen(msg_key)

                # در اولین اجرا، پیام‌های قدیمی رو فقط علامت بزن، نفرست
                if first_run:
                    new_count += 1
                    continue

                if notifications_paused:
                    new_count += 1
                    continue

                await send_notification(bot, label, tweet["text"], tweet["link"])
                await asyncio.sleep(2)

            feed_health[name] = {
                "last_check": datetime.now().strftime("%H:%M"),
                "ok": True,
                "error": "",
                "new": new_count,
            }

    except Exception as e:
        feed_health[name] = {
            "last_check": datetime.now().strftime("%H:%M"),
            "ok": False,
            "error": str(e)[:80],
            "new": 0,
        }
        logger.error(f"منبع @{name}: {e}")


async def feed_loop(bot):
    """حلقه‌ی اصلی مانیتور — تلگرام و توییتر را جداگانه چک می‌کند."""
    await asyncio.sleep(5)  # تأخیر اولیه
    prune_counter = 0
    first_run = True

    while True:
        try:
            sources = db.get_sources(active_only=True)
            for src_type, name, _, _, _, _ in sources:
                await check_feed(bot, src_type, name, first_run=first_run)
                # تأخیر بیشتر برای توییتر (جلوگیری از rate-limit)
                if src_type == "twitter":
                    await asyncio.sleep(3)
                else:
                    await asyncio.sleep(1)
            first_run = False
        except Exception as e:
            logger.error(f"خطا در حلقه مانیتور: {e}")

        # پاک‌سازی دیتابیس هر ~۱۰۰ بار
        prune_counter += 1
        if prune_counter % 100 == 0:
            db.prune_seen(days=30)

        await asyncio.sleep(POLL_INTERVAL)


# ════════════════════════════════════════════════════
#  NOTIFICATION
# ════════════════════════════════════════════════════
async def send_notification(bot, source_label: str, original_text: str, source_url: str):
    """ترجمه کن و پیام را برای مالک و ادمین‌ها بفرست."""
    # ── ضد-اسپم: اگر محتوای مشابه اخیراً ارسال شده، رد کن ──
    if _is_duplicate(original_text):
        logger.info(f"⏭️ پیام تکراری رد شد")
        return

    # ── تشخیص اهمیت ──
    priority = _detect_priority(original_text)

    # جمع‌آوری گیرنده‌ها: مالک + ادمین‌ها
    recipients = []
    if owner_chat_id:
        recipients.append(owner_chat_id)
    for admin_id, _, _ in db.get_admins():
        if admin_id not in recipients:
            recipients.append(admin_id)

    if not recipients:
        return

    loop = asyncio.get_event_loop()
    translated = await loop.run_in_executor(
        None, translate_to_farsi, original_text, MAX_TRANSLATE_LENGTH
    )

    # مدیریت طول پیام (محدودیت تلگرام: ۴۰۹۶ کاراکتر)
    max_orig = 1200
    max_trans = 1800
    orig_show = original_text[:max_orig] + (" …" if len(original_text) > max_orig else "")
    trans_show = translated[:max_trans] + (" …" if len(translated) > max_trans else "")

    message = (
        f"{'━' * 22}\n"
        f"{priority} {source_label}\n"
        f"{'━' * 22}\n\n"
        f"📝 متن اصلی:\n{orig_show}\n\n"
        f"{'─' * 22}\n"
        f"🌍 ترجمه فارسی:\n{trans_show}\n\n"
        f"🔗 {source_url}"
    )

    success = False
    for chat_id in recipients:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                disable_web_page_preview=True,
            )
            success = True
        except Exception as e:
            logger.error(f"ارسال پیام به {chat_id} ناموفق: {e}")

    if success:
        db.increment_daily_stat()


# ════════════════════════════════════════════════════
#  URL PARSING
# ════════════════════════════════════════════════════
def parse_source_url(url: str):
    """
    لینک ورودی را تحلیل کن و (type, name, clean_url) برگردان.
    پشتیبانی: t.me/channelname، @channelname، x.com/user، twitter.com/user
    """
    url = url.strip()

    # @channelname
    if url.startswith("@"):
        name = url[1:].rstrip("/")
        return ("telegram", name, f"https://t.me/{name}")

    # t.me/...
    if "t.me/" in url:
        part = url.split("t.me/")[-1]
        part = part.split("?")[0].split("#")[0]
        if part.startswith("s/"):
            part = part[2:]
        part = part.split("/")[0]
        if not part or part.startswith("+") or "joinchat" in part:
            return None
        return ("telegram", part, f"https://t.me/{part}")

    # x.com/ یا twitter.com/
    for domain in ("x.com/", "twitter.com/"):
        if domain in url:
            part = url.split(domain)[-1]
            part = part.split("?")[0].split("#")[0].split("/")[0]
            if part:
                return ("twitter", part, f"https://x.com/{part}")

    return None


# ════════════════════════════════════════════════════
#  INLINE MENUS
# ════════════════════════════════════════════════════
def main_menu():
    """منوی اصلی — دکمه مدیریت ادمین همیشه نمایش داده می‌شود
    اما فقط مالک به آن دسترسی دارد."""
    pause_label = "▶️ فعال‌سازی هشدارها" if notifications_paused else "⏸️ توقف هشدارها"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📰 آخرین پست‌ها", callback_data="latest")],
        [InlineKeyboardButton("📋 لیست منابع", callback_data="list")],
        [
            InlineKeyboardButton("➕ افزودن منبع", callback_data="add"),
            InlineKeyboardButton("➖ حذف منبع", callback_data="remove"),
        ],
        [InlineKeyboardButton("📊 وضعیت ربات", callback_data="status")],
        [InlineKeyboardButton("🧪 ارسال پیام تست", callback_data="test")],
        [InlineKeyboardButton(pause_label, callback_data="toggle_pause")],
        [InlineKeyboardButton("🛡️ مدیریت ادمین‌ها", callback_data="admins")],
        [InlineKeyboardButton("❓ راهنما", callback_data="help")],
    ])


# ════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ════════════════════════════════════════════════════
async def is_authorized(update: Update) -> bool:
    """بررسی دسترسی — مالک و ادمین‌ها دسترسی دارند."""
    cid = update.effective_chat.id
    if owner_chat_id is not None and cid == owner_chat_id:
        return True
    return db.is_admin(cid)


async def is_owner(update: Update) -> bool:
    """فقط مالک (برای عملیات حساس مثل مدیریت ادمین)."""
    return owner_chat_id is not None and update.effective_chat.id == owner_chat_id


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global owner_chat_id
    cid = update.effective_chat.id

    if owner_chat_id is None:
        owner_chat_id = cid
        db.set_meta("owner_chat_id", str(owner_chat_id))
        logger.info(f"مالک ثبت شد: {owner_chat_id}")

    # اگر نه مالکه نه ادمین
    is_me = (cid == owner_chat_id)
    is_adm = db.is_admin(cid)
    if not is_me and not is_adm:
        await update.message.reply_text("🔒 این ربات خصوصی است.")
        return

    role = "👑 مالک" if is_me else "🛡️ ادمین"
    text = (
        f"🎉 به ربات مانیتور ایردراپ خوش آمدید!\n\n"
        f"👤 نقش شما: {role}\n\n"
        "این ربات منابع شما را رصد می‌کند و هر ایردراپ یا آپدیت جدید "
        "را همراه با ترجمه فارسی برایتان می‌فرستد.\n\n"
        "از منوی زیر استفاده کنید:"
    )
    await update.message.reply_text(text, reply_markup=main_menu())


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    await update.message.reply_text("📋 منوی اصلی:", reply_markup=main_menu())


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    if context.args:
        await do_add_source(update, " ".join(context.args))
    else:
        user_states[update.effective_chat.id] = "awaiting_source"
        await update.message.reply_text(
            "🔗 لینک منبع جدید را بفرستید:\n\n"
            "نمونه‌ها:\n"
            "• `https://t.me/channelname`\n"
            "• `https://x.com/username`\n"
            "• `@channelname`",
            parse_mode="Markdown",
        )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    await show_source_list(update)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    await show_status(update)


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    await do_test(update, context)


async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    await show_latest_menu(update)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    await show_help(update)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مسیریابی پیام‌های متنی (برای حالت‌های مکالمه)."""
    cid = update.effective_chat.id

    # بررسی دسترسی
    is_me = (owner_chat_id is not None and cid == owner_chat_id)
    is_adm = db.is_admin(cid)
    if not is_me and not is_adm:
        return

    text = update.message.text or ""
    if not text or text.startswith("/"):
        return

    # حالت افزودن ادمین
    adm_state = admin_states.get(cid)
    if adm_state == "awaiting_admin_id":
        await do_add_admin(update, text)
        admin_states.pop(cid, None)
        return

    # حالت افزودن منبع
    state = user_states.get(cid)
    if state == "awaiting_source":
        await do_add_source(update, text)
        user_states.pop(cid, None)


async def do_add_admin(update: Update, raw_input: str):
    """افزودن ادمین جدید."""
    raw_input = raw_input.strip()

    # پاکسازی — فقط اعداد رو نگه دار
    digits = re.sub(r"[^\d]", "", raw_input)

    if not digits or len(digits) < 5:
        await update.message.reply_text(
            "❌ آیدی نامعتبر!\n\n"
            "آیدی عددی باید فقط شامل عدد باشد، مثل:\n"
            "`123456789`\n\n"
            "برای گرفتن آیدی، از دستور `/myid` استفاده کنید.",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )
        return

    new_admin_id = int(digits)

    # نباید مالک رو ادمین اضافه کنیم
    if new_admin_id == owner_chat_id:
        await update.message.reply_text(
            "⚠️ این آیدی مالک ربات است — نیازی به افزودن ندارد.",
            reply_markup=main_menu(),
        )
        return

    # تکراری؟
    if db.is_admin(new_admin_id):
        await update.message.reply_text(
            f"⚠️ این کاربر قبلاً ادمین است: `{new_admin_id}`",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )
        return

    db.add_admin(new_admin_id)
    await update.message.reply_text(
        f"✅ ادمین جدید اضافه شد!\n\n"
        f"🔢 Chat ID: `{new_admin_id}`\n\n"
        f"💡 این کاربر حالا می‌تواند از ربات استفاده کند.\n"
        f"به او بگویید به ربات `/start` بفرستد.",
        parse_mode="Markdown",
        reply_markup=main_menu(),
    )


# ════════════════════════════════════════════════════
#  ACTION FUNCTIONS
# ════════════════════════════════════════════════════
async def do_add_source(update: Update, url: str):
    result = parse_source_url(url)
    if result is None:
        await update.message.reply_text(
            "❌ لینک نامعتبر!\n\n"
            "فرمت صحیح:\n"
            "• `https://t.me/channelname`\n"
            "• `https://x.com/username`",
            parse_mode="Markdown",
        )
        return

    source_type, name, clean_url = result

    existing = {r[1] for r in db.get_sources(source_type)}
    if name in existing:
        await update.message.reply_text(f"⚠️ این منبع قبلاً اضافه شده: @{name}")
        return

    db.add_source(source_type, name, clean_url)

    label = "📢 کانال تلگرام" if source_type == "telegram" else "🐦 اکانت توییتر"
    await update.message.reply_text(
        f"✅ {label} اضافه شد!\n\n"
        f"   @{name}\n"
        f"   {clean_url}\n\n"
        f"🔔 رصد از همین لحظه شروع شد.",
        reply_markup=main_menu(),
    )


async def show_source_list(update: Update):
    sources = db.get_sources()
    if not sources:
        await update.effective_message.reply_text("📭 هیچ منبعی اضافه نشده.", reply_markup=main_menu())
        return

    tg = [s for s in sources if s[0] == "telegram"]
    tw = [s for s in sources if s[0] == "twitter"]
    lines = []

    if tg:
        lines.append("📢 کانال‌های تلگرام:")
        for _, name, _, _, _, _ in tg:
            lines.append(f"   • @{name}")
    if tw:
        lines.append("\n🐦 اکانت‌های توییتر:")
        for _, name, _, _, _, _ in tw:
            lines.append(f"   • @{name}")

    await update.effective_message.reply_text("\n".join(lines), reply_markup=main_menu())


async def show_remove_menu(update: Update):
    sources = db.get_sources()
    if not sources:
        await update.effective_message.reply_text("📭 منبعی برای حذف وجود ندارد.", reply_markup=main_menu())
        return

    buttons = []
    for src_type, name, _, _, _, _ in sources:
        emoji = "📢" if src_type == "telegram" else "🐦"
        buttons.append([InlineKeyboardButton(f"{emoji} @{name}  ❌", callback_data=f"rm_{src_type}_{name}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back")])
    await update.effective_message.reply_text("کدام منبع را حذف می‌کنید؟", reply_markup=InlineKeyboardMarkup(buttons))


async def show_status(update: Update):
    tg_sources = db.get_sources("telegram")
    tw_sources = db.get_sources("twitter")

    lines = ["📊 وضعیت ربات", "━" * 22, ""]
    lines.append("🤖 ربات: ✅ آنلاین")
    lines.append(f"⏸️ هشدارها: {'متوقف ⏸️' if notifications_paused else 'فعال ✅'}")
    lines.append(f"⏱️ فاصله بررسی: هر {POLL_INTERVAL} ثانیه")
    lines.append(f"📢 کانال‌های تلگرام: {len(tg_sources)}")
    lines.append(f"🐦 اکانت‌های توییتر: {len(tw_sources)}")
    lines.append(f"📨 پیام‌های پردازش‌شده: {db.seen_count()}")
    lines.append(f"📤 هشدارهای امروز: {db.get_daily_stat()}")

    if feed_health:
        lines.append("\n📡 وضعیت آخرین بررسی منابع:")
        for name, info in feed_health.items():
            icon = "✅" if info["ok"] else "❌"
            extra = "" if info["ok"] else f" — {info['error']}"
            lines.append(f"   • @{name}: {icon} ({info['last_check']}){extra}")

    await update.effective_message.reply_text("\n".join(lines), reply_markup=main_menu())


async def do_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_notification(
        context.bot,
        "🧪 پیام تست",
        "BREAKING: New massive airdrop confirmed! Connect your wallet and complete "
        "the testnet tasks to qualify. Snapshot is happening soon. Don't miss out!",
        "https://example.com",
    )
    await update.effective_message.reply_text(
        "✅ پیام تست ارسال شد.\n\n"
        "💡 این پیام باید علامت 🔴 (اولویت بالا) داشته باشد\n"
        "چون کلمات کلیدی airdrop, claim, snapshot دارد.",
        reply_markup=main_menu(),
    )


# ════════════════════════════════════════════════════
#  LATEST POSTS — آخرین پست‌ها
# ════════════════════════════════════════════════════
async def show_latest_menu(update: Update):
    """منوی انتخاب منبع برای دیدن آخرین پست‌ها."""
    sources = db.get_sources(active_only=True)
    if not sources:
        await update.effective_message.reply_text(
            "📭 هیچ منبعی اضافه نشده.", reply_markup=main_menu()
        )
        return

    buttons = []
    for src_type, name, _, _, _, _ in sources:
        emoji = "📢" if src_type == "telegram" else "🐦"
        buttons.append(
            [InlineKeyboardButton(f"{emoji} @{name}", callback_data=f"latest_{src_type}_{name}")]
        )
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back")])
    await update.effective_message.reply_text(
        "📰 آخرین پست‌ها\n\nکدام منبع را می‌خواهید ببینید؟",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_latest_posts(update: Update, context: ContextTypes.DEFAULT_TYPE, src_type: str, name: str):
    """دریافت و نمایش آخرین پست‌های یک منبع با ترجمه."""
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    label = f"📢 کانال تلگرام @{name}" if src_type == "telegram" else f"🐦 توییتر @{name}"

    try:
        if src_type == "telegram":
            posts = await fetch_channel_messages(name)
        else:
            posts = await fetch_tweets(name)

        if not posts:
            await update.effective_message.reply_text(
                f"📭 @{name}: پستی پیدا نشد.",
                reply_markup=main_menu(),
            )
            return

        # نمایش ۳ پست آخر
        count = min(3, len(posts))
        await update.effective_message.reply_text(
            f"📰 آخرین {count} پست از {label}:\n"
            f"{'━' * 22}",
        )

        for post in posts[:3]:
            text = post["text"]
            link = post["link"]

            # ترجمه
            loop = asyncio.get_event_loop()
            translated = await loop.run_in_executor(
                None, translate_to_farsi, text, MAX_TRANSLATE_LENGTH
            )

            max_orig = 800
            max_trans = 1200
            orig_show = text[:max_orig] + (" …" if len(text) > max_orig else "")
            trans_show = translated[:max_trans] + (" …" if len(translated) > max_trans else "")

            message = (
                f"📝 متن اصلی:\n{orig_show}\n\n"
                f"{'─' * 22}\n"
                f"🌍 ترجمه فارسی:\n{trans_show}\n\n"
                f"🔗 {link}"
            )
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=message,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"ارسال پست ناموفق: {e}")

            await asyncio.sleep(1)

        await update.effective_message.reply_text(
            "✅ پایان.",
            reply_markup=main_menu(),
        )

    except Exception as e:
        await update.effective_message.reply_text(
            f"❌ خطا در دریافت پست‌های @{name}:\n{str(e)[:100]}",
            reply_markup=main_menu(),
        )


async def show_help(update: Update):
    help_text = (
        "❓ راهنمای ربات\n"
        f"{'━' * 22}\n\n"
        "📖 دستورات:\n"
        "• `/start` — شروع ربات\n"
        "• `/menu` — نمایش منو\n"
        "• `/latest` — آخرین پست‌های منابع\n"
        "• `/add` — افزودن منبع جدید\n"
        "• `/list` — لیست منابع\n"
        "• `/status` — وضعیت ربات\n"
        "• `/test` — ارسال پیام تست\n"
        "• `/myid` — نمایش آیدی عددی شما\n"
        "• `/help` — این راهنما\n\n"
        "📌 نوع منابع قابل افزودن:\n"
        "• 📢 کانال تلگرام — `t.me/...`\n"
        "• 🐦 اکانت توییتر — `x.com/...`\n\n"
        "💡 نکات:\n"
        "• هر پست جدید با ترجمه فارسی ارسال می‌شود\n"
        "• 🔴 اولویت بالا | 🟡 متوسط | ⚪ عادی\n"
        "• پیام‌های تکراری از منابع مختلف فقط یک‌بار ارسال می‌شوند\n"
        "• می‌توانید هشدارها را موقتاً متوقف کنید\n"
        "• برای افزودن سریع: `/add https://t.me/name`"
    )
    await update.effective_message.reply_text(help_text, parse_mode="Markdown", reply_markup=main_menu())


async def show_admins_menu(update: Update):
    """منوی مدیریت ادمین‌ها (فقط مالک)."""
    admins = db.get_admins()
    lines = ["🛡️ مدیریت ادمین‌ها", "━" * 22, ""]

    if not admins:
        lines.append("📭 هیچ ادمینی اضافه نشده.")
    else:
        lines.append(f"👥 تعداد ادمین‌ها: {len(admins)}\n")
        for chat_id, username, added_at in admins:
            name = f"@{username}" if username else "—"
            lines.append(f"   • `{chat_id}` ({name})")

    lines.append(f"\n👑 مالک: `{owner_chat_id}`")
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ افزودن ادمین", callback_data="add_admin")],
            *[
                [InlineKeyboardButton(f"❌ حذف {cid}", callback_data=f"rmadm_{cid}")]
                for cid, _, _ in admins
            ],
            [InlineKeyboardButton("🔙 منوی اصلی", callback_data="back")],
        ]),
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش آیدی عددی کاربر — برای همه در دسترس است."""
    cid = update.effective_chat.id
    user = update.effective_user
    username = f"@{user.username}" if user.username else "بدون یوزرنیم"
    await update.message.reply_text(
        f"🆔 اطلاعات شما:\n\n"
        f"👤 نام: {user.full_name}\n"
        f"📛 یوزرنیم: {username}\n"
        f"🔢 Chat ID: `{cid}`",
        parse_mode="Markdown",
    )


# ════════════════════════════════════════════════════
#  CALLBACK QUERY HANDLER
# ════════════════════════════════════════════════════
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global notifications_paused
    query = update.callback_query
    await query.answer()

    cid = query.message.chat.id

    # بررسی دسترسی (مالک یا ادمین)
    is_me = (owner_chat_id is not None and cid == owner_chat_id)
    is_adm = db.is_admin(cid)
    if not is_me and not is_adm:
        await query.answer("🔒 خصوصی", show_alert=True)
        return

    data = query.data

    # عملیات مدیریت ادمین — فقط مالک
    if data in ("admins", "add_admin") or data.startswith("rmadm_"):
        if not is_me:
            await query.answer("🔒 فقط مالک می‌تواند ادمین‌ها را مدیریت کند", show_alert=True)
            return

    if data == "list":
        await show_source_list(update)

    elif data == "latest":
        await show_latest_menu(update)

    elif data == "add":
        user_states[query.message.chat.id] = "awaiting_source"
        await query.message.reply_text(
            "🔗 لینک منبع جدید را بفرستید:\n\n"
            "• `https://t.me/channelname`\n"
            "• `https://x.com/username`\n"
            "• `@channelname`",
            parse_mode="Markdown",
        )

    elif data == "remove":
        await show_remove_menu(update)

    elif data == "status":
        await show_status(update)

    elif data == "test":
        await do_test(update, context)

    elif data == "help":
        await show_help(update)

    elif data == "toggle_pause":
        notifications_paused = not notifications_paused
        db.set_meta("notifications_paused", "1" if notifications_paused else "0")
        msg = "⏸️ هشدارها متوقف شدند." if notifications_paused else "▶️ هشدارها فعال شدند."
        await query.message.reply_text(msg, reply_markup=main_menu())

    elif data == "back":
        await query.message.reply_text("📋 منوی اصلی:", reply_markup=main_menu())

    # ── مدیریت ادمین‌ها ──
    elif data == "admins":
        await show_admins_menu(update)

    elif data == "add_admin":
        admin_states[cid] = "awaiting_admin_id"
        await query.message.reply_text(
            "🛡️ افزودن ادمین جدید\n\n"
            "👤 **آیدی عددی (Chat ID)** کاربر را بفرستید.\n\n"
            "📐 چطور Chat ID بگیریم؟\n"
            "۱. کاربر باید اول به ربات `/start` بفرسته\n"
            "۲. بعد شما `/myid` رو بفرستید تا آیدی خودتون رو ببینید\n"
            "۳. یا از ربات‌هایی مثل `@userinfobot` استفاده کنید\n\n"
            "آیدی عددی اینطوریه: `123456789`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت", callback_data="admins")],
            ]),
        )

    elif data.startswith("rmadm_"):
        try:
            admin_id = int(data.split("_", 1)[1])
            db.remove_admin(admin_id)
            await query.message.reply_text(
                f"✅ ادمین حذف شد: `{admin_id}`",
                parse_mode="Markdown",
                reply_markup=main_menu(),
            )
        except Exception:
            await query.message.reply_text("❌ خطا در حذف ادمین.", reply_markup=main_menu())

    elif data.startswith("latest_"):
        # latest_telegram_airdropfind یا latest_twitter_Airdropinsider_
        parts = data.split("_", 2)
        if len(parts) == 3:
            src_type, name = parts[1], parts[2]
            await show_latest_posts(update, context, src_type, name)

    elif data.startswith("rm_"):
        parts = data.split("_", 2)
        if len(parts) == 3:
            src_type, name = parts[1], parts[2]
            db.remove_source(src_type, name)
            feed_health.pop(name, None)
            await query.message.reply_text(f"✅ منبع @{name} حذف شد.", reply_markup=main_menu())


# ════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════
async def post_init(application: Application):
    """راه‌اندازی پس از اتصال ربات."""
    load_state()
    init_sources()

    me = await application.bot.get_me()
    print("\n" + "═" * 50)
    print("  🪂  Airdrop Monitor Bot")
    print("═" * 50)
    print(f"  ✓ ربات متصل: @{me.username}")
    tg_count = len(db.get_sources("telegram"))
    tw_count = len(db.get_sources("twitter"))
    print(f"  ✓ کانال‌های تلگرام: {tg_count}")
    print(f"  ✓ اکانت‌های توییتر: {tw_count}")
    print(f"  ✓ فاصله بررسی: هر {POLL_INTERVAL} ثانیه")
    if owner_chat_id:
        print(f"  ✓ مالک: {owner_chat_id}")
    print("═" * 50)

    # شروع حلقه مانیتور
    asyncio.create_task(feed_loop(application.bot))
    print("\n✅ ربات در حال اجراست!")
    if not owner_chat_id:
        print("   ⚠️ در تلگرام به ربات /start بفرستید تا مالک ثبت شوید.\n")
    else:
        await application.bot.send_message(
            owner_chat_id,
            f"✅ ربات مجدداً راه‌اندازی شد!\n⏱️ {datetime.now().strftime('%H:%M:%S')}",
        )
        print()


def main():
    check_config()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # ثبت هندلرها
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
