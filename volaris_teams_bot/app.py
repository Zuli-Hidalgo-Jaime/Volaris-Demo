# app.py
import os
from aiohttp import web

from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
from botbuilder.integration.aiohttp import aiohttp_error_middleware

from router import InMemorySessionStore
from bot import VolarisBot

APP_ID = os.environ.get("MicrosoftAppId", "")
APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "")

adapter_settings = BotFrameworkAdapterSettings(APP_ID, APP_PASSWORD)
adapter = BotFrameworkAdapter(adapter_settings)

store = InMemorySessionStore()
bot = VolarisBot(store)

async def messages(req: web.Request) -> web.Response:
    body = await req.json()
    auth_header = req.headers.get("Authorization", "")
    response = web.Response(status=201)

    async def aux_func(turn_context):
        await bot.on_turn(turn_context)

    await adapter.process_activity(body, auth_header, aux_func)
    return response

async def health(req: web.Request) -> web.Response:
    return web.json_response({"ok": True})

app = web.Application(middlewares=[aiohttp_error_middleware])
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    web.run_app(app, host="0.0.0.0", port=port)