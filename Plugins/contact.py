import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import UserNotParticipant
from config import ADMIN
from database import add_user, is_banned, get_admins, admin_filter, get_force_channels, find_auto_reply, get_start_message

# RAM memory dictionaries
message_memory = {}  # {(admin_id, forwarded_msg_id): user_id}

# Constants
MAX_MEMORY_LIMIT = 100  # Max records before auto-cleaning
SENT_CONFIRM_DELETE_AFTER = 3  # seconds before "Message sent!" auto-deletes


async def get_not_joined_channels(client: Client, user_id: int) -> list:
    """Returns list of force-join channels the user hasn't joined yet."""
    channels = await get_force_channels()
    not_joined = []
    for chat in channels:
        try:
            member = await client.get_chat_member(chat, user_id)
            if member.status in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED):
                not_joined.append(chat)
        except UserNotParticipant:
            not_joined.append(chat)
        except Exception:
            continue  # skip misconfigured/inaccessible chat
    return not_joined


async def is_subscribed(client: Client, user_id: int) -> bool:
    not_joined = await get_not_joined_channels(client, user_id)
    return len(not_joined) == 0


async def force_sub_markup(client: Client, user_id: int) -> InlineKeyboardMarkup:
    not_joined = await get_not_joined_channels(client, user_id)
    buttons = []
    for chat in not_joined:
        chat_name = str(chat).lstrip("@")
        buttons.append([InlineKeyboardButton(f"📢 Join {chat_name}", url=f"https://t.me/{chat_name}")])
    buttons.append([InlineKeyboardButton("🔄 I've Joined", callback_data="check_fsub")])
    return InlineKeyboardMarkup(buttons)


@Client.on_message(filters.private & ~admin_filter & ~filters.command("start"))
async def forward_to_admin(client: Client, message: Message):
    user_id = message.from_user.id

    if await is_banned(user_id):
        return

    if not await is_subscribed(client, user_id):
        return await message.reply(
            "<blockquote><b><i>Please join our channel(s) first to use this bot.</i></b></blockquote>",
            parse_mode=enums.ParseMode.HTML,
            reply_markup=await force_sub_markup(client, user_id)
        )

    await add_user(user_id, message.from_user.first_name or "", message.from_user.username or "")

    # Automatic keyword reply — replaces forwarding when a keyword matches.
    if message.text:
        auto_reply = await find_auto_reply(message.text)
        if auto_reply:
            return await message.reply(auto_reply, quote=True, parse_mode=enums.ParseMode.HTML)

    admin_ids = set(await get_admins())
    admin_ids.add(ADMIN)

    sent_to_any = False
    for admin_id in admin_ids:
        try:
            fwd = await message.forward(admin_id)
            message_memory[(admin_id, fwd.id)] = user_id
            sent_to_any = True
        except Exception:
            continue

    if not sent_to_any:
        return await message.reply(f"❌ **Error:** Message could not be sent.")

    sent_msg = await message.reply(
        "✅ <i>Message sent!</i>",
        parse_mode=enums.ParseMode.HTML,
        quote=False
    )
    asyncio.create_task(_delete_after_delay(sent_msg))

    if len(message_memory) > MAX_MEMORY_LIMIT:
        oldest_msg_keys = list(message_memory.keys())[:50]
        for key in oldest_msg_keys:
            del message_memory[key]


async def _delete_after_delay(msg: Message):
    await asyncio.sleep(SENT_CONFIRM_DELETE_AFTER)
    try:
        await msg.delete()
    except Exception:
        pass


@Client.on_message(filters.private & admin_filter & filters.reply)
async def reply_to_user(client: Client, message: Message):
    replied_msg = message.reply_to_message
    user_id = message_memory.get((message.chat.id, replied_msg.id))
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
    if await is_subscribed(client, user_id):
        await query.message.delete()
        await query.answer("✅ Thanks for joining! You can use the bot now.", show_alert=True)
    else:
        await query.answer("❌ You haven't joined all channels yet.", show_alert=True)


def build_buttons_markup(rows):
    if not rows:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], url=b["url"]) for b in row] for row in rows])


async def send_start_message(client: Client, chat_id: int, mention: str):
    """Sends the admin-configurable /start message (used by /start and settings preview)."""
    doc = await get_start_message()
    text = doc.get("text", "").replace("{mention}", mention)
    markup = build_buttons_markup(doc.get("buttons") or [])
    media_type = doc.get("media_type")
    media_file_id = doc.get("media_file_id")
    if media_type == "photo":
        await client.send_photo(chat_id, media_file_id, caption=text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
    elif media_type == "video":
        await client.send_video(chat_id, media_file_id, caption=text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
    else:
        await client.send_message(chat_id, text, reply_markup=markup, parse_mode=enums.ParseMode.HTML)
