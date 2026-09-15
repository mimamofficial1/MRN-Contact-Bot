import asyncio
import re
from math import ceil

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from database import (
    admin_filter, add_indexed_channel, add_content_item, is_indexed_channel
)

# Bots can't call get_chat_history() (Telegram blocks messages.GetHistory for bot
# accounts), but they CAN fetch messages by known ID via get_messages() — so we
# ask the admin for the last message (forward it, or send its t.me link) and
# then walk backwards from that ID in batches.
BATCH_SIZE = 200

LINK_REGEX = re.compile(r"(?:https?://)?t\.me/(c/)?([\w\d_]+)/(\d+)")

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
    (filters.forwarded | (filters.text & filters.regex(LINK_REGEX)))
)
async def request_index(client: Client, message: Message):
    chat_id, last_msg_id = _resolve_target(message)
    if not chat_id:
        return  # not a channel forward / not a recognizable t.me link — ignore

    try:
        chat = await client.get_chat(chat_id)
    except Exception as e:
        return await message.reply(
            f"❌ **Couldn't access that channel:**\n`{e}`\n"
            f"_Make sure the bot is a member/admin there._"
        )

    pending_index[message.from_user.id] = (chat.id, last_msg_id)
    await message.reply(
        f"**Index this channel?**\n\n"
        f"Chat: `{chat.title or chat.id}`\n"
        f"Up to message ID: `{last_msg_id}`\n\n"
        f"_Text, links and images will be scanned. New posts here will also "
        f"auto-index going forward, no need to repeat this._",
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
    batches = ceil(last_msg_id / BATCH_SIZE) if last_msg_id else 0

    for batch in range(batches):
        start_id = batch * BATCH_SIZE + 1
        end_id = min(start_id + BATCH_SIZE - 1, last_msg_id)
        ids = list(range(start_id, end_id + 1))

        while True:
            try:
                messages = await client.get_messages(chat_id, ids)
                break
            except FloodWait as e:
                await asyncio.sleep(e.value + 2)
            except Exception:
                messages = []
                break

        if not isinstance(messages, list):
            messages = [messages]

        for msg in messages:
            if not msg or msg.empty:
                continue
            scanned += 1
            if not (msg.text or msg.photo):
                continue
            text = _extract_text(msg)
            if text:
                await add_content_item(chat_id, msg.id, text, has_media=bool(msg.photo))
                added += 1

        if batch % 3 == 0 or batch == batches - 1:
            try:
                await status.edit_text(
                    f"📊 **Indexing...** Batch {batch + 1}/{batches}\n"
                    f"Scanned: `{scanned}` • Indexed: `{added}`"
                )
            except Exception:
                pass

    await status.edit_text(
        f"✅ **Indexing complete**\n\n"
        f"• Scanned: `{scanned}`\n"
        f"• Indexed (text/link/image posts): `{added}`\n\n"
        f"_New posts in this channel will now be indexed automatically._"
    )


@Client.on_message(filters.channel & indexed_channel_filter & (filters.text | filters.photo))
async def auto_index_new_post(client: Client, message: Message):
    """Keeps the index current — any new post in a registered channel gets added automatically."""
    text = _extract_text(message)
    if text:
        await add_content_item(message.chat.id, message.id, text, has_media=bool(message.photo))
