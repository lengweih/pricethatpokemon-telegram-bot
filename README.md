# Pokemon Card Price Telegram Bot

An async Telegram bot for quick Pokemon TCG card price lookups from a dedicated group topic.

The bot is command-only so Telegram privacy mode can stay enabled. Search with `/price` or the shorter `/p`:

```text
/p lugia 138
/p charizard 4/102
/p mew paldean fates
/p n's pp up 153
```

## Features

- Searches Pokemon cards through the free public TCGdex REST API.
- Shows the best match with a card image when TCGdex provides one.
- Supports multi-word card names, smart apostrophes, card numbers, and best-effort set-name hints.
- Shows a compact price caption with variant, rarity, prices, source, and Singapore-time update date.
- Prefers TCGplayer pricing from TCGdex when available.
- Falls back to Cardmarket pricing from TCGdex when TCGplayer pricing is missing.
- Converts displayed prices to `DISPLAY_CURRENCY`, defaulting to SGD through Frankfurter exchange rates.
- Provides inline buttons for alternate card matches, alternate price variants, and the TCGdex source link.
- Uses in-memory TTL caches for repeated searches, callback payloads, set metadata, and exchange rates.
- Runs locally with Telegram polling and in production with a FastAPI webhook on Vercel.

PSA and graded prices are intentionally not included because TCGdex does not provide graded-market pricing.

## How Search Works

The parser is intentionally simple:

- A token like `138`, `138/195`, or `TG09/TG30` is treated as the card number.
- The remaining text is treated as the card name plus optional trailing set hint.
- If the trailing words match a TCGdex set name, they are sent as `set.name`.
- Multi-word names are searched first as typed, then with punctuation stripped, then with a wider first-token fallback.
- Broad fallbacks are locally filtered by the full card-name tokens, which prevents unrelated same-number alternatives like `Necrozma GX #153` when searching `n's pp up 153`.

Set matching is best effort. If TCGdex set lookup fails, the bot still searches by card name and number.

## Project Structure

```text
app.py                # FastAPI app for Vercel webhook + /health
bot.py                # Telegram handlers, command restrictions, inline keyboard behavior
pricing.py            # Provider interface, TCGdex client, parsing, ranking, formatting
local_polling.py      # Local polling runner for development
requirements.txt
.env.example
tests/
```

## Environment

Copy the template:

```bash
cp .env.example .env
```

Fill in:

```env
TELEGRAM_BOT_TOKEN=
ALLOWED_CHAT_IDS=
ALLOWED_TOPIC_IDS=
PRICE_PROVIDER=tcgdex
DISPLAY_CURRENCY=SGD
EXCHANGE_API_BASE=https://api.frankfurter.dev
TCGDEX_API_BASE=https://api.tcgdex.net/v2/en
TCGDEX_IMAGE_QUALITY=low
TCGDEX_IMAGE_EXTENSION=webp
WEBHOOK_SECRET_PATH=telegram/YOUR_RANDOM_PATH
WEBHOOK_SECRET_TOKEN=YOUR_RANDOM_HEADER_SECRET
CACHE_TTL_SECONDS=3600
MAX_RESULTS=5
TCGDEX_CANDIDATE_LIMIT=5
```

Notes:

- `TELEGRAM_BOT_TOKEN` comes from BotFather.
- TCGdex does not require an API key for this MVP.
- `DISPLAY_CURRENCY=SGD` converts USD/EUR source pricing to Singapore dollars when an exchange rate is available.
- `MAX_RESULTS` controls how many card results can be shown.
- `TCGDEX_CANDIDATE_LIMIT` controls how many detailed card records are fetched after the initial search. During local testing, set both values to `1` to keep API calls low.

## Telegram Setup

1. Create a bot with BotFather and save `TELEGRAM_BOT_TOKEN`.
2. In BotFather, run `/setprivacy` and keep privacy mode enabled.
3. Start the bot locally or deploy it.
4. Send `/chatid` in the exact group topic where the bot should work.
5. Put the returned IDs into `.env`:

```env
ALLOWED_CHAT_IDS=-1001234567890
ALLOWED_TOPIC_IDS=12345
```

Telegram forum topics share the same `Chat ID`; each topic has its own `Topic ID` / `message_thread_id`.
Multiple IDs can be comma-separated. Leave `ALLOWED_CHAT_IDS` empty to allow every chat, and leave `ALLOWED_TOPIC_IDS` empty to allow every topic.

## Local Development

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run with polling:

```bash
python local_polling.py
```

Local polling calls Telegram's `getUpdates` endpoint repeatedly. That is normal for local development and does not involve Vercel. The runner drops old pending updates on startup and suppresses low-level HTTP logs so your bot token is not printed in every request URL.

Allowed searches are still logged by the app, including query, chat ID, and topic ID.

## Vercel Deployment

Production uses Telegram webhooks, not polling. Vercel is only invoked when Telegram sends an update to your webhook URL. With privacy mode enabled and command-only routing, normal group messages should not create meaningful bot work.

1. Push this repo to GitHub.
2. Import the repo into Vercel.
3. Add the environment variables from `.env.example` in Vercel Project Settings.
4. Deploy.
5. Register the webhook:

```bash
curl -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://YOUR_VERCEL_DOMAIN/$WEBHOOK_SECRET_PATH" \
  -d "secret_token=$WEBHOOK_SECRET_TOKEN" \
  -d 'allowed_updates=["message","callback_query"]'
```

Test in Telegram:

```text
/p lugia 138
/p charizard 4/102
/p mew paldean fates
/p rare candy
/p n's pp up 153
```

## Tests

```bash
pytest
```

The suite covers parsing, multi-word search fallback, set matching, ranking, variant selection, price normalization, formatting, webhook filtering, and callback cache behavior.

## Provider Notes

- Current provider: `PRICE_PROVIDER=tcgdex`.
- Price source: TCGplayer via TCGdex when available, otherwise Cardmarket via TCGdex.
- Currency: source prices are USD or EUR, then converted to `DISPLAY_CURRENCY` when possible.
- Images: TCGdex `low.webp` assets by default, with a PNG fallback if Telegram rejects the primary image.
- Cache: in-memory TTL cache. On Vercel this is best-effort per warm function instance.
- Provider switching: `pricing.py` keeps provider creation and normalized card output behind a small interface, so another source can be added later without rewriting Telegram handlers.
- PriceCharting is not used because graded pricing requires a paid API for a clean implementation.

Useful TCGdex docs:

- REST card search: https://tcgdex.dev/rest/cards
- Card object and pricing fields: https://tcgdex.dev/reference/card
- Market pricing integration: https://tcgdex.dev/markets-prices
- Image URL format: https://tcgdex.dev/assets
