"""CardTrader's property names per game: each game names its language and foil properties after itself
(`mtg_language`, `pokemon_language`, `onepiece_language`; `mtg_foil`, `pokemon_reverse`, ...).

The names come from what CardTrader says a blueprint takes (`editable_properties`); when it does not say, only
Magic's are known.
"""

from .models import MAGIC

LANGUAGE_SUFFIX = "_language"
FOIL_SUFFIXES = ("_foil", "_reverse", "_holo")


def language_key(names, game):
    """The language property among names (what CardTrader takes for a blueprint); names None: Magic's, or None."""
    if names is None:
        return f"{game.slug}{LANGUAGE_SUFFIX}" if game.slug == MAGIC else None
    return next((n for n in names if n.endswith(LANGUAGE_SUFFIX)), None)


def foil_key(names, game):
    """The foil property among names; names None: Magic's, or None."""
    if names is None:
        return "mtg_foil" if game.slug == MAGIC else None
    return next((n for n in names if n.endswith(FOIL_SUFFIXES)), None)


def read_language(props):
    """The Ledger language code in the properties of an order line, of any game ("" when there is none)."""
    value = next((v for k, v in props.items() if k.endswith(LANGUAGE_SUFFIX)), None)
    return (value or "").upper()


def read_foil(props):
    """Whether the properties of an order line, of any game, say foil."""
    return any(v is True or str(v).lower() == "true" for k, v in props.items() if k.endswith(FOIL_SUFFIXES))
