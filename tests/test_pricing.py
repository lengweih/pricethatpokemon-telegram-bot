import asyncio

import pytest

from pricing import (
    MalformedQuery,
    NoCardsFound,
    PricingAPIError,
    RateLimited,
    TCGdexPricingProvider,
    available_variants,
    build_api_query,
    build_image_payload,
    build_tcgdex_image_url,
    build_card_name_search,
    build_card_name_searches,
    choose_tcgdex_price_package,
    extract_frankfurter_rate,
    filter_brief_cards_by_query_name,
    format_updated_at,
    build_japanese_search_spec,
    japanese_number_variants,
    match_pokemon_name_prefix,
    match_set_from_tokens,
    normalize_tcgdex_cardmarket_prices,
    normalize_tcgdex_tcgplayer_prices,
    parse_query,
    rank_cards,
    select_default_variant,
)


def test_parse_query_extracts_card_number() -> None:
    parsed = parse_query("lugia 138/195")

    assert parsed.is_valid
    assert parsed.name_anchor == "lugia"
    assert parsed.card_number == "138"
    assert parsed.set_hints == ()


def test_parse_query_keeps_set_terms_as_hints() -> None:
    parsed = parse_query("lugia silver tempest")

    assert parsed.is_valid
    assert parsed.name_anchor == "lugia"
    assert parsed.card_number is None
    assert parsed.set_hints == ("silver", "tempest")


def test_parse_query_detects_variant_hint() -> None:
    parsed = parse_query("charizard reverse holo 4/102")

    assert parsed.variant_hint == "reverseHolofoil"
    assert parsed.card_number == "4"
    assert parsed.name_anchor == "charizard"


def test_build_api_query_uses_name_anchor_and_number() -> None:
    parsed = parse_query("lugia 138/195")

    assert build_api_query(parsed) == "name=lugia*&localId=138"


def test_build_card_name_search_preserves_apostrophes_and_multiword_name() -> None:
    parsed = parse_query("n's pp up 153")

    assert build_card_name_search(parsed) == "n's pp up"


def test_build_card_name_search_normalizes_curly_apostrophe() -> None:
    parsed = parse_query("n’s pp up 153")

    assert parsed.normalized == "n's pp up 153"
    assert build_card_name_search(parsed) == "n's pp up"
    assert build_card_name_searches(parsed) == ("n's pp up", "n s pp up", "n")


def test_japanese_number_variants_pad_plain_numbers() -> None:
    assert japanese_number_variants("63") == ("063", "63")
    assert japanese_number_variants("063") == ("063", "63")
    assert japanese_number_variants("TG09") == ("TG09",)


def test_match_pokemon_name_prefix_uses_longest_alias() -> None:
    match = match_pokemon_name_prefix(parse_query("mr mime 63"))

    assert match is not None
    assert match.english == "Mr. Mime"
    assert match.japanese == "バリヤード"
    assert match.matched_terms == 2


def test_build_japanese_search_spec_translates_pokemon_name() -> None:
    search_spec = build_japanese_search_spec(parse_query("snorunt 63"))

    assert search_spec is not None
    assert search_spec.names == ("ユキワラシ",)
    assert search_spec.number_variants == ("063", "63")
    assert search_spec.dex_id == 361


def test_build_japanese_search_spec_accepts_direct_japanese_name() -> None:
    search_spec = build_japanese_search_spec(parse_query("ユキワラシ 63"))

    assert search_spec is not None
    assert search_spec.names == ("ユキワラシ",)
    assert search_spec.number_variants == ("063", "63")


def test_filter_brief_cards_by_query_name_removes_unrelated_number_matches() -> None:
    parsed = parse_query("n's pp up 153")
    cards = [
        {"id": "sv09-153", "name": "N's PP Up", "localId": "153"},
        {"id": "sm8-153", "name": "Necrozma GX", "localId": "153"},
        {"id": "sv03-153", "name": "Noivern ex", "localId": "153"},
    ]

    filtered_cards = filter_brief_cards_by_query_name(cards, parsed)

    assert [card["name"] for card in filtered_cards] == ["N's PP Up"]


def test_build_api_query_rejects_malformed_query() -> None:
    parsed = parse_query("138/195")

    with pytest.raises(MalformedQuery):
        build_api_query(parsed)


def test_rank_cards_prioritizes_exact_number_match() -> None:
    parsed = parse_query("lugia 138")
    cards = [
        {
            "id": "wrong",
            "name": "Lugia V",
            "number": "139",
            "set": {"name": "Silver Tempest"},
            "images": {"small": "https://example.test/wrong.png"},
            "tcgplayer": {"prices": {"normal": {"market": 1}}},
        },
        {
            "id": "right",
            "name": "Lugia V",
            "number": "138",
            "set": {"name": "Silver Tempest"},
            "images": {"small": "https://example.test/right.png"},
            "tcgplayer": {"prices": {"normal": {"market": 10}}},
        },
    ]

    ranked = rank_cards(cards, parsed)

    assert ranked[0]["id"] == "right"


def test_select_default_variant_uses_api_natural_order() -> None:
    card = {
        "tcgplayer": {
            "prices": {
                "reverseHolofoil": {"market": 2},
                "holofoil": {"market": 3},
                "normal": {"market": 1},
            }
        }
    }

    assert available_variants(card) == ["normal", "holofoil", "reverseHolofoil"]
    assert select_default_variant(card) == "normal"


def test_select_default_variant_honors_available_hint() -> None:
    card = {
        "tcgplayer": {
            "prices": {
                "normal": {"market": 1},
                "reverseHolofoil": {"market": 2},
            }
        }
    }

    assert select_default_variant(card, "reverseHolofoil") == "reverseHolofoil"


def test_search_cards_handles_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(enable_japanese_search=False)

    class FakeHTTP:
        async def get(self, path, params=None):
            return type("Response", (), {"status_code": 429})()

    monkeypatch.setattr(client, "_get_client", lambda: FakeHTTP())

    with pytest.raises(RateLimited):
        asyncio.run(client.search_cards("lugia 138"))


def test_search_cards_handles_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(enable_japanese_search=False)

    class FakeResponse:
        status_code = 200

        def json(self):
            return []

    class FakeHTTP:
        async def get(self, path, params=None):
            return FakeResponse()

    monkeypatch.setattr(client, "_get_client", lambda: FakeHTTP())

    with pytest.raises(NoCardsFound):
        asyncio.run(client.search_cards("lugia 138"))


def test_search_cards_handles_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(enable_japanese_search=False)

    class FakeResponse:
        status_code = 200

        def json(self):
            raise ValueError("bad json")

    class FakeHTTP:
        async def get(self, path, params=None):
            return FakeResponse()

    monkeypatch.setattr(client, "_get_client", lambda: FakeHTTP())

    with pytest.raises(PricingAPIError):
        asyncio.run(client.search_cards("lugia 138"))


def test_build_tcgdex_image_url_appends_quality_and_extension() -> None:
    assert (
        build_tcgdex_image_url("https://assets.tcgdex.net/en/swsh/swsh12/138")
        == "https://assets.tcgdex.net/en/swsh/swsh12/138/low.png"
    )


def test_build_image_payload_keeps_png_fallback() -> None:
    payload = build_image_payload(
        "https://assets.tcgdex.net/en/swsh/swsh12/138/low.webp",
        "https://assets.tcgdex.net/en/swsh/swsh12/138/low.png",
    )

    assert payload["small"].endswith("/low.webp")
    assert payload["fallback"].endswith("/low.png")


def test_match_set_from_tokens_uses_longest_trailing_set_phrase() -> None:
    sets = (
        {"id": "sv04.5", "name": "Paldean Fates"},
        {"id": "si1", "name": "Southern Islands"},
    )

    match = match_set_from_tokens(("ex", "paldean", "fates"), sets)

    assert match is not None
    assert match.id == "sv04.5"
    assert match.name == "Paldean Fates"
    assert match.matched_tokens == ("paldean", "fates")


def test_format_updated_at_uses_human_readable_singapore_time() -> None:
    assert format_updated_at("2026-05-06T00:42:15.000Z") == "May 6, 2026, 8:42 AM SGT"


def test_extract_frankfurter_rate_supports_v2_shape() -> None:
    payload = [{"date": "2026-05-06", "base": "EUR", "quote": "SGD", "rate": 1.4928}]

    assert extract_frankfurter_rate(payload, "EUR", "SGD") == 1.4928


def test_normalize_tcgdex_tcgplayer_prices_maps_variants() -> None:
    pricing = normalize_tcgdex_tcgplayer_prices(
        {
            "updated": "2025-08-05T20:07:54.000Z",
            "unit": "USD",
            "normal": {"lowPrice": 1, "midPrice": 2, "marketPrice": 3},
            "reverse": {"lowPrice": 4, "midPrice": 5, "marketPrice": 6},
        }
    )

    assert pricing["updatedAt"] == "2025-08-05T20:07:54.000Z"
    assert pricing["unit"] == "USD"
    assert pricing["prices"]["normal"]["market"] == 3
    assert pricing["prices"]["reverseHolofoil"]["mid"] == 5


def test_normalize_tcgdex_cardmarket_prices_maps_eur_fallback() -> None:
    pricing = normalize_tcgdex_cardmarket_prices(
        {
            "updated": "2026-05-06T00:42:15.000Z",
            "unit": "EUR",
            "avg": 10.59,
            "low": 4.99,
            "trend": 16.73,
        }
    )
    package = choose_tcgdex_price_package({"prices": {}}, pricing)

    assert pricing["prices"]["normal"]["low"] == 4.99
    assert pricing["prices"]["normal"]["mid"] == 10.59
    assert pricing["prices"]["normal"]["market"] == 16.73
    assert package["source_name"] == "Cardmarket via TCGdex"
    assert package["unit"] == "EUR"


def test_normalize_tcgdex_cardmarket_prices_uses_holo_for_holo_only_card() -> None:
    pricing = normalize_tcgdex_cardmarket_prices(
        {
            "updated": "2026-05-06T00:42:15.000Z",
            "unit": "EUR",
            "avg": 10.59,
            "low": 4.99,
            "trend": 16.73,
        },
        {"normal": False, "holo": True, "reverse": False},
    )

    assert "normal" not in pricing["prices"]
    assert pricing["prices"]["holofoil"]["market"] == 16.73


def test_search_cards_fetches_details_and_normalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(display_currency="USD", enable_japanese_search=False)

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeHTTP:
        async def get(self, path, params=None):
            if path == "/cards":
                assert params["name"] == "lugia*"
                assert params["localId"] == "138"
                return FakeResponse(
                    [
                        {
                            "id": "swsh12-138",
                            "localId": "138",
                            "name": "Lugia V",
                            "image": "https://assets.tcgdex.net/en/swsh/swsh12/138",
                        }
                    ]
                )
            return FakeResponse(
                {
                    "id": "swsh12-138",
                    "localId": "138",
                    "name": "Lugia V",
                    "rarity": "Ultra Rare",
                    "set": {"id": "swsh12", "name": "Silver Tempest"},
                    "image": "https://assets.tcgdex.net/en/swsh/swsh12/138",
                    "pricing": {
                        "tcgplayer": {
                            "updated": "2025-08-05T20:07:54.000Z",
                            "unit": "USD",
                            "normal": {"lowPrice": 1.1, "midPrice": 2.2, "marketPrice": 3.3},
                        }
                    },
                }
            )

    monkeypatch.setattr(client, "_get_client", lambda: FakeHTTP())

    response = asyncio.run(client.search_cards("lugia 138"))

    assert response.cards[0]["name"] == "Lugia V"
    assert response.cards[0]["number"] == "138"
    assert response.cards[0]["set"]["name"] == "Silver Tempest"
    assert response.cards[0]["images"]["small"].endswith("/low.webp")
    assert response.cards[0]["images"]["fallback"].endswith("/low.png")
    assert response.cards[0]["price_source_name"] == "TCGplayer via TCGdex"
    assert response.cards[0]["tcgplayer"]["prices"]["normal"]["market"] == 3.3


def test_search_cards_merges_english_and_japanese_exact_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(max_results=2, candidate_limit=2, display_currency="USD")

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeHTTP:
        def __init__(self, language="en"):
            self.language = language

        async def get(self, path, params=None):
            if path == "/cards" and self.language == "en":
                assert params["name"] == "lugia*"
                assert params["localId"] == "138"
                return FakeResponse([{"id": "swsh12-138", "localId": "138", "name": "Lugia V"}])
            if path == "/cards" and self.language == "ja":
                assert params["name"] == "ルギア*"
                assert params["localId"] == "138"
                return FakeResponse([{"id": "S11-138", "localId": "138", "name": "ルギア"}])
            if path == "/cards/swsh12-138":
                return FakeResponse(
                    {
                        "id": "swsh12-138",
                        "localId": "138",
                        "name": "Lugia V",
                        "set": {"id": "swsh12", "name": "Silver Tempest"},
                        "pricing": {"tcgplayer": {"unit": "USD", "normal": {"marketPrice": 1}}},
                    }
                )
            return FakeResponse(
                {
                    "id": "S11-138",
                    "localId": "138",
                    "name": "ルギア",
                    "dexId": [249],
                    "set": {"id": "S11", "name": "白熱のアルカナ"},
                    "pricing": {"cardmarket": {"unit": "EUR", "trend": 1}},
                }
            )

    monkeypatch.setattr(client, "_get_client", lambda language="en": FakeHTTP(language))

    response = asyncio.run(client.search_cards("lugia 138"))

    assert [card["language"] for card in response.cards] == ["en", "ja"]
    assert response.cards[1]["source_url"] == "https://api.tcgdex.net/v2/ja/cards/S11-138"


def test_search_cards_prefers_japanese_exact_over_loose_english(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(max_results=2, candidate_limit=2, display_currency="USD")

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeHTTP:
        def __init__(self, language="en"):
            self.language = language

        async def get(self, path, params=None):
            if path == "/cards" and self.language == "en" and params.get("localId") == "63":
                return FakeResponse([])
            if path == "/cards" and self.language == "en":
                return FakeResponse([{"id": "bw9-21", "localId": "21", "name": "Snorunt"}])
            if path == "/cards" and self.language == "ja":
                if params["name"] == "ユキワラシ*" and params["localId"] == "063":
                    return FakeResponse([{"id": "SV3a-063", "localId": "063", "name": "ユキワラシ"}])
                return FakeResponse([])
            if path == "/cards/bw9-21":
                return FakeResponse(
                    {
                        "id": "bw9-21",
                        "localId": "21",
                        "name": "Snorunt",
                        "set": {"id": "bw9", "name": "Plasma Blast"},
                    }
                )
            return FakeResponse(
                {
                    "id": "SV3a-063",
                    "localId": "063",
                    "name": "ユキワラシ",
                    "dexId": [361],
                    "set": {"id": "SV3a", "name": "レイジングサーフ"},
                }
            )

    monkeypatch.setattr(client, "_get_client", lambda language="en": FakeHTTP(language))

    response = asyncio.run(client.search_cards("snorunt 63"))

    assert response.cards[0]["id"] == "SV3a-063"
    assert response.cards[0]["language"] == "ja"
    assert response.cards[1]["id"] == "bw9-21"


def test_search_cards_keeps_direct_japanese_match_without_dex_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(max_results=3, candidate_limit=3, display_currency="USD")

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeHTTP:
        def __init__(self, language="en"):
            self.language = language

        async def get(self, path, params=None):
            if path == "/cards" and self.language == "en":
                assert params["name"] == "lugia*"
                assert params["localId"] == "79"
                return FakeResponse([{"id": "xy10-79", "localId": "79", "name": "Lugia BREAK"}])
            if path == "/cards" and self.language == "ja":
                assert params["name"] == "ルギア*"
                assert params["localId"] == "079"
                return FakeResponse([{"id": "S12-079", "localId": "079", "name": "ルギアV"}])
            if path == "/cards/xy10-79":
                return FakeResponse(
                    {
                        "id": "xy10-79",
                        "localId": "79",
                        "name": "Lugia BREAK",
                        "set": {"id": "xy10", "name": "Fates Collide"},
                    }
                )
            return FakeResponse(
                {
                    "id": "S12-079",
                    "localId": "079",
                    "name": "ルギアV",
                    "dexId": None,
                    "set": {"id": "S12", "name": "パラダイムトリガー"},
                }
            )

    monkeypatch.setattr(client, "_get_client", lambda language="en": FakeHTTP(language))

    response = asyncio.run(client.search_cards("lugia 79"))

    assert [card["id"] for card in response.cards] == ["xy10-79", "S12-079"]
    assert response.cards[1]["language"] == "ja"


def test_japanese_broad_fallback_filters_by_dex_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(max_results=2, candidate_limit=2, display_currency="USD")

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeHTTP:
        def __init__(self, language="en"):
            self.language = language

        async def get(self, path, params=None):
            if self.language == "en" and path == "/cards":
                return FakeResponse([])
            if self.language == "ja" and path == "/cards" and params.get("name"):
                return FakeResponse([])
            if self.language == "ja" and path == "/cards":
                assert params["localId"] == "063"
                return FakeResponse(
                    [
                        {"id": "SV7-063", "localId": "063", "name": "ゴロンダ"},
                        {"id": "SV3a-063", "localId": "063", "name": "ユキワラシ"},
                    ]
                )
            if path == "/cards/SV7-063":
                return FakeResponse({"id": "SV7-063", "localId": "063", "name": "ゴロンダ", "dexId": [675]})
            return FakeResponse({"id": "SV3a-063", "localId": "063", "name": "ユキワラシ", "dexId": [361]})

    monkeypatch.setattr(client, "_get_client", lambda language="en": FakeHTTP(language))

    response = asyncio.run(client.search_cards("snorunt 63"))

    assert [card["id"] for card in response.cards] == ["SV3a-063"]


def test_search_cards_filters_by_detected_set_name(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(max_results=1, candidate_limit=1, enable_japanese_search=False)

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeHTTP:
        async def get(self, path, params=None):
            if path == "/sets":
                return FakeResponse([{"id": "sv04.5", "name": "Paldean Fates"}])
            if path == "/cards":
                assert params["name"] == "mew*"
                assert params["set.name"] == "Paldean Fates"
                assert params["pagination:itemsPerPage"] == "1"
                return FakeResponse(
                    [
                        {
                            "id": "sv04.5-216",
                            "localId": "216",
                            "name": "Mew ex",
                            "image": "https://assets.tcgdex.net/en/sv/sv04.5/216",
                        }
                    ]
                )
            return FakeResponse(
                {
                    "id": "sv04.5-216",
                    "localId": "216",
                    "name": "Mew ex",
                    "rarity": "Double Rare",
                    "set": {"id": "sv04.5", "name": "Paldean Fates"},
                    "image": "https://assets.tcgdex.net/en/sv/sv04.5/216",
                }
            )

    monkeypatch.setattr(client, "_get_client", lambda: FakeHTTP())

    response = asyncio.run(client.search_cards("mew paldean fates"))

    assert response.cards[0]["set"]["name"] == "Paldean Fates"


def test_search_cards_uses_full_card_name_for_numbered_alternatives(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(max_results=3, candidate_limit=3, display_currency="USD", enable_japanese_search=False)
    searches: list[str] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeHTTP:
        async def get(self, path, params=None):
            if path == "/sets":
                return FakeResponse([])
            if path == "/cards":
                searches.append(params["name"])
                assert params["localId"] == "153"
                if params["name"] == "n's pp up*":
                    assert params["pagination:itemsPerPage"] == "3"
                    return FakeResponse([])
                if params["name"] == "n s pp up*":
                    assert params["pagination:itemsPerPage"] == "3"
                    return FakeResponse([])
                assert params["pagination:itemsPerPage"] == "25"
                return FakeResponse(
                    [
                        {"id": "sv09-153", "localId": "153", "name": "N's PP Up"},
                        {"id": "sm8-153", "localId": "153", "name": "Necrozma GX"},
                        {"id": "sv03-153", "localId": "153", "name": "Noivern ex"},
                    ]
                )
            return FakeResponse(
                {
                    "id": "sv09-153",
                    "localId": "153",
                    "name": "N's PP Up",
                    "rarity": "Uncommon",
                    "set": {"id": "sv09", "name": "Journey Together"},
                }
            )

    monkeypatch.setattr(client, "_get_client", lambda: FakeHTTP())

    response = asyncio.run(client.search_cards("n's pp up 153"))

    assert [card["name"] for card in response.cards] == ["N's PP Up"]
    assert searches == ["n's pp up*", "n s pp up*", "n*"]


def test_search_cards_continues_when_set_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(max_results=1, candidate_limit=1, display_currency="USD", enable_japanese_search=False)

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeHTTP:
        async def get(self, path, params=None):
            if path == "/sets":
                return type("Response", (), {"status_code": 500})()
            if path == "/cards":
                assert params["name"] == "n's pp up*"
                return FakeResponse([{"id": "sv09-153", "localId": "153", "name": "N's PP Up"}])
            return FakeResponse(
                {
                    "id": "sv09-153",
                    "localId": "153",
                    "name": "N's PP Up",
                    "set": {"id": "sv09", "name": "Journey Together"},
                }
            )

    monkeypatch.setattr(client, "_get_client", lambda: FakeHTTP())

    response = asyncio.run(client.search_cards("n's pp up 153"))

    assert response.cards[0]["name"] == "N's PP Up"


def test_normalize_converts_prices_to_display_currency(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(display_currency="SGD")

    async def fake_exchange_rate(source_unit, target_unit):
        assert source_unit == "EUR"
        assert target_unit == "SGD"
        return 1.5

    monkeypatch.setattr(client, "_get_exchange_rate", fake_exchange_rate)

    card = asyncio.run(
        client._normalize_card(
            {
                "id": "sv04.5-216",
                "localId": "216",
                "name": "Mew ex",
                "set": {"name": "Paldean Fates"},
                "pricing": {
                    "cardmarket": {
                        "updated": "2026-05-06T00:42:15.000Z",
                        "unit": "EUR",
                        "low": 4,
                        "avg": 10,
                        "trend": 20,
                    }
                },
            }
        )
    )

    assert card["prices"]["unit"] == "SGD"
    assert card["prices"]["sourceUnit"] == "EUR"
    assert card["prices"]["variants"]["normal"]["low"] == 6
    assert card["prices"]["variants"]["normal"]["mid"] == 15
    assert card["prices"]["variants"]["normal"]["market"] == 30


def test_search_cards_respects_candidate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TCGdexPricingProvider(max_results=1, candidate_limit=1, enable_japanese_search=False)
    detail_paths = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeHTTP:
        async def get(self, path, params=None):
            if path == "/cards":
                assert params["pagination:itemsPerPage"] == "1"
                return FakeResponse(
                    [
                        {"id": "swsh12-138", "localId": "138", "name": "Lugia V"},
                        {"id": "swsh12-139", "localId": "139", "name": "Lugia VSTAR"},
                    ]
                )
            detail_paths.append(path)
            return FakeResponse(
                {
                    "id": path.rsplit("/", 1)[-1],
                    "localId": "138",
                    "name": "Lugia V",
                    "set": {"name": "Silver Tempest"},
                }
            )

    monkeypatch.setattr(client, "_get_client", lambda: FakeHTTP())

    response = asyncio.run(client.search_cards("lugia"))

    assert len(response.cards) == 1
    assert detail_paths == ["/cards/swsh12-138"]
