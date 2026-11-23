"""The sale API (/api/listings/): the listings of every market, their edit form (one listing per item and market),
bulk pause/resume/delete, publishing to CardTrader and market prices."""

from django.db.models import DecimalField, ExpressionWrapper, F, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response

from .. import cardmarket, services
from ..cardtrader import CardTraderError
from ..forms import ListingForm
from ..models import CollectionItem, Listing, Market
from ..presentation import listing_badge
from .common import GAME, body, doc, form_body, form_errors, game_param, messages_response, msg, query
from .errors import ServiceError

TAGS = ["sale"]
VALUE = ExpressionWrapper(F("price") * F("quantity"), output_field=DecimalField())
MARKET = query("market", "Only this market's listings (cm: Cardmarket, ct: CardTrader)", enum=Market.values)
BULK_ACTIONS = ["pause", "resume", "delete"]


def _money(value):
    return None if value is None else f"{value:.2f}"


def _row(listing):
    item = listing.item
    return {
        "id": listing.pk,
        "item_id": item.pk,
        "name": item.card.name,
        "set_code": item.card.set_code,
        "condition": item.condition,
        "language": item.language,
        "foil": item.foil,
        "market": listing.market,
        "quantity": listing.quantity,
        "price": _money(listing.price),
        "status": listing.status,
        "error_message": listing.error_message,
        "listed_at": listing.listed_at,
        "badge": listing_badge(listing.status),
    }


def _stats(qs):
    active = qs.exclude(status=Listing.Status.SOLD)
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "active": active.count(),
        "value": _money(active.aggregate(v=Sum(VALUE))["v"] or 0),
        "sold_month": qs.filter(status=Listing.Status.SOLD, sold_at__gte=month_start).count(),
        "to_publish": qs.filter(status=Listing.Status.TO_PUBLISH).count(),
        "paused": qs.filter(status=Listing.Status.PAUSED).count(),
        "error": qs.filter(status=Listing.Status.ERROR).count(),
    }


def _market(value, default=Market.CARDTRADER):
    """A market code (default CardTrader when absent or blank); an unknown one is a validation error."""
    value = (str(value) if value is not None else "").strip() or default
    if value not in Market.values:
        raise ValidationError({"market": [f"Unknown market '{value}'."]})
    return value


@doc(TAGS, "Listings of the game, of every market (?market, ?status filter them), with the sale stats: overall "
           "(every market) and by_market",
     parameters=[GAME, MARKET, query("status", "Only the listings in this status", enum=Listing.Status.values)])
@api_view(["GET"])
def listings(request):
    params = request.query_params
    game = game_param(params)
    qs = (Listing.objects.owned_by(request.user).filter(item__card__game=game, pending_delete=False)
          .select_related("item__card"))
    stats = _stats(qs)
    by_market = {market: _stats(qs.filter(market=market)) for market in Market.values}
    status = params.get("status", "")
    if status in Listing.Status.values:
        qs = qs.filter(status=status)
    else:
        status = ""
    market = params.get("market", "")
    if market in Market.values:
        qs = qs.filter(market=market)
    else:
        market = ""
    return Response({"stats": stats, "by_market": by_market, "status": status,
                     "market": market, "results": [_row(l) for l in qs]})


def _listing_out(listing):
    return {
        "market": listing.market, "quantity": listing.quantity, "price": _money(listing.price),
        "status": listing.status, "error_message": listing.error_message, "listed_at": listing.listed_at,
        "market_listing_id": listing.market_listing_id,
    }


@doc(TAGS, "The listings of a collection item, one per market: read (GET: listings, plus listing, the CardTrader "
           "one or a new one's defaults), create or change one (PUT, market default ct), remove one (DELETE "
           "?market=, default ct: a CardTrader listing is deleted on CardTrader, another one still on its market "
           "by the next sync)",
     request=form_body("Listing", ListingForm, market=serializers.ChoiceField(
         choices=Market.choices, required=False, help_text="Default ct")),
     parameters=[query("market", "DELETE: the market of the listing to remove (default ct)", enum=Market.values)])
@extend_schema(methods=["GET"], operation_id="listing_retrieve")
@api_view(["GET", "PUT", "DELETE"])
def listing(request, item_pk):
    item = get_object_or_404(CollectionItem.objects.owned_by(request.user).select_related("card"), pk=item_pk)

    if request.method == "DELETE":
        market = _market(request.query_params.get("market"))
        existing = item.listings.filter(market=market, pending_delete=False).first()
        if existing is None:
            raise NotFound()
        try:
            services.remove_listing(existing)
        except CardTraderError as exc:
            raise ServiceError(f"Listing not removed: {exc}")
        return messages_response(msg("success", "Listing removed."))

    if request.method == "PUT":
        market = _market(request.data.get("market"))
        existing = item.listings.filter(market=market).first()
        obj = existing or Listing(item=item, market=market, price=cardmarket.suggested_price(item.card, item.foil),
                                  quantity=1)
        old_status, removed = obj.status, obj.pending_delete
        form = ListingForm(request.data, instance=obj)
        if not form.is_valid():
            raise form_errors(form)
        obj = form.save(commit=False)
        obj.pending_delete = False
        if obj.status == Listing.Status.SOLD and old_status != Listing.Status.SOLD:
            obj.sold_at = timezone.now()
        elif (
            obj.pk
            and obj.status == Listing.Status.PUBLISHED
            and ("status" not in form.changed_data and form.changed_data or removed)
        ):
            # Edited locally (or put back after a removal): the market needs the new values.
            obj.status = Listing.Status.TO_PUBLISH
        if obj.status != Listing.Status.ERROR:
            obj.error_message = ""
        obj.save()
        return messages_response(msg("success", f"Listing for {item.card.name} saved."))

    ct = item.listings.filter(market=Market.CARDTRADER, pending_delete=False).first()
    suggestion = cardmarket.suggested_price(item.card, item.foil)
    obj = ct or Listing(item=item, price=suggestion, quantity=1)
    return Response({
        "game": item.card.game.slug,
        "item": {
            "id": item.pk, "name": item.card.name, "set_code": item.card.set_code, "condition": item.condition,
            "language": item.language, "foil": item.foil, "quantity": item.quantity,
        },
        "listing": {
            "exists": obj.pk is not None, "quantity": obj.quantity, "price": _money(obj.price),
            "status": obj.status,
        },
        "listings": [_listing_out(l) for l in item.listings.filter(pending_delete=False).order_by("market")],
        "cm_suggestion": _money(suggestion),
    })


@doc(TAGS, "Act on the listings of several collection items (ids: item ids; market: only that market's, default "
           "every market): pause (take off the market at the next sync, kept here), resume (paused ones back to "
           "publish) or delete (removed here; deleted on the market by the next sync)",
     request=body("ListingsBulk", {
         "ids": serializers.ListField(child=serializers.IntegerField(),
                                      help_text="Ids of the user's collection items (others are ignored)"),
         "market": serializers.ChoiceField(choices=Market.choices, required=False),
         "action": serializers.ChoiceField(choices=BULK_ACTIONS),
     }))
@api_view(["POST"])
def bulk(request):
    ids = request.data.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ValidationError({"ids": ["A non-empty list of item ids."]})
    action = request.data.get("action")
    if action not in BULK_ACTIONS:
        raise ValidationError({"action": [f"One of {', '.join(BULK_ACTIONS)}."]})
    qs = Listing.objects.owned_by(request.user).filter(item_id__in=ids, pending_delete=False)
    if request.data.get("market"):
        qs = qs.filter(market=_market(request.data["market"]))
    n = 0
    if action == "pause":
        n = qs.filter(status__in=[Listing.Status.PUBLISHED, Listing.Status.TO_PUBLISH, Listing.Status.ERROR]).update(
            status=Listing.Status.PAUSED, error_message="", updated_at=timezone.now())
        text = f"Paused {n} listing{'s' if n != 1 else ''}."
    elif action == "resume":
        n = qs.filter(status=Listing.Status.PAUSED).update(status=Listing.Status.TO_PUBLISH,
                                                           updated_at=timezone.now())
        text = f"Resumed {n} listing{'s' if n != 1 else ''}: they will be published by the next sync."
    else:
        for obj in qs.select_related("item__card"):
            services.remove_listing(obj, remote=False)
            n += 1
        text = f"Removed {n} listing{'s' if n != 1 else ''}."
    return messages_response(msg("success" if n else "warning", text), count=n)


@doc(TAGS, "Publish the CardTrader listings waiting to be published to CardTrader (one game, or every game)",
     parameters=[query("game", "Slug of the game; empty: every game")])
@api_view(["POST"])
def publish(request):
    params = request.query_params
    game = game_param(params) if params.get("game", "").strip() else None
    try:
        result = services.publish_pending(request.user, game)
    except CardTraderError as exc:
        raise ServiceError(f"Publish failed: {exc}")
    n = result.published
    msgs = [msg("success", f"Published {n} listing{'s' if n != 1 else ''} to CardTrader.")]
    msgs += [msg("warning", f"{l.item.card.name}: {reason}") for l, reason in result.failed]
    return messages_response(*msgs)
