"""The orders API (/api/orders/): the sale orders of every market, picking and packing them, pick lists and packing
slips. Orders are imported and shipped by the browser extension (POST /api/extension/report/ and shipped/); the
server never calls a market.

Each order has a stage (Order.Stage, computed by OrderQuerySet.with_stage) and, while it still has to be shipped
(paid or CT Zero pending), a deadline (ledger/shipping.py: due_by, due)."""

from datetime import datetime, timezone as dt_timezone

from django.db.models import F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .. import services, shipping
from ..forms import IssueForm, PackForm, PickedForm
from ..models import Market, MarketConnection, Order, OrderItem
from ..presentation import OLD_ORDER_TABS, ORDER_TABS, order_badge
from .common import doc, form_body, form_errors, messages_response, msg, query
from .errors import Blocked

TAGS = ["orders"]
ROW_LIMIT = 200
_St = Order.Stage
# Orders still to be shipped: the only ones with a deadline, the only ones that can be packed
OPEN_STATES = (Order.State.PAID, Order.State.HUB_PENDING)
_TAB_FILTERS = {
    "to_pack": Q(stage__in=[_St.TO_PACK, _St.PICKING]),
    "packed": Q(stage=_St.PACKED),
    "shipped": Q(stage=_St.SHIPPED, state__in=Order.SHIPPED_STATES),
    "issues": Q(stage=_St.ISSUE),
}
_NEVER = datetime.max.replace(tzinfo=dt_timezone.utc)


def _money(value):
    return None if value is None else f"{value:.2f}"


def orders_of(user):
    """The user's orders with stage, items_count (copies) and picked_count (copies picked)."""
    return Order.objects.owned_by(user).with_stage().annotate(
        items_count=Coalesce(Sum("items__quantity"), Value(0)),
        picked_count=Coalesce(Sum("items__quantity", filter=Q(items__picked=True)), Value(0)),
    )


def to_pack_count(user):
    """Orders to pack or being picked, every market (me's orders_to_pack)."""
    return Order.objects.owned_by(user).with_stage().filter(_TAB_FILTERS["to_pack"]).count()


def _deadline(order):
    return shipping.due_by(order.paid_at, order.max_shipping_days) if order.state in OPEN_STATES else None


def _row(order, today):
    deadline = _deadline(order)
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
        "stage": order.stage,
        "due": shipping.due(deadline, today),
        "due_by": deadline,
        "picked_count": order.picked_count,
        "items_count": order.items_count,
        "packed_at": order.packed_at,
        "issue_note": order.issue_note,
        "invoice_number": order.invoice_number,
    }


def _queue_key(today):
    """To-pack order: overdue first, then by payment (oldest first)."""
    def key(order):
        return (shipping.due(_deadline(order), today) != "overdue", order.paid_at or _NEVER, order.pk)
    return key


def _to_pack_queue(user, today):
    """The ids of the user's orders to pack, in to-pack order (every market)."""
    queue = Order.objects.owned_by(user).with_stage().filter(_TAB_FILTERS["to_pack"])
    return [o.pk for o in sorted(queue.only("pk", "paid_at", "max_shipping_days", "state"), key=_queue_key(today))]


def _next_after(queue, pk):
    """The order to pack after pk: the one following it in the queue (the first when it is last or not queued);
    None when no other order is queued."""
    others = [i for i in queue if i != pk]
    if not others:
        return None
    position = queue.index(pk) if pk in queue else 0
    return others[position] if position < len(others) else others[0]


def _orders_synced_at(user, market):
    """When the extension last read the orders of market (of any market when market is ""), None if never."""
    conns = MarketConnection.objects.owned_by(user).exclude(orders_synced_at=None)
    if market:
        conns = conns.filter(market=market)
    return conns.order_by("-orders_synced_at").values_list("orders_synced_at", flat=True).first()


@doc(TAGS, "Orders of one tab (?tab: to_pack (to pack or being picked; overdue first, then by payment), packed, "
           "shipped (newest first), issues; the old keys to_ship, ct_zero and completed still work; ?market: one "
           "market's) with the tabs' counts and the shipping stats (both of the market filtered) and "
           "orders_synced_at (the extension's latest read of the orders, of that market or any). Each row has its "
           "stage and, while it has to be shipped, due (overdue, today, ok) and due_by: paid_at + max_shipping_days "
           "working days (2 when the market gives none)",
     parameters=[query("tab", "The tab (default: to_pack)", enum=[*ORDER_TABS, *OLD_ORDER_TABS]),
                 query("market", "Only this market's orders (cm, ct)", enum=Market.values)])
@api_view(["GET"])
def orders(request):
    tab = request.query_params.get("tab", "to_pack")
    tab = OLD_ORDER_TABS.get(tab, tab)
    if tab not in ORDER_TABS:
        tab = "to_pack"
    plain = Order.objects.owned_by(request.user)
    market = request.query_params.get("market", "")
    if market in Market.values:
        plain = plain.filter(market=market)
    else:
        market = ""
    qs = orders_of(request.user).filter(market=market) if market else orders_of(request.user)
    today = timezone.localdate()
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    to_ship = plain.filter(state=Order.State.PAID)
    stats = {
        "to_ship": to_ship.count(),
        "items_to_ship": to_ship.aggregate(n=Sum("size"))["n"] or 0,
        "shipped_month": plain.filter(sent_at__gte=month_start).count(),
        "shipped_today": plain.filter(sent_at__date=today).count(),
        "revenue_month": _money(
            plain.filter(paid_at__gte=month_start).exclude(state__in=[Order.State.CANCELED, Order.State.CLOSED])
            .aggregate(v=Sum("total"))["v"] or 0
        ),
    }
    with_stage = plain.with_stage()
    tabs = [{"key": key, "label": label, "count": with_stage.filter(_TAB_FILTERS[key]).count()}
            for key, label in ORDER_TABS.items()]
    shown = qs.filter(_TAB_FILTERS[tab])
    if tab == "to_pack":
        shown = sorted(shown, key=_queue_key(today))
    elif tab == "shipped":
        shown = shown.order_by(F("sent_at").desc(nulls_last=True), "-id")
    return Response({
        "tab": tab,
        "market": market,
        "stats": stats,
        "tabs": tabs,
        "orders_synced_at": _orders_synced_at(request.user, market),
        "results": [_row(o, today) for o in shown[:ROW_LIMIT]],
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
        "location": _location(it), "picked": it.picked,
    }


def _detail(obj, today, next_id):
    return {
        **_row(obj, today),
        "state": obj.state,
        "state_label": obj.state_label,
        "address_lines": obj.address_lines,
        "ship_note": obj.ship_note,
        "buyer_email": obj.buyer_email,
        "buyer_phone": obj.buyer_phone,
        "max_shipping_days": obj.max_shipping_days,
        "tracking_code": obj.tracking_code,
        "items": [_order_item(it) for it in sorted(obj.items.all(), key=_packing_key)],
        "next_id": next_id,
    }


@doc(TAGS, "One order with its address, its packing list (sorted by the items' storage location, then name; each "
           "line with picked), its stage and deadline, and next_id: the next order to pack after it (to-pack order, "
           "every market; null when none)")
@extend_schema(operation_id="order_retrieve")
@api_view(["GET"])
def order(request, pk):
    obj = get_object_or_404(orders_of(request.user).prefetch_related("items__listing__item"), pk=pk)
    today = timezone.localdate()
    return Response(_detail(obj, today, _next_after(_to_pack_queue(request.user, today), obj.pk)))


@doc(TAGS, "Tick (or untick) an order line as picked; answers the order's picked_count and items_count (copies) "
           "and its stage", request=form_body("OrderItemPicked", PickedForm))
@api_view(["PATCH"])
def order_item(request, pk, item_pk):
    item = get_object_or_404(OrderItem.objects.owned_by(request.user), order_id=pk, pk=item_pk)
    form = PickedForm(request.data)
    if not form.is_valid():
        raise form_errors(form)
    item.picked = form.cleaned_data["picked"]
    item.save(update_fields=["picked"])
    obj = orders_of(request.user).get(pk=pk)
    return Response({"picked_count": obj.picked_count, "items_count": obj.items_count, "stage": obj.stage})


def _open_order(request, pk):
    obj = get_object_or_404(orders_of(request.user), pk=pk)
    if obj.state not in OPEN_STATES:
        raise Blocked(f"Order #{obj.code} is no longer to be shipped.")
    return obj


@doc(TAGS, "Pack an order still to be shipped (409 blocked when some line is not picked, unless force, or when the "
           "order is no longer to be shipped); answers the messages and next_id, the next order to pack (to-pack "
           "order, every market; null when none)", request=form_body("OrderPack", PackForm))
@api_view(["POST"])
def pack(request, pk):
    obj = _open_order(request, pk)
    form = PackForm(request.data)
    if not form.is_valid():
        raise form_errors(form)
    if obj.picked_count < obj.items_count and not form.cleaned_data["force"]:
        raise Blocked("Pick every line first, or pack it anyway.")
    queue = _to_pack_queue(request.user, timezone.localdate())
    if obj.packed_at is None:
        obj.packed_at = timezone.now()
        obj.save(update_fields=["packed_at", "updated_at"])
    return messages_response(msg("success", f"Order #{obj.code} packed."), next_id=_next_after(queue, obj.pk))


@doc(TAGS, "Unpack an order still to be shipped (back to pack; 409 blocked when it is no longer to be shipped)")
@api_view(["POST"])
def unpack(request, pk):
    obj = _open_order(request, pk)
    if obj.packed_at is not None:
        obj.packed_at = None
        obj.save(update_fields=["packed_at", "updated_at"])
    return messages_response(msg("success", f"Order #{obj.code} is back to pack."))


@doc(TAGS, "Note a problem on an order (it goes to the Issues tab); a blank note clears it",
     request=form_body("OrderIssue", IssueForm))
@api_view(["POST"])
def issue(request, pk):
    obj = get_object_or_404(Order.objects.owned_by(request.user), pk=pk)
    form = IssueForm(request.data)
    if not form.is_valid():
        raise form_errors(form)
    note = form.cleaned_data["note"].strip()
    obj.issue_note, obj.issue_at = note, (timezone.now() if note else None)
    obj.save(update_fields=["issue_note", "issue_at", "updated_at"])
    text = f"Issue noted on order #{obj.code}." if note else f"Issue cleared on order #{obj.code}."
    return messages_response(msg("success", text))


IDS = query("ids", "Comma-separated order ids (the user's; others are ignored)")


def _selected(request, queryset):
    """The user's orders of ?ids (queryset: already the user's), in that order."""
    raw = request.query_params.get("ids", "")
    try:
        ids = list(dict.fromkeys(int(part) for part in raw.split(",") if part.strip()))
    except ValueError:
        raise ValidationError({"ids": ["Enter comma-separated order ids."]}) from None
    found = {o.pk: o for o in queryset.filter(pk__in=ids)}
    return [found[i] for i in ids if i in found]


def _pick_key(order_item):
    """Lines of the same collection item merge; without one, lines of the same name, expansion, condition, language
    and foil."""
    if order_item.listing:
        return ("item", order_item.listing.item_id)
    it = order_item
    return ("line", it.name, it.expansion, it.condition, it.language, it.foil)


@doc(TAGS, "The combined pick list of some orders: the lines merged by collection item (by name, expansion, "
           "condition, language and foil when a line has none) with the total quantity, the orders' codes and "
           "picked (every merged line picked), sorted by location (none last) then name",
     parameters=[IDS])
@extend_schema(operation_id="orders_pick_list")
@api_view(["GET"])
def pick_list(request):
    selected = _selected(request, Order.objects.owned_by(request.user).prefetch_related("items__listing__item"))
    lines = {}
    for obj in selected:
        for it in obj.items.all():
            line = lines.setdefault(_pick_key(it), {
                "location": _location(it), "name": it.name, "expansion": it.expansion,
                "condition": it.condition, "language": it.language, "foil": it.foil,
                "quantity": 0, "orders": [], "picked": True,
            })
            line["quantity"] += it.quantity
            if obj.code not in line["orders"]:
                line["orders"].append(obj.code)
            line["picked"] = line["picked"] and it.picked
    return Response({
        "orders": [{"id": o.pk, "code": o.code, "market": o.market} for o in selected],
        "lines": sorted(lines.values(), key=lambda l: (l["location"] == "", l["location"], l["name"])),
    })


@doc(TAGS, "Packing slips of some orders: the business details (as GET /api/account/business/) and each order as "
           "GET /api/orders/<id>/ answers it. Orders without an invoice number get the next ones, in the order "
           "asked: invoice_prefix + invoice_next_number zero-padded to 4 digits, which then moves on",
     parameters=[IDS])
@extend_schema(operation_id="orders_slips")
@api_view(["GET"])
def slips(request):
    from accounts.api import business_data

    selected = _selected(request, orders_of(request.user).prefetch_related("items__listing__item"))
    services.assign_invoice_numbers(request.user, selected)
    today = timezone.localdate()
    queue = _to_pack_queue(request.user, today)
    return Response({
        "business": business_data(request.user),
        "orders": [_detail(o, today, _next_after(queue, o.pk)) for o in selected],
    })
