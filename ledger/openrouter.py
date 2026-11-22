"""OpenRouter client: asks a vision model what card is in a photo.

API: https://openrouter.ai/docs (OpenAI-compatible chat completions).
"""

import base64
import json

import httpx

from .models import Language
from .recognition import Recognition

BASE_URL = "https://openrouter.ai/api/v1"

MAGIC_PROMPT = (
    "You are looking at a photo of one Magic: The Gathering card. Read it and answer with JSON only.\n"
    "- name: the card's ENGLISH name, even when the card is printed in another language.\n"
    "- set_code: the expansion code printed at the bottom left (e.g. DSK), or empty if not visible.\n"
    "- collector_number: the collector number printed at the bottom left without the total "
    "(\"0187/0286\" -> \"0187\"), or empty if not visible.\n"
    f"- language: the language the card is printed in, one of {', '.join(Language.values)}; "
    "empty if unreadable or another language.\n"
    "- foil: true if the card is foil (holographic sheen, star next to the set code).\n"
    "Never guess a collector number you cannot read. "
    "If the photo shows no Magic card (or it is unreadable), answer with an empty name and empty fields."
)

_LANGUAGE = (
    f"- language: the language the card is printed in, one of {', '.join(Language.values)}; "
    "empty if unreadable or another language.\n"
)


def _prompt(game, card, name, set_code, collector_number, foil):
    """A prompt in the Magic one's shape. set_code and collector_number ask for the form the catalog stores
    (recognition.match compares them with Expansion.code and Blueprint.collector_number)."""
    return (
        f"You are looking at a photo of one {card} card. Read it and answer with JSON only.\n"
        f"- name: the card's ENGLISH name{name}, even when the card is printed in another language.\n"
        f"- set_code: {set_code}\n"
        f"- collector_number: {collector_number}\n"
        f"{_LANGUAGE}"
        f"- foil: {foil}\n"
        "Never guess a set code or a collector number you cannot read. "
        f"If the photo shows no {game} card (or it is unreadable), answer with an empty name and empty fields."
    )


# Per game slug (ledger.models.Game). The catalog's codes and numbers, per game:
# pokemon: set codes as the PTCG ones printed since Scarlet & Violet ("svi", "pal") and the Japanese ones ("sv2a"),
#   numbers without the total ("25", "TG05", "SWSH083"); older cards print only a set symbol.
# lorcana: the set number is the expansion code ("5"); numbers without the total.
# onepiece: the number is the whole card code ("OP01-001"); expansions "op01", "st14", "eb02", "prb01".
# yugioh: the number is the whole set ID, always with "EN" ("MP14-EN040"); expansions "mp14", "ra01".
# riftbound: expansions "ogn", "sfd", "unl", "ven", "ogs", "opp"; numbers "230", "126B", "SP3".
PROMPTS = {
    "mtg": MAGIC_PROMPT,
    "pokemon": _prompt(
        "Pokémon", "Pokémon Trading Card Game",
        name=' as in its title (e.g. "Pikachu", "Charizard ex"), without attacks, HP or stage',
        set_code="the set code printed in letters at the bottom left, next to the regulation mark "
                 "(\"SVI EN 025/198\" -> \"SVI\"; Japanese cards: \"SV2a\"); "
                 "empty if the set is shown only by its symbol, or not visible.",
        collector_number="the collector number printed at the bottom without the total "
                         "(\"025/198\" -> \"025\", \"TG05/TG30\" -> \"TG05\"; a promo's whole number, "
                         "e.g. \"SWSH083\"), or empty if not visible.",
        foil="true if the card is reverse holo (holographic sheen on the card outside the artwork).",
    ),
    "lorcana": _prompt(
        "Disney Lorcana", "Disney Lorcana",
        name=' as character name and version joined by " - " (e.g. "Elsa - Snow Queen"), or the name alone for '
             "an action, item or location",
        set_code="the set number printed at the bottom left after the language "
                 "(\"123/204 · EN · 5\" -> \"5\"), or empty if not visible.",
        collector_number="the collector number printed at the bottom left without the total "
                         "(\"123/204 · EN · 5\" -> \"123\"), or empty if not visible.",
        foil="true if the card is foil (holographic sheen).",
    ),
    "onepiece": _prompt(
        "One Piece", "One Piece Card Game",
        name=' as printed in the title (e.g. "Roronoa Zoro")',
        set_code="the part before the hyphen of the card number printed at the bottom right "
                 "(\"OP01-001\" -> \"OP01\", \"ST14-010\" -> \"ST14\"); "
                 "empty for a promo (\"P-001\") or if not visible.",
        collector_number="the whole card number printed at the bottom right, with its prefix and hyphen "
                         "(\"OP01-001\"), or empty if not visible.",
        foil="true if the card is foil (holographic sheen).",
    ),
    "yugioh": _prompt(
        "Yu-Gi-Oh!", "Yu-Gi-Oh! Trading Card Game",
        name="",
        set_code="the part before the hyphen of the set ID printed below the artwork on the right "
                 "(\"LOB-EN001\" -> \"LOB\"), or empty if not visible.",
        collector_number="the whole set ID printed below the artwork on the right, with its language letters "
                         "always written as EN (\"LOB-EN001\"; \"LOB-IT001\" and \"LOB-E001\" -> "
                         "\"LOB-EN001\"), or empty if not visible.",
        foil="true if the card is foil (holographic name or artwork).",
    ),
    "riftbound": _prompt(
        "Riftbound", "Riftbound (League of Legends Trading Card Game)",
        name=' as printed in the title (e.g. "Jinx, Loose Cannon")',
        set_code="the set code printed in letters at the bottom next to the collector number (e.g. \"OGN\"), "
                 "or empty if not printed or not visible.",
        collector_number="the collector number printed at the bottom without the total, keeping any letters "
                         "(\"001/298\" -> \"001\", \"SP3\"), or empty if not visible.",
        foil="true if the card is foil (holographic sheen).",
    ),
}


def prompt_for(slug):
    """What to ask the vision model about a photo of a card of the game `slug`."""
    return PROMPTS[slug]


SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "set_code": {"type": "string"},
        "collector_number": {"type": "string"},
        "language": {"type": "string"},
        "foil": {"type": "boolean"},
    },
    "required": ["name", "set_code", "collector_number", "language", "foil"],
    "additionalProperties": False,
}


class OpenRouterError(Exception):
    pass


class OpenRouterClient:
    def __init__(self, key, *, model, base_url=BASE_URL, transport=None, timeout=20.0):
        if not key:
            raise OpenRouterError("OpenRouter API key is not configured.")
        if not model:
            raise OpenRouterError("OpenRouter model is not configured.")
        self.model = model
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}", "X-Title": "CardLedger"},
            timeout=timeout,
            transport=transport,
        )

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method, path, **kwargs):
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"OpenRouter unreachable: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.is_error:
            message = ((data or {}).get("error") or {}).get("message") if isinstance(data, dict) else None
            raise OpenRouterError(f"OpenRouter failed ({resp.status_code}): {message or resp.reason_phrase}")
        return data

    def check_key(self):
        """Raises OpenRouterError if the key is not valid."""
        return self._request("GET", "/key")

    def recognize_card(self, image_jpeg, game_slug="mtg"):
        image = base64.b64encode(image_jpeg).decode("ascii")
        data = self._request("POST", "/chat/completions", json={
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt_for(game_slug)},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
            ]}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "card", "strict": True, "schema": SCHEMA}},
            "temperature": 0,
            # Reading a card needs no reasoning; reasoning models (e.g. Qwen) would get slow and costly.
            "reasoning": {"enabled": False},
        })
        try:
            content = data["choices"][0]["message"]["content"]
            read = json.loads(content.strip().removeprefix("```json").removesuffix("```"))
            if not isinstance(read, dict):
                raise ValueError("not an object")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise OpenRouterError("OpenRouter returned an unreadable answer") from exc

        def text(key):
            value = read.get(key)
            return value.strip() if isinstance(value, str) else ""

        language = text("language").upper()
        return Recognition(
            name=text("name"),
            set_code=text("set_code").lower(),
            collector_number=text("collector_number"),
            language=language if language in Language.values else "",
            foil=read.get("foil") is True,
        )
