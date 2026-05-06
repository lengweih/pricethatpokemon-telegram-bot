from __future__ import annotations

import logging
import os
from typing import Any, cast

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from pricing import (
    MalformedQuery,
    NoCardsFound,
    PricingProvider,
    PricingAPIError,
    RateLimited,
    available_variants,
    create_price_provider,
    format_price_message,
    get_card_link,
    get_fallback_image_url,
    get_image_url,
    select_default_variant,
    summarize_card,
)


LOGGER = logging.getLogger(__name__)
MAX_QUERY_LENGTH = 120
PRICE_COMMANDS = ("price", "p")
BOT_COMMANDS = frozenset((*PRICE_COMMANDS, "chatid", "start", "help"))


def create_application() -> Application:
    load_dotenv()

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    max_results = get_int_env("MAX_RESULTS", 5)
    price_client = create_price_provider(
        provider_id=os.getenv("PRICE_PROVIDER", "tcgdex"),
        tcgdex_api_base=os.getenv("TCGDEX_API_BASE", "https://api.tcgdex.net/v2/en"),
        tcgdex_image_quality=os.getenv("TCGDEX_IMAGE_QUALITY", "low"),
        tcgdex_image_extension=os.getenv("TCGDEX_IMAGE_EXTENSION", "webp"),
        cache_ttl_seconds=get_int_env("CACHE_TTL_SECONDS", 3600),
        max_results=max_results,
        tcgdex_candidate_limit=get_int_env("TCGDEX_CANDIDATE_LIMIT", max_results),
        tcgdex_japanese_api_base=os.getenv("TCGDEX_JAPANESE_API_BASE"),
        enable_japanese_search=get_bool_env("ENABLE_JAPANESE_SEARCH", True),
        display_currency=os.getenv("DISPLAY_CURRENCY", "SGD"),
        exchange_api_base=os.getenv("EXCHANGE_API_BASE", "https://api.frankfurter.dev"),
    )

    application = (
        ApplicationBuilder()
        .token(bot_token)
        .post_shutdown(close_pricing_client)
        .build()
    )
    application.bot_data["price_client"] = price_client
    application.bot_data["allowed_chat_ids"] = parse_allowed_chat_ids(os.getenv("ALLOWED_CHAT_IDS", ""))
    application.bot_data["allowed_topic_ids"] = parse_allowed_topic_ids(os.getenv("ALLOWED_TOPIC_IDS", ""))

    application.add_handler(CommandHandler("chatid", chat_id_command))
    application.add_handler(CommandHandler(PRICE_COMMANDS, price_command))
    application.add_handler(CommandHandler(["start", "help"], help_command))
    application.add_handler(CallbackQueryHandler(callback_query))
    application.add_error_handler(error_handler)
    return application


async def close_pricing_client(application: Application) -> None:
    price_client = application.bot_data.get("price_client")
    if hasattr(price_client, "close"):
        await price_client.close()


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_chat_allowed(update, context):
        return

    await update.message.reply_text(
        "Use /price followed by a Pokemon card search.\n\n"
        "Examples:\n"
        "/price lugia 138\n"
        "/price charizard 4/102\n"
        "/price lugia silver tempest\n"
        "/price rare candy\n\n"
        "Shortcut: /p lugia 138",
    )


async def chat_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    await update.message.reply_text(
        f"Chat ID: {update.effective_chat.id}\n"
        f"Topic ID: {format_topic_id(get_message_thread_id(update))}\n\n"
        "Use ALLOWED_CHAT_IDS for the group and ALLOWED_TOPIC_IDS for a specific topic."
    )


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_chat_allowed(update, context):
        LOGGER.info(
            "Ignoring /price outside allowed chat/topic: chat_id=%s topic_id=%s",
            get_chat_id(update),
            format_topic_id(get_message_thread_id(update)),
        )
        return

    raw_query = " ".join(context.args).strip()
    LOGGER.info(
        "Search requested: command=/price query=%r chat_id=%s topic_id=%s",
        raw_query,
        get_chat_id(update),
        format_topic_id(get_message_thread_id(update)),
    )

    if not raw_query:
        await update.message.reply_text(
            "Send a card name after /price.\n"
            "Example: /price lugia 138"
        )
        return

    if len(raw_query) > MAX_QUERY_LENGTH:
        await update.message.reply_text(f"Please keep searches under {MAX_QUERY_LENGTH} characters.")
        return

    price_client = get_price_client(context)

    try:
        response = await price_client.search_cards(raw_query)
    except MalformedQuery:
        await update.message.reply_text(
            "Send a card name, optionally followed by a number or set.\n"
            "Example: lugia 138"
        )
        return
    except NoCardsFound:
        await update.message.reply_text("I could not find a matching Pokemon TCG card.")
        return
    except RateLimited:
        await update.message.reply_text("The pricing API is rate limited right now. Try again in a minute.")
        return
    except PricingAPIError:
        LOGGER.exception("Pricing API error for query: %s", raw_query)
        await update.message.reply_text("The pricing API is having trouble right now. Try again shortly.")
        return

    lookup_id = price_client.store_lookup(response.cards)
    top_card = response.cards[0]
    variant = select_default_variant(top_card, response.parsed.variant_hint)
    keyboard = build_result_keyboard(lookup_id, response.cards, current_index=0, variant_key=variant)

    await send_card_message(update.message, top_card, variant, keyboard)


async def callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    if not is_chat_allowed(update, context):
        await query.answer("This bot is not enabled in this chat.")
        return

    await query.answer()
    if not query.data or not query.message:
        return

    action = parse_callback_data(query.data)
    if not action:
        await query.message.reply_text("That button is no longer valid. Please search again.")
        return

    action_type, lookup_id, card_index, variant_key = action
    price_client = get_price_client(context)
    cards = price_client.get_lookup(lookup_id)
    if not cards:
        await query.message.reply_text("That result expired. Please search again.")
        return

    if card_index < 0 or card_index >= len(cards):
        await query.message.reply_text("That result is no longer available. Please search again.")
        return

    card = cards[card_index]
    if action_type == "card":
        variant_key = select_default_variant(card)
    elif variant_key not in available_variants(card):
        await query.message.reply_text("That price variant is no longer available. Please search again.")
        return

    keyboard = build_result_keyboard(lookup_id, cards, current_index=card_index, variant_key=variant_key)
    await send_card_message(query.message, card, variant_key, keyboard)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.exception("Unhandled bot error", exc_info=context.error)


def get_price_client(context: ContextTypes.DEFAULT_TYPE) -> PricingProvider:
    price_client = context.application.bot_data.get("price_client")
    if not hasattr(price_client, "search_cards"):
        raise RuntimeError("Pricing provider is not configured")
    return cast(PricingProvider, price_client)


def parse_allowed_chat_ids(raw_chat_ids: str) -> frozenset[int]:
    return parse_allowed_ids(raw_chat_ids, "ALLOWED_CHAT_IDS")


def parse_allowed_topic_ids(raw_topic_ids: str) -> frozenset[int]:
    return parse_allowed_ids(raw_topic_ids, "ALLOWED_TOPIC_IDS")


def parse_allowed_ids(raw_ids: str, env_name: str) -> frozenset[int]:
    chat_ids: set[int] = set()
    for raw_chat_id in raw_ids.split(","):
        raw_chat_id = raw_chat_id.strip()
        if not raw_chat_id:
            continue
        try:
            chat_ids.add(int(raw_chat_id))
        except ValueError as exc:
            raise RuntimeError(f"Invalid {env_name} value: {raw_chat_id}") from exc
    return frozenset(chat_ids)


def get_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid {name} value: {raw_value}") from exc


def get_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name, "").strip().lower()
    if not raw_value:
        return default
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"Invalid {name} value: {raw_value}")


def is_chat_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    allowed_chat_ids = context.application.bot_data.get("allowed_chat_ids", frozenset())
    allowed_topic_ids = context.application.bot_data.get("allowed_topic_ids", frozenset())

    if allowed_chat_ids:
        if not update.effective_chat or update.effective_chat.id not in allowed_chat_ids:
            return False

    if allowed_topic_ids and get_message_thread_id(update) not in allowed_topic_ids:
        return False

    return True


def get_message_thread_id(update: Update) -> int | None:
    message = update.effective_message
    if not message:
        return None
    message_thread_id = getattr(message, "message_thread_id", None)
    return int(message_thread_id) if message_thread_id is not None else None


def get_chat_id(update: Update) -> int | None:
    if not update.effective_chat:
        return None
    return int(update.effective_chat.id)


def format_topic_id(topic_id: int | None) -> str:
    return str(topic_id) if topic_id is not None else "none"


def parse_callback_data(data: str) -> tuple[str, str, int, str | None] | None:
    parts = data.split(":")
    if len(parts) < 3:
        return None

    action_type = parts[0]
    lookup_id = parts[1]
    try:
        card_index = int(parts[2])
    except ValueError:
        return None

    if action_type == "card" and len(parts) == 3:
        return action_type, lookup_id, card_index, None
    if action_type == "var" and len(parts) == 4:
        return action_type, lookup_id, card_index, parts[3]
    return None


async def send_card_message(
    message: Any,
    card: dict[str, Any],
    variant_key: str | None,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    text = format_price_message(card, variant_key, compact=True)
    image_url = get_image_url(card)
    fallback_image_url = get_fallback_image_url(card)

    if image_url:
        try:
            await reply_photo(message, image_url, text, keyboard)
            return
        except BadRequest:
            if fallback_image_url:
                LOGGER.warning("Primary image was rejected by Telegram; retrying PNG fallback")
                try:
                    await reply_photo(message, fallback_image_url, text, keyboard)
                    return
                except TelegramError:
                    LOGGER.exception("PNG fallback image also failed; sending text-only result")
            else:
                LOGGER.exception("Image was rejected by Telegram; sending text-only result")
        except TimedOut:
            LOGGER.warning("Timed out sending card image; sending text-only fallback")
        except TelegramError:
            LOGGER.exception("Could not send card image; sending text-only result")

    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def reply_photo(
    message: Any,
    image_url: str,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> None:
    await message.reply_photo(
        photo=image_url,
        caption=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        connect_timeout=15,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=15,
    )


def build_result_keyboard(
    lookup_id: str,
    cards: tuple[dict[str, Any], ...],
    current_index: int,
    variant_key: str | None,
) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    current_card = cards[current_index]
    card_link = get_card_link(current_card)

    if card_link:
        label, url = card_link
        rows.append([InlineKeyboardButton(label, url=url)])

    variant_buttons = [
        InlineKeyboardButton(
            format_variant_button_label(variant),
            callback_data=f"var:{lookup_id}:{current_index}:{variant}",
        )
        for variant in available_variants(current_card)
        if variant != variant_key
    ]
    rows.extend(chunk_buttons(variant_buttons, size=2))

    for index, card in enumerate(cards):
        if index == current_index:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    truncate(
                        f"{index + 1}. {summarize_card(card, include_language=has_mixed_languages(cards))}",
                        max_length=60,
                    ),
                    callback_data=f"card:{lookup_id}:{index}",
                )
            ]
        )

    if not rows:
        return None
    return InlineKeyboardMarkup(rows)


def chunk_buttons(buttons: list[InlineKeyboardButton], size: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[index : index + size] for index in range(0, len(buttons), size)]


def has_mixed_languages(cards: tuple[dict[str, Any], ...]) -> bool:
    return len({str(card.get("language") or "en") for card in cards}) > 1


def format_variant_button_label(variant: str) -> str:
    return {
        "normal": "Normal",
        "holofoil": "Holo",
        "reverseHolofoil": "Reverse",
        "1stEditionNormal": "1st ed",
        "1stEditionHolofoil": "1st holo",
    }.get(variant, variant)


def truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 3].rstrip() + "..."
