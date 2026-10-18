from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def user_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Contact Admin")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def admin_dashboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Users", callback_data="dashboard:users:0"), InlineKeyboardButton(text="Statistics", callback_data="dashboard:stats")],
            [InlineKeyboardButton(text="Open tickets", callback_data="dashboard:tickets"), InlineKeyboardButton(text="Broadcast", callback_data="dashboard:broadcast")],
            [InlineKeyboardButton(text="Search", callback_data="dashboard:search")],
        ]
    )


def ticket_actions(ticket_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Reply", callback_data=f"ticket:reply:{ticket_id}")],
            [InlineKeyboardButton(text="History", callback_data=f"ticket:history:{ticket_id}"), InlineKeyboardButton(text="Quick reply", callback_data=f"ticket:quick:{ticket_id}")],
            [InlineKeyboardButton(text="Mark pending", callback_data=f"ticket:status:{ticket_id}:pending"), InlineKeyboardButton(text="Resolve", callback_data=f"ticket:status:{ticket_id}:resolved")],
        ]
    )


def user_pagination(page: int, has_next: bool) -> InlineKeyboardMarkup:
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(text="Previous", callback_data=f"dashboard:users:{page - 1}"))
    if has_next:
        buttons.append(InlineKeyboardButton(text="Next", callback_data=f"dashboard:users:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def topic_actions(user_id: int, status: str, muted: bool) -> InlineKeyboardMarkup:
    close_label = "Reopen" if status == "resolved" else "Close"
    close_action = "reopen" if status == "resolved" else "close"
    mute_label = "Unmute reminders" if muted else "Mute reminders"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Claim", callback_data=f"inbox:claim:{user_id}"),
                InlineKeyboardButton(text="Transfer", callback_data=f"inbox:transfer:{user_id}"),
            ],
            [
                InlineKeyboardButton(text="Priority", callback_data=f"inbox:priority:{user_id}"),
                InlineKeyboardButton(text=mute_label, callback_data=f"inbox:mute:{user_id}"),
            ],
            [InlineKeyboardButton(text=close_label, callback_data=f"inbox:{close_action}:{user_id}")],
        ]
    )


def transfer_admins(user_id: int, admins: list[dict]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{admin.get('name') or admin['admin_id']} ({admin['admin_id']})"[:60],
                callback_data=f"inbox:assign:{user_id}:{admin['admin_id']}",
            )]
            for admin in admins
        ]
    )


def priority_options(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Normal", callback_data=f"inbox:setpriority:{user_id}:normal")],
            [InlineKeyboardButton(text="High", callback_data=f"inbox:setpriority:{user_id}:high")],
            [InlineKeyboardButton(text="Urgent", callback_data=f"inbox:setpriority:{user_id}:urgent")],
        ]
    )


def setup_support_group_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Use this group", callback_data="setup:confirm")]
        ]
    )
