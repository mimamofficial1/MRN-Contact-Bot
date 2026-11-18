import os
from pyrogram import Client
from aiohttp import web
from config import API_ID, API_HASH, BOT_TOKEN, ADMIN
from database import ensure_indexes, should_send_startup_notice
from ai import close_session, is_enabled, GEMINI_MODEL

STARTUP_TEXT = "✨ ᴍʀɴ ᴄᴏɴᴛᴀᴄᴛ ʙᴏᴛ ɪs ɴᴏᴡ ᴏɴʟɪɴᴇ !"

routes = web.RouteTableDef()

@routes.get("/", allow_head=True)
async def root_route(request):
    return web.Response(text="<h3 align='center'><b>Bot is Alive</b></h3>", content_type='text/html')

async def web_server():
    app = web.Application(client_max_size=30_000_000)
    app.add_routes(routes)
    return app

class Bot(Client):
    def __init__(self):
        super().__init__(
            "contact_bot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            plugins=dict(root="Plugins"),
            workers=64,
            sleep_threshold=15,
            skip_updates=True  # never replay messages that arrived while the bot was offline
        )
        self._web_runner = None

    async def start(self, *args, **kwargs):
        self._web_runner = web.AppRunner(await web_server())
        await self._web_runner.setup()
        try:
            await web.TCPSite(self._web_runner, "0.0.0.0", int(os.getenv("PORT", 8080))).start()
            print("Web server started.")
        except Exception as e:
            print(f"Web server error: {e}")

        # Indexes make per-message DB lookups and channel indexing fast (safe to run on every start).
        try:
            await ensure_indexes()
            print("Database indexes ready.")
        except Exception as e:
            print(f"Database index setup error: {e}")

        await super().start(*args, **kwargs)

        me = await self.get_me()
        print(f"Bot Started as {me.first_name}")

        print(f"[ai] AI replies ENABLED (model {GEMINI_MODEL})" if is_enabled()
              else "[ai] AI replies are OFF: GEMINI_API_KEY is not set")

        # Premium emoji: loads emoji packs + learns your style in the background (sends no messages).
        try:
            from Plugins.premium_emoji import start_premium
            start_premium(self)
        except Exception as e:
            print(f"Premium emoji not started: {e}")

        # One message per start; skipped if one went out in the last 30s (crash-loop protection).
        if ADMIN:
            try:
                if await should_send_startup_notice():
                    await self.send_message(ADMIN, STARTUP_TEXT)
            except Exception as e:
                print(f"Error sending message to admin: {e}")

    async def stop(self, *args, **kwargs):
        await super().stop(*args, **kwargs)
        await close_session()
        if self._web_runner:
            await self._web_runner.cleanup()
        print("Bot stopped.")

if __name__ == "__main__":
    Bot().run()
