# app.py
import logging
import os

from aiohttp import web
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
    aiohttp_error_middleware,
)

from bot import VolarisBot
from channel_sender import safe_send_text
from router import InMemorySessionStore


class _BotAuthConfig:
    """
    BotBuilder Python expects APP_* settings.
    Map Azure-standard MicrosoftApp* env vars to those keys.
    """

    def __init__(self):
        tenant_id = (
            os.environ.get("MicrosoftAppTenantId", "").strip()
            or os.environ.get("MicrosoftTenantId", "").strip()
        )
        app_type = os.environ.get("MicrosoftAppType", "").strip()
        if not app_type:
            # Backward compatibility with existing secret-based settings.
            app_type = "SingleTenant" if tenant_id else "MultiTenant"

        self.APP_TYPE = app_type
        self.APP_ID = os.environ.get("MicrosoftAppId", "").strip()
        self.APP_PASSWORD = os.environ.get("MicrosoftAppPassword", "").strip()
        self.APP_TENANTID = tenant_id


bot_auth = ConfigurationBotFrameworkAuthentication(_BotAuthConfig())
adapter = CloudAdapter(bot_auth)

store = InMemorySessionStore()
bot = VolarisBot(store)


async def on_error(turn_context, error):
    logging.error("Unhandled bot error: %s", error, exc_info=True)
    sent = await safe_send_text(turn_context, "Ocurrio un error interno. Intenta de nuevo.")
    if not sent:
        logging.error("Failed to send on_error notification back to the channel.")


adapter.on_turn_error = on_error


async def messages(req: web.Request) -> web.Response:
    return await adapter.process(req, bot)


async def health(req: web.Request) -> web.Response:
    return web.json_response({"ok": True})


app = web.Application(middlewares=[aiohttp_error_middleware])
app.router.add_post("/api/messages", messages)
app.router.add_get("/health", health)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    web.run_app(app, host="0.0.0.0", port=port)
