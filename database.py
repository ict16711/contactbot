from datetime import datetime, timedelta, timezone
import secrets

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, uri: str, database_name: str) -> None:
        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[database_name]
        self.users = self.db.users
        self.tickets = self.db.tickets
        self.admins = self.db.admins
        self.settings = self.db.settings
        self.broadcasts = self.db.broadcasts
        self.support_topics = self.db.support_topics
        self.support_messages = self.db.support_messages
        self.support_logs = self.db.support_logs

    async def initialize(self, bootstrap_admin_ids: list[int], bot_name: str) -> None:
        await self.users.create_index("user_id", unique=True)
        await self.tickets.create_index("ticket_id", unique=True)
        indexes = await self.tickets.index_information()
        active_index = indexes.get("user_id_1")
        if active_index and active_index.get("partialFilterExpression") != {
            "status": {"$in": ["open", "pending"]}
        }:
            await self.tickets.drop_index("user_id_1")
        await self.tickets.update_many(
            {"status": "in_progress"},
            {"$set": {"status": "pending", "awaiting_admin": False}},
        )
        await self.tickets.create_index(
            "user_id",
            unique=True,
            partialFilterExpression={"status": {"$in": ["open", "pending"]}},
        )
        await self.tickets.create_index([("created_at", -1)])
        await self.admins.create_index("admin_id", unique=True)
        await self.broadcasts.create_index([("created_at", -1)])
        await self.support_topics.create_index("user_id", unique=True)
        topic_index_name = "support_group_id_1_thread_id_1"
        topic_indexes = await self.support_topics.index_information()
        topic_index = topic_indexes.get(topic_index_name)
        partial_mapping = {"thread_id": {"$exists": True}}
        if topic_index and topic_index.get("partialFilterExpression") != partial_mapping:
            await self.support_topics.drop_index(topic_index_name)
        await self.support_topics.create_index(
            [("support_group_id", 1), ("thread_id", 1)],
            unique=True,
            partialFilterExpression=partial_mapping,
        )
        await self.support_topics.create_index([("status", 1), ("updated_at", -1)])
        await self.support_messages.create_index(
            [("source_chat_id", 1), ("source_message_id", 1)], unique=True
        )
        await self.support_messages.create_index(
            [("destination_chat_id", 1), ("destination_message_id", 1)]
        )
        await self.support_logs.create_index([("created_at", -1)])
        await self.settings.update_one(
            {"_id": "bot"},
            {
                "$setOnInsert": {
                    "bot_name": bot_name,
                    "welcome_text": f"Welcome to {bot_name}. Select Contact Admin when you are ready to send a message.",
                    "contact_label": "Contact Admin",
                    "quick_replies": [],
                }
            },
            upsert=True,
        )

        for admin_id in bootstrap_admin_ids:
            await self.admins.update_one(
                {"admin_id": admin_id},
                {
                    "$setOnInsert": {
                        "admin_id": admin_id,
                        "name": "Bootstrap admin",
                        "permissions": ["*"],
                        "created_at": utcnow(),
                    }
                },
                upsert=True,
            )

    async def close(self) -> None:
        self.client.close()

    async def save_user(self, user) -> dict:
        now = utcnow()
        values = {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "last_seen": now,
        }
        await self.users.update_one(
            {"user_id": user.id},
            {
                "$set": values,
                "$setOnInsert": {
                    "user_id": user.id,
                    "joined_at": now,
                    "status": "active",
                },
            },
            upsert=True,
        )
        return await self.users.find_one({"user_id": user.id})

    async def check_message_allowed(
        self,
        user_id: int,
        fingerprint: str,
        cooldown_seconds: int = 3,
        duplicate_seconds: int = 60,
    ) -> str:
        user = await self.users.find_one({"user_id": user_id})
        if not user or user.get("status") == "banned":
            return "banned"

        now = utcnow()
        last_message_at = user.get("last_message_at")
        if last_message_at and last_message_at.tzinfo is None:
            last_message_at = last_message_at.replace(tzinfo=timezone.utc)
        if last_message_at:
            elapsed = (now - last_message_at).total_seconds()
            if user.get("last_message_fingerprint") == fingerprint and elapsed < duplicate_seconds:
                return "duplicate"
            if elapsed < cooldown_seconds:
                return "cooldown"

        filter_query = {"user_id": user_id}
        if last_message_at:
            filter_query["last_message_at"] = user["last_message_at"]
        else:
            filter_query["last_message_at"] = {"$exists": False}
        result = await self.users.update_one(
            filter_query,
            {"$set": {"last_message_at": now, "last_message_fingerprint": fingerprint}},
        )
        return "accepted" if result.modified_count else "cooldown"

    async def release_failed_message_attempt(self, user_id: int, fingerprint: str) -> None:
        await self.users.update_one(
            {"user_id": user_id, "last_message_fingerprint": fingerprint},
            {"$unset": {"last_message_at": "", "last_message_fingerprint": ""}},
        )

    async def get_or_create_ticket(self, user_id: int) -> dict:
        ticket = await self.tickets.find_one(
            {"user_id": user_id, "status": {"$in": ["open", "pending"]}}
        )
        if ticket:
            return ticket

        ticket = {
            "ticket_id": f"T-{secrets.token_hex(4).upper()}",
            "user_id": user_id,
            "status": "open",
            "awaiting_admin": False,
            "query_count": 0,
            "response_count": 0,
            "history": [],
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }
        try:
            await self.tickets.insert_one(ticket)
        except DuplicateKeyError:
            ticket = await self.tickets.find_one(
                {"user_id": user_id, "status": {"$in": ["open", "pending"]}}
            )
        return ticket

    async def add_query(
        self,
        ticket_id: str,
        message_id: int,
        chat_id: int,
        text: str | None,
        content_type: str,
        file_id: str | None,
    ) -> bool:
        history_entry = {
            "direction": "user",
            "message_id": message_id,
            "chat_id": chat_id,
            "text": text,
            "content_type": content_type,
            "file_id": file_id,
            "created_at": utcnow(),
        }
        notify_result = await self.tickets.update_one(
            {"ticket_id": ticket_id, "awaiting_admin": {"$ne": True}},
            {
                "$push": {"history": history_entry},
                "$inc": {"query_count": 1},
                "$set": {"updated_at": utcnow(), "status": "open", "awaiting_admin": True},
            },
        )
        if notify_result.matched_count:
            return True

        await self.tickets.update_one(
            {"ticket_id": ticket_id},
            {
                "$push": {"history": history_entry},
                "$inc": {"query_count": 1},
                "$set": {"updated_at": utcnow(), "status": "open"},
            },
        )
        return False

    async def add_response(
        self,
        ticket_id: str,
        admin_id: int,
        source_chat_id: int | None,
        source_message_id: int | None,
        destination_chat_id: int,
        destination_message_id: int,
        text: str | None,
        content_type: str,
        file_id: str | None,
    ) -> dict | None:
        return await self.tickets.find_one_and_update(
            {"ticket_id": ticket_id},
            {
                "$push": {
                    "history": {
                        "direction": "admin",
                        "admin_id": admin_id,
                        "source_chat_id": source_chat_id,
                        "source_message_id": source_message_id,
                        "destination_chat_id": destination_chat_id,
                        "destination_message_id": destination_message_id,
                        "text": text,
                        "content_type": content_type,
                        "file_id": file_id,
                        "created_at": utcnow(),
                    }
                },
                "$inc": {"response_count": 1},
                "$set": {"updated_at": utcnow(), "status": "pending", "awaiting_admin": False},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def add_admin_delivery(self, ticket_id: str, admin_id: int, message_id: int) -> None:
        await self.tickets.update_one(
            {"ticket_id": ticket_id},
            {"$push": {"admin_messages": {"admin_id": admin_id, "message_id": message_id}}},
        )

    async def get_support_group_id(self) -> int | None:
        settings = await self.settings.find_one({"_id": "bot"}) or {}
        return settings.get("support_group_id")

    async def set_support_group(self, group_id: int, title: str) -> None:
        await self.settings.update_one(
            {"_id": "bot"},
            {"$set": {"support_group_id": group_id, "support_group_title": title}},
            upsert=True,
        )

    async def get_support_topic(self, user_id: int) -> dict | None:
        return await self.support_topics.find_one({"user_id": user_id})

    async def get_topic_by_thread(self, group_id: int, thread_id: int) -> dict | None:
        return await self.support_topics.find_one(
            {"support_group_id": group_id, "thread_id": thread_id}
        )

    async def claim_topic_creation(self, user_id: int, group_id: int, force: bool = False) -> bool:
        now = utcnow()
        query = {
            "user_id": user_id,
            "$or": [
                {"creation_lease_until": {"$exists": False}},
                {"creation_lease_until": {"$lte": now}},
            ],
        }
        if not force:
            query["$and"] = [
                {
                    "$or": [
                        {"thread_id": {"$exists": False}},
                        {"support_group_id": {"$ne": group_id}},
                    ]
                }
            ]
        try:
            await self.support_topics.find_one_and_update(
                query,
                {
                    "$set": {
                        "support_group_id": group_id,
                        "creation_lease_until": now + timedelta(minutes=2),
                    },
                    "$setOnInsert": {"user_id": user_id, "created_at": now},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return True
        except DuplicateKeyError:
            return False

    async def release_topic_creation(self, user_id: int) -> None:
        await self.support_topics.update_one(
            {"user_id": user_id},
            {"$unset": {"creation_lease_until": ""}},
        )

    async def save_support_topic(self, topic: dict) -> dict:
        try:
            await self.support_topics.update_one(
                {"user_id": topic["user_id"]},
                {
                    "$set": topic,
                    "$unset": {"creation_lease_until": ""},
                    "$setOnInsert": {"created_at": utcnow()},
                },
                upsert=True,
            )
        except DuplicateKeyError:
            pass
        return await self.get_support_topic(topic["user_id"])

    async def update_support_topic(
        self,
        user_id: int,
        values: dict,
        unset_fields: list[str] | None = None,
    ) -> dict | None:
        values["updated_at"] = utcnow()
        update = {"$set": values}
        if unset_fields:
            update["$unset"] = {field: "" for field in unset_fields}
        return await self.support_topics.find_one_and_update(
            {"user_id": user_id},
            update,
            return_document=ReturnDocument.AFTER,
        )

    async def note_topic_user_message(self, user_id: int) -> dict | None:
        return await self.support_topics.find_one_and_update(
            {"user_id": user_id},
            {
                "$set": {
                    "status": "new",
                    "awaiting_admin": True,
                    "last_user_message_at": utcnow(),
                    "updated_at": utcnow(),
                },
                "$inc": {"unread_count": 1},
                "$unset": {"archived_at": ""},
            },
            return_document=ReturnDocument.AFTER,
        )

    async def note_topic_admin_reply(self, user_id: int, admin_id: int) -> dict | None:
        return await self.support_topics.find_one_and_update(
            {"user_id": user_id},
            {
                "$set": {
                    "status": "waiting",
                    "awaiting_admin": False,
                    "unread_count": 0,
                    "last_admin_id": admin_id,
                    "last_admin_message_at": utcnow(),
                    "updated_at": utcnow(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def reserve_support_message(self, record: dict) -> bool:
        now = utcnow()
        record = {**record, "delivery_status": "processing", "processing_at": now}
        try:
            await self.support_messages.insert_one(record)
            return True
        except DuplicateKeyError:
            stale_before = now.timestamp() - 120
            result = await self.support_messages.update_one(
                {
                    "source_chat_id": record["source_chat_id"],
                    "source_message_id": record["source_message_id"],
                    "$or": [
                        {"delivery_status": "failed"},
                        {"processing_at": {"$lt": datetime.fromtimestamp(stale_before, timezone.utc)}},
                    ],
                },
                {"$set": {"delivery_status": "processing", "processing_at": now}},
            )
            return result.modified_count > 0

    async def finish_support_message(
        self,
        source_chat_id: int,
        source_message_id: int,
        destination_chat_id: int,
        destination_message_id: int,
        thread_id: int,
    ) -> None:
        await self.support_messages.update_one(
            {"source_chat_id": source_chat_id, "source_message_id": source_message_id},
            {
                "$set": {
                    "destination_chat_id": destination_chat_id,
                    "destination_message_id": destination_message_id,
                    "thread_id": thread_id,
                    "delivery_status": "delivered",
                    "delivered_at": utcnow(),
                }
            },
        )

    async def fail_support_message(self, source_chat_id: int, source_message_id: int, error: str) -> None:
        await self.support_messages.update_one(
            {"source_chat_id": source_chat_id, "source_message_id": source_message_id},
            {"$set": {"delivery_status": "failed", "last_error": error[:500]}},
        )

    async def get_reply_message(self, chat_id: int, message_id: int) -> dict | None:
        return await self.support_messages.find_one(
            {
                "delivery_status": "delivered",
                "$or": [
                    {"source_chat_id": chat_id, "source_message_id": message_id},
                    {"destination_chat_id": chat_id, "destination_message_id": message_id},
                ],
            }
        )

    async def log_support_event(self, event: str, **details) -> None:
        await self.support_logs.insert_one(
            {"event": event, "details": details, "created_at": utcnow()}
        )

    async def topics_for_reminder(self, before: datetime, limit: int = 100) -> list[dict]:
        return await self.support_topics.find(
            {
                "awaiting_admin": True,
                "muted": {"$ne": True},
                "last_user_message_at": {"$lte": before},
                "$or": [
                    {"last_reminded_at": {"$exists": False}},
                    {"last_reminded_at": {"$lte": before}},
                ],
            }
        ).limit(limit).to_list(length=limit)

    async def claim_topic_reminder(self, user_id: int, before: datetime, now: datetime) -> bool:
        topic = await self.support_topics.find_one_and_update(
            {
                "user_id": user_id,
                "awaiting_admin": True,
                "muted": {"$ne": True},
                "last_user_message_at": {"$lte": before},
                "$or": [
                    {"last_reminded_at": {"$exists": False}},
                    {"last_reminded_at": {"$lte": before}},
                ],
            },
            {"$set": {"last_reminded_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        return topic is not None

    async def topics_for_auto_close(self, before: datetime, limit: int = 100) -> list[dict]:
        return await self.support_topics.find(
            {"status": {"$ne": "resolved"}, "updated_at": {"$lte": before}, "archived_at": {"$exists": False}}
        ).limit(limit).to_list(length=limit)

    async def claim_topic_auto_close(self, user_id: int, before: datetime, now: datetime) -> dict | None:
        return await self.support_topics.find_one_and_update(
            {
                "user_id": user_id,
                "status": {"$ne": "resolved"},
                "updated_at": {"$lte": before},
                "archived_at": {"$exists": False},
            },
            {
                "$set": {
                    "status": "resolved",
                    "awaiting_admin": False,
                    "archived_at": now,
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def set_ticket_status(self, ticket_id: str, status: str) -> dict | None:
        if status not in {"open", "pending", "resolved"}:
            raise ValueError(f"Unsupported ticket status: {status}")
        return await self.tickets.find_one_and_update(
            {"ticket_id": ticket_id},
            {
                "$set": {
                    "status": status,
                    "awaiting_admin": status == "open",
                    "updated_at": utcnow(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def statistics(self) -> dict:
        ticket_counts = await self.tickets.aggregate(
            [
                {
                    "$group": {
                        "_id": None,
                        "tickets": {"$sum": 1},
                        "queries": {"$sum": "$query_count"},
                        "responses": {"$sum": "$response_count"},
                    }
                }
            ]
        ).to_list(length=1)
        statuses = await self.tickets.aggregate(
            [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
        ).to_list(length=None)
        return {
            "users": await self.users.count_documents({}),
            "active_users": await self.users.count_documents({"status": "active"}),
            "banned_users": await self.users.count_documents({"status": "banned"}),
            "broadcasts": await self.broadcasts.count_documents({}),
            "tickets": ticket_counts[0] if ticket_counts else {"tickets": 0, "queries": 0, "responses": 0},
            "statuses": {item["_id"]: item["count"] for item in statuses},
        }
