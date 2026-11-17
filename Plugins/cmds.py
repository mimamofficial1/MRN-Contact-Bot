import os
import sys
import asyncio
from pyrogram import Client, filters
from pyrogram.types import Message
from database import admin_filter, add_user, is_banned
from Plugins.contact import fsub_gate, send_start_message, flood_ok

@Client.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    mention_user = message.from_user.mention

    if await is_banned(user_id):
        return

    if message.from_user.is_bot or not flood_ok(user_id):
        return

    if await fsub_gate(client, message):
        return

    await add_user(user_id, message.from_user.first_name or "", message.from_user.username or "")
    await send_start_message(client, message.chat.id, mention_user)

@Client.on_message(filters.command("restart") & filters.private & admin_filter)
async def restart_bot(client: Client, message: Message):
    await message.reply_text("**🔄 Restarting bot...**\n__You'll get a message when it's back online.__")
    await asyncio.sleep(1)
    os.execl(sys.executable, sys.executable, *sys.argv)
