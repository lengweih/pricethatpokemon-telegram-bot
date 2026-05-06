import pytest

from bot import (
    build_result_keyboard,
    format_topic_id,
    parse_allowed_chat_ids,
    parse_allowed_topic_ids,
    parse_callback_data,
)
from pricing import TCGdexPricingProvider, format_price_message, get_image_url


CARD = {
    "id": "swsh12-138",
    "name": "Lugia V",
    "number": "138",
    "rarity": "Ultra Rare",
    "set": {"name": "Silver Tempest"},
    "images": {"small": "https://example.test/lugia.png"},
    "price_source_name": "TCGplayer via TCGdex",
    "source_url": "https://api.tcgdex.net/v2/en/cards/swsh12-138",
    "source_link_label": "View TCGdex data",
    "tcgplayer": {
        "updatedAt": "2024-01-01T00:00:00.000Z",
        "prices": {
            "normal": {"low": 1.1, "mid": 2.2, "market": 3.3},
            "holofoil": {"low": 4.4, "mid": 5.5, "market": 6.6},
        },
    },
}


def test_format_price_message_with_prices() -> None:
    message = format_price_message(CARD, "normal")

    assert "<b>Lugia V</b>" in message
    assert "Silver Tempest #138" in message
    assert "Variant: Normal" in message
    assert "Low: $1.10" in message
    assert "Mid: $2.20" in message
    assert "Market: $3.30" in message
    assert "Source: TCGplayer via TCGdex" in message
    assert "Updated: January 1, 2024, 8:00 AM SGT" in message


def test_format_price_message_without_prices() -> None:
    card = {
        "name": "Some Card",
        "number": "1",
        "set": {"name": "Some Set"},
        "tcgplayer": {},
    }

    message = format_price_message(card)

    assert "Prices: unavailable" in message


def test_format_price_message_with_cardmarket_fallback() -> None:
    card = {
        "name": "Lugia V",
        "number": "138",
        "rarity": "Holo Rare V",
        "set": {"name": "Silver Tempest"},
        "price_source_name": "Cardmarket via TCGdex",
        "prices": {
            "source_name": "Cardmarket via TCGdex",
            "updatedAt": "2026-05-06T00:42:15.000Z",
            "unit": "EUR",
            "labels": {"low": "Low", "mid": "Avg", "market": "Trend"},
            "variants": {"normal": {"low": 4.99, "mid": 10.59, "market": 16.73}},
        },
    }

    message = format_price_message(card, "normal")

    assert "Source: Cardmarket via TCGdex" in message
    assert "Low: €4.99" in message
    assert "Avg: €10.59" in message
    assert "Trend: €16.73" in message
    assert "Updated: May 6, 2026, 8:42 AM SGT" in message


def test_format_price_message_compact() -> None:
    card = {
        "name": "Lugia V",
        "number": "138",
        "rarity": "Holo Rare V",
        "set": {"name": "Silver Tempest"},
        "price_source_name": "Cardmarket via TCGdex",
        "prices": {
            "updatedAt": "2026-05-06T00:42:15.000Z",
            "unit": "EUR",
            "labels": {"low": "Low", "mid": "Avg", "market": "Trend"},
            "variants": {"holofoil": {"low": 4.99, "mid": 10.59, "market": 16.73}},
        },
    }

    message = format_price_message(card, "holofoil", compact=True)

    assert message == (
        "<b>Lugia V</b> - Silver Tempest #138\n"
        "Holofoil - Holo Rare V\n"
        "Low: €4.99 | Avg: €10.59 | Trend: €16.73\n"
        "Source: Cardmarket\n"
        "Updated: May 6, 2026, 8:42 AM SGT"
    )


def test_format_price_message_compact_with_converted_currency() -> None:
    card = {
        "name": "Lugia V",
        "number": "138",
        "rarity": "Holo Rare V",
        "set": {"name": "Silver Tempest"},
        "price_source_name": "Cardmarket via TCGdex",
        "prices": {
            "updatedAt": "2026-05-06T00:42:15.000Z",
            "unit": "SGD",
            "sourceUnit": "EUR",
            "labels": {"low": "Low", "mid": "Avg", "market": "Trend"},
            "variants": {"holofoil": {"low": 7.45, "mid": 15.8, "market": 24.97}},
        },
    }

    message = format_price_message(card, "holofoil", compact=True)

    assert "Low: S$7.45 | Avg: S$15.80 | Trend: S$24.97" in message
    assert "Source: Cardmarket (EUR-&gt;SGD)" in message
    assert "Updated: May 6, 2026, 8:42 AM SGT" in message


def test_get_image_url_returns_none_when_missing() -> None:
    assert get_image_url({"images": {}}) is None


def test_parse_callback_data_for_card() -> None:
    assert parse_callback_data("card:abc123:2") == ("card", "abc123", 2, None)


def test_parse_callback_data_for_variant() -> None:
    assert parse_callback_data("var:abc123:0:holofoil") == ("var", "abc123", 0, "holofoil")


def test_parse_allowed_chat_ids_allows_empty_value() -> None:
    assert parse_allowed_chat_ids("") == frozenset()


def test_parse_allowed_chat_ids_accepts_negative_group_ids() -> None:
    assert parse_allowed_chat_ids("-1001234567890, 12345") == frozenset({-1001234567890, 12345})


def test_parse_allowed_chat_ids_rejects_invalid_values() -> None:
    with pytest.raises(RuntimeError):
        parse_allowed_chat_ids("-100123,nope")


def test_parse_allowed_topic_ids_accepts_topic_ids() -> None:
    assert parse_allowed_topic_ids("123,456") == frozenset({123, 456})


def test_parse_allowed_topic_ids_rejects_invalid_values() -> None:
    with pytest.raises(RuntimeError):
        parse_allowed_topic_ids("topic-abc")


def test_format_topic_id_handles_missing_topic() -> None:
    assert format_topic_id(None) == "none"
    assert format_topic_id(42) == "42"


def test_expired_lookup_cache_returns_none() -> None:
    client = TCGdexPricingProvider(cache_ttl_seconds=1)

    assert client.get_lookup("missing") is None


def test_build_result_keyboard_includes_variant_and_alternative_buttons() -> None:
    other_card = {
        "id": "base1-4",
        "name": "Charizard",
        "number": "4",
        "set": {"name": "Base"},
        "tcgplayer": {"prices": {"normal": {"market": 1}}},
    }

    keyboard = build_result_keyboard("abc123", (CARD, other_card), 0, "normal")

    assert keyboard is not None
    button_texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "View TCGdex data" in button_texts
    assert "Holo" in button_texts
    assert any("Charizard" in text for text in button_texts)
