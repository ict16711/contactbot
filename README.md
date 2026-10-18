# Contact Bot

Telegram support bot with a MongoDB-backed supergroup inbox. Each user has a persistent forum topic; messages and replies are mirrored between the private chat and that topic. Topic mappings and message IDs are stored in MongoDB so deleted topics can be recreated and duplicate Telegram updates safely ignored.

## Requirements

- Python 3.10 or newer
- MongoDB
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A supergroup with Topics enabled

## Run

1. Copy `.env.example` to `.env` and set `BOT_TOKEN` and your Telegram numeric ID in `ADMIN_IDS` (comma-separated for multiple bootstrap owners).
2. Install dependencies with `python -m pip install -r requirements.txt`.
3. Start MongoDB, then run `python bot.py`.
4. Add the bot to the support supergroup as an administrator with Manage Topics permission. Enable Topics if needed.
5. An authorized owner runs `/setup` in the supergroup.
6. Open the bot in Telegram and send `/start`, then select Contact Admin.

Bootstrap admins have all permissions and can manage other admins using `/admins add TELEGRAM_ID [reply,tickets,users,broadcast,stats]` and `/admins remove TELEGRAM_ID`. Authorized admins must also be members of the support supergroup to reply in topics. As a group administrator, the bot receives topic messages regardless of privacy mode.

## User flow

Users select **Contact Admin** and can send text, photos, videos, PDFs, documents, stickers, and voice notes. A three-second cooldown and duplicate detection reduce spam; banned users are blocked. The first contact creates a forum topic named with the user's username or display name and Telegram ID. Later messages reuse that topic; deleted topics are recreated automatically. Admin replies in the topic are delivered privately, with reply-to links preserved when the referenced message is mapped. Bot-generated status cards and reminders are never forwarded to users.

Topic buttons let authorized admins claim, transfer, prioritize, mute reminders, close, or reopen conversations. Topic status, assigned admin, last responder, unread count, and priority are reflected in MongoDB and the topic control card. Statuses are New, In Progress, Waiting, and Resolved. Unanswered topics receive periodic reminders unless muted. Set `AUTO_CLOSE_AFTER_SECONDS` to a positive number to close inactive topics; zero disables auto-close. `REMINDER_INTERVAL_SECONDS` defaults to one hour.

Edit bot response copy, support-flow captions, message/media type detection, duplicate fingerprints, payload extraction, and status-card text in `message.py`. `bot.py` keeps the routing and command logic; `keyboards.py` keeps button labels and layouts.

## Admin commands

- `/admin` opens the dashboard.
- `/reply TICKET_ID [text]` replies to a ticket. Without text, send the next message or media as the reply.
- `/broadcast` starts a broadcast draft; send the content and confirm the preview to deliver it to active users.
- `/users [page]` lists registered users.
- `/search [query]` searches by Telegram ID, username, or ticket ID.
- `/ban TELEGRAM_ID` and `/unban TELEGRAM_ID` manage user access.
- `/stats` reports users, tickets, queries, replies, and broadcasts.
- `/admins [list|add TELEGRAM_ID [permissions]|remove TELEGRAM_ID]` manages admins.
- `/quickreplies [list|add TEXT|remove NUMBER]` manages reusable responses, shown from a ticket's Quick reply button.

Permission names are `reply`, `tickets`, `users`, `broadcast`, `stats`, and `admin_management`. Broadcasts require confirmation and report delivered/failed counts. MongoDB collections `support_topics`, `support_messages`, and `support_logs` store topic mappings, mirrored Telegram message IDs/media metadata, and operational events. Set `MONGODB_URI`, `MONGODB_DATABASE`, `BOT_NAME`, and optional `SUPPORT_GROUP_ID` in `.env` to customize the deployment. Keep the bot worker running continuously for routing and reminders.