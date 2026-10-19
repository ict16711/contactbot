import os

from dotenv import load_dotenv

load_dotenv()


def _admin_ids() -> list[int]:
    value = os.getenv("ADMIN_IDS", "")
    return [int(item.strip()) for item in value.split(",") if item.strip()]


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "contactbot")
ADMIN_IDS = _admin_ids()
BOT_NAME = os.getenv("BOT_NAME", "")
REMINDER_INTERVAL_SECONDS = int(os.getenv("REMINDER_INTERVAL_SECONDS", "3600"))
AUTO_CLOSE_AFTER_SECONDS = int(os.getenv("AUTO_CLOSE_AFTER_SECONDS", "0"))
