import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyParameters

from config import (
    ADMIN_IDS,
    AUTO_CLOSE_AFTER_SECONDS,
    BOT_NAME,
    BOT_TOKEN,
    MONGODB_DATABASE,
    MONGODB_URI,
    REMINDER_INTERVAL_SECONDS,
    SUPPORT_GROUP_ID,
)
from database import Database, utcnow
from admin.handlers import create_admin_routers
from message import (
    blocked_user_caption,
    contact_confirmation_caption,
    contact_cooldown_caption,
    contact_delivery_error_caption,
    contact_duplicate_caption,
    contact_prompt_caption,
    contact_topic_duplicate_caption,
    contact_unavailable_caption,
    file_id as message_file_id,
    fingerprint as message_fingerprint,
    payload as message_payload,
    render,
    reminder_caption,
    setup_required_caption,
    welcome_caption,
)
from keyboards import (
    topic_actions,
    user_menu,
)

logging.basicConfig(level=logging.INFO)
router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)


class ContactFlow(StatesGroup):
    active = State()


db: Database
bot: Bot


def display_name(user) -> str:
    name = " ".join(part for part in (user.first_name, user.last_name) if part)
    return name or "Unknown"


def topic_title(user, status: str = "new", priority: str = "normal") -> str:
    if isinstance(user, dict):
        user_id = user.get("id", user.get("user_id"))
        username = user.get("username")
        name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
        label = f"@{username}" if username else name or "Unknown"
    else:
        user_id = user.id
        username = user.username
        label = f"@{username}" if username else display_name(user)
    label = " ".join(label.split())
    prefix = f"{priority.upper()} | {status.upper()} | "
    suffix = f" | {user_id}"
    return f"{prefix}{label[:max(1, 128 - len(prefix) - len(suffix))]}{suffix}"


def is_missing_topic(error: TelegramBadRequest) -> bool:
    description = str(error).lower()
    return any(
        text in description
        for text in (
            "message thread not found",
            "topic not found",
            "topic_id_invalid",
            "topic is closed",
            "topic_closed",
            "thread is closed",
        )
    )


async def telegram_with_retry(method, *args, **kwargs):
    for attempt in range(3):
        try:
            return await method(*args, **kwargs)
        except TelegramRetryAfter as error:
            if attempt == 2:
                raise
            await asyncio.sleep(error.retry_after)
        except (TelegramNetworkError, TelegramServerError):
            if attempt == 2:
                raise
            await asyncio.sleep(2 ** attempt)


async def copy_message_with_retry(**kwargs):
    return await telegram_with_retry(bot.copy_message, **kwargs)


async def create_user_topic(
    user,
    group_id: int,
    previous: dict | None = None,
    force: bool = False,
) -> dict:
    claimed = await db.claim_topic_creation(user.id, group_id, force=force)
    if not claimed:
        previous_thread = previous.get("thread_id") if previous else None
        for _ in range(50):
            existing = await db.get_support_topic(user.id)
            if (
                existing
                and existing.get("support_group_id") == group_id
                and existing.get("thread_id")
                and not existing.get("creation_lease_until")
                and (not force or existing["thread_id"] != previous_thread)
            ):
                return existing
            await asyncio.sleep(0.2)
        raise RuntimeError("Another worker is still preparing this support topic. Please retry shortly.")
    status = previous.get("status", "new") if previous else "new"
    priority = previous.get("priority", "normal") if previous else "normal"
    try:
        created = await telegram_with_retry(
            bot.create_forum_topic,
            group_id,
            name=topic_title(user, status, priority),
        )
        topic = {
            "user_id": user.id,
            "support_group_id": group_id,
            "thread_id": created.message_thread_id,
            "topic_name": topic_title(user, status, priority),
            "status": status,
            "priority": priority,
            "assigned_admin": previous.get("assigned_admin") if previous else None,
            "muted": previous.get("muted", False) if previous else False,
            "unread_count": 0,
            "awaiting_admin": False,
            "updated_at": utcnow(),
        }
        saved = await db.save_support_topic(topic)
    except Exception:
        await db.release_topic_creation(user.id)
        raise
    if saved and saved["thread_id"] != created.message_thread_id:
        try:
            await bot.delete_forum_topic(group_id, created.message_thread_id)
        except TelegramBadRequest:
            logging.warning("Could not remove duplicate topic %s", created.message_thread_id)
        return saved
    await db.log_support_event(
        "topic_recreated" if previous else "topic_created",
        user_id=user.id,
        group_id=group_id,
        thread_id=created.message_thread_id,
    )
    return saved or topic


async def ensure_user_topic(user) -> dict:
    group_id = await db.get_support_group_id()
    if not group_id:
        raise RuntimeError(setup_required_caption())
    topic = await db.get_support_topic(user.id)
    force_recreate = False
    if topic and topic.get("support_group_id") == group_id:
        if topic.get("archived_at"):
            try:
                await telegram_with_retry(bot.reopen_forum_topic, group_id, topic["thread_id"])
                topic = await db.update_support_topic(
                    user.id,
                    {"status": "new", "awaiting_admin": True, "unread_count": 0},
                    unset_fields=["archived_at"],
                )
                return topic
            except TelegramBadRequest as error:
                if not is_missing_topic(error):
                    raise
                force_recreate = True
        else:
            return topic
    return await create_user_topic(user, group_id, topic, force=force_recreate)


async def refresh_topic_control(topic: dict) -> None:
    user = await db.users.find_one({"user_id": topic["user_id"]})
    if not user:
        return
    status = topic.get("status", "new")
    priority = topic.get("priority", "normal")
    name = topic_title(user, status, priority)
    try:
        await telegram_with_retry(
            bot.edit_forum_topic,
            topic["support_group_id"],
            topic["thread_id"],
            name=name,
        )
    except TelegramBadRequest as error:
        if is_missing_topic(error):
            return
    assigned = topic.get("assigned_admin")
    admin = await db.admins.find_one({"admin_id": assigned}) if assigned else None
    last_admin = await db.admins.find_one({"admin_id": topic.get("last_admin_id")}) if topic.get("last_admin_id") else None
    text = render(
        "topic_control",
        name=name,
        assigned=admin.get("name", assigned) if admin else "Unassigned",
        unread=topic.get("unread_count", 0),
        awaiting="yes" if topic.get("awaiting_admin") else "no",
        last_reply=last_admin.get("name", topic["last_admin_id"]) if last_admin else "none",
    )
    markup = topic_actions(topic["user_id"], status, topic.get("muted", False))
    control_id = topic.get("control_message_id")
    if control_id:
        try:
            await bot.edit_message_text(
                text,
                topic["support_group_id"],
                control_id,
                reply_markup=markup,
            )
            await db.update_support_topic(topic["user_id"], {"topic_name": name})
            return
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).lower():
                return
    sent = await telegram_with_retry(
        bot.send_message,
        topic["support_group_id"],
        text,
        message_thread_id=topic["thread_id"],
        reply_markup=markup,
    )
    await db.update_support_topic(
        topic["user_id"],
        {"topic_name": name, "control_message_id": sent.message_id},
    )


def topic_reply_parameters(mapping: dict | None) -> ReplyParameters | None:
    if not mapping:
        return None
    message_id = mapping.get("destination_message_id")
    return ReplyParameters(message_id=message_id, allow_sending_without_reply=True) if message_id else None


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    user = await db.save_user(message.from_user)
    if user.get("status") == "banned":
        await message.answer(blocked_user_caption())
        return
    await state.clear()
    settings = await db.settings.find_one({"_id": "bot"}) or {}
    await message.answer(settings.get("welcome_text", welcome_caption(BOT_NAME)), reply_markup=user_menu())


@router.message(F.text == "Contact Admin")
async def begin_contact(message: Message, state: FSMContext) -> None:
    user = await db.save_user(message.from_user)
    if user.get("status") == "banned":
        await message.answer(blocked_user_caption())
        return
    await state.set_state(ContactFlow.active)
    await message.answer(contact_prompt_caption())


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(render("cancelled"), reply_markup=user_menu())


@router.message(lambda message: not (message.text and message.text.startswith("/")))
async def receive_contact(message: Message) -> None:
    user = await db.save_user(message.from_user)
    if user.get("status") == "banned":
        await message.answer(blocked_user_caption())
        return
    result = await db.check_message_allowed(message.from_user.id, message_fingerprint(message))
    if result == "duplicate":
        await message.answer(contact_duplicate_caption())
        return
    if result == "cooldown":
        await message.answer(contact_cooldown_caption())
        return
    try:
        topic = await ensure_user_topic(message.from_user)
    except RuntimeError as error:
        await message.answer(str(error))
        return
    record = {
        "user_id": message.from_user.id,
        "direction": "user",
        "source_chat_id": message.chat.id,
        "source_message_id": message.message_id,
        "support_group_id": topic["support_group_id"],
        "thread_id": topic["thread_id"],
        **message_payload(message),
        "created_at": utcnow(),
    }
    if not await db.reserve_support_message(record):
        await message.answer(contact_topic_duplicate_caption(topic["thread_id"]))
        return
    reply_mapping = None
    if message.reply_to_message:
        reply_mapping = await db.get_reply_message(message.chat.id, message.reply_to_message.message_id)
    try:
        copied = await copy_message_with_retry(
            chat_id=topic["support_group_id"],
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=topic["thread_id"],
            reply_parameters=topic_reply_parameters(reply_mapping),
        )
    except TelegramBadRequest as error:
        if not is_missing_topic(error):
            await db.fail_support_message(message.chat.id, message.message_id, str(error))
            await db.release_failed_message_attempt(message.from_user.id, message_fingerprint(message))
            await db.log_support_event("user_message_delivery_failed", user_id=message.from_user.id, error=str(error))
            await message.answer(contact_delivery_error_caption())
            return
        try:
            if "closed" in str(error).lower():
                await telegram_with_retry(bot.reopen_forum_topic, topic["support_group_id"], topic["thread_id"])
            else:
                topic = await create_user_topic(message.from_user, topic["support_group_id"], topic, force=True)
            copied = await copy_message_with_retry(
                chat_id=topic["support_group_id"],
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                message_thread_id=topic["thread_id"],
                reply_parameters=topic_reply_parameters(reply_mapping),
            )
            await db.log_support_event("topic_recreated", user_id=message.from_user.id, thread_id=topic["thread_id"])
        except Exception as retry_error:
            await db.fail_support_message(message.chat.id, message.message_id, str(retry_error))
            await db.release_failed_message_attempt(message.from_user.id, message_fingerprint(message))
            await message.answer(contact_unavailable_caption())
            return
    except (TelegramNetworkError, TelegramServerError, TelegramForbiddenError, TelegramRetryAfter) as error:
        await db.fail_support_message(message.chat.id, message.message_id, str(error))
        await db.release_failed_message_attempt(message.from_user.id, message_fingerprint(message))
        await db.log_support_event("user_message_delivery_failed", user_id=message.from_user.id, error=str(error))
        await message.answer(contact_unavailable_caption())
        return

    await db.finish_support_message(
        message.chat.id,
        message.message_id,
        topic["support_group_id"],
        copied.message_id,
        topic["thread_id"],
    )
    topic = await db.note_topic_user_message(message.from_user.id) or topic
    await refresh_topic_control(topic)
    await message.answer(contact_confirmation_caption(topic["thread_id"]))


async def support_maintenance_worker() -> None:
    while True:
        now = utcnow()
        if REMINDER_INTERVAL_SECONDS > 0:
            reminder_cutoff = now - timedelta(seconds=REMINDER_INTERVAL_SECONDS)
            for topic in await db.topics_for_reminder(reminder_cutoff):
                if not await db.claim_topic_reminder(topic["user_id"], reminder_cutoff, now):
                    continue
                try:
                    await bot.send_message(
                        topic["support_group_id"],
                        reminder_caption(topic["user_id"]),
                        message_thread_id=topic["thread_id"],
                    )
                except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramServerError) as error:
                    await db.log_support_event(
                        "reminder_delivery_failed",
                        user_id=topic["user_id"],
                        error=str(error),
                    )
        if AUTO_CLOSE_AFTER_SECONDS > 0:
            close_cutoff = now - timedelta(seconds=AUTO_CLOSE_AFTER_SECONDS)
            candidates = await db.topics_for_auto_close(close_cutoff)
            for candidate in candidates:
                topic = await db.claim_topic_auto_close(candidate["user_id"], close_cutoff, now)
                if not topic:
                    continue
                try:
                    await refresh_topic_control(topic)
                    await telegram_with_retry(bot.close_forum_topic, topic["support_group_id"], topic["thread_id"])
                    await db.log_support_event("topic_auto_closed", user_id=candidate["user_id"])
                except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramServerError) as error:
                    await db.log_support_event(
                        "topic_auto_close_failed",
                        user_id=candidate["user_id"],
                        error=str(error),
                    )
        await asyncio.sleep(60)


async def main() -> None:
    global db, bot
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in the environment or .env file")
    db = Database(MONGODB_URI, MONGODB_DATABASE)
    await db.initialize(ADMIN_IDS, BOT_NAME)
    bot = Bot(BOT_TOKEN)
    if SUPPORT_GROUP_ID:
        await db.set_support_group(SUPPORT_GROUP_ID, "Configured support group")
    dispatcher = Dispatcher()
    admin_private_router, admin_group_router = create_admin_routers(
        db,
        bot,
        {
            "create_user_topic": create_user_topic,
            "copy_message_with_retry": copy_message_with_retry,
            "is_missing_topic": is_missing_topic,
            "message_payload": message_payload,
            "refresh_topic_control": refresh_topic_control,
            "telegram_with_retry": telegram_with_retry,
            "topic_reply_parameters": topic_reply_parameters,
        },
    )
    dispatcher.include_router(router)
    dispatcher.include_router(admin_private_router)
    dispatcher.include_router(admin_group_router)
    maintenance_task = asyncio.create_task(support_maintenance_worker())
    try:
        await dispatcher.start_polling(bot)
    finally:
        maintenance_task.cancel()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
