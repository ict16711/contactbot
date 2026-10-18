import asyncio
import hashlib
import logging
import re
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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

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
from keyboards import (
    admin_dashboard,
    ticket_actions,
    topic_actions,
    transfer_admins,
    user_menu,
    user_pagination,
    priority_options,
)

logging.basicConfig(level=logging.INFO)
router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
group_router = Router()
group_router.message.filter(F.chat.type == ChatType.SUPERGROUP)
group_router.callback_query.filter(F.message.chat.type == ChatType.SUPERGROUP)


class ContactFlow(StatesGroup):
    active = State()


class ReplyFlow(StatesGroup):
    waiting_message = State()


class BroadcastFlow(StatesGroup):
    waiting_message = State()


class SearchFlow(StatesGroup):
    waiting_query = State()


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


async def admin_permissions(admin_id: int) -> set[str]:
    admin = await db.admins.find_one({"admin_id": admin_id})
    return set(admin.get("permissions", [])) if admin else set()


async def require_admin(message: Message, permission: str) -> bool:
    permissions = await admin_permissions(message.from_user.id)
    if "*" not in permissions and permission not in permissions:
        await message.answer("You are not authorized to use this command.")
        return False
    return True


async def require_callback_admin(callback: CallbackQuery, permission: str) -> bool:
    permissions = await admin_permissions(callback.from_user.id)
    if "*" not in permissions and permission not in permissions:
        await callback.answer("You are not authorized to do that.", show_alert=True)
        return False
    return True


async def send_user_page(message: Message, page: int) -> None:
    page = max(0, page)
    cursor = db.users.find().sort("joined_at", -1).skip(page * 10).limit(11)
    users = await cursor.to_list(length=11)
    lines = ["Registered users:"]
    for user in users[:10]:
        username = f"@{user['username']}" if user.get("username") else "no username"
        lines.append(f"{user['user_id']} | {user.get('first_name') or 'Unknown'} | {username} | {user.get('status', 'active')}")
    await message.answer("\n".join(lines), reply_markup=user_pagination(page, len(users) > 10))


async def perform_broadcast(admin_id: int, source_chat_id: int, source_message_id: int, status_message: Message) -> None:
    users = db.users.find({"status": "active"}, {"user_id": 1})
    delivered = 0
    failed = 0
    async for user in users:
        try:
            await bot.copy_message(user["user_id"], source_chat_id, source_message_id)
            delivered += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        except Exception:
            logging.exception("Broadcast delivery failed for user %s", user["user_id"])
            failed += 1
    await db.broadcasts.insert_one(
        {"admin_id": admin_id, "created_at": utcnow(), "delivered": delivered, "failed": failed}
    )
    await status_message.edit_text(f"Broadcast complete. Delivered: {delivered}; failed: {failed}.")


def message_file_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id
    for field in ("video", "document", "voice", "audio", "animation", "video_note", "sticker"):
        media = getattr(message, field, None)
        if media:
            return media.file_id
    return None


def message_fingerprint(message: Message) -> str:
    value = "\0".join(
        (message.content_type, message.text or message.caption or "", message_file_id(message) or "")
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
        raise RuntimeError("Support inbox is not configured. Ask an owner to run /setup in the support supergroup.")
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
    text = (
        f"{name}\n"
        f"Assigned: {admin.get('name', assigned) if admin else 'Unassigned'}\n"
        f"Unread: {topic.get('unread_count', 0)} | Awaiting admin: {'yes' if topic.get('awaiting_admin') else 'no'}\n"
        f"Last reply: {last_admin.get('name', topic['last_admin_id']) if last_admin else 'none'}"
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


@group_router.message(Command("setup"))
async def setup_support_group(message: Message) -> None:
    permissions = await admin_permissions(message.from_user.id)
    if "*" not in permissions and "admin_management" not in permissions:
        await message.answer("Only an authorized owner can configure the support inbox.")
        return
    chat = await bot.get_chat(message.chat.id)
    if not chat.is_forum:
        await message.answer("Enable Topics for this supergroup before running /setup.")
        return
    bot_user = await bot.get_me()
    bot_member = await bot.get_chat_member(message.chat.id, bot_user.id)
    if bot_member.status != "administrator" or not getattr(bot_member, "can_manage_topics", False):
        await message.answer("Make the bot an administrator with Manage Topics permission, then run /setup again.")
        return
    await db.set_support_group(message.chat.id, chat.title or "Support Inbox")
    await db.log_support_event("support_group_setup", group_id=message.chat.id, owner_id=message.from_user.id)
    await message.answer("Support inbox configured. New user conversations will get their own topic here.")


@group_router.message(
    F.message_thread_id,
    lambda message: message.from_user and not message.from_user.is_bot and not (message.text and message.text.startswith("/")),
)
async def route_admin_topic_message(message: Message) -> None:
    group_id = await db.get_support_group_id()
    if not group_id or message.chat.id != group_id:
        return
    permissions = await admin_permissions(message.from_user.id)
    if "*" not in permissions and "reply" not in permissions:
        return
    topic = await db.get_topic_by_thread(message.chat.id, message.message_thread_id)
    if not topic:
        return
    record = {
        "user_id": topic["user_id"],
        "direction": "admin",
        "source_chat_id": message.chat.id,
        "source_message_id": message.message_id,
        "support_group_id": message.chat.id,
        "thread_id": message.message_thread_id,
        "media_type": message.content_type,
        "text": message.text or message.caption,
        "file_id": message_file_id(message),
        "created_at": utcnow(),
    }
    if not await db.reserve_support_message(record):
        return
    reply_mapping = None
    if message.reply_to_message:
        reply_mapping = await db.get_reply_message(message.chat.id, message.reply_to_message.message_id)
    try:
        copied = await copy_message_with_retry(
            chat_id=topic["user_id"],
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            reply_parameters=topic_reply_parameters(reply_mapping),
        )
    except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramServerError) as error:
        await db.fail_support_message(message.chat.id, message.message_id, str(error))
        await db.log_support_event(
            "admin_reply_delivery_failed",
            user_id=topic["user_id"],
            admin_id=message.from_user.id,
            error=str(error),
        )
        await message.reply("Could not deliver that reply to the user.")
        return
    await db.finish_support_message(
        message.chat.id,
        message.message_id,
        topic["user_id"],
        copied.message_id,
        message.message_thread_id,
    )
    topic = await db.note_topic_admin_reply(topic["user_id"], message.from_user.id) or topic
    await refresh_topic_control(topic)


@group_router.callback_query(F.data.startswith("inbox:"))
async def inbox_action_callback(callback: CallbackQuery) -> None:
    if not await require_callback_admin(callback, "tickets"):
        return
    parts = callback.data.split(":")
    action = parts[1]
    user_id = int(parts[2])
    topic = await db.get_support_topic(user_id)
    if not topic or topic["support_group_id"] != callback.message.chat.id:
        await callback.answer("Conversation not found", show_alert=True)
        return

    if action == "claim":
        topic = await db.update_support_topic(
            user_id,
            {"assigned_admin": callback.from_user.id, "status": "in_progress"},
        )
        await db.log_support_event("conversation_claimed", user_id=user_id, admin_id=callback.from_user.id)
    elif action == "transfer":
        admins = await db.admins.find({"admin_id": {"$ne": callback.from_user.id}}).to_list(length=100)
        if not admins:
            await callback.answer("No other admins are available", show_alert=True)
            return
        await bot.send_message(
            topic["support_group_id"],
            "Choose an admin to assign this conversation:",
            reply_markup=transfer_admins(user_id, admins),
            message_thread_id=topic["thread_id"],
        )
        await callback.answer()
        return
    elif action == "assign":
        admin_id = int(parts[3])
        admin = await db.admins.find_one({"admin_id": admin_id})
        if not admin:
            await callback.answer("That admin is no longer authorized", show_alert=True)
            return
        topic = await db.update_support_topic(
            user_id,
            {"assigned_admin": admin_id, "status": "in_progress"},
        )
        await db.log_support_event(
            "conversation_transferred",
            user_id=user_id,
            from_admin=callback.from_user.id,
            to_admin=admin_id,
        )
    elif action == "priority":
        await bot.send_message(
            topic["support_group_id"],
            "Choose priority:",
            reply_markup=priority_options(user_id),
            message_thread_id=topic["thread_id"],
        )
        await callback.answer()
        return
    elif action == "setpriority":
        priority = parts[3]
        if priority not in {"normal", "high", "urgent"}:
            await callback.answer("Invalid priority", show_alert=True)
            return
        topic = await db.update_support_topic(user_id, {"priority": priority})
        await db.log_support_event("priority_changed", user_id=user_id, admin_id=callback.from_user.id, priority=priority)
    elif action == "mute":
        muted = not topic.get("muted", False)
        topic = await db.update_support_topic(user_id, {"muted": muted})
        await db.log_support_event("reminders_muted", user_id=user_id, admin_id=callback.from_user.id, muted=muted)
    elif action == "close":
        topic = await db.update_support_topic(
            user_id,
            {"status": "resolved", "awaiting_admin": False, "unread_count": 0},
        )
        await refresh_topic_control(topic)
        try:
            await telegram_with_retry(bot.close_forum_topic, topic["support_group_id"], topic["thread_id"])
            topic = await db.update_support_topic(user_id, {"archived_at": utcnow()})
        except TelegramBadRequest as error:
            await db.log_support_event("topic_close_failed", user_id=user_id, error=str(error))
    elif action == "reopen":
        try:
            await telegram_with_retry(bot.reopen_forum_topic, topic["support_group_id"], topic["thread_id"])
        except TelegramBadRequest as error:
            if is_missing_topic(error):
                user = await bot.get_chat(user_id)
                topic = await create_user_topic(user, topic["support_group_id"], topic, force=True)
            else:
                await callback.answer("Could not reopen the topic", show_alert=True)
                return
        topic = await db.update_support_topic(
            user_id,
            {"status": "new", "awaiting_admin": True},
            unset_fields=["archived_at"],
        )
    else:
        await callback.answer("Unknown action", show_alert=True)
        return

    if topic:
        await refresh_topic_control(topic)
    await callback.answer("Conversation updated")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    user = await db.save_user(message.from_user)
    if user.get("status") == "banned":
        await message.answer("You cannot use this bot.")
        return
    await state.clear()
    settings = await db.settings.find_one({"_id": "bot"}) or {}
    await message.answer(
        settings.get("welcome_text", f"Welcome to {BOT_NAME}. Select Contact Admin when you are ready to send a message."),
        reply_markup=user_menu(),
    )


@router.message(Command("admin"))
async def admin_command(message: Message, state: FSMContext) -> None:
    if not await require_admin(message, "tickets"):
        return
    await state.clear()
    await message.answer("Admin dashboard", reply_markup=admin_dashboard())


@router.message(F.text == "Contact Admin")
async def begin_contact(message: Message, state: FSMContext) -> None:
    user = await db.save_user(message.from_user)
    if user.get("status") == "banned":
        await message.answer("You cannot use this bot.")
        return
    await state.set_state(ContactFlow.active)
    await message.answer("Send your message, photo, document, or voice note. Use /cancel to stop.")


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.", reply_markup=user_menu())


@router.message(lambda message: not (message.text and message.text.startswith("/")))
async def receive_contact(message: Message) -> None:
    user = await db.save_user(message.from_user)
    if user.get("status") == "banned":
        await message.answer("You cannot use this bot.")
        return
    result = await db.check_message_allowed(message.from_user.id, message_fingerprint(message))
    if result == "duplicate":
        await message.answer("That message was already received. Please wait for an admin reply.")
        return
    if result == "cooldown":
        await message.answer("Please wait a few seconds before sending another message.")
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
        "media_type": message.content_type,
        "text": message.text or message.caption,
        "file_id": message_file_id(message),
        "created_at": utcnow(),
    }
    if not await db.reserve_support_message(record):
        await message.answer(f"Message already received in support topic {topic['thread_id']}.")
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
            await message.answer("I could not deliver that message to support. Please try again.")
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
            await message.answer("I could not reach the support inbox. Please try again later.")
            return
    except (TelegramNetworkError, TelegramServerError, TelegramForbiddenError, TelegramRetryAfter) as error:
        await db.fail_support_message(message.chat.id, message.message_id, str(error))
        await db.release_failed_message_attempt(message.from_user.id, message_fingerprint(message))
        await db.log_support_event("user_message_delivery_failed", user_id=message.from_user.id, error=str(error))
        await message.answer("I could not reach the support inbox. Please try again later.")
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
    await message.answer(f"Your message was sent to support. Topic ID: {topic['thread_id']}.")


@router.message(Command("reply"))
async def reply_command(message: Message, state: FSMContext) -> None:
    if not await require_admin(message, "reply"):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Usage: /reply TICKET_ID [message]")
        return
    ticket = await db.tickets.find_one({"ticket_id": parts[1].upper()})
    if not ticket:
        await message.answer("Ticket not found.")
        return
    if len(parts) == 3:
        await deliver_reply(message, ticket, parts[2])
        return
    await state.set_state(ReplyFlow.waiting_message)
    await state.update_data(ticket_id=ticket["ticket_id"])
    await message.answer(f"Send your reply for {ticket['ticket_id']} now. Text and media are supported; /cancel stops.")


async def deliver_reply(message: Message, ticket: dict, source: Message | str) -> None:
    source_chat_id = None
    source_message_id = None
    file_id = None
    if isinstance(source, str):
        sent = await bot.send_message(ticket["user_id"], source)
        text = source
        content_type = "text"
        message_id = sent.message_id
        chat_id = sent.chat.id
    else:
        source_chat_id = source.chat.id
        source_message_id = source.message_id
        text = source.text or source.caption
        content_type = source.content_type
        file_id = message_file_id(source)
        sent = await bot.copy_message(ticket["user_id"], source_chat_id, source_message_id)
        message_id = sent.message_id
        chat_id = ticket["user_id"]
    await db.add_response(
        ticket["ticket_id"],
        message.from_user.id,
        source_chat_id,
        source_message_id,
        chat_id,
        message_id,
        text,
        content_type,
        file_id,
    )
    await message.answer(f"Reply sent to ticket {ticket['ticket_id']}.")


@router.message(ReplyFlow.waiting_message)
async def receive_reply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ticket = await db.tickets.find_one({"ticket_id": data.get("ticket_id")})
    await state.clear()
    if not ticket:
        await message.answer("Ticket not found.")
        return
    try:
        await deliver_reply(message, ticket, message)
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.answer("Could not deliver the reply. The user may have blocked the bot.")


@router.message(F.reply_to_message)
async def direct_ticket_reply(message: Message) -> None:
    if not await require_admin(message, "reply"):
        return
    ticket = await db.tickets.find_one(
        {
            "admin_messages": {
                "$elemMatch": {
                    "admin_id": message.from_user.id,
                    "message_id": message.reply_to_message.message_id,
                }
            }
        }
    )
    if not ticket:
        await message.answer("Reply to a copied user message to respond to its ticket.")
        return
    try:
        await deliver_reply(message, ticket, message)
    except (TelegramForbiddenError, TelegramBadRequest):
        await message.answer("Could not deliver the reply. The user may have blocked the bot.")


@router.message(Command("users"))
async def users_command(message: Message) -> None:
    if not await require_admin(message, "users"):
        return
    parts = (message.text or "").split()
    try:
        page = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        page = 0
    await send_user_page(message, page)


@router.message(Command("search"))
async def search_command(message: Message, state: FSMContext) -> None:
    if not await require_admin(message, "tickets"):
        return
    query = (message.text or "").partition(" ")[2].strip()
    if not query:
        await state.set_state(SearchFlow.waiting_query)
        await message.answer("Send a Telegram ID, username, ticket ID, or search term.")
        return
    await run_search(message, query)


async def run_search(message: Message, query: str) -> None:
    if not query.strip():
        await message.answer("Send a search term, Telegram ID, username, or ticket ID.")
        return
    ticket = await db.tickets.find_one({"ticket_id": query.upper()})
    if not ticket:
        ticket = await db.tickets.find_one(
            {"history.text": {"$regex": re.escape(query), "$options": "i"}}
        )
    if ticket:
        await message.answer(
            f"Ticket {ticket['ticket_id']} | user {ticket['user_id']} | {ticket['status']} | "
            f"queries {ticket['query_count']} | replies {ticket['response_count']}",
            reply_markup=ticket_actions(ticket["ticket_id"]),
        )
        return
    escaped_query = re.escape(query.lstrip("@"))
    filters = [
        {"username": {"$regex": escaped_query, "$options": "i"}},
        {"first_name": {"$regex": re.escape(query), "$options": "i"}},
    ]
    if query.isdigit():
        filters.insert(0, {"user_id": int(query)})
    matches = await db.users.find({"$or": filters}).limit(10).to_list(length=10)
    if not matches:
        await message.answer("No matching user or ticket.")
        return
    lines = [
        f"{item['user_id']} | {item.get('first_name') or 'Unknown'} | "
        f"{'@' + item['username'] if item.get('username') else 'no username'} | {item.get('status', 'active')}"
        for item in matches
    ]
    await message.answer("Matches:\n" + "\n".join(lines))


@router.message(SearchFlow.waiting_query)
async def receive_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    await run_search(message, message.text or message.caption or "")


async def set_user_status(message: Message, status: str, command: str) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(f"Usage: /{command} TELEGRAM_ID")
        return
    user_id = int(parts[1])
    result = await db.users.update_one({"user_id": user_id}, {"$set": {"status": status}})
    if result.matched_count:
        await message.answer(f"User {user_id} is now {status}.")
    else:
        await message.answer("User not found. They must start the bot first.")


@router.message(Command("ban"))
async def ban_command(message: Message) -> None:
    if await require_admin(message, "users"):
        await set_user_status(message, "banned", "ban")


@router.message(Command("unban"))
async def unban_command(message: Message) -> None:
    if await require_admin(message, "users"):
        await set_user_status(message, "active", "unban")


@router.message(Command("stats"))
async def stats_command(message: Message) -> None:
    if not await require_admin(message, "stats"):
        return
    stats = await db.statistics()
    tickets = stats["tickets"]
    statuses = stats["statuses"]
    await message.answer(
        f"Users: {stats['users']} ({stats['active_users']} active, {stats['banned_users']} banned)\n"
        f"Tickets: {tickets['tickets']} ({statuses.get('open', 0)} open, {statuses.get('pending', 0)} pending, {statuses.get('resolved', 0)} resolved)\n"
        f"Queries: {tickets.get('queries', 0)} | Replies: {tickets.get('responses', 0)}\n"
        f"Broadcasts: {stats['broadcasts']}"
    )


@router.message(Command("admins"))
async def admins_command(message: Message) -> None:
    if not await require_admin(message, "admin_management"):
        return
    parts = (message.text or "").split()
    action = parts[1].lower() if len(parts) > 1 else "list"
    if action == "list":
        admins = await db.admins.find().sort("admin_id", 1).to_list(length=None)
        lines = []
        for item in admins:
            username = f" (@{item['username']})" if item.get("username") else ""
            permissions = ", ".join(item.get("permissions", []))
            lines.append(f"{item['admin_id']} | {item.get('name', 'Admin')}{username} | {permissions}")
        await message.answer("Authorized admins:\n" + ("\n".join(lines) if lines else "None"))
    elif action == "add" and len(parts) >= 3 and parts[2].isdigit():
        admin_id = int(parts[2])
        permissions = parts[3].split(",") if len(parts) > 3 else ["reply", "tickets", "users", "broadcast", "stats"]
        profile = {"admin_id": admin_id, "name": "Admin", "permissions": permissions}
        try:
            telegram_profile = await bot.get_chat(admin_id)
            name = " ".join(part for part in (telegram_profile.first_name, telegram_profile.last_name) if part)
            profile["name"] = name or telegram_profile.username or "Admin"
            if telegram_profile.username:
                profile["username"] = telegram_profile.username
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        await db.admins.update_one(
            {"admin_id": admin_id},
            {"$set": profile, "$setOnInsert": {"created_at": utcnow()}},
            upsert=True,
        )
        await message.answer(f"Admin {admin_id} saved with permissions: {', '.join(permissions)}")
    elif action == "remove" and len(parts) == 3 and parts[2].isdigit():
        admin_id = int(parts[2])
        if admin_id in ADMIN_IDS:
            await message.answer("Bootstrap admins cannot be removed here; update ADMIN_IDS instead.")
            return
        await db.admins.delete_one({"admin_id": admin_id})
        await message.answer(f"Admin {admin_id} removed.")
    else:
        await message.answer("Usage: /admins [list|add TELEGRAM_ID [permission,...]|remove TELEGRAM_ID]")


@router.message(Command("quickreplies"))
async def quick_replies_command(message: Message) -> None:
    if not await require_admin(message, "admin_management"):
        return
    parts = (message.text or "").split(maxsplit=2)
    action = parts[1].lower() if len(parts) > 1 else "list"
    settings = await db.settings.find_one({"_id": "bot"}) or {}
    quick_replies = settings.get("quick_replies", [])
    if action == "list":
        lines = [f"{index + 1}. {text}" for index, text in enumerate(quick_replies)]
        await message.answer("Quick replies:\n" + ("\n".join(lines) if lines else "None configured."))
    elif action == "add" and len(parts) == 3:
        quick_replies.append(parts[2])
        await db.settings.update_one({"_id": "bot"}, {"$set": {"quick_replies": quick_replies}})
        await message.answer(f"Added quick reply {len(quick_replies)}.")
    elif action == "remove" and len(parts) == 3 and parts[2].isdigit():
        index = int(parts[2]) - 1
        if index < 0 or index >= len(quick_replies):
            await message.answer("Quick reply number not found.")
            return
        quick_replies.pop(index)
        await db.settings.update_one({"_id": "bot"}, {"$set": {"quick_replies": quick_replies}})
        await message.answer("Quick reply removed.")
    else:
        await message.answer("Usage: /quickreplies [list|add TEXT|remove NUMBER]")


@router.message(Command("broadcast"))
async def broadcast_command(message: Message, state: FSMContext) -> None:
    if not await require_admin(message, "broadcast"):
        return
    await state.set_state(BroadcastFlow.waiting_message)
    await message.answer("Send the message or media to broadcast. It will be previewed before delivery; /cancel aborts.")


@router.message(BroadcastFlow.waiting_message)
async def broadcast_preview(message: Message, state: FSMContext) -> None:
    if not await require_admin(message, "broadcast"):
        await state.clear()
        return
    await state.update_data(source_chat_id=message.chat.id, source_message_id=message.message_id)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Send to all users", callback_data="broadcast:confirm"),
            InlineKeyboardButton(text="Cancel", callback_data="broadcast:cancel"),
        ]]
    )
    await message.answer("Confirm this broadcast?", reply_markup=markup)


@router.callback_query(F.data.startswith("broadcast:"))
async def broadcast_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_callback_admin(callback, "broadcast"):
        return
    action = callback.data.split(":", 1)[1]
    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("Broadcast cancelled.")
    else:
        data = await state.get_data()
        await state.clear()
        await perform_broadcast(callback.from_user.id, data["source_chat_id"], data["source_message_id"], callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("ticket:reply:"))
async def ticket_reply_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_callback_admin(callback, "reply"):
        return
    ticket_id = callback.data.rsplit(":", 1)[1]
    if not await db.tickets.find_one({"ticket_id": ticket_id}):
        await callback.answer("Ticket not found", show_alert=True)
        return
    await state.set_state(ReplyFlow.waiting_message)
    await state.update_data(ticket_id=ticket_id)
    await callback.message.answer(f"Send your reply for {ticket_id}. Text and media are supported.")
    await callback.answer()


@router.callback_query(F.data.startswith("ticket:history:"))
async def ticket_history_callback(callback: CallbackQuery) -> None:
    if not await require_callback_admin(callback, "tickets"):
        return
    ticket_id = callback.data.rsplit(":", 1)[1]
    ticket = await db.tickets.find_one({"ticket_id": ticket_id})
    if not ticket:
        await callback.answer("Ticket not found", show_alert=True)
        return
    await callback.answer("Loading history")
    for entry in ticket.get("history", []):
        if entry.get("direction") == "user":
            source_chat_id = entry.get("chat_id")
            source_message_id = entry.get("message_id")
        else:
            source_chat_id = entry.get("source_chat_id") or entry.get("destination_chat_id")
            source_message_id = entry.get("source_message_id") or entry.get("destination_message_id")
        if source_chat_id and source_message_id:
            try:
                await bot.copy_message(callback.from_user.id, source_chat_id, source_message_id)
            except (TelegramForbiddenError, TelegramBadRequest):
                await callback.message.answer("Could not load one item from ticket history.")


@router.callback_query(F.data.startswith("ticket:quick:"))
async def quick_reply_menu_callback(callback: CallbackQuery) -> None:
    if not await require_callback_admin(callback, "reply"):
        return
    ticket_id = callback.data.rsplit(":", 1)[1]
    ticket = await db.tickets.find_one({"ticket_id": ticket_id})
    if not ticket:
        await callback.answer("Ticket not found", show_alert=True)
        return
    settings = await db.settings.find_one({"_id": "bot"}) or {}
    quick_replies = settings.get("quick_replies", [])
    if not quick_replies:
        await callback.answer("No quick replies configured", show_alert=True)
        return
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text[:60], callback_data=f"ticket:quickreply:{ticket_id}:{index}")]
            for index, text in enumerate(quick_replies)
        ]
    )
    await callback.message.answer(f"Quick replies for {ticket_id}:", reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("ticket:quickreply:"))
async def quick_reply_callback(callback: CallbackQuery) -> None:
    if not await require_callback_admin(callback, "reply"):
        return
    _, _, _, ticket_id, index_text = callback.data.split(":", 4)
    ticket = await db.tickets.find_one({"ticket_id": ticket_id})
    settings = await db.settings.find_one({"_id": "bot"}) or {}
    quick_replies = settings.get("quick_replies", [])
    index = int(index_text)
    if not ticket or index >= len(quick_replies):
        await callback.answer("Ticket or quick reply not found", show_alert=True)
        return
    text = quick_replies[index]
    sent = await bot.send_message(ticket["user_id"], text)
    await db.add_response(
        ticket_id,
        callback.from_user.id,
        None,
        None,
        sent.chat.id,
        sent.message_id,
        text,
        "text",
        None,
    )
    await callback.answer("Reply sent")
    await callback.message.answer(f"Quick reply sent to {ticket_id}.")


@router.callback_query(F.data.startswith("ticket:status:"))
async def ticket_status_callback(callback: CallbackQuery) -> None:
    if not await require_callback_admin(callback, "tickets"):
        return
    _, _, ticket_id, status = callback.data.split(":", 3)
    if status not in {"open", "pending", "resolved"}:
        await callback.answer("Invalid ticket status", show_alert=True)
        return
    ticket = await db.set_ticket_status(ticket_id, status)
    if not ticket:
        await callback.answer("Ticket not found", show_alert=True)
        return
    await callback.message.edit_text(f"Ticket {ticket_id} | {status.replace('_', ' ')}", reply_markup=ticket_actions(ticket_id))
    await callback.answer("Status updated")


@router.callback_query(F.data.startswith("dashboard:"))
async def dashboard_callback(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":", 2)
    if len(action) > 1 and action[1] == "stats":
        if not await require_callback_admin(callback, "stats"):
            return
        stats = await db.statistics()
        tickets = stats["tickets"]
        await callback.message.answer(
            f"Users: {stats['users']} ({stats['active_users']} active)\nTickets: {tickets['tickets']}\n"
            f"Queries: {tickets.get('queries', 0)} | Replies: {tickets.get('responses', 0)}"
        )
    elif len(action) > 1 and action[1] == "users":
        if not await require_callback_admin(callback, "users"):
            return
        await send_user_page(callback.message, int(action[2]) if len(action) > 2 else 0)
    elif len(action) > 1 and action[1] == "tickets":
        if not await require_callback_admin(callback, "tickets"):
            return
        tickets = await db.tickets.find({"status": {"$in": ["open", "pending"]}}).sort("updated_at", -1).limit(10).to_list(length=10)
        if not tickets:
            await callback.message.answer("No open tickets.")
        for ticket in tickets:
            await callback.message.answer(
                f"{ticket['ticket_id']} | user {ticket['user_id']} | {ticket['status']} | {ticket['query_count']} messages",
                reply_markup=ticket_actions(ticket["ticket_id"]),
            )
    elif len(action) > 1 and action[1] == "broadcast":
        if not await require_callback_admin(callback, "broadcast"):
            return
        await state.set_state(BroadcastFlow.waiting_message)
        await callback.message.answer("Send the message or media to broadcast. It will be previewed before delivery.")
    elif len(action) > 1 and action[1] == "search":
        if not await require_callback_admin(callback, "tickets"):
            return
        await state.set_state(SearchFlow.waiting_query)
        await callback.message.answer("Send a Telegram ID, username, ticket ID, or search term.")
    await callback.answer()


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
                        f"Reminder: conversation {topic['user_id']} is still awaiting an admin reply.",
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
    dispatcher.include_router(router)
    dispatcher.include_router(group_router)
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
