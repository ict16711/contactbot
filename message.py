import hashlib

from aiogram.types import Message


MESSAGES = {
    "welcome": "Welcome to {bot_name}. Select Contact Admin when you are ready to send a message.",
    "contact_prompt": "Send your message, photo, document, or voice note. Use /cancel to stop.",
    "unauthorized_command": "You are not authorized to use this command.",
    "unauthorized_action": "You are not authorized to do that.",
    "blocked_user": "You cannot use this bot.",
    "contact_duplicate": "That message was already received. Please wait for an admin reply.",
    "contact_cooldown": "Please wait a few seconds before sending another message.",
    "contact_delivery_error": "I could not deliver that message to support. Please try again.",
    "contact_unavailable": "I could not reach the support inbox. Please try again later.",
    "contact_confirmation": "Your message was sent to support. Topic ID: {thread_id}.",
    "contact_topic_duplicate": "Message already received in support topic {thread_id}.",
    "setup_required": "Support inbox is not configured. Ask an owner to run /setup in the support supergroup.",
    "reply_delivery_error": "Could not deliver that reply to the user.",
    "reply_unavailable": "Could not deliver the reply. The user may have blocked the bot.",
    "admin_dashboard": "Admin dashboard",
    "cancelled": "Cancelled.",
    "reply_usage": "Usage: /reply TICKET_ID [message]",
    "ticket_not_found": "Ticket not found.",
    "reply_sent": "Reply sent to ticket {ticket_id}.",
    "reply_prompt": "Send your reply for {ticket_id} now. Text and media are supported; /cancel stops.",
    "direct_reply_prompt": "Reply to a copied user message to respond to its ticket.",
    "search_prompt": "Send a Telegram ID, username, ticket ID, or search term.",
    "search_empty_prompt": "Send a search term, Telegram ID, username, or ticket ID.",
    "search_no_results": "No matching user or ticket.",
    "search_matches": "Matches:\n{lines}",
    "ban_usage": "Usage: /{command} TELEGRAM_ID",
    "user_status_changed": "User {user_id} is now {status}.",
    "user_not_found": "User not found. They must start the bot first.",
    "stats": (
        "Users: {users} ({active_users} active, {banned_users} banned)\n"
        "Tickets: {ticket_count} ({open_count} open, {pending_count} pending, {resolved_count} resolved)\n"
        "Queries: {queries} | Replies: {responses}\nBroadcasts: {broadcasts}"
    ),
    "admins_title": "Authorized admins:",
    "admins_empty": "None",
    "admin_saved": "Admin {admin_id} saved with permissions: {permissions}",
    "bootstrap_admin_locked": "Bootstrap admins cannot be removed here; update ADMIN_IDS instead.",
    "admin_removed": "Admin {admin_id} removed.",
    "admins_usage": "Usage: /admins [list|add TELEGRAM_ID [permission,...]|remove TELEGRAM_ID]",
    "quick_replies_title": "Quick replies:",
    "quick_replies_empty": "None configured.",
    "quick_reply_added": "Added quick reply {number}.",
    "quick_reply_missing": "Quick reply number not found.",
    "quick_reply_removed": "Quick reply removed.",
    "quick_replies_usage": "Usage: /quickreplies [list|add TEXT|remove NUMBER]",
    "broadcast_prompt": "Send the message or media to broadcast. It will be previewed before delivery; /cancel aborts.",
    "broadcast_confirm_prompt": "Confirm this broadcast?",
    "broadcast_confirmed": "Broadcast complete. Delivered: {delivered}; failed: {failed}.",
    "broadcast_cancelled": "Broadcast cancelled.",
    "broadcast_dashboard_prompt": "Send the message or media to broadcast. It will be previewed before delivery.",
    "ticket_reply_prompt": "Send your reply for {ticket_id}. Text and media are supported.",
    "history_loading": "Loading history",
    "history_item_unavailable": "Could not load one item from ticket history.",
    "quick_reply_not_configured": "No quick replies configured",
    "quick_reply_for_ticket": "Quick replies for {ticket_id}:",
    "quick_reply_selection_missing": "Ticket or quick reply not found",
    "quick_reply_sent": "Quick reply sent",
    "quick_reply_sent_to_ticket": "Quick reply sent to {ticket_id}.",
    "invalid_ticket_status": "Invalid ticket status",
    "invalid_priority": "Invalid priority",
    "status_updated": "Status updated",
    "conversation_not_found": "Conversation not found",
    "admins_unavailable": "No other admins are available",
    "admin_no_longer_authorized": "That admin is no longer authorized",
    "choose_admin": "Choose an admin to assign this conversation:",
    "choose_priority": "Choose priority:",
    "topic_reopen_failed": "Could not reopen the topic",
    "unknown_inbox_action": "Unknown action",
    "inbox_action_done": "Conversation updated",
    "setup_owner_only": "Only an authorized owner can configure the support inbox.",
    "setup_topics_required": "Enable Topics for this supergroup before running /setup.",
    "setup_bot_permissions": "Make the bot an administrator with Manage Topics permission, then run /setup again.",
    "setup_complete": "Support inbox configured. New user conversations will get their own topic here.",
    "setup_confirm_prompt": "Use this supergroup as the support inbox?",
    "setup_confirm_missing": "This setup button is no longer valid. Run /setup again.",
    "user_list_title": "Registered users:",
    "user_list_entry": "{user_id} | {name} | {username} | {status}",
    "ticket_summary": "Ticket {ticket_id} | user {user_id} | {status} | queries {queries} | replies {responses}",
    "ticket_status": "Ticket {ticket_id} | {status}",
    "search_user_entry": "{user_id} | {name} | {username} | {status}",
    "admin_list_entry": "{admin_id} | {name}{username} | {permissions}",
    "quick_reply_entry": "{number}. {text}",
    "stats_dashboard": "Users: {users} ({active_users} active)\nTickets: {tickets}\nQueries: {queries} | Replies: {responses}",
    "ticket_list_entry": "{ticket_id} | user {user_id} | {status} | {messages} messages",
    "topic_control": (
        "{name}\nAssigned: {assigned}\nUnread: {unread} | Awaiting admin: {awaiting}\n"
        "Last reply: {last_reply}"
    ),
    "open_tickets_empty": "No open tickets.",
    "broadcast_delivery_failed": "Broadcast delivery failed for user {user_id}",
    "inbox_reminder": "Reminder: conversation {user_id} is still awaiting an admin reply.",
}


def render(key: str, **values) -> str:
    return MESSAGES[key].format(**values)


def file_id(message: Message) -> str | None:
    if message.photo:
        return message.photo[-1].file_id
    for field in ("video", "document", "voice", "audio", "animation", "video_note", "sticker"):
        media = getattr(message, field, None)
        if media:
            return media.file_id
    return None


def text_or_caption(message: Message) -> str | None:
    return message.text or message.caption


def payload(message: Message) -> dict[str, str | None]:
    return {
        "media_type": message.content_type,
        "text": text_or_caption(message),
        "file_id": file_id(message),
    }


def fingerprint(message: Message) -> str:
    value = "\0".join(
        (message.content_type, text_or_caption(message) or "", file_id(message) or "")
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def welcome_caption(bot_name: str) -> str:
    return render("welcome", bot_name=bot_name)


def contact_prompt_caption() -> str:
    return render("contact_prompt")


def blocked_user_caption() -> str:
    return render("blocked_user")


def contact_duplicate_caption() -> str:
    return render("contact_duplicate")


def contact_cooldown_caption() -> str:
    return render("contact_cooldown")


def contact_delivery_error_caption() -> str:
    return render("contact_delivery_error")


def contact_unavailable_caption() -> str:
    return render("contact_unavailable")


def contact_confirmation_caption(thread_id: int) -> str:
    return render("contact_confirmation", thread_id=thread_id)


def contact_topic_duplicate_caption(thread_id: int) -> str:
    return render("contact_topic_duplicate", thread_id=thread_id)


def setup_required_caption() -> str:
    return render("setup_required")


def reply_delivery_error_caption() -> str:
    return render("reply_delivery_error")


def reply_unavailable_caption() -> str:
    return render("reply_unavailable")


def reminder_caption(user_id: int) -> str:
    return render("inbox_reminder", user_id=user_id)