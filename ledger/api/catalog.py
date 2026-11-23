"""The catalog API (/api/catalog/): search every Cardmarket product (cards, sealed, accessories) with Cardmarket
prices and owned copies."""

from collections import defaultdict
from datetime import timedelta

from django import forms
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import F, Max, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .. import cardmarket, snapshots
from ..catalog import normalize_number
from ..models import (Blueprint, CardmarketPrice, Category, CollectionItem, MissingCardReport, Order, OrderItem,
                      PriceSnapshot)
from ..search import SearchText, normalize_all, search_blueprints
from .common import GAME, PAGE, doc, form_body, form_errors, game_param, messages_response, msg, page_payload, query

TAGS = ["catalog"]
PRICE_FIELDS = ("trend", "low", "avg", "trend_foil", "low_foil", "avg_foil")
ORDERINGS = ["trend", "-trend", "name", "year"]
SOLD_DAYS = 30
CHANGE_DAYS = 30
HISTORY_DAYS = 90
NOT_SOLD = [Order.State.CANCELED, Order.State.CLOSED]  # as the overview's revenue
# Minimum pg_trgm similarity of q to a product name for "Did you mean".
SUGGESTION_THRESHOLD = 0.3


def _sold(user, game):
    """{Cardmarket product id: copies of game the user sold in the last SOLD_DAYS days} (order item → listing →
    item → card's printing; canceled and merged orders left out)."""
    since = timezone.now() - timedelta(days=SOLD_DAYS)
    rows = (OrderItem.objects.owned_by(user)
            .exclude(order__state__in=NOT_SOLD)
            .annotate(at=Coalesce("order__paid_at", "order__created_at"))
            .filter(at__gte=since, listing__item__card__game=game,
                    listing__item__card__cm_product_id__isnull=False)
            .values_list("listing__item__card__cm_product_id").annotate(n=Sum("quantity")))
    return dict(rows)


def _locations(user, game):
    """{Cardmarket product id: sorted distinct non-empty locations of the user's items of that printing}."""
    found = defaultdict(set)
    rows = (CollectionItem.objects.owned_by(user).filter(card__game=game, card__cm_product_id__isnull=False)
            .exclude(location="").values_list("card__cm_product_id", "location").distinct())
    for product, location in rows:
        found[product].add(location)
    return {product: sorted(locations) for product, locations in found.items()}


def _suggestion(game, q):
    """The game's product name closest to q (pg_trgm similarity on the normalised names), None when none is close."""
    words = " ".join(normalize_all([q])).strip()
    if not words:
        return None
    best = (Blueprint.objects.filter(expansion__game=game)
            .annotate(sim=TrigramSimilarity(SearchText("name"), words))
            .filter(sim__gte=SUGGESTION_THRESHOLD).order_by("-sim", "name").values_list("name", flat=True).first())
    return best


def _money(value):
    return None if value is None else f"{value:.2f}"


def _category(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@doc(TAGS, "Search the catalog of a game (?game, Magic by default) by ?q, set, number, category, owned=1, rarity, "
           "finish=foil; ordering; page. q matches its words, in any order, against the start of the words of the "
           "name (case, accents and punctuation ignored), set codes and, when it names a set, collector numbers; "
           "results come most relevant first. With no filter (searched: false) it lists the latest releases: the "
           "products of the newest expansions first. rarities lists the game's rarities; a search with no results "
           "gives a close product name as suggestion (null when none). change_30d: the signed % change of the "
           "Cardmarket trend over the last 30 days (\"+12.5\"; the foil trend with finish=foil; null without a price "
           "30 days ago, from the daily price snapshots)", parameters=[
    query("q", "Words of the name in any order, set codes and, with a set code, collector numbers"),
    query("set", "Exact set code"),
    query("number", "Collector number"),
    GAME,
    query("category", "Cardmarket category id of the game (see /api/catalog/categories/)"),
    query("owned", "1: only the products you own", enum=["1"]),
    query("rarity", "Exact rarity (see rarities in the response)"),
    query("finish", "foil: only the products with a Cardmarket foil price", enum=["foil"]),
    query("ordering", "trend / -trend: Cardmarket trend ascending / descending (unpriced last); name; year: newest "
                      "expansion first. Default: relevance (search) or latest releases", enum=ORDERINGS),
    PAGE,
])
@api_view(["GET"])
def catalog(request):
    params = request.query_params
    game = game_param(params)
    q = params.get("q", "").strip()
    set_code = params.get("set", "").strip().lower()
    number = normalize_number(params.get("number", ""))
    category = _category(params.get("category"))
    owned = params.get("owned") == "1"
    rarity = params.get("rarity", "").strip()
    finish = "foil" if params.get("finish") == "foil" else ""
    ordering = params.get("ordering", "")
    if ordering not in ORDERINGS:
        ordering = ""
    filters = {"q": q, "set": set_code, "number": number, "category": category, "owned": owned, "rarity": rarity,
               "finish": finish, "ordering": ordering}
    searched = bool(q or set_code or number or category is not None or owned or rarity or finish)

    owned_qty = cardmarket.owned_quantities(request.user, game)
    # Prices are prefetched: a join would turn INNER once filtered on cm_product_id, hiding unpriced printings.
    printings = (Blueprint.objects.filter(expansion__game=game).select_related("expansion", "category")
                 .prefetch_related("cm_product"))
    if searched:
        printings = printings.order_by("name", "expansion__code")
    else:
        # Latest releases first (release date).
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
    if rarity:
        printings = printings.filter(rarity=rarity)
    if finish:
        foil_priced = CardmarketPrice.objects.filter(
            Q(trend_foil__isnull=False) | Q(low_foil__isnull=False) | Q(avg_foil__isnull=False))
        printings = printings.filter(cm_product_id__in=foil_priced.values("id_product"))
    if q:
        printings = search_blueprints(printings, q)  # orders by relevance
    if ordering:
        printings = printings.order_by(*_ordering(ordering))
    sold = _sold(request.user, game)
    locations = _locations(request.user, game)
    then = {}

    def change_30d(bp):
        price, old = bp.cm_product, then.get(bp.cm_product_id)
        if price is None or old is None:
            return None
        return snapshots.change_pct(price.trend_foil if finish else price.trend, old[1] if finish else old[0])

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
            "accepts_language": True,  # what a market takes is checked by the extension when it publishes
            "owned_qty": owned_qty.get(bp.cm_product_id),
            "rarity": bp.rarity,
            "year": bp.expansion.released_at.year if bp.expansion.released_at else None,
            "sold_30d": sold.get(bp.cm_product_id, 0),
            "owned_locations": locations.get(bp.cm_product_id, []),
            "prices": None if price is None else {f: _money(getattr(price, f)) for f in PRICE_FIELDS},
            "change_30d": change_30d(bp),
        }

    page = page_payload(request, printings, lambda bp: bp)
    then.update(snapshots.trends_on([bp.cm_product_id for bp in page["results"]],
                                    timezone.localdate() - timedelta(days=CHANGE_DAYS)))
    page["results"] = [printing(bp) for bp in page["results"]]
    return Response({
        "searched": searched,
        "filters": filters,
        **page,
        "prices_date": CardmarketPrice.objects.filter(blueprint__expansion__game=game)
        .aggregate(d=Max("prices_date"))["d"],
        "rarities": list(Blueprint.objects.filter(expansion__game=game).exclude(rarity="")
                         .values_list("rarity", flat=True).distinct().order_by("rarity")),
        "suggestion": _suggestion(game, q) if q and not page["count"] else None,
    })


def _days(value, default, top):
    try:
        days = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(days, 1), top)


@doc(TAGS, "The Cardmarket trend history of a catalog product (its Cardmarket id, ct_id in the catalog rows) over "
           "the last ?days (default 90, at most 400), oldest first: one point per day the trend or foil trend "
           "changed, the first point dated the window's start with the value it had then (when known)", parameters=[
    OpenApiParameter("days", OpenApiTypes.INT, OpenApiParameter.QUERY, description="Days back (1-400, default 90)"),
])
@api_view(["GET"])
def history(request, ct_id):
    get_object_or_404(Blueprint, cm_product_id=ct_id)
    days = _days(request.query_params.get("days"), HISTORY_DAYS, PriceSnapshot.KEEP_DAYS)
    start = timezone.localdate() - timedelta(days=days)
    rows = list(PriceSnapshot.objects.filter(cm_product_id=ct_id, date__gte=start).order_by("date"))
    points = [{"date": r.date, "trend": _money(r.trend), "trend_foil": _money(r.trend_foil)} for r in rows]
    if not rows or rows[0].date > start:
        before = snapshots.trends_on([ct_id], start).get(ct_id)
        if before is not None:
            points.insert(0, {"date": start, "trend": _money(before[0]), "trend_foil": _money(before[1])})
    return Response({"points": points})


def _ordering(ordering):
    """The order_by of ?ordering; ties by name, set and product id."""
    first = {
        "trend": F("cm_product__trend").asc(nulls_last=True),
        "-trend": F("cm_product__trend").desc(nulls_last=True),
        "name": F("name").asc(),
        "year": F("expansion__released_at").desc(nulls_last=True),
    }[ordering]
    return [first, "name", "expansion__code", "cm_product_id"]


class MissingCardForm(forms.ModelForm):
    class Meta:
        model = MissingCardReport
        fields = ["q", "note"]


@doc(TAGS, "Report a product missing from the catalog of the game of ?game (q: what was searched, note: optional "
           "details), for the catalog maintainers (Django admin)", request=form_body("MissingCard", MissingCardForm),
     parameters=[GAME])
@api_view(["POST"])
def report_missing(request):
    game = game_param(request.query_params)
    form = MissingCardForm(request.data)
    if not form.is_valid():
        raise form_errors(form)
    form.instance.user, form.instance.game = request.user, game
    form.save()
    return messages_response(msg("success", "Thanks: we will look for it and add it to the catalog."))


@doc(TAGS, "The Cardmarket categories of a game that have products in the catalog, for the category filter",
     parameters=[GAME])
@api_view(["GET"])
def categories(request):
    game = game_param(request.query_params)
    rows = Category.objects.filter(game=game, blueprints__isnull=False).distinct().order_by("name", "cm_id")
    return Response([{"id": c.cm_id, "name": c.name} for c in rows])
