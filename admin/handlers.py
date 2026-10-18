import logging
import re
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramServerError,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import ADMIN_IDS
from database import utcnow
from keyboards import (
    admin_dashboard,
    priority_options,
    ticket_actions,
    topic_actions,
    transfer_admins,
    user_pagination,
)
from message import (
    render,
    reply_delivery_error_caption,
    reply_unavailable_caption,
)


class ReplyFlow(StatesGroup):
    waiting_message = State()


class BroadcastFlow(StatesGroup):
    waiting_message = State()


class SearchFlow(StatesGroup):
    waiting_query = State()


class AdminHandlers:
    def __init__(self, db, bot, support_services: dict) -> None:
        self.db = db
        self.bot = bot
        self.support = support_services
        self.private_router = Router(name="private_admin")
        self.private_router.message.filter(F.chat.type == ChatType.PRIVATE)
        self.private_router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
        self.group_router = Router(name="support_admin")
        self.group_router.message.filter(F.chat.type == ChatType.SUPERGROUP)
        self.group_router.callback_query.filter(F.message.chat.type == ChatType.SUPERGROUP)
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.private_router.message.register(self.admin_command, Command("admin"))
        self.private_router.message.register(self.reply_command, Command("reply"))
        self.private_router.message.register(self.users_command, Command("users"))
        self.private_router.message.register(self.search_command, Command("search"))
        self.private_router.message.register(self.ban_command, Command("ban"))
        self.private_router.message.register(self.unban_command, Command("unban"))
        self.private_router.message.register(self.stats_command, Command("stats"))
        self.private_router.message.register(self.admins_command, Command("admins"))
        self.private_router.message.register(self.quick_replies_command, Command("quickreplies"))
        self.private_router.message.register(self.broadcast_command, Command("broadcast"))
        self.private_router.message.register(self.receive_reply, ReplyFlow.waiting_message)
        self.private_router.message.register(self.broadcast_preview, BroadcastFlow.waiting_message)
        self.private_router.message.register(self.receive_search, SearchFlow.waiting_query)
        self.private_router.message.register(self.direct_ticket_reply, F.reply_to_message)

        self.private_router.callback_query.register(self.broadcast_callback, F.data.startswith("broadcast:"))
        self.private_router.callback_query.register(self.ticket_reply_callback, F.data.startswith("ticket:reply:"))
        self.private_router.callback_query.register(self.ticket_history_callback, F.data.startswith("ticket:history:"))
        self.private_router.callback_query.register(self.quick_reply_menu_callback, F.data.startswith("ticket:quick:"))
        self.private_router.callback_query.register(self.quick_reply_callback, F.data.startswith("ticket:quickreply:"))
        self.private_router.callback_query.register(self.ticket_status_callback, F.data.startswith("ticket:status:"))
        self.private_router.callback_query.register(self.dashboard_callback, F.data.startswith("dashboard:"))

        self.group_router.message.register(self.setup_support_group, Command("setup"))
        self.group_router.message.register(
            self.route_admin_topic_message,
            F.message_thread_id,
            lambda message: message.from_user
            and not message.from_user.is_bot
            and not (message.text and message.text.startswith("/")),
        )
        self.group_router.callback_query.register(self.inbox_action_callback, F.data.startswith("inbox:"))

    async def admin_permissions(self, admin_id: int) -> set[str]:
        admin = await self.db.admins.find_one({"admin_id": admin_id})
        return set(admin.get("permissions", [])) if admin else set()

    async def require_admin(self, message: Message, permission: str) -> bool:
        permissions = await self.admin_permissions(message.from_user.id)
        if "*" not in permissions and permission not in permissions:
            await message.answer(render("unauthorized_command"))
            return False
        return True

    async def require_callback_admin(self, callback: CallbackQuery, permission: str) -> bool:
        permissions = await self.admin_permissions(callback.from_user.id)
        if "*" not in permissions and permission not in permissions:
            await callback.answer(render("unauthorized_action"), show_alert=True)
            return False
        return True

    async def send_user_page(self, message: Message, page: int) -> None:
        page = max(0, page)
        cursor = self.db.users.find().sort("joined_at", -1).skip(page * 10).limit(11)
        users = await cursor.to_list(length=11)
        lines = [render("user_list_title")]
        for user in users[:10]:
            username = f"@{user['username']}" if user.get("username") else "no username"
            lines.append(
                render(
                    "user_list_entry",
                    user_id=user["user_id"],
                    name=user.get("first_name") or "Unknown",
                    username=username,
                    status=user.get("status", "active"),
                )
            )
        await message.answer("\n".join(lines), reply_markup=user_pagination(page, len(users) > 10))

    async def perform_broadcast(
        self,
        admin_id: int,
        source_chat_id: int,
        source_message_id: int,
        status_message: Message,
    ) -> None:
        users = self.db.users.find({"status": "active"}, {"user_id": 1})
        delivered = 0
        failed = 0
        async for user in users:
            try:
                await self.bot.copy_message(user["user_id"], source_chat_id, source_message_id)
                delivered += 1
            except (TelegramForbiddenError, TelegramBadRequest):
                failed += 1
            except Exception:
                logging.exception("Broadcast delivery failed for user %s", user["user_id"])
                failed += 1
        await self.db.broadcasts.insert_one(
            {"admin_id": admin_id, "created_at": utcnow(), "delivered": delivered, "failed": failed}
        )
        await status_message.edit_text(render("broadcast_confirmed", delivered=delivered, failed=failed))

    async def setup_support_group(self, message: Message) -> None:
        permissions = await self.admin_permissions(message.from_user.id)
        if "*" not in permissions and "admin_management" not in permissions:
            await message.answer(render("setup_owner_only"))
            return
        chat = await self.bot.get_chat(message.chat.id)
        if not chat.is_forum:
            await message.answer(render("setup_topics_required"))
            return
        bot_user = await self.bot.get_me()
        bot_member = await self.bot.get_chat_member(message.chat.id, bot_user.id)
        if bot_member.status != "administrator" or not getattr(bot_member, "can_manage_topics", False):
            await message.answer(render("setup_bot_permissions"))
            return
        await self.db.set_support_group(message.chat.id, chat.title or "Support Inbox")
        await self.db.log_support_event(
            "support_group_setup", group_id=message.chat.id, owner_id=message.from_user.id
        )
        await message.answer(render("setup_complete"))

    async def route_admin_topic_message(self, message: Message) -> None:
        group_id = await self.db.get_support_group_id()
        if not group_id or message.chat.id != group_id:
            return
        permissions = await self.admin_permissions(message.from_user.id)
        if "*" not in permissions and "reply" not in permissions:
            return
        topic = await self.db.get_topic_by_thread(message.chat.id, message.message_thread_id)
        if not topic:
            return
        record = {
            "user_id": topic["user_id"],
            "direction": "admin",
            "source_chat_id": message.chat.id,
            "source_message_id": message.message_id,
            "support_group_id": message.chat.id,
            "thread_id": message.message_thread_id,
            **self.support["message_payload"](message),
            "created_at": utcnow(),
        }
        if not await self.db.reserve_support_message(record):
            return
        reply_mapping = None
        if message.reply_to_message:
            reply_mapping = await self.db.get_reply_message(
                message.chat.id, message.reply_to_message.message_id
            )
        try:
            copied = await self.support["copy_message_with_retry"](
                chat_id=topic["user_id"],
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                reply_parameters=self.support["topic_reply_parameters"](reply_mapping),
            )
        except (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramServerError) as error:
            await self.db.fail_support_message(message.chat.id, message.message_id, str(error))
            await self.db.log_support_event(
                "admin_reply_delivery_failed",
                user_id=topic["user_id"],
                admin_id=message.from_user.id,
                error=str(error),
            )
            await message.reply(render("reply_delivery_error"))
            return
        await self.db.finish_support_message(
            message.chat.id,
            message.message_id,
            topic["user_id"],
            copied.message_id,
            message.message_thread_id,
        )
        topic = await self.db.note_topic_admin_reply(topic["user_id"], message.from_user.id) or topic
        await self.support["refresh_topic_control"](topic)

    async def inbox_action_callback(self, callback: CallbackQuery) -> None:
        if not await self.require_callback_admin(callback, "tickets"):
            return
        parts = callback.data.split(":")
        action = parts[1]
        user_id = int(parts[2])
        topic = await self.db.get_support_topic(user_id)
        if not topic or topic["support_group_id"] != callback.message.chat.id:
            await callback.answer(render("conversation_not_found"), show_alert=True)
            return

        if action == "claim":
            topic = await self.db.update_support_topic(
                user_id,
                {"assigned_admin": callback.from_user.id, "status": "in_progress"},
            )
            await self.db.log_support_event("conversation_claimed", user_id=user_id, admin_id=callback.from_user.id)
        elif action == "transfer":
            admins = await self.db.admins.find({"admin_id": {"$ne": callback.from_user.id}}).to_list(length=100)
            if not admins:
                await callback.answer(render("admins_unavailable"), show_alert=True)
                return
            await self.bot.send_message(
                topic["support_group_id"],
                render("choose_admin"),
                reply_markup=transfer_admins(user_id, admins),
                message_thread_id=topic["thread_id"],
            )
            await callback.answer()
            return
        elif action == "assign":
            admin_id = int(parts[3])
            admin = await self.db.admins.find_one({"admin_id": admin_id})
            if not admin:
                await callback.answer(render("admin_no_longer_authorized"), show_alert=True)
                return
            topic = await self.db.update_support_topic(
                user_id,
                {"assigned_admin": admin_id, "status": "in_progress"},
            )
            await self.db.log_support_event(
                "conversation_transferred",
                user_id=user_id,
                from_admin=callback.from_user.id,
                to_admin=admin_id,
            )
        elif action == "priority":
            await self.bot.send_message(
                topic["support_group_id"],
                render("choose_priority"),
                reply_markup=priority_options(user_id),
                message_thread_id=topic["thread_id"],
            )
            await callback.answer()
            return
        elif action == "setpriority":
            priority = parts[3]
            if priority not in {"normal", "high", "urgent"}:
                await callback.answer(render("invalid_priority"), show_alert=True)
                return
            topic = await self.db.update_support_topic(user_id, {"priority": priority})
            await self.db.log_support_event(
                "priority_changed", user_id=user_id, admin_id=callback.from_user.id, priority=priority
            )
        elif action == "mute":
            muted = not topic.get("muted", False)
            topic = await self.db.update_support_topic(user_id, {"muted": muted})
            await self.db.log_support_event(
                "reminders_muted", user_id=user_id, admin_id=callback.from_user.id, muted=muted
            )
        elif action == "close":
            topic = await self.db.update_support_topic(
                user_id,
                {"status": "resolved", "awaiting_admin": False, "unread_count": 0},
            )
            await self.support["refresh_topic_control"](topic)
            try:
                await self.support["telegram_with_retry"](
                    self.bot.close_forum_topic, topic["support_group_id"], topic["thread_id"]
                )
                topic = await self.db.update_support_topic(user_id, {"archived_at": utcnow()})
            except TelegramBadRequest as error:
                await self.db.log_support_event("topic_close_failed", user_id=user_id, error=str(error))
        elif action == "reopen":
            try:
                await self.support["telegram_with_retry"](
                    self.bot.reopen_forum_topic, topic["support_group_id"], topic["thread_id"]
                )
            except TelegramBadRequest as error:
                if self.support["is_missing_topic"](error):
                    user = await self.bot.get_chat(user_id)
                    topic = await self.support["create_user_topic"](
                        user, topic["support_group_id"], topic, force=True
                    )
                else:
                    await callback.answer(render("topic_reopen_failed"), show_alert=True)
                    return
            topic = await self.db.update_support_topic(
                user_id,
                {"status": "new", "awaiting_admin": True},
                unset_fields=["archived_at"],
            )
        else:
            await callback.answer(render("unknown_inbox_action"), show_alert=True)
            return

        if topic:
            await self.support["refresh_topic_control"](topic)
        await callback.answer(render("inbox_action_done"))

    async def admin_command(self, message: Message, state: FSMContext) -> None:
        if not await self.require_admin(message, "tickets"):
            return
        await state.clear()
        await message.answer(render("admin_dashboard"), reply_markup=admin_dashboard())

    async def reply_command(self, message: Message, state: FSMContext) -> None:
        if not await self.require_admin(message, "reply"):
            return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 2:
            await message.answer(render("reply_usage"))
            return
        ticket = await self.db.tickets.find_one({"ticket_id": parts[1].upper()})
        if not ticket:
            await message.answer(render("ticket_not_found"))
            return
        if len(parts) == 3:
            await self.deliver_reply(message, ticket, parts[2])
            return
        await state.set_state(ReplyFlow.waiting_message)
        await state.update_data(ticket_id=ticket["ticket_id"])
        await message.answer(render("reply_prompt", ticket_id=ticket["ticket_id"]))

    async def deliver_reply(self, message: Message, ticket: dict, source: Message | str) -> None:
        source_chat_id = None
        source_message_id = None
        file_id = None
        if isinstance(source, str):
            sent = await self.bot.send_message(ticket["user_id"], source)
            text = source
            content_type = "text"
            message_id = sent.message_id
            chat_id = sent.chat.id
        else:
            source_chat_id = source.chat.id
            source_message_id = source.message_id
            source_payload = self.support["message_payload"](source)
            text = source_payload["text"]
            content_type = source_payload["media_type"]
            file_id = source_payload["file_id"]
            sent = await self.bot.copy_message(ticket["user_id"], source_chat_id, source_message_id)
            message_id = sent.message_id
            chat_id = ticket["user_id"]
        await self.db.add_response(
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
        await message.answer(render("reply_sent", ticket_id=ticket["ticket_id"]))

    async def receive_reply(self, message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        ticket = await self.db.tickets.find_one({"ticket_id": data.get("ticket_id")})
        await state.clear()
        if not ticket:
            await message.answer(render("ticket_not_found"))
            return
        try:
            await self.deliver_reply(message, ticket, message)
        except (TelegramForbiddenError, TelegramBadRequest):
            await message.answer(reply_unavailable_caption())

    async def direct_ticket_reply(self, message: Message) -> None:
        if not await self.require_admin(message, "reply"):
            return
        ticket = await self.db.tickets.find_one(
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
            await message.answer(render("direct_reply_prompt"))
            return
        try:
            await self.deliver_reply(message, ticket, message)
        except (TelegramForbiddenError, TelegramBadRequest):
            await message.answer(reply_unavailable_caption())

    async def users_command(self, message: Message) -> None:
        if not await self.require_admin(message, "users"):
            return
        parts = (message.text or "").split()
        try:
            page = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            page = 0
        await self.send_user_page(message, page)

    async def search_command(self, message: Message, state: FSMContext) -> None:
        if not await self.require_admin(message, "tickets"):
            return
        query = (message.text or "").partition(" ")[2].strip()
        if not query:
            await state.set_state(SearchFlow.waiting_query)
            await message.answer(render("search_prompt"))
            return
        await self.run_search(message, query)

    async def run_search(self, message: Message, query: str) -> None:
        if not query.strip():
            await message.answer(render("search_empty_prompt"))
            return
        ticket = await self.db.tickets.find_one({"ticket_id": query.upper()})
        if not ticket:
            ticket = await self.db.tickets.find_one(
                {"history.text": {"$regex": re.escape(query), "$options": "i"}}
            )
        if ticket:
            await message.answer(
                render(
                    "ticket_summary",
                    ticket_id=ticket["ticket_id"],
                    user_id=ticket["user_id"],
                    status=ticket["status"],
                    queries=ticket["query_count"],
                    responses=ticket["response_count"],
                ),
                reply_markup=ticket_actions(ticket["ticket_id"]),
            )
            return
        filters = [
            {"username": {"$regex": re.escape(query.lstrip("@")), "$options": "i"}},
            {"first_name": {"$regex": re.escape(query), "$options": "i"}},
        ]
        if query.isdigit():
            filters.insert(0, {"user_id": int(query)})
        matches = await self.db.users.find({"$or": filters}).limit(10).to_list(length=10)
        if not matches:
            await message.answer(render("search_no_results"))
            return
        lines = [
            render(
                "search_user_entry",
                user_id=item["user_id"],
                name=item.get("first_name") or "Unknown",
                username=f"@{item['username']}" if item.get("username") else "no username",
                status=item.get("status", "active"),
            )
            for item in matches
        ]
        await message.answer(render("search_matches", lines="\n".join(lines)))

    async def receive_search(self, message: Message, state: FSMContext) -> None:
        await state.clear()
        await self.run_search(message, self.support["text_or_caption"](message) or "")

    async def set_user_status(self, message: Message, status: str, command: str) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer(render("ban_usage", command=command))
            return
        user_id = int(parts[1])
        result = await self.db.users.update_one({"user_id": user_id}, {"$set": {"status": status}})
        if result.matched_count:
            await message.answer(render("user_status_changed", user_id=user_id, status=status))
        else:
            await message.answer(render("user_not_found"))

    async def ban_command(self, message: Message) -> None:
        if await self.require_admin(message, "users"):
            await self.set_user_status(message, "banned", "ban")

    async def unban_command(self, message: Message) -> None:
        if await self.require_admin(message, "users"):
            await self.set_user_status(message, "active", "unban")

    async def stats_command(self, message: Message) -> None:
        if not await self.require_admin(message, "stats"):
            return
        stats = await self.db.statistics()
        tickets = stats["tickets"]
        statuses = stats["statuses"]
        await message.answer(
            render(
                "stats",
                users=stats["users"],
                active_users=stats["active_users"],
                banned_users=stats["banned_users"],
                ticket_count=tickets["tickets"],
                open_count=statuses.get("open", 0),
                pending_count=statuses.get("pending", 0),
                resolved_count=statuses.get("resolved", 0),
                queries=tickets.get("queries", 0),
                responses=tickets.get("responses", 0),
                broadcasts=stats["broadcasts"],
            )
        )

    async def admins_command(self, message: Message) -> None:
        if not await self.require_admin(message, "admin_management"):
            return
        parts = (message.text or "").split()
        action = parts[1].lower() if len(parts) > 1 else "list"
        if action == "list":
            admins = await self.db.admins.find().sort("admin_id", 1).to_list(length=None)
            lines = []
            for item in admins:
                username = f" (@{item['username']})" if item.get("username") else ""
                permissions = ", ".join(item.get("permissions", []))
                lines.append(
                    render(
                        "admin_list_entry",
                        admin_id=item["admin_id"],
                        name=item.get("name", "Admin"),
                        username=username,
                        permissions=permissions,
                    )
                )
            await message.answer(
                render("admins_title") + "\n" + ("\n".join(lines) if lines else render("admins_empty"))
            )
        elif action == "add" and len(parts) >= 3 and parts[2].isdigit():
            admin_id = int(parts[2])
            permissions = parts[3].split(",") if len(parts) > 3 else ["reply", "tickets", "users", "broadcast", "stats"]
            profile = {"admin_id": admin_id, "name": "Admin", "permissions": permissions}
            try:
                telegram_profile = await self.bot.get_chat(admin_id)
                name = " ".join(
                    part for part in (telegram_profile.first_name, telegram_profile.last_name) if part
                )
                profile["name"] = name or telegram_profile.username or "Admin"
                if telegram_profile.username:
                    profile["username"] = telegram_profile.username
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
            await self.db.admins.update_one(
                {"admin_id": admin_id},
                {"$set": profile, "$setOnInsert": {"created_at": utcnow()}},
                upsert=True,
            )
            await message.answer(
                render("admin_saved", admin_id=admin_id, permissions=", ".join(permissions))
            )
        elif action == "remove" and len(parts) == 3 and parts[2].isdigit():
            admin_id = int(parts[2])
            if admin_id in ADMIN_IDS:
                await message.answer(render("bootstrap_admin_locked"))
                return
            await self.db.admins.delete_one({"admin_id": admin_id})
            await message.answer(render("admin_removed", admin_id=admin_id))
        else:
            await message.answer(render("admins_usage"))

    async def quick_replies_command(self, message: Message) -> None:
        if not await self.require_admin(message, "admin_management"):
            return
        parts = (message.text or "").split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "list"
        settings = await self.db.settings.find_one({"_id": "bot"}) or {}
        quick_replies = settings.get("quick_replies", [])
        if action == "list":
            lines = [
                render("quick_reply_entry", number=index + 1, text=text)
                for index, text in enumerate(quick_replies)
            ]
            await message.answer(
                render("quick_replies_title")
                + "\n"
                + ("\n".join(lines) if lines else render("quick_replies_empty"))
            )
        elif action == "add" and len(parts) == 3:
            quick_replies.append(parts[2])
            await self.db.settings.update_one({"_id": "bot"}, {"$set": {"quick_replies": quick_replies}})
            await message.answer(render("quick_reply_added", number=len(quick_replies)))
        elif action == "remove" and len(parts) == 3 and parts[2].isdigit():
            index = int(parts[2]) - 1
            if index < 0 or index >= len(quick_replies):
                await message.answer(render("quick_reply_missing"))
                return
            quick_replies.pop(index)
            await self.db.settings.update_one({"_id": "bot"}, {"$set": {"quick_replies": quick_replies}})
            await message.answer(render("quick_reply_removed"))
        else:
            await message.answer(render("quick_replies_usage"))

    async def broadcast_command(self, message: Message, state: FSMContext) -> None:
        if not await self.require_admin(message, "broadcast"):
            return
        await state.set_state(BroadcastFlow.waiting_message)
        await message.answer(render("broadcast_prompt"))

    async def broadcast_preview(self, message: Message, state: FSMContext) -> None:
        if not await self.require_admin(message, "broadcast"):
            await state.clear()
            return
        await state.update_data(source_chat_id=message.chat.id, source_message_id=message.message_id)
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Send to all users", callback_data="broadcast:confirm"),
                    InlineKeyboardButton(text="Cancel", callback_data="broadcast:cancel"),
                ]
            ]
        )
        await message.answer(render("broadcast_confirm_prompt"), reply_markup=markup)

    async def broadcast_callback(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self.require_callback_admin(callback, "broadcast"):
            return
        action = callback.data.split(":", 1)[1]
        if action == "cancel":
            await state.clear()
            await callback.message.edit_text(render("broadcast_cancelled"))
        else:
            data = await state.get_data()
            await state.clear()
            await self.perform_broadcast(
                callback.from_user.id,
                data["source_chat_id"],
                data["source_message_id"],
                callback.message,
            )
        await callback.answer()

    async def ticket_reply_callback(self, callback: CallbackQuery, state: FSMContext) -> None:
        if not await self.require_callback_admin(callback, "reply"):
            return
        ticket_id = callback.data.rsplit(":", 1)[1]
        if not await self.db.tickets.find_one({"ticket_id": ticket_id}):
            await callback.answer(render("ticket_not_found"), show_alert=True)
            return
        await state.set_state(ReplyFlow.waiting_message)
        await state.update_data(ticket_id=ticket_id)
        await callback.message.answer(render("ticket_reply_prompt", ticket_id=ticket_id))
        await callback.answer()

    async def ticket_history_callback(self, callback: CallbackQuery) -> None:
        if not await self.require_callback_admin(callback, "tickets"):
            return
        ticket_id = callback.data.rsplit(":", 1)[1]
        ticket = await self.db.tickets.find_one({"ticket_id": ticket_id})
        if not ticket:
            await callback.answer(render("ticket_not_found"), show_alert=True)
            return
        await callback.answer(render("history_loading"))
        for entry in ticket.get("history", []):
            if entry.get("direction") == "user":
                source_chat_id = entry.get("chat_id")
                source_message_id = entry.get("message_id")
            else:
                source_chat_id = entry.get("source_chat_id") or entry.get("destination_chat_id")
                source_message_id = entry.get("source_message_id") or entry.get("destination_message_id")
            if source_chat_id and source_message_id:
                try:
                    await self.bot.copy_message(callback.from_user.id, source_chat_id, source_message_id)
                except (TelegramForbiddenError, TelegramBadRequest):
                    await callback.message.answer(render("history_item_unavailable"))

    async def quick_reply_menu_callback(self, callback: CallbackQuery) -> None:
        if not await self.require_callback_admin(callback, "reply"):
            return
        ticket_id = callback.data.rsplit(":", 1)[1]
        ticket = await self.db.tickets.find_one({"ticket_id": ticket_id})
        if not ticket:
            await callback.answer(render("ticket_not_found"), show_alert=True)
            return
        settings = await self.db.settings.find_one({"_id": "bot"}) or {}
        quick_replies = settings.get("quick_replies", [])
        if not quick_replies:
            await callback.answer(render("quick_reply_not_configured"), show_alert=True)
            return
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=text[:60], callback_data=f"ticket:quickreply:{ticket_id}:{index}")]
                for index, text in enumerate(quick_replies)
            ]
        )
        await callback.message.answer(render("quick_reply_for_ticket", ticket_id=ticket_id), reply_markup=markup)
        await callback.answer()

    async def quick_reply_callback(self, callback: CallbackQuery) -> None:
        if not await self.require_callback_admin(callback, "reply"):
            return
        _, _, _, ticket_id, index_text = callback.data.split(":", 4)
        ticket = await self.db.tickets.find_one({"ticket_id": ticket_id})
        settings = await self.db.settings.find_one({"_id": "bot"}) or {}
        quick_replies = settings.get("quick_replies", [])
        index = int(index_text)
        if not ticket or index >= len(quick_replies):
            await callback.answer(render("quick_reply_selection_missing"), show_alert=True)
            return
        text = quick_replies[index]
        sent = await self.bot.send_message(ticket["user_id"], text)
        await self.db.add_response(
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
        await callback.answer(render("quick_reply_sent"))
        await callback.message.answer(render("quick_reply_sent_to_ticket", ticket_id=ticket_id))

    async def ticket_status_callback(self, callback: CallbackQuery) -> None:
        if not await self.require_callback_admin(callback, "tickets"):
            return
        _, _, ticket_id, status = callback.data.split(":", 3)
        if status not in {"open", "pending", "resolved"}:
            await callback.answer(render("invalid_ticket_status"), show_alert=True)
            return
        ticket = await self.db.set_ticket_status(ticket_id, status)
        if not ticket:
            await callback.answer(render("ticket_not_found"), show_alert=True)
            return
        await callback.message.edit_text(
            render("ticket_status", ticket_id=ticket_id, status=status.replace("_", " ")),
            reply_markup=ticket_actions(ticket_id),
        )
        await callback.answer(render("status_updated"))

    async def dashboard_callback(self, callback: CallbackQuery, state: FSMContext) -> None:
        action = callback.data.split(":", 2)
        if len(action) > 1 and action[1] == "stats":
            if not await self.require_callback_admin(callback, "stats"):
                return
            stats = await self.db.statistics()
            tickets = stats["tickets"]
            await callback.message.answer(
                render(
                    "stats_dashboard",
                    users=stats["users"],
                    active_users=stats["active_users"],
                    tickets=tickets["tickets"],
                    queries=tickets.get("queries", 0),
                    responses=tickets.get("responses", 0),
                )
            )
        elif len(action) > 1 and action[1] == "users":
            if not await self.require_callback_admin(callback, "users"):
                return
            await self.send_user_page(callback.message, int(action[2]) if len(action) > 2 else 0)
        elif len(action) > 1 and action[1] == "tickets":
            if not await self.require_callback_admin(callback, "tickets"):
                return
            tickets = await self.db.tickets.find(
                {"status": {"$in": ["open", "pending"]}}
            ).sort("updated_at", -1).limit(10).to_list(length=10)
            if not tickets:
                await callback.message.answer(render("open_tickets_empty"))
            for ticket in tickets:
                await callback.message.answer(
                    render(
                        "ticket_list_entry",
                        ticket_id=ticket["ticket_id"],
                        user_id=ticket["user_id"],
                        status=ticket["status"],
                        messages=ticket["query_count"],
                    ),
                    reply_markup=ticket_actions(ticket["ticket_id"]),
                )
        elif len(action) > 1 and action[1] == "broadcast":
            if not await self.require_callback_admin(callback, "broadcast"):
                return
            await state.set_state(BroadcastFlow.waiting_message)
            await callback.message.answer(render("broadcast_dashboard_prompt"))
        elif len(action) > 1 and action[1] == "search":
            if not await self.require_callback_admin(callback, "tickets"):
                return
            await state.set_state(SearchFlow.waiting_query)
            await callback.message.answer(render("search_prompt"))
        await callback.answer()


def create_admin_routers(db, bot, support_services: dict) -> tuple[Router, Router]:
    handlers = AdminHandlers(db, bot, support_services)
    return handlers.private_router, handlers.group_router
