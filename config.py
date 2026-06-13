"""بارگذاری تنظیمات از فایل .env"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN") or ""
RSSHUB_URL = (os.getenv("RSSHUB_URL") or "https://rsshub.app").rstrip("/")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL") or "300")
MAX_TRANSLATE_LENGTH = int(os.getenv("MAX_TRANSLATE_LENGTH") or "4500")

# مسیر دیتابیس — روی Railway از volume پایدار استفاده می‌شود
# روی سیستم محلی: data.db (در همان پوشه)
DATABASE_PATH = os.getenv("DATABASE_PATH") or "data.db"


def check_config():
    """بررسی صحت تنظیمات قبل از اجرا"""
    if not BOT_TOKEN:
        print("\n" + "=" * 50)
        print("  ❌ BOT_TOKEN خالی است!")
        print("=" * 50)
        print("  📄 فایل .env را از روی .env.example بسازید و پر کنید.")
        print("  🔑 توکن را از @BotFather در تلگرام بگیرید.")
        print("=" * 50 + "\n")
        sys.exit(1)
