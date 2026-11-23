"""Cardmarket's daily price guide of each game, and the price lookups of the Ledger's cards.

A catalog blueprint is a Cardmarket product, so its id is the row of the price guide. The guides are downloaded by
the nightly catalog sync (ledger/catalog_sync.py).
"""

from datetime import datetime
from decimal import Decimal

import httpx
from django.db.models import Case, DecimalField, OuterRef, Subquery, Value, When
from django.db.models.functions import NullIf

from .catalog import blueprint_of, normalize_number
from .models import Blueprint, Card, CardmarketPrice

PRICE_GUIDE_URL = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_{}.json"
USER_AGENT = "cardledger/0.1"

# Price guide key -> CardmarketPrice field. The finish keys are "-foil" in most games and "-holo" in Pokémon's guide:
# both fill the *_foil fields.
PRICE_FIELDS = {
    "low": "low", "trend": "trend", "avg": "avg", "avg1": "avg1", "avg7": "avg7", "avg30": "avg30",
    "low-foil": "low_foil", "trend-foil": "trend_foil", "avg-foil": "avg_foil",
    "avg1-foil": "avg1_foil", "avg7-foil": "avg7_foil", "avg30-foil": "avg30_foil",
}


class CardmarketError(Exception):
    pass


def make_client(transport=None):
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=120, follow_redirects=True, transport=transport,
    )


# --- lookups ---------------------------------------------------------------

def blueprint_value(field):
    """The value of a field of a collection item's catalog blueprint, as a subquery (None when the card has none)."""
    return Subquery(Blueprint.objects.filter(cm_product_id=OuterRef("card__cm_product_id")).values(field)[:1])


def item_guide_price(field):
    """A price guide field (trend, avg7, avg30, ...) of a collection item's printing, as an expression: the *_foil
    one for a foil card (not a product); None when unknown or 0.00 (the guide's value for a missing finish)."""
    price = Case(
        When(foil=True, card__is_product=False, then=blueprint_value(f"cm_product__{field}_foil")),
        default=blueprint_value(f"cm_product__{field}"),
        output_field=DecimalField(max_digits=10, decimal_places=2),
    )
    return NullIf(price, Value(Decimal("0.00")))


def trend_price():
    """The Cardmarket trend of a collection item's printing (trend_foil for a foil card), None when unknown or 0.00."""
    return item_guide_price("trend")


def blueprint_for(card):
    """The catalog printing of a Ledger card (catalog.blueprint_of)."""
    return blueprint_of(card)


def suggested_price(card, foil):
    """Cardmarket trend to suggest when listing a copy of card (foil trend for foil copies), or None."""
    bp = blueprint_for(card)
    if bp is None:
        return None
    price = CardmarketPrice.objects.filter(pk=bp.cm_product_id).first()
    if price is None:
        return None
    trend = price.trend_foil if foil and not card.is_product else price.trend
    return trend or None  # the guide holds 0.00 for a finish that doesn't exist


def owned_quantities(owner, game):
    """{Cardmarket product id: copies of game's cards in owner's collection}, matching cards like blueprint_for()."""
    from django.db.models import Q, Sum

    per_card = list(
        Card.objects.filter(game=game).annotate(qty=Sum("items__quantity", filter=Q(items__owner=owner)))
        .filter(qty__gt=0).values_list("cm_product_id", "set_code", "collector_number", "qty")
    )
    printings = Blueprint.objects.filter(expansion__game=game)
    known = set(printings.filter(cm_product_id__in=[c[0] for c in per_card if c[0]])
                .values_list("cm_product_id", flat=True))
    wanted = {(s.lower(), normalize_number(n)) for cm, s, n, _ in per_card if cm not in known and normalize_number(n)}
    by_printing = {}
    if wanted:
        rows = printings.filter(
            expansion__code__in={s for s, _ in wanted}, collector_number__in={n for _, n in wanted}
        ).order_by("-cm_product_id").values_list("expansion__code", "collector_number", "cm_product_id")
        by_printing = {(s, n): cm for s, n, cm in rows}
    owned = {}
    for cm, s, n, qty in per_card:
        cm_id = cm if cm in known else by_printing.get((s.lower(), normalize_number(n)))
        if cm_id is not None:
            owned[cm_id] = owned.get(cm_id, 0) + qty
    return owned


# --- download ----------------------------------------------------------------

def _money(value):
    return None if value is None else Decimal(str(value))


def price_guide_url(cm_game_id):
    """The URL of the price guide of a game, by Cardmarket's idGame."""
    return PRICE_GUIDE_URL.format(cm_game_id)


FOIL_KEYS = tuple(PRICE_FIELDS)
HOLO_KEYS = tuple(key.removesuffix("-foil") + "-holo" if key.endswith("-foil") else key for key in PRICE_FIELDS)


def _values(row):
    """The raw values of a guide row in PRICE_FIELDS order, the finish read from "-holo" keys when it has them."""
    keys = HOLO_KEYS if any(key.endswith("-holo") for key in row) else FOIL_KEYS
    return tuple(row.get(key) for key in keys)


def fetch_guide(client, cm_game_id):
    """({idProduct: raw values in PRICE_FIELDS order}, createdAt) from today's price guide of a game.

    The values stay as parsed from the JSON: the guide has over 100k rows, and models with a Decimal per field
    would cost hundreds of MB on the worker. price_row() builds the model when it is written.
    """
    try:
        response = client.get(price_guide_url(cm_game_id))
        response.raise_for_status()
        guide = response.json()
        del response
        guide_date = datetime.strptime(guide["createdAt"], "%Y-%m-%dT%H:%M:%S%z")
        prices = {row["idProduct"]: _values(row) for row in guide["priceGuides"]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CardmarketError(f"Cardmarket price guide: {type(exc).__name__}: {exc}") from exc
    return prices, guide_date


def price_row(id_product, values, guide_date):
    """The CardmarketPrice of a fetch_guide() entry."""
    return CardmarketPrice(
        id_product=id_product, prices_date=guide_date,
        **{f: _money(v) for f, v in zip(PRICE_FIELDS.values(), values)},
    )
