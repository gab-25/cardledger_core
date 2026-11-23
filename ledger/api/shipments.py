"""The shipments API (/api/shipments/): the parcels of the shipped orders (one per order, created when an order
becomes sent: services.record_shipment). No tracking provider: the status (ShipmentQuerySet.with_status) comes from
expected_by, the market state and what the seller marks."""

from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .. import shipping
from ..forms import ShipmentForm
from ..models import Market, Shipment
from ..presentation import SHIPMENT_TABS
from .common import PAGE, doc, form_body, form_errors, messages_response, msg, page_payload, query

TAGS = ["shipments"]
_St = Shipment.Status
_TAB_FILTERS = {
    "in_transit": Q(status__in=[_St.IN_TRANSIT, _St.LATE, _St.NO_TRACKING]),
    "delivered": Q(status=_St.DELIVERED),
    "problems": Q(problem=True),
    "returned": Q(status=_St.RETURNED),
}


def _money(value):
    return None if value is None else f"{value:.2f}"


def shipments_of(user, today):
    return Shipment.objects.owned_by(user).with_status(today).select_related("order")


def problem_count(user):
    """Late shipments and untracked ones past their expected date (me's shipment_problems)."""
    return shipments_of(user, timezone.localdate()).filter(problem=True).count()


def _row(s, today):
    order = s.order
    shipped_on = shipping.local_date(s.shipped_at)
    return {
        "id": s.pk,
        "order_id": order.pk,
        "order_code": order.code,
        "market": order.market,
        "buyer_username": order.buyer_username,
        "buyer_name": order.buyer_name,
        "buyer_email": order.buyer_email,
        "ship_city": order.ship_city,
        "ship_country_code": order.ship_country_code,
        "carrier": s.carrier,
        "shipping_method": order.shipping_method,
        "tracking_code": s.tracking_code,
        "tracked": s.tracked,
        "shipped_at": s.shipped_at,
        "expected_by": s.expected_by,
        "delivered_at": s.delivered_at,
        "returned_at": s.returned_at,
        "postage_cost": _money(s.postage_cost),
        "note": s.note,
        "total": _money(order.total),
        "status": s.status,
        "days_since_shipped": (today - shipped_on).days if shipped_on else None,
    }


def _kpis(user, qs, today):
    month_ago = timezone.now() - timedelta(days=30)
    delivered = qs.filter(status=_St.DELIVERED, delivered_at__gte=month_ago)
    delivered_30d = delivered.count()
    on_time = delivered.filter(delivered_at__date__lte=F("expected_by")).count()
    problems = qs.filter(problem=True).aggregate(n=Count("pk"), value=Sum("order__total"))
    postage = qs.filter(shipped_at__gte=month_ago, postage_cost__isnull=False).aggregate(
        n=Count("pk"), total=Sum("postage_cost"))
    in_transit = shipments_of(user, today).filter(_TAB_FILTERS["in_transit"])
    by_market = dict(in_transit.order_by().values_list("order__market").annotate(n=Count("pk")))
    return {
        "in_transit": qs.filter(_TAB_FILTERS["in_transit"]).count(),
        "in_transit_by_market": {m: by_market.get(m, 0) for m in Market.values},
        "delivered_30d": delivered_30d,
        "on_time_pct": f"{Decimal(on_time * 100) / delivered_30d:.1f}" if delivered_30d else None,
        "problems": problems["n"],
        "value_at_risk": _money(problems["value"]) if problems["n"] else None,
        "postage_30d": _money(postage["total"]),
        "postage_per_order": _money(postage["total"] / postage["n"]) if postage["n"] else None,
    }


@doc(TAGS, "Shipments of one tab (?tab: in_transit (on the way, problems included), delivered, problems (late, "
           "or untracked past expected_by), returned; ?market: one market's; ?page), newest first, with the tabs' "
           "counts and the KPIs (of the market filtered, except in_transit_by_market): in_transit, delivered_30d "
           "(delivered in the last 30 days), on_time_pct (of those, delivered by expected_by: a percent with one "
           "decimal, e.g. \"98.6\"; null with none), problems, value_at_risk (their orders' total), postage_30d "
           "and postage_per_order (shipped in the last 30 days with a postage cost). Status: returned, delivered "
           "(marked, or arrived on the market), no_tracking (untracked method and no code), late (past expected_by), in_transit",
     parameters=[query("tab", "The tab (default: in_transit)", enum=list(SHIPMENT_TABS)),
                 query("market", "Only this market's shipments (cm, ct)", enum=Market.values), PAGE])
@api_view(["GET"])
def shipments(request):
    tab = request.query_params.get("tab", "in_transit")
    if tab not in SHIPMENT_TABS:
        tab = "in_transit"
    today = timezone.localdate()
    qs = shipments_of(request.user, today)
    market = request.query_params.get("market", "")
    if market in Market.values:
        qs = qs.filter(order__market=market)
    else:
        market = ""
    shown = qs.filter(_TAB_FILTERS[tab]).order_by(F("shipped_at").desc(nulls_last=True), "-id")
    return Response({
        "tab": tab,
        "market": market,
        "tabs": [{"key": key, "label": label, "count": qs.filter(_TAB_FILTERS[key]).count()}
                 for key, label in SHIPMENT_TABS.items()],
        "kpis": _kpis(request.user, qs, today),
        **page_payload(request, shown, lambda s: _row(s, today)),
    })


@doc(TAGS, "Change a shipment: carrier, tracking_code, postage_cost, note (a key left out keeps its value), "
           "delivered / returned (true: marked now; false: the mark is cleared; delivered clears returned)",
     request=form_body("ShipmentChanges", ShipmentForm))
@extend_schema(operation_id="shipment_partial_update")
@api_view(["PATCH"])
def shipment(request, pk):
    obj = get_object_or_404(Shipment.objects.owned_by(request.user).select_related("order"), pk=pk)
    form = ShipmentForm(request.data, instance=obj)
    if not form.is_valid():
        raise form_errors(form)
    form.save()
    return messages_response(msg("success", f"Shipment of order #{obj.order.code} updated."))
