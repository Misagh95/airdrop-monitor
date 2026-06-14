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
admin_states: dict[int, str] = {}       # chat_id → admin conversation state

DEDUP_WINDOW = 3600  # ۱ ساعت
recent_content_hashes: dict[str, float] = {}

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
    if not db.get_sources():
        # لیست کامل کانال‌های تلگرامی (منابع خودت + منابع جدید تخصصی)
        telegrams = [
            "shadatofficialYT",   # منبع خودت
            "airdropfind",        # منبع خودت
            "CryptoRank_Drops",   # دکمه شکار ایردراپ کریپتورنک
            "CryptoRank_News",    # اخبار جذب سرمایه پروژه‌ها
            "Galxe_Official",     # کانال رسمی گلکس
            "Galxe_Answers",      # پاسخ‌نامه‌های کوئیز گلکس
            "Crypto_Quests",      # مانیتورینگ کمپین‌های پلتفرم‌ها
            "AirdropAlert",       # ددلاین‌ها و زمان‌بندی ایردراپ‌ها
            "AirdropStash",       # تست‌نت‌ها و مراجع اصلی وب۳
            "AirdropRating"       # امتیازدهی و فاندامنتال پروژه‌ها
        ]
        
        # لیست کامل اکانت‌های توییتر (منابع خودت + منابع جدید تخصصی)
        twitters = [
            "rajib9336",          # منبع خودت
            "Airdropinsider_",    # منبع خودت
            "CryptoRank_io",      # توییتر اصلی کریپتورنک
            "Galxe",              # توییتر اصلی گلکس
            "Airdrop_Oasis",      # ترد نویس و راهنمای گام‌به‌گام
            "WatcherGuru",        # اخبار فوری و اسنپ‌شات‌ها
            "ZombitCrypto"        # مانیتورینگ دقیق‌تر جذب سرمایه
        ]
        
        # تزریق خودکار کانال‌های تلگرام به دیتابیس ربات
        for name in telegrams:
            db.add_source("telegram", name, f"https://t.me/{name}")
            
        # تزریق خودکار اکانت‌های توییتر به دیتابیس ربات
        for name in twitters:
            db.add_source("twitter", name, f"https://x.com/{name}")
            
        logger.info("🎯 لیست جامع منابع (شامل منابع شخصی، کریپتورنک و گلکس) با موفقیت هماهنگ و ذخیره شد.")

# ════════════════════════════════════════════════════
#  RSS FETCHING & MONITORING
# ════════════════════════════════════════════════════
async def fetch_rss(url: str):
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            body = await resp.text()
            return feedparser.parse(body)

def clean_html(text: str) -> str:
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
    import hashlib
    cleaned = re.sub(r"https?://\S+", "", text.lower())
    cleaned = re.sub(r"[^\w]", "", cleaned)
    return hashlib.md5(cleaned.encode()).hexdigest()

def _is_duplicate(text: str) -> bool:
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
    text_lower = text.lower()
    for kw in HIGH_PRIORITY_KEYWORDS:
        if kw in text_lower:
            return "🔴"
    for kw in MEDIUM_PRIORITY_KEYWORDS:
        if kw in text_lower:
            return "🟡"
    return "⚪"

async def check_feed(bot, src_type: str, name: str, first_run: bool = False):
    label = f"📢 کانال تلگرام @{name}" if src_type == "telegram" else f"🐦 توییتر @{name}"
    new_count = 0
    try:
        if src_type == "telegram":
            messages = await fetch_channel_messages(name)
            if messages is None:
                raise Exception("خطا در دریافت صفحه")
            for msg in messages:
                msg_key = f"tg:{msg['id']}"
                if db.is_seen(msg_key):
                    continue
                db.mark_seen(msg_key)
                if first_run or notifications_paused:
                    new_count += 1
                    continue
                await send_notification(bot, label, msg["text"], msg["link"])
                await asyncio.sleep(2)
        else:
            tweets = await fetch_tweets(name)
            if tweets is None:
                raise Exception("خطا در دریافت توییت‌ها")
            for tweet in tweets[:5]:
                msg_key = f"tw:{name}:{tweet['id']}"
                if db.is_seen(msg_key):
                    continue
                db.mark_seen(msg_key)
                if first_run or notifications_paused:
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
    await asyncio.sleep(5)
    prune_counter = 0
    first_run = True
    while True:
        try:
            sources = db.get_sources(active_only=True)
            for src_type, name, _, _, _, _ in sources:
                await check_feed(bot, src_type, name, first_run=first_run)
                if src_type == "twitter":
                    await asyncio.sleep(3)
                else:
                    await asyncio.sleep(1)
            first_run = False
        except Exception as e:
            logger.error(f"خطا در حلقه مانیتور: {e}")
        prune_counter += 1
        if prune_counter % 100 == 0:
            db.prune_seen(days=30)
        await asyncio.sleep(POLL_INTERVAL)

# ════════════════════════════════════════════════════
#  NOTIFICATION
# ════════════════════════════════════════════════════
async def send_notification(bot, source_label: str, original_text: str, source_url: str):
    if _is_duplicate(original_text):
        logger.info("⏭️ پیام تکراری رد شد")
        return
    priority = _detect_priority(original_text)
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

    max_orig, max_trans = 1200, 1800
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
    url = url.strip()
    if url.startswith("@"):
        name = url[1:].rstrip("/")
        return ("telegram", name, f"https://t.me/{name}")
    if "t.me/" in url:
        part = url.split("t.me/")[-1].split("?")[0].split("#")[0]
        if part.startswith("s/"):
            part = part[2:]
        part = part.split("/")[0]
        if not part or part.startswith("+") or "joinchat" in part:
            return None
        return ("telegram", part, f"https://t.me/{part}")
    for domain in ("x.com/", "twitter.com/"):
        if domain in url:
            part = url.split(domain)[-1].split("?")[0].split("#")[0].split("/")[0]
            if part:
                return ("twitter", part, f"https://x.com/{part}")
    return None

# ════════════════════════════════════════════════════
#  INLINE MENUS
# ════════════════════════════════════════════════════
def main_menu():
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
    cid = update.effective_chat.id
    if owner_chat_id is not None and cid == owner_chat_id:
        return True
    return db.is_admin(cid)

async def is_owner(update: Update) -> bool:
    return owner_chat_id is not None and update.effective_chat.id == owner_chat_id

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global owner_chat_id
    cid = update.effective_chat.id
    if owner_chat_id is None:
        owner_chat_id = cid
        db.set_meta("owner_chat_id", str(owner_chat_id))
        logger.info(f"مالک ثبت شد: {owner_chat_id}")

    is_me = (cid == owner_chat_id)
    is_adm = db.is_admin(cid)
    if not is_me and not is_adm:
        await update.message.reply_text("🔒 این ربات خصوصی است.")
        return

    # پاکسازی وضعیت‌های قبلی هنگام بازگشت به استارت
    user_states.pop(cid, None)
    admin_states.pop(cid, None)

    role = "👑 مالک" if is_me else "🛡️ ادمین"
    text = (
        f"🎉 به ربات مانیتور ایردراپ خوش آمدید!\n\n"
        f"👤 نقش شما: {role}\n\n"
        f"این ربات منابع شما را رصد می‌کند و هر ایردراپ یا آپدیت جدید "
        f"را همراه با ترجمه فارسی برایتان می‌فرستد.\n\n"
        f"از منوی زیر استفاده کنید:"
    )
    await update.message.reply_text(text, reply_markup=main_menu())

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update): return
    cid = update.effective_chat.id
    user_states.pop(cid, None)
    admin_states.pop(cid, None)
    await update.message.reply_text("📋 منوی اصلی:", reply_markup=main_menu())

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update): return
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
    if not await is_authorized(update): return
    await show_source_list(update)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update): return
    await show_status(update)

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update): return
    await do_test(update, context)

async def cmd_latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update): return
    await show_latest_menu(update)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update): return
    await show_help(update)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    is_me = (owner_chat_id is not None and cid == owner_chat_id)
    is_adm = db.is_admin(cid)
    if not is_me and not is_adm:
        return

    text = update.message.text or ""
    if not text or text.startswith("/"):
        return

    if admin_states.get(cid) == "awaiting_admin_id":
        await do_add_admin(update, text)
        admin_states.pop(cid, None)
        return

    if user_states.get(cid) == "awaiting_source":
        await do_add_source(update, text)
        user_states.pop(cid, None)

async def do_add_admin(update: Update, raw_input: str):
    raw_input = raw_input.strip()
    digits = re.sub(r"[^\d]", "", raw_input)
    if not digits or len(digits) < 5:
        await update.message.reply_text(
            "❌ آیدی نامعتبر!\n\nآیدی عددی باید فقط شامل عدد باشد.\nبرای گرفتن آیدی از دستور `/myid` استفاده کنید.",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )
        return
    new_admin_id = int(digits)
    if new_admin_id == owner_chat_id:
        await update.message.reply_text("⚠️ این آیدی مالک ربات است.", reply_markup=main_menu())
        return
    if db.is_admin(new_admin_id):
        await update.message.reply_text(f"⚠️ این کاربر قبلاً ادمین است: `{new_admin_id}`", parse_mode="Markdown", reply_markup=main_menu())
        return
    db.add_admin(new_admin_id)
    await update.message.reply_text(f"✅ ادمین جدید اضافه شد!\n\n🔢 Chat ID: `{new_admin_id}`", parse_mode="Markdown", reply_markup=main_menu())

# ════════════════════════════════════════════════════
#  ACTION FUNCTIONS
# ════════════════════════════════════════════════════
async def do_add_source(update: Update, url: str):
    result = parse_source_url(url)
    if result is None:
        await update.message.reply_text("❌ لینک نامعتبر!\n\nفرمت صحیح:\n• `https://t.me/name`\n• `https://x.com/name`", parse_mode="Markdown")
        return
    source_type, name, clean_url = result
    existing = {r[1] for r in db.get_sources(source_type)}
    if name in existing:
        await update.message.reply_text(f"⚠️ این منبع قبلاً اضافه شده: @{name}")
        return
    db.add_source(source_type, name, clean_url)
    label = "📢 کانال تلگرام" if source_type == "telegram" else "🐦 اکانت توییتر"
    await update.message.reply_text(f"✅ {label} اضافه شد!\n\n@{name}\n\n🔔 رصد شروع شد.", reply_markup=main_menu())

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
        for _, name, _, _, _, _ in tg: lines.append(f"   • @{name}")
    if tw:
        lines.append("\n🐦 اکانت‌های توییتر:")
        for _, name, _, _, _, _ in tw: lines.append(f"   • @{name}")
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
    lines = [
        "📊 وضعیت ربات", "━" * 22, "",
        "🤖 ربات: ✅ آنلاین",
        f"⏸️ هشدارها: {'متوقف ⏸️' if notifications_paused else 'فعال ✅'}",
        f"⏱️ فاصله بررسی: هر {POLL_INTERVAL} ثانیه",
        f"📢 کانال‌های تلگرام: {len(tg_sources)}",
        f"🐦 اکانت‌های توییتر: {len(tw_sources)}",
        f"📨 پیام‌های پردازش‌شده: {db.seen_count()}",
        f"📤 هشدارهای امروز: {db.get_daily_stat()}"
    ]
    if feed_health:
        lines.append("\n📡 وضعیت آخرین بررسی منابع:")
        for name, info in feed_health.items():
            icon = "✅" if info["ok"] else "❌"
            extra = "" if info["ok"] else f" — {info['error']}"
            lines.append(f"   • @{name}: {icon} ({info['last_check']}){extra}")
    await update.effective_message.reply_text("\n".join(lines), reply_markup=main_menu())

async def do_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_notification(
        context.bot, "🧪 پیام تست",
        "BREAKING: New massive airdrop confirmed! Connect your wallet and complete testnet tasks. Snapshot soon!",
        "https://example.com"
    )
    await update.effective_message.reply_text("✅ پیام تست ارسال شد. (باید نشان 🔴 داشته باشد)", reply_markup=main_menu())

# ════════════════════════════════════════════════════
#  LATEST POSTS — آخرین پست‌ها
# ════════════════════════════════════════════════════
async def show_latest_menu(update: Update):
    sources = db.get_sources(active_only=True)
    if not sources:
        await update.effective_message.reply_text("📭 هیچ منبعی اضافه نشده.", reply_markup=main_menu())
        return
    buttons = []
    for src_type, name, _, _, _, _ in sources:
        emoji = "📢" if src_type == "telegram" else "🐦"
        buttons.append([InlineKeyboardButton(f"{emoji} @{name}", callback_data=f"latest_{src_type}_{name}")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back")])
    await update.effective_message.reply_text("📰 آخرین پست‌ها\n\nکدام منبع را می‌خواهید ببینید؟", reply_markup=InlineKeyboardMarkup(buttons))

async def show_latest_posts(update: Update, context: ContextTypes.DEFAULT_TYPE, src_type: str, name: str):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    label = f"📢 کانال تلگرام @{name}" if src_type == "telegram" else f"🐦 توییتر @{name}"
    try:
        posts = await fetch_channel_messages(name) if src_type == "telegram" else await fetch_tweets(name)
        if not posts:
            await update.effective_message.reply_text(f"📭 @{name}: پستی پیدا نشد.", reply_markup=main_menu())
            return
        count = min(3, len(posts))
        await update.effective_message.reply_text(f"📰 آخرین {count} پست از {label}:\n{'━' * 22}")
        for post in posts[:3]:
            text, link = post["text"], post["link"]
            loop = asyncio.get_event_loop()
            translated = await loop.run_in_executor(None, translate_to_farsi, text, MAX_TRANSLATE_LENGTH)
            max_orig, max_trans = 800, 1200
            orig_show = text[:max_orig] + (" …" if len(text) > max_orig else "")
            trans_show = translated[:max_trans] + (" …" if len(translated) > max_trans else "")
            message = f"📝 متن اصلی:\n{orig_show}\n\n{'─' * 22}\n🌍 ترجمه فارسی:\n{trans_show}\n\n🔗 {link}"
            try:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=message, disable_web_page_preview=True)
            except Exception as e:
                logger.error(f"ارسال پست ناموفق: {e}")
            await asyncio.sleep(1)
        await update.effective_message.reply_text("✅ پایان.", reply_markup=main_menu())
    except Exception as e:
        await update.effective_message.reply_text(f"❌ خطا در دریافت پست‌های @{name}:\n{str(e)[:100]}", reply_markup=main_menu())

async def show_help(update: Update):
    help_text = (
        "❓ راهنمای ربات\n"
        f"{'━' * 22}\n\n"
        "📖 دستورات:\n"
        "• `/start` — شروع ربات\n"
        "• `/menu` — نمایش منو\n"
        "• `/latest` — آخرین پست‌ها\n"
        "• `/add` — افزودن منبع\n"
        "• `/list` — لیست منابع\n"
        "• `/status` — وضعیت ربات\n"
        "• `/myid` — دریافت چت آیدی شما\n"
    )
    await update.effective_message.reply_text(help_text, reply_markup=main_menu())

async def show_admins_menu(update: Update):
    admins = db.get_admins()
    lines = ["🛡️ مدیریت ادمین‌ها", "━" * 22, ""]
    if not admins:
        lines.append("📭 هیچ ادمینی اضافه نشده.")
    else:
        lines.append(f"👥 تعداد ادمین‌ها: {len(admins)}\n")
        for chat_id, username, _ in admins:
            name = f"@{username}" if username else "—"
            lines.append(f"   • `{chat_id}` ({name})")
    lines.append(f"\n👑 مالک: `{owner_chat_id}`")
    buttons = [[InlineKeyboardButton("➕ افزودن ادمین", callback_data="add_admin")]]
    for cid, _, _ in admins:
        buttons.append([InlineKeyboardButton(f"❌ حذف {cid}", callback_data=f"rmadm_{cid}")])
    buttons.append([InlineKeyboardButton("🔙 منوی اصلی", callback_data="back")])
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = f"@{user.username}" if user.username else "بدون یوزرنیم"
    await update.message.reply_text(f"🆔 اطلاعات شما:\n\n👤 نام: {user.full_name}\n📛 یوزرنیم: {username}\n🔢 Chat ID: `{update.effective_chat.id}`", parse_mode="Markdown")

# ════════════════════════════════════════════════════
#  CALLBACK QUERY HANDLER
# ════════════════════════════════════════════════════
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global notifications_paused
    query = update.callback_query
    await query.answer()
    cid = query.message.chat.id

    is_me = (owner_chat_id is not None and cid == owner_chat_id)
    is_adm = db.is_admin(cid)
    if not is_me and not is_adm:
        await query.answer("🔒 خصوصی", show_alert=True)
        return

    data = query.data

    # بازنشانی وضعیت‌ها در صورت کلیک روی منوهای دیگر برای جلوگیری از قفل شدن حالت چندمرحله‌ای
    if data not in ["add", "add_admin"]:
        user_states.pop(cid, None)
        admin_states.pop(cid, None)

    if data in ("admins", "add_admin") or data.startswith("rmadm_"):
        if not is_me:
            await query.answer("🔒 فقط مالک می‌تواند ادمین‌ها را مدیریت کند", show_alert=True)
            return

    if data == "list":
        await show_source_list(update)
    elif data == "latest":
        await show_latest_menu(update)
    elif data == "add":
        user_states[cid] = "awaiting_source"
        await query.message.reply_text("🔗 لینک منبع جدید (تلگرام یا توییتر) را بفرستید:", parse_mode="Markdown")
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
    elif data == "admins":
        await show_admins_menu(update)
    elif data == "add_admin":
        admin_states[cid] = "awaiting_admin_id"
        await query.message.reply_text("🛡️ افزودن ادمین\n\nChat ID عددی کاربر را بفرستید:", parse_mode="Markdown")
    elif data.startswith("rmadm_"):
        try:
            admin_id = int(data.split("_", 1)[1])
            db.remove_admin(admin_id)
            await query.message.reply_text(f"✅ ادمین حذف شد: `{admin_id}`", parse_mode="Markdown", reply_markup=main_menu())
        except Exception:
            await query.message.reply_text("❌ خطا در حذف ادمین.", reply_markup=main_menu())
    elif data.startswith("latest_"):
        parts = data.split("_", 2)
        if len(parts) == 3:
            await show_latest_posts(update, context, parts[1], parts[2])
    elif data.startswith("rm_"):
        parts = data.split("_", 2)
        if len(parts) == 3:
            db.remove_source(parts[1], parts[2])
            feed_health.pop(parts[2], None)
            await query.message.reply_text(f"❌ منبع @{parts[2]} حذف شد.", reply_markup=main_menu())

# ════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ════════════════════════════════════════════════════
def main():
    check_config()
    init_sources()
    load_state()

    # ساخت Application بر اساس فریم‌ورک نسخه جدید python-telegram-bot
    app = Application.builder().token(BOT_TOKEN).build()

    # ثبت هندلرهای دستوری (Commands)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("latest", cmd_latest))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("myid", cmd_myid))

    # ثبت هندلر کلیک روی دکمه‌های درون‌برنامه‌ای (Callback Queries)
    app.add_handler(CallbackQueryHandler(on_callback))

    # ثبت هندلر پیام‌های متنی برای سیستم مکالمه (انتظار برای دریافت لینک یا چت‌آیدی ادمین)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # راه‌اندازی حلقه مانیتورینگ فیدها به صورت Background Task در لوپِ جاریِ asyncio
    loop = asyncio.get_event_loop()
    loop.create_task(feed_loop(app.bot))

    # شروع به کار ربات (Long Polling)
    logger.info("🤖 ربات مانیتور ایردراپ با موفقیت روشن شد و در حال شنود است...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
