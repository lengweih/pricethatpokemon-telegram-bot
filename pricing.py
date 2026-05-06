from __future__ import annotations

import asyncio
import copy
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from cachetools import TTLCache


NUMBER_RE = re.compile(r"^(?P<number>[a-z]{0,5}\d{1,4}[a-z]?)(?:/[a-z]{0,5}\d{1,4}[a-z]?)?$", re.IGNORECASE)
TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)
SMART_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "ʼ": "'",
        "`": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
    }
)

VARIANT_ORDER = (
    "normal",
    "holofoil",
    "reverseHolofoil",
    "1stEditionNormal",
    "1stEditionHolofoil",
    "unlimited",
    "unlimitedHolofoil",
)
BROAD_NAME_FALLBACK_PAGE_SIZE = 25

VARIANT_LABELS = {
    "normal": "Normal",
    "holofoil": "Holofoil",
    "reverseHolofoil": "Reverse holo",
    "1stEditionNormal": "1st edition",
    "1stEditionHolofoil": "1st edition holo",
    "unlimited": "Unlimited",
    "unlimitedHolofoil": "Unlimited holo",
}

VARIANT_HINT_TERMS = {
    "normal": "normal",
    "holo": "holofoil",
    "holofoil": "holofoil",
    "foil": "holofoil",
    "reverse": "reverseHolofoil",
    "rev": "reverseHolofoil",
    "1st": "1stEditionNormal",
    "first": "1stEditionNormal",
    "unlimited": "unlimited",
}
IGNORED_VARIANT_WORDS = frozenset(
    {
        "normal",
        "holo",
        "holofoil",
        "foil",
        "reverse",
        "rev",
        "1st",
        "first",
        "edition",
        "unlimited",
    }
)

TCGDEX_VARIANT_MAP = {
    "normal": "normal",
    "holo": "holofoil",
    "holofoil": "holofoil",
    "reverse": "reverseHolofoil",
    "reverse-holofoil": "reverseHolofoil",
    "reverseHolofoil": "reverseHolofoil",
    "1st-edition": "1stEditionNormal",
    "1st-edition-normal": "1stEditionNormal",
    "firstEdition": "1stEditionNormal",
    "1st-edition-holofoil": "1stEditionHolofoil",
    "firstEditionHolofoil": "1stEditionHolofoil",
    "unlimited": "unlimited",
    "unlimited-holofoil": "unlimitedHolofoil",
}


class PricingError(Exception):
    """Base exception for lookup failures."""


class MalformedQuery(PricingError):
    """Raised when a user search cannot become a useful API query."""


class NoCardsFound(PricingError):
    """Raised when the provider returns no matching cards."""


class RateLimited(PricingError):
    """Raised when the provider reports rate limiting."""


class PricingAPIError(PricingError):
    """Raised when the provider is unavailable or returns bad data."""


@dataclass(frozen=True)
class ParsedQuery:
    raw: str
    normalized: str
    tokens: tuple[str, ...]
    name_anchor: str | None
    card_number: str | None
    set_hints: tuple[str, ...]
    variant_hint: str | None
    is_valid: bool
    reason: str | None = None
    search_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchResponse:
    parsed: ParsedQuery
    cards: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SetMatch:
    id: str
    name: str
    matched_tokens: tuple[str, ...]


class PricingProvider(Protocol):
    provider_id: str
    provider_name: str

    async def close(self) -> None:
        ...

    def store_lookup(self, cards: tuple[dict[str, Any], ...]) -> str:
        ...

    def get_lookup(self, lookup_id: str) -> tuple[dict[str, Any], ...] | None:
        ...

    async def search_cards(self, raw_query: str) -> SearchResponse:
        ...


class CachedProvider:
    provider_id = "base"
    provider_name = "Pricing provider"

    def __init__(
        self,
        cache_ttl_seconds: int = 3600,
        max_results: int = 5,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.max_results = max_results
        self.timeout_seconds = timeout_seconds
        self.search_cache: TTLCache[str, SearchResponse] = TTLCache(maxsize=512, ttl=cache_ttl_seconds)
        self.lookup_cache: TTLCache[str, tuple[dict[str, Any], ...]] = TTLCache(maxsize=256, ttl=cache_ttl_seconds)

    async def close(self) -> None:
        return None

    def store_lookup(self, cards: tuple[dict[str, Any], ...]) -> str:
        lookup_id = secrets.token_urlsafe(6)
        self.lookup_cache[lookup_id] = cards
        return lookup_id

    def get_lookup(self, lookup_id: str) -> tuple[dict[str, Any], ...] | None:
        return self.lookup_cache.get(lookup_id)


def create_price_provider(
    provider_id: str,
    cache_ttl_seconds: int = 3600,
    max_results: int = 5,
    timeout_seconds: float = 10.0,
    tcgdex_api_base: str = "https://api.tcgdex.net/v2/en",
    tcgdex_image_quality: str = "low",
    tcgdex_image_extension: str = "webp",
    tcgdex_candidate_limit: int | None = None,
    display_currency: str = "SGD",
    exchange_api_base: str = "https://api.frankfurter.dev",
) -> PricingProvider:
    normalized_provider_id = provider_id.strip().lower()
    if normalized_provider_id in {"", "tcgdex"}:
        return TCGdexPricingProvider(
            base_url=tcgdex_api_base,
            image_quality=tcgdex_image_quality,
            image_extension=tcgdex_image_extension,
            candidate_limit=tcgdex_candidate_limit,
            display_currency=display_currency,
            exchange_api_base=exchange_api_base,
            cache_ttl_seconds=cache_ttl_seconds,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError(f"Unsupported PRICE_PROVIDER: {provider_id}")


def normalize_query(raw_query: str) -> str:
    normalized = raw_query.translate(SMART_PUNCTUATION_TRANSLATION)
    return " ".join(normalized.strip().lower().split())


def tokenize(value: str) -> tuple[str, ...]:
    return tuple(match.group(0).lower() for match in TOKEN_RE.finditer(value))


def parse_query(raw_query: str) -> ParsedQuery:
    normalized = normalize_query(raw_query)
    tokens = tokenize(normalized)

    if len(normalized) < 2 or not tokens:
        return ParsedQuery(raw_query, normalized, tokens, None, None, (), None, False, "empty")

    card_number: str | None = None
    searchable_tokens: list[str] = []
    search_terms: list[str] = []

    for term in normalized.split():
        cleaned_term = term.strip(".,;:()[]{}")
        number_match = NUMBER_RE.match(cleaned_term)
        if number_match and card_number is None:
            card_number = number_match.group("number")
            continue

        for token in tokenize(cleaned_term):
            if token not in IGNORED_VARIANT_WORDS:
                searchable_tokens.append(token)
        if cleaned_term and not all(token in IGNORED_VARIANT_WORDS for token in tokenize(cleaned_term)):
            search_terms.append(cleaned_term)

    variant_hint = detect_variant_hint(tokens)

    if not searchable_tokens:
        return ParsedQuery(
            raw_query,
            normalized,
            tokens,
            None,
            card_number,
            (),
            variant_hint,
            False,
            "missing_name",
            tuple(search_terms),
        )

    name_anchor = searchable_tokens[0]
    set_hints = tuple(searchable_tokens[1:])
    return ParsedQuery(
        raw_query,
        normalized,
        tokens,
        name_anchor,
        card_number,
        set_hints,
        variant_hint,
        True,
        None,
        tuple(search_terms),
    )


def detect_variant_hint(tokens: tuple[str, ...]) -> str | None:
    token_set = set(tokens)
    if "reverse" in token_set or "rev" in token_set:
        return "reverseHolofoil"
    if ("1st" in token_set or "first" in token_set) and "holo" in token_set:
        return "1stEditionHolofoil"
    if "1st" in token_set or "first" in token_set:
        return "1stEditionNormal"
    if "unlimited" in token_set and "holo" in token_set:
        return "unlimitedHolofoil"
    for token in tokens:
        if token in VARIANT_HINT_TERMS:
            return VARIANT_HINT_TERMS[token]
    return None


def build_search_params(
    parsed: ParsedQuery,
    page_size: int = 25,
    include_card_number: bool = True,
    set_name: str | None = None,
    name_search: str | None = None,
) -> dict[str, str]:
    if not parsed.is_valid or not parsed.name_anchor:
        raise MalformedQuery("Send a card name, optionally followed by a card number or set.")

    params = {
        "name": f"{name_search or parsed.name_anchor}*",
        "pagination:page": "1",
        "pagination:itemsPerPage": str(page_size),
    }
    if include_card_number and parsed.card_number:
        params["localId"] = parsed.card_number
    if set_name:
        params["set.name"] = set_name
    return params


def build_api_query(parsed: ParsedQuery) -> str:
    params = build_search_params(parsed)
    return "&".join(f"{key}={value}" for key, value in params.items() if not key.startswith("pagination:"))


def build_card_name_search(parsed: ParsedQuery, set_match: SetMatch | None = None) -> str:
    terms = list(parsed.search_terms)
    if not terms and parsed.name_anchor:
        terms = [parsed.name_anchor, *parsed.set_hints]

    if set_match:
        normalized_matched_set = normalize_set_name(" ".join(set_match.matched_tokens))
        for start_index in range(len(terms)):
            trailing_terms = terms[start_index:]
            if normalize_set_name(" ".join(trailing_terms)) == normalized_matched_set:
                terms = terms[:start_index]
                break

    if not terms and parsed.name_anchor:
        terms = [parsed.name_anchor]
    return " ".join(terms)


def build_card_name_searches(parsed: ParsedQuery, set_match: SetMatch | None = None) -> tuple[str, ...]:
    primary_search = build_card_name_search(parsed, set_match)
    searches: list[str] = []

    for search in (primary_search, strip_search_punctuation(primary_search), parsed.name_anchor or ""):
        normalized_search = normalize_query(search)
        if normalized_search and normalized_search not in searches:
            searches.append(normalized_search)

    return tuple(searches)


def strip_search_punctuation(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def filter_brief_cards_by_query_name(
    cards: list[dict[str, Any]],
    parsed: ParsedQuery,
    set_match: SetMatch | None = None,
    allow_loose: bool = False,
) -> list[dict[str, Any]]:
    expected_name_tokens = set(tokenize(build_card_name_search(parsed, set_match)))
    if not expected_name_tokens or len(expected_name_tokens) <= 1:
        return cards

    filtered_cards = [
        card
        for card in cards
        if expected_name_tokens.issubset(set(tokenize(str(card.get("name") or ""))))
    ]
    if filtered_cards:
        return filtered_cards
    return cards if allow_loose else []


def available_variants(card: dict[str, Any]) -> list[str]:
    prices = get_price_variants(card)
    if not isinstance(prices, dict):
        return []
    return [variant for variant in VARIANT_ORDER if isinstance(prices.get(variant), dict)]


def select_default_variant(card: dict[str, Any], variant_hint: str | None = None) -> str | None:
    variants = available_variants(card)
    if not variants:
        return None

    if variant_hint in variants:
        return variant_hint

    if variant_hint == "1stEditionNormal" and "1stEditionHolofoil" in variants:
        return "1stEditionHolofoil"
    if variant_hint == "holofoil" and "1stEditionHolofoil" in variants:
        return "1stEditionHolofoil"
    if variant_hint == "unlimited" and "unlimitedHolofoil" in variants:
        return "unlimitedHolofoil"

    for variant in VARIANT_ORDER:
        if variant in variants:
            return variant
    return variants[0]


def rank_cards(cards: list[dict[str, Any]], parsed: ParsedQuery) -> list[dict[str, Any]]:
    query_name_tokens = set(parsed.name_anchor and [parsed.name_anchor] or ())
    query_name_tokens.update(parsed.set_hints)
    set_hints = set(parsed.set_hints)
    expected_number = (parsed.card_number or "").lower()

    def score_card(indexed_card: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        index, card = indexed_card
        score = 0
        card_number = str(card.get("number") or "").lower()
        card_name_tokens = set(tokenize(str(card.get("name") or "")))
        set_name_tokens = set(tokenize(str(card.get("set", {}).get("name") or "")))

        if expected_number:
            if card_number == expected_number:
                score += 80
            elif card_number.startswith(expected_number) or expected_number.startswith(card_number):
                score += 30

        if parsed.name_anchor and str(card.get("name") or "").lower().startswith(parsed.name_anchor):
            score += 20

        score += 12 * len(query_name_tokens & card_name_tokens)
        score += 8 * len(set_hints & set_name_tokens)

        if parsed.variant_hint and parsed.variant_hint in available_variants(card):
            score += 8

        if available_variants(card):
            score += 5

        if card.get("images", {}).get("small"):
            score += 2

        return score, -index

    ranked = sorted(enumerate(cards), key=score_card, reverse=True)
    return [copy.deepcopy(card) for _, card in ranked]


def get_card_link(card: dict[str, Any]) -> tuple[str, str] | None:
    label = card.get("source_link_label")
    url = card.get("source_url")
    if label and url:
        return str(label), str(url)
    return None


def get_image_url(card: dict[str, Any]) -> str | None:
    image_url = card.get("images", {}).get("small")
    return str(image_url) if image_url else None


def get_fallback_image_url(card: dict[str, Any]) -> str | None:
    image_url = card.get("images", {}).get("fallback")
    return str(image_url) if image_url else None


def summarize_card(card: dict[str, Any]) -> str:
    name = str(card.get("name") or "Unknown card")
    set_name = str(card.get("set", {}).get("name") or "Unknown set")
    number = str(card.get("number") or "?")
    return f"{name} - {set_name} #{number}"


def format_money(value: Any) -> str:
    return format_money_for_unit(value, "USD")


def format_money_for_unit(value: Any, unit: str) -> str:
    if isinstance(value, bool) or value is None:
        return "n/a"
    if isinstance(value, int | float):
        symbol = {"USD": "$", "EUR": "€", "SGD": "S$"}.get(unit.upper())
        if symbol:
            return f"{symbol}{value:.2f}"
        return f"{value:.2f} {unit.upper()}"
    return "n/a"


def get_price_variants(card: dict[str, Any]) -> dict[str, Any]:
    normalized_prices = card.get("prices", {})
    if isinstance(normalized_prices, dict) and isinstance(normalized_prices.get("variants"), dict):
        return normalized_prices["variants"]
    prices = card.get("tcgplayer", {}).get("prices", {})
    return prices if isinstance(prices, dict) else {}


def get_price_unit(card: dict[str, Any]) -> str:
    normalized_prices = card.get("prices", {})
    if isinstance(normalized_prices, dict) and normalized_prices.get("unit"):
        return str(normalized_prices["unit"])
    return str(card.get("tcgplayer", {}).get("unit") or "USD")


def get_price_labels(card: dict[str, Any]) -> dict[str, str]:
    normalized_prices = card.get("prices", {})
    if isinstance(normalized_prices, dict) and isinstance(normalized_prices.get("labels"), dict):
        return normalized_prices["labels"]
    return {"low": "Low", "mid": "Mid", "market": "Market"}


def get_price_conversion_text(card: dict[str, Any]) -> str | None:
    normalized_prices = card.get("prices", {})
    if not isinstance(normalized_prices, dict):
        return None
    source_unit = normalized_prices.get("sourceUnit") or normalized_prices.get("originalUnit")
    target_unit = normalized_prices.get("unit")
    if source_unit and target_unit and source_unit != target_unit:
        return f"{source_unit}->{target_unit}"
    return None


def get_price_updated_at(card: dict[str, Any]) -> str | None:
    normalized_prices = card.get("prices", {})
    if isinstance(normalized_prices, dict) and normalized_prices.get("updatedAt"):
        return str(normalized_prices["updatedAt"])
    updated_at = card.get("tcgplayer", {}).get("updatedAt")
    return str(updated_at) if updated_at else None


def format_updated_at(value: str | None) -> str | None:
    if not value:
        return None

    parsed = parse_datetime(value)
    if not parsed:
        return value

    parsed_local = parsed.astimezone(ZoneInfo("Asia/Singapore"))
    time_text = parsed_local.strftime("%I:%M %p").lstrip("0")
    return f"{parsed_local:%B} {parsed_local.day}, {parsed_local.year}, {time_text} SGT"


def parse_datetime(value: str) -> datetime | None:
    normalized = value.strip()
    if not normalized:
        return None

    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(normalized, "%Y/%m/%d")
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_price_message(card: dict[str, Any], variant_key: str | None = None, compact: bool = False) -> str:
    name = escape(str(card.get("name") or "Unknown card"))
    set_name = escape(str(card.get("set", {}).get("name") or "Unknown set"))
    number = escape(str(card.get("number") or "?"))
    rarity = escape(str(card.get("rarity") or "Unknown rarity"))
    source_name = escape(str(card.get("price_source_name") or card.get("provider_name") or "Pricing provider"))
    conversion_text = escape(str(get_price_conversion_text(card) or ""))
    selected_variant = variant_key or select_default_variant(card)

    if compact:
        lines = [f"<b>{name}</b> - {set_name} #{number}"]
    else:
        lines = [
            f"<b>{name}</b>",
            f"{set_name} #{number}",
            f"Rarity: {rarity}",
        ]

    prices = get_price_variants(card)
    variant_prices = prices.get(selected_variant) if selected_variant else None
    unit = get_price_unit(card)
    price_labels = get_price_labels(card)

    if selected_variant and isinstance(variant_prices, dict):
        variant_label = escape(VARIANT_LABELS.get(selected_variant, selected_variant))
        low_label = escape(price_labels.get("low", "Low"))
        mid_label = escape(price_labels.get("mid", "Mid"))
        market_label = escape(price_labels.get("market", "Market"))
        low_price = format_money_for_unit(variant_prices.get("low"), unit)
        mid_price = format_money_for_unit(variant_prices.get("mid"), unit)
        market_price = format_money_for_unit(variant_prices.get("market"), unit)

        if compact:
            lines.append(f"{variant_label} - {rarity}")
            lines.append(
                f"{low_label}: {low_price} | "
                f"{mid_label}: {mid_price} | "
                f"{market_label}: {market_price}"
            )
        else:
            lines.append(f"Variant: {variant_label}")
            lines.extend(
                [
                    f"{low_label}: {low_price}",
                    f"{mid_label}: {mid_price}",
                    f"{market_label}: {market_price}",
                ]
            )
    else:
        lines.append("Prices: unavailable from TCGplayer")

    updated_at = get_price_updated_at(card)
    source_detail = f"{source_name} ({conversion_text})" if conversion_text else source_name
    lines.append(f"Source: {source_detail}" if not compact else compact_source_line(source_detail, updated_at))
    if updated_at:
        if not compact:
            lines.append(f"Updated: {escape(str(format_updated_at(updated_at)))}")

    return "\n".join(lines)


def compact_source_line(source_name: str, updated_at: str | None) -> str:
    source = source_name.replace(" via TCGdex", "")
    compact_date = format_updated_at_date(updated_at)
    if compact_date:
        return f"Source: {source}\nUpdated: {escape(compact_date)}"
    return f"Source: {source}"


def format_updated_at_date(value: str | None) -> str | None:
    if not value:
        return None
    parsed = parse_datetime(value)
    if not parsed:
        return value
    parsed_utc = parsed.astimezone(timezone.utc)
    parsed_local = parsed_utc.astimezone(ZoneInfo("Asia/Singapore"))
    time_text = parsed_local.strftime("%I:%M %p").lstrip("0")
    return f"{parsed_local:%B} {parsed_local.day}, {parsed_local.year}, {time_text} SGT"


class TCGdexPricingProvider(CachedProvider):
    provider_id = "tcgdex"
    provider_name = "TCGdex"

    def __init__(
        self,
        base_url: str = "https://api.tcgdex.net/v2/en",
        image_quality: str = "low",
        image_extension: str = "webp",
        candidate_limit: int | None = None,
        display_currency: str = "SGD",
        exchange_api_base: str = "https://api.frankfurter.dev",
        cache_ttl_seconds: int = 3600,
        max_results: int = 5,
        timeout_seconds: float = 10.0,
    ) -> None:
        super().__init__(
            cache_ttl_seconds=cache_ttl_seconds,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
        )
        self.base_url = base_url.rstrip("/")
        self.image_quality = image_quality
        self.image_extension = image_extension
        self.candidate_limit = max(1, candidate_limit or max_results)
        self.display_currency = display_currency.strip().upper()
        self.exchange_api_base = exchange_api_base.rstrip("/")
        self.set_cache: TTLCache[str, tuple[dict[str, Any], ...]] = TTLCache(maxsize=1, ttl=cache_ttl_seconds)
        self.exchange_rate_cache: TTLCache[str, float] = TTLCache(maxsize=16, ttl=cache_ttl_seconds)
        self._client: httpx.AsyncClient | None = None
        self._exchange_client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._exchange_client:
            await self._exchange_client.aclose()
            self._exchange_client = None

    async def search_cards(self, raw_query: str) -> SearchResponse:
        parsed = parse_query(raw_query)
        if not parsed.is_valid:
            raise MalformedQuery("Send a card name, optionally followed by a card number or set.")

        cached = self.search_cache.get(parsed.normalized)
        if cached:
            return cached

        cards = await self._search_and_rank(parsed)
        search_response = SearchResponse(parsed=parsed, cards=cards)
        self.search_cache[parsed.normalized] = search_response
        return search_response

    async def _search_and_rank(self, parsed: ParsedQuery) -> tuple[dict[str, Any], ...]:
        set_match = await self._match_set(parsed)
        brief_cards = await self._search_brief_cards(parsed, include_card_number=True, set_match=set_match)
        if not brief_cards and parsed.card_number:
            brief_cards = await self._search_brief_cards(parsed, include_card_number=False, set_match=set_match)
        if not brief_cards:
            raise NoCardsFound("No matching cards found.")

        detail_cards = await self._fetch_card_details(brief_cards[: self.candidate_limit])
        if not detail_cards:
            raise NoCardsFound("No matching cards found.")

        normalized_cards = [await self._normalize_card(card) for card in detail_cards]
        ranked_cards = tuple(rank_cards(normalized_cards, parsed)[: self.max_results])
        if not ranked_cards:
            raise NoCardsFound("No matching cards found.")
        return ranked_cards

    async def _search_brief_cards(
        self,
        parsed: ParsedQuery,
        include_card_number: bool,
        set_match: SetMatch | None = None,
    ) -> list[dict[str, Any]]:
        for name_search in build_card_name_searches(parsed, set_match):
            page_size = self._brief_search_page_size(parsed, name_search, set_match)
            params = build_search_params(
                parsed,
                page_size=page_size,
                include_card_number=include_card_number,
                set_name=set_match.name if set_match else None,
                name_search=name_search,
            )
            payload = await self._request_json("/cards", params=params)
            if not isinstance(payload, list):
                raise PricingAPIError("TCGdex returned malformed search data.")

            cards = [card for card in payload if isinstance(card, dict)]
            cards = filter_brief_cards_by_query_name(
                cards,
                parsed,
                set_match=set_match,
            )
            if cards:
                return cards
        return []

    def _brief_search_page_size(
        self,
        parsed: ParsedQuery,
        name_search: str,
        set_match: SetMatch | None,
    ) -> int:
        expected_name_tokens = set(tokenize(build_card_name_search(parsed, set_match)))
        searched_name_tokens = set(tokenize(name_search))
        if parsed.card_number and len(expected_name_tokens) > 1 and len(searched_name_tokens) <= 1:
            return max(self.candidate_limit, BROAD_NAME_FALLBACK_PAGE_SIZE)
        return self.candidate_limit

    async def _match_set(self, parsed: ParsedQuery) -> SetMatch | None:
        if not parsed.set_hints:
            return None
        try:
            sets = await self._get_sets()
        except PricingAPIError:
            return None
        return match_set_from_tokens(parsed.set_hints, sets)

    async def _get_sets(self) -> tuple[dict[str, Any], ...]:
        cached_sets = self.set_cache.get("sets")
        if cached_sets is not None:
            return cached_sets

        payload = await self._request_json("/sets")
        if not isinstance(payload, list):
            raise PricingAPIError("TCGdex returned malformed set data.")

        sets = tuple(item for item in payload if isinstance(item, dict))
        self.set_cache["sets"] = sets
        return sets

    async def _fetch_card_details(self, brief_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tasks = [self._fetch_one_card_detail(card) for card in brief_cards if card.get("id")]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        details: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, RateLimited):
                raise result
            if isinstance(result, Exception):
                continue
            if isinstance(result, dict):
                details.append(result)
        return details

    async def _fetch_one_card_detail(self, brief_card: dict[str, Any]) -> dict[str, Any]:
        card_id = str(brief_card["id"])
        payload = await self._request_json(f"/cards/{card_id}")
        if not isinstance(payload, dict):
            raise PricingAPIError("TCGdex returned malformed card data.")
        return payload

    async def _request_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        try:
            response = await self._get_client().get(path, params=params)
        except httpx.TimeoutException as exc:
            raise PricingAPIError("TCGdex timed out.") from exc
        except httpx.HTTPError as exc:
            raise PricingAPIError("TCGdex could not be reached.") from exc

        if response.status_code == 429:
            raise RateLimited("TCGdex rate limit was hit.")
        if response.status_code == 404:
            raise NoCardsFound("No matching cards found.")
        if response.status_code >= 500:
            raise PricingAPIError("TCGdex is temporarily unavailable.")
        if response.status_code >= 400:
            raise PricingAPIError("TCGdex rejected that search.")

        try:
            return response.json()
        except ValueError as exc:
            raise PricingAPIError("TCGdex returned malformed data.") from exc

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds)
        return self._client

    def _get_exchange_client(self) -> httpx.AsyncClient:
        if self._exchange_client is None:
            self._exchange_client = httpx.AsyncClient(base_url=self.exchange_api_base, timeout=self.timeout_seconds)
        return self._exchange_client

    async def _normalize_card(self, card: dict[str, Any]) -> dict[str, Any]:
        card_id = str(card.get("id") or "")
        image_url = build_tcgdex_image_url(
            card.get("image"),
            quality=self.image_quality,
            extension=self.image_extension,
        )
        fallback_image_url = build_tcgdex_image_url(card.get("image"), quality="low", extension="png")
        raw_pricing = card.get("pricing", {})
        if not isinstance(raw_pricing, dict):
            raw_pricing = {}
        tcgplayer_pricing = normalize_tcgdex_tcgplayer_prices(raw_pricing.get("tcgplayer"))
        cardmarket_pricing = normalize_tcgdex_cardmarket_prices(raw_pricing.get("cardmarket"), card.get("variants"))
        normalized_prices = choose_tcgdex_price_package(tcgplayer_pricing, cardmarket_pricing)
        normalized_prices = await self._convert_price_package(normalized_prices)

        return {
            "id": card_id,
            "name": card.get("name"),
            "number": card.get("localId"),
            "rarity": card.get("rarity"),
            "set": {
                "id": card.get("set", {}).get("id"),
                "name": card.get("set", {}).get("name"),
            },
            "images": build_image_payload(image_url, fallback_image_url),
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "price_source_name": normalized_prices["source_name"],
            "source_url": f"{self.base_url}/cards/{card_id}" if card_id else None,
            "source_link_label": "View TCGdex data",
            "prices": normalized_prices,
            "tcgplayer": tcgplayer_pricing,
            "cardmarket": cardmarket_pricing,
        }

    async def _convert_price_package(self, price_package: dict[str, Any]) -> dict[str, Any]:
        source_unit = str(price_package.get("unit") or "").upper()
        target_unit = self.display_currency
        if not target_unit or source_unit == target_unit or not price_package.get("variants"):
            return price_package

        rate = await self._get_exchange_rate(source_unit, target_unit)
        if rate is None:
            return price_package

        converted_package = copy.deepcopy(price_package)
        converted_package["unit"] = target_unit
        converted_package["originalUnit"] = source_unit
        converted_package["conversionRate"] = rate
        converted_package["sourceUnit"] = source_unit

        for variant_prices in converted_package["variants"].values():
            if not isinstance(variant_prices, dict):
                continue
            for key, value in list(variant_prices.items()):
                if isinstance(value, bool) or not isinstance(value, int | float):
                    continue
                variant_prices[key] = round(value * rate, 2)
        return converted_package

    async def _get_exchange_rate(self, source_unit: str, target_unit: str) -> float | None:
        cache_key = f"{source_unit}:{target_unit}"
        cached_rate = self.exchange_rate_cache.get(cache_key)
        if cached_rate is not None:
            return cached_rate

        try:
            response = await self._get_exchange_client().get(
                "/v2/rates",
                params={"base": source_unit, "quotes": target_unit},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None

        rate = extract_frankfurter_rate(payload, source_unit, target_unit)
        if rate is not None:
            self.exchange_rate_cache[cache_key] = rate
        return rate


def build_tcgdex_image_url(raw_image_url: Any, quality: str = "low", extension: str = "png") -> str | None:
    if not raw_image_url:
        return None
    image_url = str(raw_image_url).rstrip("/")
    if image_url.endswith((".png", ".jpg", ".webp")):
        return image_url
    return f"{image_url}/{quality}.{extension}"


def build_image_payload(image_url: str | None, fallback_image_url: str | None) -> dict[str, str]:
    payload: dict[str, str] = {}
    if image_url:
        payload["small"] = image_url
    if fallback_image_url and fallback_image_url != image_url:
        payload["fallback"] = fallback_image_url
    return payload


def match_set_from_tokens(set_hint_tokens: tuple[str, ...], sets: tuple[dict[str, Any], ...]) -> SetMatch | None:
    if not set_hint_tokens:
        return None

    normalized_sets = {
        normalize_set_name(str(set_info.get("name") or "")): set_info
        for set_info in sets
        if set_info.get("name")
    }

    # The set name is normally the trailing phrase after the card name, e.g.
    # "mew ex paldean fates" should match "paldean fates", not "ex paldean fates".
    for start_index in range(len(set_hint_tokens)):
        candidate_tokens = set_hint_tokens[start_index:]
        candidate_name = normalize_set_name(" ".join(candidate_tokens))
        set_info = normalized_sets.get(candidate_name)
        if set_info:
            return SetMatch(
                id=str(set_info.get("id") or ""),
                name=str(set_info.get("name") or ""),
                matched_tokens=candidate_tokens,
            )

    return None


def normalize_set_name(value: str) -> str:
    return " ".join(tokenize(value))


def extract_frankfurter_rate(payload: Any, source_unit: str, target_unit: str) -> float | None:
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            if str(item.get("base", "")).upper() != source_unit:
                continue
            if str(item.get("quote", "")).upper() != target_unit:
                continue
            rate = item.get("rate")
            return float(rate) if isinstance(rate, int | float) else None

    if isinstance(payload, dict):
        rates = payload.get("rates", {})
        if isinstance(rates, dict):
            rate = rates.get(target_unit)
            return float(rate) if isinstance(rate, int | float) else None

    return None


def normalize_tcgdex_tcgplayer_prices(raw_tcgplayer: Any) -> dict[str, Any]:
    if not isinstance(raw_tcgplayer, dict):
        return {"prices": {}}

    prices: dict[str, dict[str, Any]] = {}
    for provider_variant, internal_variant in TCGDEX_VARIANT_MAP.items():
        raw_prices = raw_tcgplayer.get(provider_variant)
        if not isinstance(raw_prices, dict):
            continue
        prices[internal_variant] = {
            "low": raw_prices.get("lowPrice"),
            "mid": raw_prices.get("midPrice"),
            "market": raw_prices.get("marketPrice"),
            "high": raw_prices.get("highPrice"),
            "directLow": raw_prices.get("directLowPrice"),
        }

    return {
        "updatedAt": raw_tcgplayer.get("updated"),
        "unit": raw_tcgplayer.get("unit", "USD"),
        "prices": prices,
    }


def normalize_tcgdex_cardmarket_prices(raw_cardmarket: Any, card_variants: Any = None) -> dict[str, Any]:
    if not isinstance(raw_cardmarket, dict):
        return {"prices": {}}

    prices: dict[str, dict[str, Any]] = {}
    standard_variant = "normal"
    if isinstance(card_variants, dict) and not card_variants.get("normal") and card_variants.get("holo"):
        standard_variant = "holofoil"

    normal_prices = {
        "low": raw_cardmarket.get("low"),
        "mid": raw_cardmarket.get("avg"),
        "market": raw_cardmarket.get("trend"),
    }
    holo_prices = {
        "low": raw_cardmarket.get("low-holo"),
        "mid": raw_cardmarket.get("avg-holo"),
        "market": raw_cardmarket.get("trend-holo"),
    }

    if has_any_price(normal_prices):
        prices[standard_variant] = normal_prices
    if has_any_price(holo_prices):
        prices["holofoil"] = holo_prices

    return {
        "updatedAt": raw_cardmarket.get("updated"),
        "unit": raw_cardmarket.get("unit", "EUR"),
        "prices": prices,
    }


def choose_tcgdex_price_package(
    tcgplayer_pricing: dict[str, Any],
    cardmarket_pricing: dict[str, Any],
) -> dict[str, Any]:
    if tcgplayer_pricing.get("prices"):
        return {
            "source_name": "TCGplayer via TCGdex",
            "updatedAt": tcgplayer_pricing.get("updatedAt"),
            "unit": tcgplayer_pricing.get("unit", "USD"),
            "labels": {"low": "Low", "mid": "Mid", "market": "Market"},
            "variants": tcgplayer_pricing["prices"],
        }
    if cardmarket_pricing.get("prices"):
        return {
            "source_name": "Cardmarket via TCGdex",
            "updatedAt": cardmarket_pricing.get("updatedAt"),
            "unit": cardmarket_pricing.get("unit", "EUR"),
            "labels": {"low": "Low", "mid": "Avg", "market": "Trend"},
            "variants": cardmarket_pricing["prices"],
        }
    return {
        "source_name": "TCGdex",
        "updatedAt": None,
        "unit": "USD",
        "labels": {"low": "Low", "mid": "Mid", "market": "Market"},
        "variants": {},
    }


def has_any_price(prices: dict[str, Any]) -> bool:
    return any(value not in (None, 0) for value in prices.values())
