import os
import sys
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from config import ADMIN

@Client.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    mention_user = message.from_user.mention
    start_text = f"""<blockquote><b><i>💌Hello, {mention_user} Dear Subscriber, Welcome To Our Bot

☆ Through This Bot You Can Contact Us...

🔵You Ask Something, Promotion, etc.
🟢You Will Get Updates.
🔴You Can Request Movie Series & TV Show

Join : https://t.me/Mrn_Officialx
━━━━━━━━━━━━━━━━━━━━━
😘Join - Share - Like >>> @Mrn_Officialx⚡
</i></b></blockquote>"""
    await client.send_photo(
        chat_id=message.chat.id,
        photo="https://files.catbox.moe/n69gs0.jpg",
        caption=start_text,
        parse_mode=enums.ParseMode.HTML
    )

@Client.on_message(filters.command("restart") & filters.private & filters.user(ADMIN))
async def restart_bot(client: Client, message: Message):
    steve = await message.reply_text("**🔄 Restarting bot...**")
    await asyncio.sleep(3)
    await steve.edit("**✅ Bot restarted successfully**")
    os.execl(sys.executable, sys.executable, *sys.argv)
