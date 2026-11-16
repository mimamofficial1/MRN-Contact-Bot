import re
from datetime import datetime, timezone
from math import ceil

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne
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
chat_history_col = db.chat_history
content_col = db.content_index
indexed_channels_col = db.indexed_channels
message_map_col = db.message_map


# ---------------- Indexes ----------------

async def _make_index(col, keys, **kwargs):
    try:
        await col.create_index(keys, **kwargs)
    except Exception as e:
        if kwargs.get("unique"):
            # Existing duplicate data can block a unique index — fall back to a plain one.
            plain = {k: v for k, v in kwargs.items() if k != "unique"}
            try:
                await col.create_index(keys, **plain)
                print(f"[db] unique index failed on {col.name} ({e}); created non-unique instead")
            except Exception as e2:
                print(f"[db] index failed on {col.name}: {e2}")
        else:
            print(f"[db] index failed on {col.name}: {e}")


async def ensure_indexes():
    """Creates all indexes (safe to call on every startup)."""
    await _make_index(users_col, "user_id", unique=True)
    await _make_index(banned_col, "user_id", unique=True)
    await _make_index(admins_col, "user_id", unique=True)
    await _make_index(chat_history_col, "user_id", unique=True)
    await _make_index(chat_history_col, "updated_at", expireAfterSeconds=7 * 24 * 3600)  # AI memory expires after 7 days
    await _make_index(auto_replies_col, "keyword", unique=True)
    await _make_index(force_channels_col, "chat", unique=True)
    await _make_index(indexed_channels_col, "chat_id", unique=True)
    await _make_index(content_col, [("chat_id", 1), ("message_id", 1)], unique=True)
    await _make_index(message_map_col, [("admin_id", 1), ("msg_id", 1)], unique=True)
    await _make_index(message_map_col, "created_at", expireAfterSeconds=30 * 24 * 3600)
    await ensure_content_text_index()


# ---------------- Users ----------------

async def add_user(user_id: int, first_name: str = "", username: str = ""):
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "first_name": first_name, "username": username}},
        upsert=True
    )

async def remove_user(user_id: int):
    await users_col.delete_one({"user_id": user_id})

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

# Admin IDs are cached in RAM: admin_filter runs on every incoming message in
# several handlers, so hitting Mongo each time was wasteful.
_admin_ids = None

async def _admin_set() -> set:
    global _admin_ids
    if _admin_ids is None:
        _admin_ids = {doc["user_id"] async for doc in admins_col.find({})}
    return _admin_ids

async def add_admin(user_id: int):
    if user_id == ADMIN:
        return  # owner is always admin
    await admins_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)
    (await _admin_set()).add(user_id)

async def remove_admin(user_id: int):
    await admins_col.delete_one({"user_id": user_id})
    (await _admin_set()).discard(user_id)

async def get_admins():
    global _admin_ids
    _admin_ids = {doc["user_id"] async for doc in admins_col.find({})}
    return list(_admin_ids)

async def admins_count() -> int:
    return (await admins_col.count_documents({"user_id": {"$ne": ADMIN}})) + 1  # +1 for owner (ADMIN)

async def is_admin(user_id: int) -> bool:
    if user_id == ADMIN:
        return True
    return user_id in await _admin_set()


async def _admin_filter_func(_, __, message):
    if not message.from_user:
        return False
    return await is_admin(message.from_user.id)

# Dynamic replacement for filters.user(ADMIN) — checks ADMIN + DB admins.
admin_filter = filters.create(_admin_filter_func)


# ---------------- Automatic Replies ----------------

# Word-boundary characters (Devanagari matras aren't matched by \w, so add the block).
_WORD = r"[\w\u0900-\u097F]"

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
    """Whole-word/phrase match, so keyword 'hi' no longer fires on 'this' or 'which'."""
    if not text:
        return None
    text_lower = text.lower()
    async for doc in auto_replies_col.find({}):
        keyword = doc["keyword"].strip()
        if not keyword:
            continue
        if re.search(rf"(?<!{_WORD}){re.escape(keyword)}(?!{_WORD})", text_lower):
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
    "media_type": "photo",   # "photo", "video" or None (text only)
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
    # Missing fields fall back to defaults (e.g. admin set only media before anyone ran /start).
    doc = await settings_col.find_one({"_id": "start_message"}) or {}
    return {**DEFAULT_START_MESSAGE, **doc}

async def set_start_message_field(field: str, value):
    await settings_col.update_one({"_id": "start_message"}, {"$set": {field: value}}, upsert=True)


# ---------------- AI Chat History ----------------

MAX_HISTORY_TURNS = 8  # number of user+model exchange pairs kept per user
MAX_TURN_CHARS = 1500  # keeps stored history (and Gemini prompts) small

async def get_chat_history(user_id: int) -> list:
    doc = await chat_history_col.find_one({"user_id": user_id})
    return doc["turns"] if doc else []

async def add_chat_turns(user_id: int, user_text: str, model_text: str):
    turns = await get_chat_history(user_id)
    turns.append({"role": "user", "text": user_text[:MAX_TURN_CHARS]})
    turns.append({"role": "model", "text": model_text[:MAX_TURN_CHARS]})
    turns = turns[-(MAX_HISTORY_TURNS * 2):]
    await chat_history_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "turns": turns, "updated_at": datetime.now(timezone.utc)}},
        upsert=True
    )


# ---------------- Admin-side reply mapping ----------------
# Maps "message forwarded to admin X" -> original user, so replies keep working
# after restarts and for users who hide their forward info. Auto-expires (30 days).

async def save_message_map(admin_id: int, msg_id: int, user_id: int):
    await message_map_col.update_one(
        {"admin_id": admin_id, "msg_id": msg_id},
        {"$set": {"user_id": user_id, "created_at": datetime.now(timezone.utc)}},
        upsert=True
    )

async def get_mapped_user(admin_id: int, msg_id: int):
    doc = await message_map_col.find_one({"admin_id": admin_id, "msg_id": msg_id})
    return doc["user_id"] if doc else None


# ---------------- Content Index (scanned channel content, searchable) ----------------

_content_index_ready = False

async def ensure_content_text_index():
    """Creates the Mongo text index once (safe to call repeatedly — no-ops after the first time)."""
    global _content_index_ready
    if _content_index_ready:
        return
    await content_col.create_index([("text", "text")])
    _content_index_ready = True

async def add_indexed_channel(chat_id: int):
    await indexed_channels_col.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id}}, upsert=True)

async def is_indexed_channel(chat_id: int) -> bool:
    return await indexed_channels_col.find_one({"chat_id": chat_id}) is not None

def _content_update(chat_id: int, message_id: int, text: str, has_media: bool, html: str):
    return UpdateOne(
        {"chat_id": chat_id, "message_id": message_id},
        {"$set": {"chat_id": chat_id, "message_id": message_id, "text": text,
                  "html": html or text, "has_media": has_media}},
        upsert=True
    )

async def add_content_item(chat_id: int, message_id: int, text: str, has_media: bool = False, html: str = ""):
    """Stores one scanned post so it can be searched and re-sent later."""
    if not text:
        return
    await ensure_content_text_index()
    await content_col.bulk_write([_content_update(chat_id, message_id, text, has_media, html)])

async def add_content_items(chat_id: int, items: list):
    """Bulk version for indexing runs. items: list of (message_id, text, has_media, html)."""
    ops = [_content_update(chat_id, mid, text, has_media, html) for mid, text, has_media, html in items if text]
    if ops:
        await ensure_content_text_index()
        await content_col.bulk_write(ops, ordered=False)

async def remove_content_item(chat_id: int, message_id: int):
    await content_col.delete_one({"chat_id": chat_id, "message_id": message_id})

async def content_count() -> int:
    return await content_col.count_documents({})

async def clear_content_index():
    """Wipes all scanned content (registered channels stay registered, so auto-index of new posts keeps working)."""
    await content_col.delete_many({})


# Chatty / generic words that must not trigger an indexed-content reply on their own
# (otherwise "hi", "movie link do" etc. would return a random post).
STOPWORDS = {
    # english
    "the", "and", "for", "you", "are", "was", "can", "could", "would", "please", "pls", "plz", "hello",
    "hey", "thanks", "thank", "okay", "this", "that", "with", "have", "has", "his", "her", "its", "our",
    "your", "from", "about", "any", "all", "get", "got", "give", "send", "want", "need", "how", "what",
    "when", "where", "who", "why", "which", "there", "here", "some", "new", "latest", "full", "free",
    "movie", "movies", "film", "films", "series", "show", "shows", "web", "episode", "episodes", "season",
    "link", "links", "download", "watch", "online", "hindi", "english", "dubbed", "quality", "available",
    # hinglish / hindi
    "bhai", "bro", "sir", "yaar", "kya", "hai", "hain", "kab", "kaise", "kaha", "kahan", "kyu", "kyun",
    "mujhe", "mereko", "muje", "dedo", "dijiye", "chahiye", "bhejo", "bhej", "milega", "mil", "nahi",
    "nhi", "aur", "ka", "ki", "ke", "ko", "se", "me", "mein", "par", "pe", "wala", "wali", "wale",
    "kar", "karo", "karna", "ho", "hoga", "hogi", "abhi", "plzz", "pliz",
}
_TOKEN_RE = re.compile(r"[\w\u0900-\u097F]+")

def _query_tokens(query: str) -> list:
    tokens = []
    for t in _TOKEN_RE.findall(query.lower()):
        if len(t) < 3 or t in STOPWORDS or t in tokens:
            continue
        tokens.append(t)
    return tokens[:8]

def _token_in_text(token: str, text_lower: str) -> bool:
    if token in text_lower:
        return True
    # light plural tolerance (the Mongo text index stems words too)
    return len(token) > 3 and token.endswith("s") and token[:-1] in text_lower

def _covers_query(tokens: list, text: str) -> bool:
    """Short queries must match fully, longer ones at least 75% of their meaningful words."""
    text_lower = text.lower()
    hits = sum(1 for t in tokens if _token_in_text(t, text_lower))
    need = len(tokens) if len(tokens) <= 2 else ceil(len(tokens) * 0.75)
    return hits >= need

async def search_content(query: str, limit: int = 1) -> list:
    """
    Full-text search over indexed content. Returns up to `limit` docs, best first.
    Mongo's $text is OR-based and has no relevance cutoff, so candidates are
    re-checked here: only posts that actually cover the user's meaningful words
    are returned (generic chat like "hi" / "movie link do" returns nothing).
    """
    if not query:
        return []
    tokens = _query_tokens(query)
    if not tokens:
        return []
    await ensure_content_text_index()
    cursor = content_col.find(
        {"$text": {"$search": " ".join(tokens)}},
        {"score": {"$meta": "textScore"}, "chat_id": 1, "message_id": 1, "text": 1, "html": 1, "has_media": 1}
    ).sort([("score", {"$meta": "textScore"})]).limit(max(limit * 10, 10))
    results = []
    async for doc in cursor:
        if _covers_query(tokens, doc.get("text", "")):
            results.append(doc)
            if len(results) >= limit:
                break
    return results
