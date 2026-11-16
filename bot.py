import os
from pyrogram import Client
from aiohttp import web
from config import API_ID, API_HASH, BOT_TOKEN, ADMIN
from database import ensure_indexes
from ai import close_session

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
            sleep_threshold=15
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

        if ADMIN:
            try:
                await self.send_message(ADMIN, f"**🤖 {me.first_name} Contact Bot is started...**")
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
