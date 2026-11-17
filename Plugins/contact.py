import asyncio
import html
import time
from collections import deque
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserNotParticipant
from config import ADMIN, COOLDOWN_SECONDS
from database import (
    add_user, is_banned, get_admins, admin_filter, get_force_channels,
    find_auto_reply, get_start_message, get_chat_history, add_chat_turns,
    search_content, save_message_map, get_mapped_user
)
from ai import ask_gemini

# Constants
SENT_CONFIRM_DELETE_AFTER = 3  # seconds before "Message sent!" auto-deletes
FSUB_OK_TTL = 60               # seconds a "fully subscribed" result is cached per user
AI_FOOTER = "\n\n🤖 AI reply • your message was also forwarded to the admin."
FSUB_TEXT = "<blockquote><b><i>Please join our channel(s) first to use this bot.</i></b></blockquote>"

# RAM caches / state
_fsub_ok = {}            # {user_id: expiry_ts} users verified as subscribed to every force-join chat
_fsub_warned = set()     # chats we already warned the owner about
_chat_info = {}          # {stored_chat: (title, join_url)}
_last_bot_reply = {}     # {user_id: monotonic ts} cooldown for AI / indexed-content replies
_confirmed_groups = {}   # {media_group_id: ts} so an album gets one "Message sent!" only
_flood = {}              # {user_id: deque of timestamps} per-user flood guard
_error_notice_at = {}    # {user_id: ts} throttles the "could not be sent" reply

FLOOD_MAX = 15           # more than this many messages ...
FLOOD_WINDOW = 10        # ... within this many seconds => ignored (stops runaway loops)


# ---------------- Force join ----------------

def to_chat_ref(chat):
    """Converts a stored/typed chat value into something Pyrogram resolves correctly
    (numeric IDs must be int — a numeric *string* is treated as a username)."""
    chat = str(chat).strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if chat.lower().startswith(prefix):
            rest = chat[len(prefix):].split("?")[0].strip("/")
            if rest.startswith(("+", "joinchat")) or "/" in rest:
                return chat  # invite link — leave as is
            return "@" + rest
    if chat.lstrip("-").isdigit():
        return int(chat)
    return chat


async def _warn_fsub_problem(client: Client, chat, error):
    print(f"[fsub] can't verify membership in {chat}: {error}")
    if chat in _fsub_warned:
        return
    _fsub_warned.add(chat)
    try:
        await client.send_message(
            ADMIN,
            f"⚠️ <b>Force-join problem</b>\n"
            f"Can't check membership for <code>{html.escape(str(chat))}</code>.\n"
            f"Make sure the bot is an <b>admin</b> there. Until fixed this chat is NOT enforced.\n\n"
            f"<code>{html.escape(str(error))}</code>",
            parse_mode=enums.ParseMode.HTML
        )
    except Exception:
        pass


async def get_not_joined_channels(client: Client, user_id: int, use_cache: bool = True) -> list:
    """Returns list of force-join channels the user hasn't joined yet."""
    if use_cache and _fsub_ok.get(user_id, 0) > time.time():
        return []

    not_joined = []
    for chat in await get_force_channels():
        try:
            member = await client.get_chat_member(to_chat_ref(chat), user_id)
            left = member.status in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED)
            restricted_out = (member.status == enums.ChatMemberStatus.RESTRICTED
                              and getattr(member, "is_member", True) is False)
            if left or restricted_out:
                not_joined.append(chat)
        except UserNotParticipant:
            not_joined.append(chat)
        except Exception as e:
            await _warn_fsub_problem(client, chat, e)  # misconfigured chat: tell the owner, don't block users
            continue

    if not not_joined:
        _fsub_ok[user_id] = time.time() + FSUB_OK_TTL
        if len(_fsub_ok) > 10000:
            now = time.time()
            for uid in [u for u, t in _fsub_ok.items() if t < now]:
                del _fsub_ok[uid]
    return not_joined


async def is_subscribed(client: Client, user_id: int, use_cache: bool = True) -> bool:
    return len(await get_not_joined_channels(client, user_id, use_cache)) == 0


async def _chat_button(client: Client, chat):
    """Join button for one force-join chat (works for usernames, numeric IDs and private channels)."""
    if chat not in _chat_info:
        ref = to_chat_ref(chat)
        title, url = str(chat).lstrip("@"), None
        try:
            info = await client.get_chat(ref)
            title = info.title or title
            if info.username:
                url = f"https://t.me/{info.username}"
            else:
                url = info.invite_link or await client.export_chat_invite_link(info.id)
        except Exception as e:
            print(f"[fsub] couldn't build join link for {chat}: {e}")
            if isinstance(ref, str) and ref.startswith("@"):
                url = f"https://t.me/{ref[1:]}"
        if not url:
            return None
        _chat_info[chat] = (title, url)
    title, url = _chat_info[chat]
    return InlineKeyboardButton(f"📢 Join {title}", url=url)


async def force_sub_markup(client: Client, user_id: int, not_joined: list = None) -> InlineKeyboardMarkup:
    if not_joined is None:
        not_joined = await get_not_joined_channels(client, user_id, use_cache=False)
    buttons = []
    for chat in not_joined:
        button = await _chat_button(client, chat)
        if button:
            buttons.append([button])
    buttons.append([InlineKeyboardButton("🔄 I've Joined", callback_data="check_fsub")])
    return InlineKeyboardMarkup(buttons)


async def fsub_gate(client: Client, message: Message) -> bool:
    """Returns True (after replying with join buttons) if the user still has to join something."""
    user_id = message.from_user.id
    not_joined = await get_not_joined_channels(client, user_id)  # one check, reused for the buttons
    if not not_joined:
        return False
    await message.reply(
        FSUB_TEXT,
        parse_mode=enums.ParseMode.HTML,
        reply_markup=await force_sub_markup(client, user_id, not_joined)
    )
    return True


# ---------------- Helpers ----------------

def flood_ok(user_id: int) -> bool:
    """Loop breaker: a real person never sends 15+ messages in 10s, a feedback loop always does."""
    now = time.monotonic()
    q = _flood.setdefault(user_id, deque())
    while q and now - q[0] > FLOOD_WINDOW:
        q.popleft()
    if len(q) >= FLOOD_MAX:
        if len(q) == FLOOD_MAX:  # log once per burst
            print(f"[flood] ignoring user {user_id}: more than {FLOOD_MAX} messages in {FLOOD_WINDOW}s")
            q.append(now)
        return False
    q.append(now)
    if len(_flood) > 5000:
        for uid in [u for u, d in _flood.items() if not d or now - d[-1] > FLOOD_WINDOW]:
            del _flood[uid]
    return True


def _cooldown_ok(user_id: int) -> bool:
    """Rate-limits bot-generated replies (AI / indexed content). Forwarding to admin is never limited."""
    now = time.monotonic()
    if now - _last_bot_reply.get(user_id, -1e9) < COOLDOWN_SECONDS:
        return False
    _last_bot_reply[user_id] = now
    if len(_last_bot_reply) > 5000:
        for uid in [u for u, t in _last_bot_reply.items() if now - t > 60]:
            del _last_bot_reply[uid]
    return True


def _first_in_group(message: Message) -> bool:
    gid = message.media_group_id
    if not gid:
        return True
    if gid in _confirmed_groups:
        return False
    _confirmed_groups[gid] = time.monotonic()
    if len(_confirmed_groups) > 500:
        now = time.monotonic()
        for g in [g for g, t in _confirmed_groups.items() if now - t > 120]:
            del _confirmed_groups[g]
    return True


async def _send_indexed(client: Client, message: Message, match: dict) -> bool:
    """Sends an indexed post back to the user. Returns False if it couldn't be sent."""
    try:
        if match.get("has_media"):
            # No caption override: keeps the original caption with its formatting and links.
            await client.copy_message(message.chat.id, match["chat_id"], match["message_id"])
        else:
            text = match.get("html") or match.get("text") or ""
            try:
                await message.reply(text, parse_mode=enums.ParseMode.HTML)
            except Exception:
                await message.reply(match.get("text") or "", parse_mode=enums.ParseMode.DISABLED)
        return True
    except Exception as e:
        print(f"[index] couldn't send indexed post: {e}")  # source post may be deleted/inaccessible
        return False


# ---------------- Main flow ----------------

@Client.on_message(filters.private & ~admin_filter & ~filters.command("start"))
async def forward_to_admin(client: Client, message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id

    # Never process the bot's own messages (or other bots'), nor the bot's own messages bounced back
    # as forwards — that is how a forward loop feeds itself.
    me = getattr(client, "me", None)
    if message.from_user.is_bot or (me and user_id == me.id):
        return
    fwd_from = getattr(message, "forward_from", None)
    if me and fwd_from and fwd_from.id == me.id:
        return

    if await is_banned(user_id):
        return

    if not flood_ok(user_id):
        return

    if await fsub_gate(client, message):
        return

    await add_user(user_id, message.from_user.first_name or "", message.from_user.username or "")

    # Automatic keyword reply — replaces forwarding when a keyword matches.
    if message.text:
        auto_reply = await find_auto_reply(message.text)
        if auto_reply:
            return await message.reply(auto_reply, parse_mode=enums.ParseMode.HTML)

    admin_ids = set(await get_admins())
    admin_ids.add(ADMIN)

    sent_to_any = False
    for admin_id in admin_ids:
        try:
            fwd = await message.forward(admin_id)
        except Exception:
            continue
        sent_to_any = True
        try:
            await save_message_map(admin_id, fwd.id, user_id)
        except Exception as e:
            print(f"[map] couldn't save reply mapping: {e}")

    if not sent_to_any:
        now = time.monotonic()
        if now - _error_notice_at.get(user_id, -1e9) > 30:  # don't spam the error itself
            _error_notice_at[user_id] = now
            await message.reply("❌ **Error:** Message could not be sent.")
        return

    # Bot-generated replies (indexed content, then AI) — plain text only, and rate-limited
    # per user. The admin has already received the forward above either way.
    if message.text and _cooldown_ok(user_id):
        # Indexed content match — if the user's text matches something scanned
        # from a registered channel (see Plugins/indexer.py), send that directly.
        try:
            matches = await search_content(message.text, limit=1)
        except Exception as e:
            print(f"[index] search failed: {e}")
            matches = []
        if matches and await _send_indexed(client, message, matches[0]):
            return

        # AI assistant reply — only when GEMINI_API_KEY is configured (see config.py).
        history = await get_chat_history(user_id)
        ai_reply = await ask_gemini(history, message.text)
        if ai_reply:
            try:
                # Parse mode off: model output often contains stray *, _ or < that break Telegram parsing.
                await message.reply(ai_reply[:3800] + AI_FOOTER, parse_mode=enums.ParseMode.DISABLED)
                await add_chat_turns(user_id, message.text, ai_reply)
                return
            except Exception as e:
                print(f"[ai] couldn't send AI reply: {e}")

    if not _first_in_group(message):
        return
    sent_msg = await message.reply(
        "✅ <i>Message sent!</i>",
        parse_mode=enums.ParseMode.HTML
    )
    asyncio.create_task(_delete_after_delay(sent_msg))


async def _delete_after_delay(msg: Message):
    await asyncio.sleep(SENT_CONFIRM_DELETE_AFTER)
    try:
        await msg.delete()
    except Exception:
        pass


# Admin commands are excluded so e.g. "/settings" sent as a reply isn't copied to the user.
@Client.on_message(
    filters.private & admin_filter & filters.reply
    & ~filters.command(["start", "settings", "menu", "restart", "clearindex"])
)
async def reply_to_user(client: Client, message: Message):
    replied_msg = message.reply_to_message
    if not replied_msg:
        return await message.reply("❌ **User ID not detected.**")
    user_id = await get_mapped_user(message.chat.id, replied_msg.id)
    if not user_id and replied_msg.forward_from:
        user_id = replied_msg.forward_from.id
    if not user_id:
        return await message.reply("❌ **User ID not detected.**")
    try:
        await message.copy(user_id)
    except Exception as e:
        await message.reply(f"⚠️ **Error sending to user:**\n`{e}`")


@Client.on_callback_query(filters.regex("^check_fsub$"))
async def check_fsub_cb(client: Client, query):
    user_id = query.from_user.id
    if await is_subscribed(client, user_id, use_cache=False):
        await query.message.delete()
        await query.answer("✅ Thanks for joining! You can use the bot now.", show_alert=True)
    else:
        await query.answer("❌ You haven't joined all channels yet.", show_alert=True)


def build_buttons_markup(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], url=b["url"]) for b in row] for row in rows])


async def send_start_message(client: Client, chat_id: int, mention: str):
    """Sends the admin-configurable /start message (used by /start and settings preview).
    Falls back to text-only if the media or buttons are rejected, so /start never goes silent."""
    doc = await get_start_message()
    text = doc.get("text", "").replace("{mention}", mention)
    markup = build_buttons_markup(doc.get("buttons") or [])
    media_type = doc.get("media_type")
    media_file_id = doc.get("media_file_id")
    try:
        if media_type == "photo" and media_file_id:
            await client.send_photo(chat_id, media_file_id, caption=text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        elif media_type == "video" and media_file_id:
            await client.send_video(chat_id, media_file_id, caption=text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        else:
            await client.send_message(chat_id, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
        return
    except Exception as e:
        print(f"[start] full start message failed ({e}); falling back to text only")
    try:
        await client.send_message(chat_id, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        print(f"[start] text+buttons failed ({e}); sending without buttons")
        await client.send_message(chat_id, text, parse_mode=enums.ParseMode.HTML)
