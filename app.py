from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from telegram import Update
from telegram.ext import Application

from bot import create_application


load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logging.getLogger("httpx").setLevel(logging.WARNING)
LOGGER = logging.getLogger(__name__)
BOT_COMMANDS = frozenset({"price", "p", "chatid", "start", "help"})

app = FastAPI()
_telegram_app: Application | None = None
_telegram_initialized = False
_telegram_lock = asyncio.Lock()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"ok": "true"}


@app.post("/{secret_path:path}")
async def telegram_webhook(
    secret_path: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    expected_path = normalize_secret_path(os.getenv("WEBHOOK_SECRET_PATH", ""))
    expected_token = os.getenv("WEBHOOK_SECRET_TOKEN", "")

    if not expected_path:
        raise HTTPException(status_code=500, detail="WEBHOOK_SECRET_PATH is not configured")
    if secret_path != expected_path:
        raise HTTPException(status_code=404, detail="Not found")
    if not expected_token:
        raise HTTPException(status_code=500, detail="WEBHOOK_SECRET_TOKEN is not configured")
    if x_telegram_bot_api_secret_token != expected_token:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await request.json()
    if not should_process_update_payload(payload):
        return {"ok": True}

    telegram_app = await get_telegram_application()
    update = Update.de_json(payload, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


async def get_telegram_application() -> Application:
    global _telegram_app, _telegram_initialized

    async with _telegram_lock:
        if _telegram_app is None:
            _telegram_app = create_application()
        if not _telegram_initialized:
            await _telegram_app.initialize()
            _telegram_initialized = True
            LOGGER.info("Telegram application initialized")
    return _telegram_app


def normalize_secret_path(value: str) -> str:
    return value.strip("/")


def should_process_update_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False

    if payload.get("callback_query"):
        return True

    message = payload.get("message")
    if not isinstance(message, dict):
        return False

    text = message.get("text")
    if not isinstance(text, str):
        return False

    command = extract_bot_command(text)
    return command in BOT_COMMANDS


def extract_bot_command(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    command_part = text.split(maxsplit=1)[0][1:]
    command = command_part.split("@", maxsplit=1)[0].lower()
    return command or None
