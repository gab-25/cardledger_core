"""The catalog API (/api/catalog/): search every Cardmarket product (cards, sealed, accessories) with Cardmarket
prices and owned copies."""

from django.db.models import F, Max
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .. import cardmarket
from ..catalog import normalize_number
from ..models import MAGIC, Blueprint, CardmarketPrice, Category, Game
from ..search import search_blueprints
from .common import PAGE, doc, page_payload, query

TAGS = ["catalog"]
PRICE_FIELDS = ("trend", "low", "avg", "trend_foil", "low_foil", "avg_foil")


def _money(value):
    return None if value is None else f"{value:.2f}"


def _game(params):
    """The game of ?game (a slug, Magic by default); an unknown one is a validation error."""
    slug = params.get("game", "").strip() or MAGIC
    try:
        return Game.by_slug(slug)
    except Game.DoesNotExist:
        raise ValidationError({"game": [f"Unknown game '{slug}'."]}) from None


GAME = query("game", f"Slug of the game (default {MAGIC}): mtg, pokemon, lorcana, onepiece, yugioh, riftbound")


def _category(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@doc(TAGS, "Search the catalog of a game (?game, Magic by default) by ?q, set, number, category, owned=1; page. q "
           "matches its words, in any order, against the start of the words of the name (case, accents and "
           "punctuation ignored), set codes and, when it names a set, collector numbers; results come most relevant "
           "first. With no filter (searched: false) it lists the latest releases: the products of the newest "
           "expansions first", parameters=[
    query("q", "Words of the name in any order, set codes and, with a set code, collector numbers"),
    query("set", "Exact set code"),
    query("number", "Collector number"),
    GAME,
    query("category", "Cardmarket category id of the game (see /api/catalog/categories/)"),
    query("owned", "1: only the products you own", enum=["1"]),
    PAGE,
])
@api_view(["GET"])
def catalog(request):
    params = request.query_params
    game = _game(params)
    q = params.get("q", "").strip()
    set_code = params.get("set", "").strip().lower()
    number = normalize_number(params.get("number", ""))
    category = _category(params.get("category"))
    owned = params.get("owned") == "1"
    filters = {"q": q, "set": set_code, "number": number, "category": category, "owned": owned}
    searched = bool(q or set_code or number or category is not None or owned)

    owned_qty = cardmarket.owned_quantities(request.user, game)
    # Prices are prefetched: a join would turn INNER once filtered on cm_product_id, hiding unpriced printings.
    printings = (Blueprint.objects.filter(expansion__game=game).select_related("expansion", "category")
                 .prefetch_related("cm_product"))
    if searched:
        printings = printings.order_by("name", "expansion__code")
    else:
        # Latest releases first (Scryfall's release date).
        printings = printings.order_by(F("expansion__released_at").desc(nulls_last=True), "name", "expansion__code",
                                       "cm_product_id")
    if set_code:
        printings = printings.filter(expansion__code=set_code)
    if number:
        printings = printings.filter(collector_number=number)
    if category is not None:
        printings = printings.filter(category__cm_id=category)
    if owned:
        printings = printings.filter(cm_product_id__in=owned_qty)
    if q:
        printings = search_blueprints(printings, q)  # orders by relevance

    def printing(bp):
        price = bp.cm_product
        return {
            "ct_id": bp.cm_product_id,  # the name the UI knows: now the Cardmarket product id
            "name": bp.name,
            "version": "",
            "image_url": bp.image_url,
            "image_large_url": bp.image_large_url,
            "expansion_code": bp.expansion.code,
            "expansion_name": bp.expansion.name,
            "collector_number": bp.collector_number,
            "category_id": bp.category.cm_id if bp.category else None,
            "category_name": bp.category.name if bp.category else None,
            "is_product": bp.is_product,
            "accepts_language": True,  # what CardTrader takes is checked when publishing
            "owned_qty": owned_qty.get(bp.cm_product_id),
            "prices": None if price is None else {f: _money(getattr(price, f)) for f in PRICE_FIELDS},
        }

    return Response({
        "searched": searched,
        "filters": filters,
        **page_payload(request, printings, printing),
        "prices_date": CardmarketPrice.objects.filter(blueprint__expansion__game=game)
        .aggregate(d=Max("prices_date"))["d"],
    })


@doc(TAGS, "The Cardmarket categories of a game that have products in the catalog, for the category filter",
     parameters=[GAME])
@api_view(["GET"])
def categories(request):
    game = _game(request.query_params)
    rows = Category.objects.filter(game=game, blueprints__isnull=False).distinct().order_by("name", "cm_id")
    return Response([{"id": c.cm_id, "name": c.name} for c in rows])
