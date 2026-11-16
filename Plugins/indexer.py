import asyncio
import re
from math import ceil

from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from database import (
    admin_filter, add_indexed_channel, add_content_item, add_content_items,
    remove_content_item, is_indexed_channel, clear_content_index
)

# Bots can't call get_chat_history() (Telegram blocks messages.GetHistory for bot
# accounts), but they CAN fetch messages by known ID via get_messages() — so we
# ask the admin for the last message (forward it, or send its t.me link) and
# then walk backwards from that ID in batches.
BATCH_SIZE = 200
MAX_CONSECUTIVE_ERRORS = 3  # abort the run instead of silently scanning nothing

LINK_REGEX = re.compile(r"(?:https?://)?t\.me/(c/)?([\w\d_]+)/(\d+)")
# Only a message that is *just* a post link starts an index request, so links
# the admin sends inside normal text never trigger the prompt by accident.
LINK_ONLY_REGEX = re.compile(r"^\s*(?:https?://)?t\.me/(?:c/)?[\w\d_]+/\d+\s*$")

# pending_index[admin_id] = (chat_id, last_msg_id) — set once a forward/link is
# resolved, consumed when the admin taps "Yes, index it".
pending_index = {}


async def _is_indexed_channel_func(_, __, message):
    if not message.chat:
        return False
    return await is_indexed_channel(message.chat.id)

# Matches only channels the admin has already registered via a previous index run.
indexed_channel_filter = filters.create(_is_indexed_channel_func)


def _extract_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


def _extract_html(message: Message) -> str:
    """Same text with formatting/hyperlinks kept, so re-sent posts look like the original."""
    if message.text:
        return message.text.html
    if message.caption:
        return message.caption.html
    return ""


def _has_media(message: Message) -> bool:
    return bool(message.media) and message.media != enums.MessageMediaType.WEB_PAGE


def _resolve_target(message: Message):
    """Returns (chat_id, last_message_id) from a forwarded channel post or a
    t.me link, else (None, None)."""
    if message.forward_from_chat:
        return message.forward_from_chat.id, message.forward_from_message_id
    if message.text:
        match = LINK_REGEX.search(message.text)
        if match:
            is_private, chat_part, msg_id = match.groups()
            chat_id = int(f"-100{chat_part}") if is_private else f"@{chat_part}"
            return chat_id, int(msg_id)
    return None, None


@Client.on_message(
    filters.private & admin_filter &
    (filters.forwarded | (filters.text & filters.regex(LINK_ONLY_REGEX)))
)
async def request_index(client: Client, message: Message):
    chat_id, last_msg_id = _resolve_target(message)
    if not chat_id:
        return  # not a channel forward / not a recognizable t.me link — ignore
    if not last_msg_id:
        return await message.reply("❌ **Couldn't read the post ID.** Send the post's t.me link instead.")

    try:
        chat = await client.get_chat(chat_id)
    except Exception as e:
        return await message.reply(
            f"❌ **Couldn't access that channel:**\n`{e}`\n"
            f"__Make sure the bot is a member/admin there.__"
        )

    pending_index[message.from_user.id] = (chat.id, last_msg_id)
    await message.reply(
        f"**Index this channel?**\n\n"
        f"Chat: `{chat.title or chat.id}`\n"
        f"Up to message ID: `{last_msg_id}`\n\n"
        f"__Text, links and captions (of photos, videos, files) will be scanned. New posts here will also "
        f"auto-index going forward, no need to repeat this.__",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, index it", callback_data="do_index")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_index")]
        ])
    )


@Client.on_callback_query(filters.regex("^cancel_index$") & admin_filter)
async def cancel_index_cb(client: Client, query: CallbackQuery):
    pending_index.pop(query.from_user.id, None)
    await query.message.edit_text("Cancelled.")


@Client.on_callback_query(filters.regex("^do_index$") & admin_filter)
async def do_index_cb(client: Client, query: CallbackQuery):
    target = pending_index.pop(query.from_user.id, None)
    if not target:
        return await query.answer("This request has expired.", show_alert=True)

    chat_id, last_msg_id = target
    await add_indexed_channel(chat_id)
    status = query.message
    await status.edit_text(f"🔍 **Indexing started...** (up to message `{last_msg_id}`)")

    scanned = 0
    added = 0
    failed_batches = 0
    consecutive_errors = 0
    last_error = None
    batches = ceil(last_msg_id / BATCH_SIZE) if last_msg_id else 0

    for batch in range(batches):
        start_id = batch * BATCH_SIZE + 1
        end_id = min(start_id + BATCH_SIZE - 1, last_msg_id)
        ids = list(range(start_id, end_id + 1))

        while True:
            try:
                messages = await client.get_messages(chat_id, ids)
                consecutive_errors = 0
                break
            except FloodWait as e:
                await asyncio.sleep(e.value + 2)
            except Exception as e:
                last_error = e
                consecutive_errors += 1
                failed_batches += 1
                messages = []
                break

        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            return await status.edit_text(
                f"❌ **Indexing stopped — Telegram keeps returning an error.**\n\n"
                f"`{last_error}`\n\n"
                f"__Make sure the bot is a member/admin of that channel, then try again.__\n"
                f"Indexed so far: `{added}`"
            )

        if not isinstance(messages, list):
            messages = [messages]

        items = []
        for msg in messages:
            if not msg or msg.empty:
                continue
            scanned += 1
            text = _extract_text(msg)
            if text:
                items.append((msg.id, text, _has_media(msg), _extract_html(msg)))
        if items:
            await add_content_items(chat_id, items)
            added += len(items)

        if batch % 3 == 0 or batch == batches - 1:
            try:
                await status.edit_text(
                    f"📊 **Indexing...** Batch {batch + 1}/{batches}\n"
                    f"Scanned: `{scanned}` • Indexed: `{added}`"
                )
            except Exception:
                pass

    warn = f"\n⚠️ Batches that errored: `{failed_batches}` (last: `{last_error}`)" if failed_batches else ""
    await status.edit_text(
        f"✅ **Indexing complete**\n\n"
        f"• Scanned: `{scanned}`\n"
        f"• Indexed (posts with text/link/caption): `{added}`{warn}\n\n"
        f"__New posts in this channel will now be indexed automatically.__"
    )


@Client.on_message(filters.channel & indexed_channel_filter)
async def auto_index_new_post(client: Client, message: Message):
    """Keeps the index current — any new post in a registered channel gets added automatically."""
    text = _extract_text(message)
    if text:
        await add_content_item(message.chat.id, message.id, text, has_media=_has_media(message), html=_extract_html(message))


@Client.on_edited_message(filters.channel & indexed_channel_filter)
async def auto_index_edited_post(client: Client, message: Message):
    """An edited post replaces its old indexed version (or is dropped if its text was removed)."""
    text = _extract_text(message)
    if text:
        await add_content_item(message.chat.id, message.id, text, has_media=_has_media(message), html=_extract_html(message))
    else:
        await remove_content_item(message.chat.id, message.id)


@Client.on_deleted_messages(filters.channel)
async def auto_index_deleted_posts(client: Client, messages):
    """Deleted channel posts are removed from the index so they're never re-sent."""
    for m in messages:
        if m.chat:
            await remove_content_item(m.chat.id, m.id)


@Client.on_message(filters.command("clearindex") & filters.private & admin_filter)
async def clearindex_cmd(client: Client, message: Message):
    await message.reply(
        "⚠️ **This will permanently delete ALL indexed content.**\n"
        "__Registered channels stay registered — new posts will still auto-index after this.__\n\n"
        "Are you sure?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, clear everything", callback_data="do_clearindex")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_index")]
        ])
    )


@Client.on_callback_query(filters.regex("^do_clearindex$") & admin_filter)
async def do_clearindex_cb(client: Client, query: CallbackQuery):
    await clear_content_index()
    await query.message.edit_text("🗑️ **All indexed content has been cleared.**")
