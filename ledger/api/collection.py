"""The collection API (/api/collection/): list with filters, views and stats, add from the catalog, edit/remove copies,
bulk actions, CSV import and export."""

from decimal import Decimal

from datetime import timedelta

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Now
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from .. import services
from ..cardmarket import blueprint_value, trend_price
from ..forms import BulkForm, CatalogItemForm, CsvImportForm, ItemForm
from ..models import Card, Category, CollectionItem, Condition, Language, Listing, SyncLog
from ..presentation import item_badge
from .common import GAME, PAGE, doc, form_body, form_errors, game_param, messages_response, msg, page_payload, query

TAGS = ["collection"]
VALUE = ExpressionWrapper(F("trend") * F("quantity"), output_field=DecimalField())
COST_VALUE = ExpressionWrapper(F("cost") * F("quantity"), output_field=DecimalField())
CATALOG_ITEM = form_body("CatalogItem", CatalogItemForm)
ITEM = form_body("Item", ItemForm)
BULK = form_body("CollectionBulk", BulkForm, ids=serializers.ListField(
    child=serializers.IntegerField(), help_text="Ids of the user's items of the game (others are ignored)"))

STALE_DAYS = 90
BULK_UNDER = Decimal("0.50")  # trend under which an item is bulk
BELOW_TREND = Decimal("0.95")  # a listing price more than 5% under the trend
ACTIVE = [Listing.Status.PUBLISHED, Listing.Status.TO_PUBLISH]  # a listing not sold
VIEWS = ["all", "below_trend", "not_listed", "stale", "bulk"]


def _views():
    """The collection views (?view): Q filters on items annotated with trend (None: every item).

    A "listing" here is an active one (published or to publish): a sold listing counts as no listing.
    """
    stale_before = timezone.now() - timedelta(days=STALE_DAYS)
    return {
        "all": None,
        "below_trend": Q(listing__status__in=ACTIVE, listing__price__lt=F("trend") * BELOW_TREND),
        "not_listed": Q(listing__isnull=True) | Q(listing__status=Listing.Status.SOLD),
        "stale": Q(created_at__lt=stale_before, quantity__gt=0),
        "bulk": Q(trend__lt=BULK_UNDER),
    }


def _money(value):
    return None if value is None else f"{value:.2f}"


def _price(item):
    return _money(item.trend)


def _listing_price(item):
    """The price of the item's active listing, None without one (or when it is sold)."""
    listing = getattr(item, "listing", None)
    return _money(listing.price) if listing is not None and listing.status in ACTIVE else None


def _with_blueprint(items):
    return items.annotate(category_ct_id=blueprint_value("category__cm_id"),
                          category_name=blueprint_value("category__name"),
                          image_url=blueprint_value("image_url"),
                          image_large_url=blueprint_value("image_large_url"),
                          trend=trend_price())


def _category(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _choices(choices):
    return [[value, label] for value, label in choices]


@doc(TAGS, "Choices of the card and listing forms: conditions, languages, listing statuses")
@api_view(["GET"])
def choices(request):
    return Response({
        "conditions": _choices(Condition.choices),
        "languages": _choices(Language.choices),
        "listing_statuses": _choices(Listing.Status.choices),
    })


def _row(item):
    card = item.card
    return {
        "id": item.pk,
        "name": card.name,
        "set_code": card.set_code,
        "is_product": card.is_product,
        "category_id": item.category_ct_id,
        "category_name": item.category_name,
        "image_url": item.image_url or "",
        "image_large_url": item.image_large_url or "",
        "foil": item.foil,
        "condition": item.condition,
        "condition_label": item.get_condition_display(),
        "language": item.language,
        "quantity": item.quantity,
        "price": _price(item),
        "badge": item_badge(item.status),
        "trend": _price(item),
        "cost": _money(item.cost),
        "location": item.location,
        "listing_price": _listing_price(item),
        "added_at": item.created_at,
    }


def _collection(request):
    user = request.user
    params = request.query_params
    game = game_param(params)
    views = _views()
    items = _with_blueprint(CollectionItem.objects.owned_by(user).filter(card__game=game)
                            .select_related("card", "listing"))
    counts = {f"{name}_n": Count("pk", filter=views[name]) for name in ("below_trend", "not_listed", "stale")}
    stats = items.aggregate(total=Sum("quantity"), value=Sum(VALUE), cost_value=Sum(COST_VALUE), **counts)
    for name in ("below_trend", "not_listed", "stale"):
        stats[name] = stats.pop(f"{name}_n")
    stats["unique"] = Card.objects.filter(items__owner=user, game=game).distinct().count()
    stats["for_sale"] = (
        Listing.objects.owned_by(user).filter(item__card__game=game).exclude(status=Listing.Status.SOLD)
        .aggregate(n=Sum("quantity"))["n"] or 0
    )
    stats["value"] = _money(stats["value"])
    stats["cost_value"] = _money(stats["cost_value"])

    q = params.get("q", "").strip()
    foil = params.get("foil") == "1"
    set_code = params.get("set", "")
    cond = params.get("cond", "")
    category = _category(params.get("category"))
    location = params.get("location", "")
    view = params.get("view", "")
    if view not in VIEWS:
        view = "all"
    if q:
        items = items.filter(Q(card__name__icontains=q) | Q(card__set_code__iexact=q))
    if foil:
        items = items.filter(foil=True)
    if set_code:
        items = items.filter(card__set_code=set_code)
    if cond in Condition.values:
        items = items.filter(condition=cond)
    if category is not None:
        # a category id is only unique within a game, and the items are already the game's
        items = items.filter(category_ct_id=category)
    if location:
        items = items.filter(location=location)
    # the views' counts honour the other filters, not ?view itself
    view_counts = items.aggregate(**{name: Count("pk", filter=views[name]) for name in VIEWS})
    if views[view] is not None:
        items = items.filter(views[view])
    owned_products = (Card.objects.filter(items__owner=user, game=game, cm_product_id__isnull=False)
                      .values("cm_product_id"))
    categories = (Category.objects.filter(game=game, blueprints__cm_product_id__in=owned_products).distinct()
                  .order_by("name", "cm_id"))

    return Response({
        **page_payload(request, items, _row),
        "stats": stats,
        "views": view_counts,
        "view": view,
        "sets": list(Card.objects.filter(items__owner=user).filter(game=game).values_list("set_code", flat=True)
                     .distinct().order_by("set_code")),
        "categories": [{"id": c.cm_id, "name": c.name} for c in categories],
        "filtered": bool(q or foil or set_code or cond or category is not None or location or view != "all"),
    })


@doc(TAGS, "The collection (GET: ?q, foil, set, cond, category, location, view, page) or add a catalog product to it "
           "(POST)", request=CATALOG_ITEM)
@extend_schema(methods=["GET"], parameters=[
    query("q", "Name or exact set code"),
    query("foil", "1: foils only", enum=["1"]),
    query("set", "Exact set code"),
    query("cond", "Condition", enum=Condition.values),
    query("category", "CardTrader category id of the item's catalog product"),
    query("location", "Exact storage location"),
    query("view", "all (default); below_trend: active listing more than 5% under the trend; not_listed: no active "
                  f"listing; stale: added over {STALE_DAYS} days ago and still in stock; bulk: trend under 0.50",
          enum=VIEWS),
    GAME,
    PAGE,
])
@api_view(["GET", "POST"])
def collection(request):
    if request.method == "GET":
        return _collection(request)
    form = CatalogItemForm(request.data, owner=request.user)
    if not form.is_valid():
        raise form_errors(form)
    item, merged = form.save()
    added = form.cleaned_data["quantity"]
    if merged:
        return messages_response(msg("success", f"Added {added}× {item.card.name} to the copies you already had."),
                                 id=item.pk)
    return messages_response(msg("success", f"Added {added}× {item.card.name}."),
                             status=status.HTTP_201_CREATED, id=item.pk)


@doc(TAGS, "One collection item: read (GET), change its copies (PUT) or remove it (DELETE)", request=ITEM)
@extend_schema(methods=["GET"], operation_id="collection_item_retrieve")
@api_view(["GET", "PUT", "DELETE"])
def item(request, pk):
    obj = get_object_or_404(_with_blueprint(CollectionItem.objects.owned_by(request.user).select_related("card__game")), pk=pk)
    if request.method == "DELETE":
        name = obj.card.name
        obj.delete()
        return messages_response(msg("success", f"Removed {name} from the collection."))
    if request.method == "PUT":
        form = ItemForm(request.data, instance=obj)
        if not form.is_valid():
            raise form_errors(form)
        form.save()
        return messages_response(msg("success", f"Saved {obj.card.name}."))
    card = obj.card
    return Response({
        "id": obj.pk,
        "game": card.game.slug,
        "name": card.name,
        "set_code": card.set_code,
        "collector_number": card.collector_number,
        "is_product": card.is_product,
        "category_id": obj.category_ct_id,
        "category_name": obj.category_name,
        "condition": obj.condition,
        "language": obj.language,
        "foil": obj.foil,
        "quantity": obj.quantity,
        "price": _price(obj),
        "has_listing": hasattr(obj, "listing"),
        "cost": _money(obj.cost),
        "location": obj.location,
    })


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _pct(pct):
    return f"trend {pct.normalize():+f}%" if pct else "trend"


@doc(TAGS, "Act on several items of the game of ?game: set_location (location), delete, list (create or update a "
           "listing to publish at trend × (1 + pct/100), rounded to 0.05) or reprice (the same, existing listings "
           "only)", request=BULK, parameters=[GAME])
@api_view(["POST"])
def bulk(request):
    game = game_param(request.query_params)
    ids = request.data.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ValidationError({"ids": ["A non-empty list of item ids."]})
    form = BulkForm(request.data)
    if not form.is_valid():
        raise form_errors(form)
    action, data = form.cleaned_data["action"], form.cleaned_data
    items = CollectionItem.objects.owned_by(request.user).filter(card__game=game, pk__in=ids)
    n = items.count()
    if not n:
        return messages_response(msg("warning", "None of the selected items is in this collection."), count=0)

    if action == "set_location":
        location = data["location"].strip()
        items.update(location=location, updated_at=Now())
        text = (f"Moved {_plural(n, 'item')} to {location}." if location
                else f"Cleared the location of {_plural(n, 'item')}.")
        return messages_response(msg("success", text), count=n)
    if action == "delete":
        items.delete()
        return messages_response(msg("success", f"Removed {_plural(n, 'item')} from the collection."), count=n)

    pct = data["pct"] or Decimal("0")
    result = services.list_at_trend(items.annotate(trend=trend_price()), pct, reprice=action == "reprice")
    verb = "Repriced" if action == "reprice" else "Listed"
    msgs = [msg("success", f"{verb} {_plural(result.listed, 'item')} at {_pct(pct)}.")]
    if result.no_trend:
        msgs.append(msg("warning", f"Skipped {_plural(result.no_trend, 'item')} without a Cardmarket trend."))
    if result.no_copies:
        msgs.append(msg("warning", f"Skipped {_plural(result.no_copies, 'item')} with no copies left."))
    if result.no_listing:
        msgs.append(msg("warning", f"Skipped {_plural(result.no_listing, 'item')} without a listing."))
    return messages_response(*msgs, count=result.listed)


@doc(TAGS, "CSV import: the columns (GET) or import a file (POST multipart 'file') into the game of ?game",
     request={"multipart/form-data": form_body("CsvImport", CsvImportForm)}, parameters=[GAME])
@api_view(["GET", "POST"])
@parser_classes([MultiPartParser])
def import_csv(request):
    game = game_param(request.query_params)
    if request.method == "GET":
        return Response({"columns": list(services.CSV_FIELDS)})
    form = CsvImportForm(data={}, files=request.FILES)
    if form.is_valid():
        try:
            created, updated = services.import_csv(form.cleaned_data["file"], request.user, game)
        except services.CsvImportError as exc:
            form.add_error("file", str(exc))
        else:
            return messages_response(msg("success", f"Import complete: {created} new, {updated} updated."))
    raise form_errors(form)


@doc(TAGS, "Download the collection of the game of ?game as CSV", parameters=[GAME])
@api_view(["GET"])
def export_csv(request):
    game = game_param(request.query_params)
    items = CollectionItem.objects.owned_by(request.user).filter(card__game=game)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="cardledger-{game.slug}-{stamp}.csv"'
    services.export_csv(items, response)
    SyncLog.objects.create(owner=request.user, message=f"CSV export: {game.name} collection exported ({items.count():,} rows)")
    return response
