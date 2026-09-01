from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram import filters
from config import DATABASE_URI, DATABASE_NAME, ADMIN

client = AsyncIOMotorClient(DATABASE_URI)
db = client[DATABASE_NAME]

users_col = db.users
admins_col = db.admins
banned_col = db.banned
auto_replies_col = db.auto_replies
force_channels_col = db.force_channels
settings_col = db.settings


# ---------------- Users ----------------

async def add_user(user_id: int, first_name: str = "", username: str = ""):
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "first_name": first_name, "username": username}},
        upsert=True
    )

async def is_user_exist(user_id: int) -> bool:
    return await users_col.find_one({"user_id": user_id}) is not None

async def get_all_users():
    return users_col.find({})

async def total_users_count() -> int:
    return await users_col.count_documents({})


# ---------------- Banned ----------------

async def ban_user(user_id: int):
    await banned_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)

async def unban_user(user_id: int):
    await banned_col.delete_one({"user_id": user_id})

async def is_banned(user_id: int) -> bool:
    return await banned_col.find_one({"user_id": user_id}) is not None

async def get_banned_users():
    cursor = banned_col.find({})
    return [doc["user_id"] async for doc in cursor]

async def banned_users_count() -> int:
    return await banned_col.count_documents({})


# ---------------- Admins ----------------

async def add_admin(user_id: int):
    await admins_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)

async def remove_admin(user_id: int):
    await admins_col.delete_one({"user_id": user_id})

async def get_admins():
    cursor = admins_col.find({})
    return [doc["user_id"] async for doc in cursor]

async def admins_count() -> int:
    return (await admins_col.count_documents({})) + 1  # +1 for owner (ADMIN)

async def is_admin(user_id: int) -> bool:
    if user_id == ADMIN:
        return True
    return await admins_col.find_one({"user_id": user_id}) is not None


async def _admin_filter_func(_, __, message):
    if not message.from_user:
        return False
    return await is_admin(message.from_user.id)

# Dynamic replacement for filters.user(ADMIN) — checks ADMIN + DB admins.
admin_filter = filters.create(_admin_filter_func)


# ---------------- Automatic Replies ----------------

async def add_auto_reply(keyword: str, reply_text: str):
    await auto_replies_col.update_one(
        {"keyword": keyword.lower()},
        {"$set": {"keyword": keyword.lower(), "reply": reply_text}},
        upsert=True
    )

async def remove_auto_reply(keyword: str):
    await auto_replies_col.delete_one({"keyword": keyword.lower()})

async def get_auto_replies():
    cursor = auto_replies_col.find({})
    return [doc async for doc in cursor]

async def find_auto_reply(text: str):
    if not text:
        return None
    text_lower = text.lower()
    async for doc in auto_replies_col.find({}):
        if doc["keyword"] in text_lower:
            return doc["reply"]
    return None


# ---------------- Force Join Channels ----------------

async def add_force_channel(chat: str):
    await force_channels_col.update_one({"chat": chat}, {"$set": {"chat": chat}}, upsert=True)

async def remove_force_channel(chat: str):
    await force_channels_col.delete_one({"chat": chat})

async def get_force_channels():
    cursor = force_channels_col.find({})
    return [doc["chat"] async for doc in cursor]


# ---------------- Bot Settings (start message) ----------------

DEFAULT_START_MESSAGE = {
    "_id": "start_message",
    "media_type": None,   # "photo" or "video"
    "media_file_id": "https://files.catbox.moe/n69gs0.jpg",
    "text": (
        "💌Hello, {mention} Dear Subscriber, Welcome To Our Bot\n\n"
        "☆ Through This Bot You Can Contact Us...\n\n"
        "🔵You Ask Something, Promotion, etc.\n"
        "🟢You Will Get Updates.\n"
        "🔴You Can Request Movie Series & TV Show\n\n"
        "Join : https://t.me/Mrn_Officialx\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "😘Join - Share - Like >>> @Mrn_Officialx⚡"
    ),
    "buttons": []  # list of rows, each row is a list of {"text": ..., "url": ...}
}

async def get_start_message() -> dict:
    doc = await settings_col.find_one({"_id": "start_message"})
    if not doc:
        doc = DEFAULT_START_MESSAGE.copy()
        await settings_col.update_one({"_id": "start_message"}, {"$set": doc}, upsert=True)
    return doc

async def set_start_message_field(field: str, value):
    await settings_col.update_one({"_id": "start_message"}, {"$set": {field: value}}, upsert=True)
