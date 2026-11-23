"""Turn a photo of a card into a catalog printing (Blueprint), with the game's vision index."""

import re
from dataclasses import dataclass, field

from cardledger_vision import recognize

from .models import Blueprint


@dataclass
class Match:
    """blueprint is set only for a unique, certain match; candidates are ordered by visual similarity.
    reason: ok | ambiguous | not_found | no_card."""

    blueprint: Blueprint | None
    candidates: list = field(default_factory=list)
    reason: str = "not_found"


def _suffixed(game):
    """Other games' catalog names carry a suffix the card does not print (Pokémon attacks "[...]", One Piece code
    "(...)"). Magic's suffixes (tokens, versions, art series) tell printings apart: never stripped."""
    return game.slug != "mtg"


def same_name(bp, name, game):
    """The printing carries `name`, a line's read_name (for a split card, its front face; outside Magic, without
    its trailing " [...]" or "(...)": lines scanned before the vision index hold a name read on the card)."""
    a, b = bp.name.casefold(), name.strip().casefold()
    if a == b or a.split(" // ")[0] == b:
        return True
    return _suffixed(game) and re.sub(r"\s+[\[(][^\[(]*[\])]$", "", a) == b


def match_photo(photo, index, game, expansion):
    """The printing of `game` that the photo shows, by its vision index; expansion: the session's locked one, or
    None. Ids the catalog no longer has are dropped; a certain id that is gone leaves its candidates to confirm.
    reason: ok | ambiguous | not_found | no_card."""
    result = recognize(photo, index, lock=expansion.code if expansion is not None else None)
    if result.outcome in ("no_card", "not_found"):
        # not_found: nothing is close to the photo, its candidates are unrelated nearest entries: show none.
        return Match(None, [], result.outcome)
    found = {
        b.cm_product_id: b
        for b in Blueprint.objects.filter(expansion__game=game, cm_product_id__in=result.candidates)
        .select_related("expansion")
    }
    candidates = [found[i] for i in result.candidates if i in found]
    if result.outcome == "ok" and result.product_id in found:
        return Match(found[result.product_id], candidates, "ok")
    # A certain id the catalog lost (outcome ok), or an ambiguous answer: the owner picks among what is left.
    return Match(None, candidates, "ambiguous" if candidates else "not_found")
