from pyrogram import Client, filters
from pyrogram.types import Message
from database import (
    admin_filter, add_indexed_channel, add_content_item, is_indexed_channel
)


async def _is_indexed_channel_func(_, __, message):
    if not message.chat:
        return False
    return await is_indexed_channel(message.chat.id)

# Matches only channels the admin has already registered via /index.
indexed_channel_filter = filters.create(_is_indexed_channel_func)


def _extract_text(message: Message) -> str:
    return (message.text or message.caption or "").strip()


@Client.on_message(filters.command("index") & filters.private & admin_filter)
async def index_channel_cmd(client: Client, message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        return await message.reply(
            "**Usage:** `/index <channel_username_or_id> [limit]`\n"
            "_The bot must already be a member/admin of that channel._\n"
            "_Leave `limit` empty to scan the entire history._"
        )

    target = parts[1]
    limit = 0
    if len(parts) > 2:
        try:
            limit = int(parts[2])
        except ValueError:
            limit = 0

    try:
        chat = await client.get_chat(target)
    except Exception as e:
        return await message.reply(f"❌ **Couldn't access that channel:**\n`{e}`")

    await add_indexed_channel(chat.id)
    status = await message.reply(f"🔍 **Indexing `{chat.title or chat.id}`...** this may take a while.")

    scanned = 0
    added = 0
    async for msg in client.get_chat_history(chat.id, limit=limit):
        scanned += 1
        if not (msg.text or msg.photo):
            continue
        text = _extract_text(msg)
        if text:
            await add_content_item(chat.id, msg.id, text, has_media=bool(msg.photo))
            added += 1

    await status.edit(
        f"✅ **Indexing complete for `{chat.title or chat.id}`**\n\n"
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
