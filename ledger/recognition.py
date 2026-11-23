"""Turn what the vision model read on a card into a catalog printing (Blueprint)."""

import re
from dataclasses import dataclass, field

from cardledger_vision import recognize
from django.db.models import Q

from . import catalog
from .models import Blueprint

MAX_CANDIDATES = 5


@dataclass
class Recognition:
    """What the model read. language: a Ledger code or "" (unreadable/unsupported)."""

    name: str = ""
    set_code: str = ""
    collector_number: str = ""
    language: str = ""
    foil: bool = False


@dataclass
class Match:
    """blueprint is set only for a unique, certain match. reason: ok | ambiguous | not_found | language."""

    blueprint: Blueprint | None
    candidates: list = field(default_factory=list)
    reason: str = "not_found"


def _catalog(game):
    """Every printing of the game scanned."""
    return Blueprint.objects.filter(expansion__game=game)


def _scope(rec, expansion, game):
    """(blueprints to search, certain_allowed). Matches outside a known expansion are never certain."""
    code = rec.set_code.strip().lower()
    if expansion is not None and (not code or code == expansion.code):
        return Blueprint.objects.filter(expansion=expansion), True
    if code:
        # A printed code wins over the locked expansion: boosters hold bonus-sheet and List cards.
        found = catalog.expansion_by_code(code, game)
        if found is not None:
            return Blueprint.objects.filter(expansion=found), True
    if expansion is not None:
        return Blueprint.objects.filter(expansion=expansion), False
    # No usable set code: search the whole local catalog of the game, and let the owner confirm.
    return _catalog(game), False


def _suffixed(game):
    """Other games' catalog names carry a suffix the card does not print (Pokémon attacks "[...]", One Piece code
    "(...)"). Magic's suffixes (tokens, versions, art series) tell printings apart: never stripped."""
    return game.slug != "mtg"


def _by_name(qs, name, game):
    name = name.strip()
    if not name:
        return qs.none()
    cond = Q(name__iexact=name) | Q(name__istartswith=f"{name} // ")
    if _suffixed(game):
        cond |= Q(name__istartswith=f"{name} [") | Q(name__istartswith=f"{name} (")
    return qs.filter(cond)


def same_name(bp, name, game):
    """The printing carries the name read (for a split card, its front face; outside Magic, without its
    trailing " [...]" or "(...)")."""
    a, b = bp.name.casefold(), name.strip().casefold()
    if a == b or a.split(" // ")[0] == b:
        return True
    return _suffixed(game) and re.sub(r"\s+[\[(][^\[(]*[\])]$", "", a) == b


def _number_hits(scope, number):
    """Printings numbered `number` (normalized): the catalog keeps the generator's numbers, zero-padded or not
    ("025"), so leading zeros and case are ignored on its side too."""
    return scope.filter(collector_number__iregex=rf"^0*{re.escape(number)}$")


def _candidates(scope, name, game, hits=()):
    """Cards with the name read first (number hits, then the scope, then anywhere if the scope has none),
    then number hits with another name. Within each group: by Cardmarket product id."""
    named = list(_by_name(_catalog(game), name, game).select_related("expansion"))
    in_scope = set(scope.filter(pk__in=[b.pk for b in named]).values_list("pk", flat=True))
    if in_scope:
        named = [b for b in named if b.pk in in_scope]
    hit_ids = {h.pk for h in hits}
    named_ids = {b.pk for b in named}

    def rank(bp):
        place = 0 if bp.pk in hit_ids else 1 if bp.pk in in_scope else 2
        return (place if bp.pk in named_ids else 3, bp.cm_product_id)

    others = [h for h in hits if h.pk not in named_ids]
    return sorted(named + others, key=rank)[:MAX_CANDIDATES]


def match(rec, expansion, game):
    """The printing of `game` that rec is; expansion: the session's locked one, or None."""
    scope, certain_allowed = _scope(rec, expansion, game)
    number = catalog.normalize_number(rec.collector_number)
    certain = None

    if number:
        hits = list(_number_hits(scope, number).select_related("expansion"))
        named_hits = [h for h in hits if rec.name and same_name(h, rec.name, game)]
        if len(named_hits) == 1 or (len(hits) == 1 and not rec.name.strip()):
            certain = (named_hits or hits)[0]
        elif hits:
            return Match(None, _candidates(scope, rec.name, game, hits), "ambiguous")

    if certain is None:
        named = list(_by_name(scope, rec.name, game).select_related("expansion")[:2])
        if len(named) != 1:
            candidates = _candidates(scope, rec.name, game)
            return Match(None, candidates, "ambiguous" if candidates else "not_found")
        certain = named[0]

    if not certain_allowed:
        return Match(None, [certain], "ambiguous")
    if not rec.language:
        return Match(None, [certain], "language")
    return Match(certain, [certain], "ok")


def match_photo(photo, index, game, expansion):
    """The printing of `game` that the photo shows, by its vision index; expansion: the session's locked one, or
    None. Ids the catalog no longer has are dropped; a certain id that is gone leaves its candidates to confirm.
    reason: ok | ambiguous | not_found | no_card."""
    result = recognize(photo, index, lock=expansion.code if expansion is not None else None)
    if result.outcome == "no_card":
        return Match(None, [], "no_card")
    found = {
        b.cm_product_id: b
        for b in Blueprint.objects.filter(expansion__game=game, cm_product_id__in=result.candidates)
        .select_related("expansion")
    }
    candidates = [found[i] for i in result.candidates if i in found]
    if result.outcome == "ok" and result.product_id in found:
        return Match(found[result.product_id], candidates, "ok")
    if result.outcome == "not_found" and not candidates:
        return Match(None, [], "not_found")
    # A certain id the catalog lost, or an ambiguous answer: the owner picks among what is left.
    return Match(None, candidates, "ambiguous" if candidates else "not_found")
