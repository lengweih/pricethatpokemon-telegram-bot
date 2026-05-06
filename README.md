# Pokemon Card Price Telegram Bot

A small async Telegram bot that looks up Pokemon TCG card prices from Telegram commands.

The MVP is optimized for a dedicated Telegram group topic. Users search with `/price`, such as `/price lugia 138`, `/price charizard 4/102`, or `/price lugia silver tempest`.

## What It Does

- Searches Pokemon TCG cards through TCGdex.
- Shows the best match with a card image when available.
- Displays TCGplayer USD low, mid, and market prices from TCGdex card responses when available.
- Falls back to Cardmarket EUR low, average, and trend prices when TCGplayer data is missing.
- Shows a compact price summary with source and date when available.
- Provides inline buttons for alternate card matches and alternate price variants.
- Uses best-effort in-memory TTL caching to reduce repeated API calls.
- Supports Vercel FastAPI webhooks for production and local polling for development.

PSA and graded prices are intentionally omitted in the MVP because TCGdex provides raw marketplace pricing, not graded pricing.

## Project Structure

```text
app.py                # FastAPI app for Vercel webhook + /health
bot.py                # Telegram handlers and inline keyboard behavior
pricing.py            # Provider interface, TCGdex client, parsing, ranking, formatting
local_polling.py      # Local polling runner
requirements.txt
.env.example
tests/
```

## Setup

Create a bot with BotFather. TCGdex does not require an API key for this MVP.

In BotFather, run `/setprivacy` for this bot and keep privacy mode enabled. The bot is command-only, so it does not need to receive every normal group/topic message.

Copy the environment template:

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

Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run locally with polling:

```bash
python local_polling.py
```

Local polling drops old pending Telegram updates on startup, so it should only answer new messages sent after the process starts.
It also suppresses low-level HTTP request logs so your bot token is not printed in every Telegram API URL.
Allowed searches are still logged by the app, including the query, chat ID, and topic ID.

For group chats, keep BotFather privacy mode enabled and use `/price ...` commands. This prevents Telegram from sending every plain text topic message to the bot.

To restrict the bot to one Telegram chat or topic:

1. Start the bot locally or deploy it.
2. Send `/chatid` in the group topic you want to allow.
3. Copy the returned IDs into `.env`:

```env
ALLOWED_CHAT_IDS=-1001234567890
ALLOWED_TOPIC_IDS=12345
```

Telegram forum topics share the same `Chat ID`; the `Topic ID` is the per-topic `message_thread_id`. Multiple chats or topics can be comma-separated. Leave `ALLOWED_CHAT_IDS` empty to allow all chats, and leave `ALLOWED_TOPIC_IDS` empty to allow all topics.

## Vercel Deployment

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
/price lugia 138
/price charizard 4/102
/price lugia silver tempest
/price rare candy
```

## Tests

```bash
pytest
```

The tests cover parsing, query construction, ranking, default variant selection, price formatting, and callback expiration behavior.

## Notes

- Data source: TCGdex REST API.
- Price source shown to users: TCGplayer via TCGdex when available, otherwise Cardmarket via TCGdex.
- Currency: TCGdex prices are native USD or EUR, then converted to `DISPLAY_CURRENCY=SGD` using Frankfurter exchange rates when conversion is available.
- Images: TCGdex `low.webp` assets by default, with a PNG fallback if Telegram rejects the primary image.
- Cache: in-memory TTL cache. On Vercel this is best-effort per warm function instance.
- Local API-call control: `MAX_RESULTS=1` and `TCGDEX_CANDIDATE_LIMIT=1` will fetch/show only one candidate per search, which is useful while testing.
- Command-only mode: lookups are handled only by `/price`; non-command text is ignored, and the Vercel webhook returns before app initialization for non-command messages.
- Provider switching: `PRICE_PROVIDER=tcgdex` is the only implemented provider today, but `pricing.py` isolates provider creation and response normalization so another provider can be added behind the same bot interface.
- PriceCharting is not used because PSA/graded pricing requires a paid API for a clean implementation.

Useful TCGdex docs:

- REST card search: https://tcgdex.dev/rest/cards
- Card object and pricing fields: https://tcgdex.dev/reference/card
- Market pricing integration: https://tcgdex.dev/markets-prices
- Image URL format: https://tcgdex.dev/assets
