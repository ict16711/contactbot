import os

from dotenv import load_dotenv

load_dotenv()


def _admin_ids() -> list[int]:
    value = os.getenv("ADMIN_IDS", "8023740449")
    return [int(item.strip()) for item in value.split(",") if item.strip()]


BOT_TOKEN = os.getenv("BOT_TOKEN", "8644295040:AAGmNCqRwlR7sSsa1l7HUmL-liJd2Yb64L0")
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://hrbots94:Yatin123@cluster0.okvj0sm.mongodb.net/")
MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "contactbot")
ADMIN_IDS = _admin_ids()
BOT_NAME = os.getenv("BOT_NAME", "HRVERTEX_bot")
REMINDER_INTERVAL_SECONDS = int(os.getenv("REMINDER_INTERVAL_SECONDS", "3600"))
AUTO_CLOSE_AFTER_SECONDS = int(os.getenv("AUTO_CLOSE_AFTER_SECONDS", "0"))
