"""The orders API (/api/orders/): the sale orders of every market and their packing list. Orders are imported and
shipped by the browser extension (POST /api/extension/report/ and shipped/)."""

from django.db.models import Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.response import Response

from ..models import Market, MarketConnection, Order
from ..presentation import ORDER_TABS, order_badge
from .common import doc, query

TAGS = ["orders"]
ROW_LIMIT = 200


def _money(value):
    return None if value is None else f"{value:.2f}"


def _row(order):
    return {
        "id": order.pk,
        "code": order.code,
        "market": order.market,
        "market_order_id": order.market_order_id,
        "packing_number": order.packing_number,
        "can_ship": order.can_ship,
        "buyer_username": order.buyer_username,
        "buyer_name": order.buyer_name,
        "via_cardtrader_zero": order.via_cardtrader_zero,
        "ship_city": order.ship_city,
        "ship_country_code": order.ship_country_code,
        "shipping_method": order.shipping_method,
        "tracked": order.tracked,
        "size": order.size,
        "sent_at": order.sent_at,
        "paid_at": order.paid_at,
        "total": _money(order.total),
        "items_total": _money(order.items_total),
        "shipping_cost": _money(order.shipping_cost),
        "market_fee": _money(order.market_fee),
        "net": _money(order.net),
        "currency": order.currency,
        "badge": order_badge(order.state),
    }


def _orders_synced_at(user, market):
    """When the extension last read the orders of market (of any market when market is ""), None if never."""
    conns = MarketConnection.objects.owned_by(user).exclude(orders_synced_at=None)
    if market:
        conns = conns.filter(market=market)
    return conns.order_by("-orders_synced_at").values_list("orders_synced_at", flat=True).first()


@doc(TAGS, "Orders of one tab (?tab: to_ship, ct_zero, shipped, completed, issues; ?market: one market's) with "
           "the shipping stats (of the market filtered) and orders_synced_at (the extension's latest read of the "
           "orders, of that market or any)",
     parameters=[query("tab", "The tab (default: to_ship)", enum=list(ORDER_TABS)),
                 query("market", "Only this market's orders (cm, ct)", enum=Market.values)])
@api_view(["GET"])
def orders(request):
    tab = request.query_params.get("tab", "to_ship")
    if tab not in ORDER_TABS:
        tab = "to_ship"
    qs = Order.objects.owned_by(request.user).prefetch_related("items")
    market = request.query_params.get("market", "")
    if market in Market.values:
        qs = qs.filter(market=market)
    else:
        market = ""
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    to_ship = qs.filter(state=Order.State.PAID)
    stats = {
        "to_ship": to_ship.count(),
        "items_to_ship": to_ship.aggregate(n=Sum("size"))["n"] or 0,
        "shipped_month": qs.filter(sent_at__gte=month_start).count(),
        "revenue_month": _money(
            qs.filter(paid_at__gte=month_start).exclude(state__in=[Order.State.CANCELED, Order.State.CLOSED])
            .aggregate(v=Sum("total"))["v"] or 0
        ),
    }
    tabs = [{"key": key, "label": label, "count": qs.filter(state__in=states).count()}
            for key, (label, states) in ORDER_TABS.items()]
    shown = qs.filter(state__in=ORDER_TABS[tab][1])
    if tab == "to_ship":
        shown = shown.order_by("packing_number", "paid_at")
    return Response({
        "tab": tab,
        "market": market,
        "stats": stats,
        "tabs": tabs,
        "orders_synced_at": _orders_synced_at(request.user, market),
        "results": [_row(o) for o in shown[:ROW_LIMIT]],
    })


def _location(order_item):
    """The storage location of the collection item an order line sold ("" when unknown)."""
    return order_item.listing.item.location if order_item.listing else ""


def _packing_key(order_item):
    """Packing list order: by location (lines without one last), then by name."""
    location = _location(order_item)
    return (location == "", location, order_item.name)


def _order_item(it):
    return {
        "id": it.pk, "name": it.name, "expansion": it.expansion, "foil": it.foil,
        "condition": it.condition, "language": it.language, "quantity": it.quantity,
        "price": _money(it.price), "item_pk": it.listing.item_id if it.listing else None,
        "location": _location(it),
    }


@doc(TAGS, "One order with its address and packing list (sorted by the items' storage location, then name)")
@extend_schema(operation_id="order_retrieve")
@api_view(["GET"])
def order(request, pk):
    obj = get_object_or_404(Order.objects.owned_by(request.user).prefetch_related("items__listing__item"), pk=pk)
    return Response({
        **_row(obj),
        "state": obj.state,
        "state_label": obj.state_label,
        "address_lines": obj.address_lines,
        "ship_note": obj.ship_note,
        "buyer_email": obj.buyer_email,
        "buyer_phone": obj.buyer_phone,
        "max_shipping_days": obj.max_shipping_days,
        "tracking_code": obj.tracking_code,
        "items": [_order_item(it) for it in sorted(obj.items.all(), key=_packing_key)],
    })
