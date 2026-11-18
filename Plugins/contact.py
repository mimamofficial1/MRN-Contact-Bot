import asyncio
import html
import os
import time
from collections import deque
from pyrogram import Client, filters, enums, raw
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserNotParticipant, UserIsBlocked, InputUserDeactivated, RPCError
from config import ADMIN, COOLDOWN_SECONDS
from database import (
    add_user, is_banned, get_admins, admin_filter, get_force_channels,
    find_auto_reply, get_start_message, get_chat_history, add_chat_turns,
    search_content, save_message_map, get_mapped_user, remove_content_item
)
from ai import ask_gemini, is_enabled, status as ai_status
from Plugins.indexer import LINK_ONLY_REGEX

# Constants
SENT_CONFIRM_DELETE_AFTER = 3  # seconds before "Message sent!" auto-deletes
CONFIRM_AFTER = 1.5            # if the AI/index answer isn't ready by then, "Message sent!" is shown at once
FSUB_OK_TTL = 60               # seconds a "fully subscribed" result is cached per user
AI_FOOTER = "\n\n🤖 AI reply • your message was also forwarded to the admin."
REACT_OK = "👍"      # shown on the admin's reply once it reached the user
REACT_FAIL = "🥴"    # shown on the admin's reply when it could not be delivered
REPLY_HINT = "⚠️ <i>Reply to a forwarded message to send a message to that user.</i>"
NOT_SENT_TEXT = "❌ Message <b>not</b> sent!\n<i>The user blocked the bot or deleted the account.</i>"
# Admin commands: never copied to a user when sent as a reply, never answered with the reply hint.
ADMIN_COMMANDS = ["start", "settings", "menu", "restart", "clearindex",
                  "setemoji", "emojion", "emojioff", "emojibtn", "emojipack", "delemoji", "emoji", "emojilist", "aitest"]
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


def _group_once(kind: str, message: Message) -> bool:
    """True for a normal message and for the FIRST item of an album (per `kind`), so an album of 10
    photos produces one confirmation / one info message / one hint, not ten."""
    gid = message.media_group_id
    if not gid:
        return True
    key = (kind, gid)
    if key in _confirmed_groups:
        return False
    _confirmed_groups[key] = time.monotonic()
    if len(_confirmed_groups) > 500:
        now = time.monotonic()
        for k in [k for k, t in _confirmed_groups.items() if now - t > 120]:
            del _confirmed_groups[k]
    return True


def _first_in_group(message: Message) -> bool:
    return _group_once("confirm", message)


async def _react(client: Client, message: Message, emoji: str):
    """Reaction on a message (never breaks the flow if Telegram refuses it)."""
    try:
        await client.send_reaction(chat_id=message.chat.id, message_id=message.id, emoji=emoji)
    except Exception as e:
        print(f"[react] couldn't react with {emoji}: {e}")


def _user_info(user) -> tuple:
    """Admin-side info message under every forwarded user message: who sent it + a profile button."""
    name = " ".join(x for x in (user.first_name, getattr(user, "last_name", None)) if x) or "User"
    text = (
        f'👆 Message sent by <a href="tg://user?id={user.id}">{html.escape(name)}</a> '
        f'[<code>{user.id}</code>] #id{user.id}\n'
        f'👉 <i>To answer, reply to this message.</i>'
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("👤 User profile", url=f"tg://user?id={user.id}")]])
    return text, markup


def _rnd_id() -> int:
    return int.from_bytes(os.urandom(8), "big", signed=True)


async def _forward_exact(client: Client, to_chat_id: int, from_chat_id, message_id: int, reply_to_id: int = None):
    """Server-side copy WITHOUT the "Forwarded from" header (photo/video/file posts): media, caption,
    formatting, premium emoji and buttons come over exactly as in the channel."""
    extra = {}
    if reply_to_id:
        extra["reply_to"] = raw.types.InputReplyToMessage(reply_to_msg_id=reply_to_id)
    await client.invoke(raw.functions.messages.ForwardMessages(
        from_peer=await client.resolve_peer(from_chat_id),
        to_peer=await client.resolve_peer(to_chat_id),
        id=[message_id],
        random_id=[_rnd_id()],
        drop_author=True,
        **extra
    ))


async def _get_raw_post(client: Client, chat_id, message_id: int):
    """The channel post exactly as Telegram stores it (text, entities, link-preview settings, buttons).
    Returns None if the post no longer exists. Raises if it can't be read."""
    peer = await client.resolve_peer(chat_id)
    ref = [raw.types.InputMessageID(id=message_id)]
    if isinstance(peer, raw.types.InputPeerChannel):
        res = await client.invoke(raw.functions.channels.GetMessages(
            channel=raw.types.InputChannel(channel_id=peer.channel_id, access_hash=peer.access_hash), id=ref))
    else:
        res = await client.invoke(raw.functions.messages.GetMessages(id=ref))
    post = (getattr(res, "messages", None) or [None])[0]
    return post if isinstance(post, raw.types.Message) else None


async def _send_post_copy(client: Client, to_chat_id: int, post, reply_to_id: int = None):
    """Re-sends a text post with EVERYTHING the channel post has: the same entities (premium emoji, hidden
    links, bold...), the same link preview (its own URL, large/small, above/below the text) and the same buttons.
    A plain re-send can't do that: Telegram would build the preview from the first link in the text instead."""
    media = post.media
    web = media.webpage if isinstance(media, raw.types.MessageMediaWebPage) else None
    peer = await client.resolve_peer(to_chat_id)
    reply_to = raw.types.InputReplyToMessage(reply_to_msg_id=reply_to_id) if reply_to_id else None

    async def send(entities):
        common = dict(
            peer=peer, message=post.message or "", random_id=_rnd_id(), entities=entities or None,
            reply_markup=post.reply_markup, reply_to=reply_to, invert_media=True if post.invert_media else None,
        )
        if isinstance(web, raw.types.WebPage) and web.url:        # the post shows a specific preview
            return await client.invoke(raw.functions.messages.SendMedia(
                media=raw.types.InputMediaWebPage(
                    url=web.url,
                    force_large_media=True if media.force_large_media else None,
                    force_small_media=True if media.force_small_media else None,
                ), **common))
        # no preview on the original post (media is None) -> make sure Telegram doesn't invent one
        return await client.invoke(raw.functions.messages.SendMessage(
            no_webpage=True if media is None else None, **common))

    try:
        return await send(post.entities)
    except RPCError as e:
        if not any(isinstance(x, raw.types.MessageEntityCustomEmoji) for x in (post.entities or [])):
            raise
        # this bot can't use the post's premium emoji (bot owner without Premium?) -> same post, normal emoji
        print(f"[index] premium emoji refused ({type(e).__name__}); sending with normal emoji")
        plain = [x for x in (post.entities or []) if not isinstance(x, raw.types.MessageEntityCustomEmoji)]
        return await send(plain)


async def _send_indexed(client: Client, message: Message, match: dict) -> bool:
    """Sends an indexed post back to the user exactly as it is in the channel. Returns False if it couldn't be sent."""
    chat_id, post_id = match["chat_id"], match["message_id"]
    post = None
    try:
        post = await _get_raw_post(client, chat_id, post_id)
        if post is None:  # deleted from the channel -> never serve the stale copy, drop it from the index
            await remove_content_item(chat_id, post_id)
            return False
    except Exception as e:
        print(f"[index] couldn't read the original post ({type(e).__name__}: {e})")

    try:
        if post is not None and (post.media is None or isinstance(post.media, raw.types.MessageMediaWebPage)):
            await _send_post_copy(client, message.chat.id, post, reply_to_id=message.id)   # text post (+ its preview)
        else:
            await _forward_exact(client, message.chat.id, chat_id, post_id, reply_to_id=message.id)  # photo/video/file
        return True
    except Exception as e:
        print(f"[index] exact copy failed ({type(e).__name__}: {e}); falling back to a re-send")
    try:
        if match.get("has_media"):
            await client.copy_message(message.chat.id, chat_id, post_id)
        else:
            text = match.get("html") or match.get("text") or ""
            try:
                await message.reply(text, parse_mode=enums.ParseMode.HTML)
            except Exception:
                await message.reply(match.get("text") or "", parse_mode=enums.ParseMode.DISABLED)
        return True
    except Exception as e:
        print(f"[index] couldn't send indexed post: {e}")  # source post may be inaccessible
        return False


# ---------------- Main flow ----------------

_bg_tasks = set()


def _spawn(coro) -> "asyncio.Task":
    """Background task that is kept referenced until it finishes (so it can't be garbage-collected mid-way)."""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


async def _none():
    return None


async def _safe_add_user(user):
    try:
        await add_user(user.id, user.first_name or "", user.username or "")
    except Exception as e:
        print(f"[db] couldn't save user {user.id}: {e}")


async def _typing(client: Client, chat_id: int):
    try:
        await client.send_chat_action(chat_id, enums.ChatAction.TYPING)
    except Exception:
        pass


async def _deliver_to_admin(client: Client, admin_id: int, message: Message, user_id: int,
                            info_text: str, info_markup, send_info: bool) -> bool:
    """Forward the user's message to one admin, followed by the info message. All admins run in parallel."""
    try:
        fwd = await message.forward(admin_id)
    except Exception:
        return False
    mapped_ids = [fwd.id]
    if send_info:
        try:
            info = await client.send_message(
                admin_id, info_text, reply_to_message_id=fwd.id,
                reply_markup=info_markup, parse_mode=enums.ParseMode.HTML
            )
            mapped_ids.append(info.id)
        except Exception as e:
            print(f"[info] couldn't send user info to {admin_id}: {e}")

    async def save(mid):  # the admin can reply to the forwarded message OR the info message
        try:
            await save_message_map(admin_id, mid, user_id)
        except Exception as e:
            print(f"[map] couldn't save reply mapping: {e}")
    await asyncio.gather(*(save(mid) for mid in mapped_ids))
    return True


async def _ai_answer(client: Client, message: Message, user_id: int):
    _spawn(_typing(client, message.chat.id))  # "typing..." while the AI thinks
    history = await get_chat_history(user_id)
    return await ask_gemini(history, message.text)


async def _prepare_reply(client: Client, message: Message, user_id: int):
    """Runs WHILE the message is being forwarded to the admins: first an indexed channel post
    (see Plugins/indexer.py), otherwise the AI answer. Returns ("index", doc) / ("ai", text) / None."""
    try:
        matches = await search_content(message.text, limit=1)
    except Exception as e:
        print(f"[index] search failed: {e}")
        matches = []
    if matches:
        return ("index", matches[0])
    if not is_enabled():
        return None
    ai_reply = await _ai_answer(client, message, user_id)
    return ("ai", ai_reply) if ai_reply else None


async def _send_ai_reply(message: Message, user_id: int, ai_reply: str) -> bool:
    try:
        # Parse mode off: model output often contains stray *, _ or < that break Telegram parsing.
        await message.reply(ai_reply[:3800] + AI_FOOTER, parse_mode=enums.ParseMode.DISABLED)
    except Exception as e:
        print(f"[ai] couldn't send AI reply: {e}")
        return False
    try:
        await add_chat_turns(user_id, message.text, ai_reply)
    except Exception as e:  # the user already got the answer; only the AI's memory of it is lost
        print(f"[ai] couldn't save chat history: {e}")
    return True


async def _deliver_reply(client: Client, message: Message, user_id: int, reply) -> bool:
    kind, payload = reply
    if kind == "index":
        if await _send_indexed(client, message, payload):
            return True
        if not is_enabled():
            return False
        payload = await _ai_answer(client, message, user_id)  # the post couldn't be sent -> let the AI answer
        if not payload:
            return False
    return await _send_ai_reply(message, user_id, payload)


async def _confirm(message: Message) -> bool:
    """The short auto-deleting "Message sent!" notice (once per album)."""
    if not _first_in_group(message):
        return False
    try:
        sent_msg = await message.reply("✅ <i>Message sent!</i>", parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        print(f"[confirm] couldn't send confirmation: {e}")
        return False
    asyncio.create_task(_delete_after_delay(sent_msg))
    return True


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

    if not flood_ok(user_id):  # no database work at all for a flood
        return

    if await is_banned(user_id):
        return

    if await fsub_gate(client, message):
        return

    _spawn(_safe_add_user(message.from_user))  # not needed for the answer -> don't make the user wait for it

    # Keyword auto-reply (replaces forwarding when it matches) and the admin list are independent lookups.
    auto_reply, admin_list = await asyncio.gather(
        find_auto_reply(message.text) if message.text else _none(), get_admins()
    )
    if auto_reply:
        return await message.reply(auto_reply, parse_mode=enums.ParseMode.HTML)

    admin_ids = set(admin_list)
    admin_ids.add(ADMIN)

    # Bot-generated replies (indexed post, then AI) are prepared in the background while the message is
    # forwarded — text only, rate-limited per user. The admins get the forward either way.
    prep = _spawn(_prepare_reply(client, message, user_id)) if (message.text and _cooldown_ok(user_id)) else None

    send_info = _group_once("info", message)
    info_text, info_markup = _user_info(message.from_user)
    results = await asyncio.gather(*(
        _deliver_to_admin(client, admin_id, message, user_id, info_text, info_markup, send_info)
        for admin_id in admin_ids
    ))

    if not any(results):
        if prep:
            prep.cancel()
        now = time.monotonic()
        if now - _error_notice_at.get(user_id, -1e9) > 30:  # don't spam the error itself
            _error_notice_at[user_id] = now
            await message.reply("❌ **Error:** Message could not be sent.")
        return

    if prep is None:
        await _confirm(message)
        return

    # Answer ready quickly -> send just the answer. Still working -> confirm immediately, answer follows.
    done, _pending = await asyncio.wait({prep}, timeout=CONFIRM_AFTER)
    confirmed = False
    if not done:
        confirmed = await _confirm(message)
    try:
        reply = await prep
    except Exception as e:
        print(f"[reply] preparing the answer failed: {e}")
        reply = None
    if reply and await _deliver_reply(client, message, user_id, reply):
        return
    if not confirmed:
        await _confirm(message)


async def _delete_after_delay(msg: Message):
    await asyncio.sleep(SENT_CONFIRM_DELETE_AFTER)
    try:
        await msg.delete()
    except Exception:
        pass


@Client.on_message(filters.private & admin_filter & filters.reply & ~filters.command(ADMIN_COMMANDS))
async def reply_to_user(client: Client, message: Message):
    replied_msg = message.reply_to_message
    user_id = None
    if replied_msg:
        user_id = await get_mapped_user(message.chat.id, replied_msg.id)
        if not user_id and replied_msg.forward_from:
            user_id = replied_msg.forward_from.id
    if not user_id:
        return await message.reply(REPLY_HINT, parse_mode=enums.ParseMode.HTML)
    try:
        await message.copy(user_id)
    except (UserIsBlocked, InputUserDeactivated):
        await _react(client, message, REACT_FAIL)
        return await message.reply(NOT_SENT_TEXT, parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        await _react(client, message, REACT_FAIL)
        return await message.reply(
            f"⚠️ <b>Error sending to user:</b>\n<code>{html.escape(str(e))}</code>", parse_mode=enums.ParseMode.HTML
        )
    await _react(client, message, REACT_OK)


# An admin message that is not a reply (and not a command / channel forward / post link) reaches nobody.
@Client.on_message(
    filters.private & admin_filter & ~filters.reply & ~filters.command(ADMIN_COMMANDS)
    & ~filters.forwarded & ~filters.regex(LINK_ONLY_REGEX)
)
async def admin_needs_reply(client: Client, message: Message):
    if _group_once("hint", message):
        await message.reply(REPLY_HINT, parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.command("aitest") & filters.private & admin_filter)
async def ai_test_cmd(client: Client, message: Message):
    """Admin: checks the AI right now and shows the exact reason if it fails."""
    info = ai_status()
    if not info["configured"]:
        return await message.reply(
            "❌ <b>AI is OFF</b>\n<code>GEMINI_API_KEY</code> is not set in the bot's environment variables.\n"
            "Add it (key from aistudio.google.com) and restart the bot.", parse_mode=enums.ParseMode.HTML)
    wait = await message.reply("⏳ Testing the AI...")
    started = time.monotonic()
    answer = await ask_gemini([], "Reply with exactly one word: OK")
    took = time.monotonic() - started
    info = ai_status()
    if answer:
        text = (f"✅ <b>AI is working</b>\nModel: <code>{html.escape(info['model'])}</code>\n"
                f"Time: {took:.1f}s\nReply: {html.escape(answer[:100])}")
    else:
        text = (f"❌ <b>AI failed</b> ({took:.1f}s)\nModel: <code>{html.escape(info['model'])}</code>\n"
                f"Reason: <code>{html.escape(str(info['last_error']))}</code>")
    await wait.edit_text(text, parse_mode=enums.ParseMode.HTML)


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
