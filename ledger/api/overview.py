"""The overview API (/api/overview/): the dashboard of one game (?game, default Magic; never all of them): revenue,
profit, sell-through, stock value by set, price movers and alerts.

- Orders are not per game, so as in the reports an order counts for ?game when one of its lines is of that game (line
  → listing → item → card's game), for the share of its value in those lines (price × quantity; evenly when no line
  has a price); lines not linked to a collection item belong to no game. Revenue (Σ total × share), orders (those
  counted), basket (revenue / orders) and gross profit (the game's lines with a cost) are of ?market when given, over
  ?period: 7d / 30d (rolling, the previous 7 / 30 days before), qtd / ytd (since the start of the quarter / year,
  local dates; the previous period is the same span from the start of the previous quarter / year). The *_30d names
  are kept for compatibility and follow the period too.
- revenue_weeks: the last 12 Monday-start weeks (whatever the period) with the game's share of the orders' total, and
  its lines' price × quantity split into singles and sealed by the card's kind.
- sell_through = copies sold in the period / (copies in stock now + copies sold in the period) × 100, i.e. sold / (stock
  at the period start + copies added in it), one decimal; sell_through_prev is the previous period's, its end stock
  being now's + sold − added in the period (added: copies of the items created in it). Sold copies follow ?market.
- stock_by_set: the game's STOCK_SETS sets with the largest trend value in stock, largest first.
- movers and the collection alerts are of ?game; the orders to pack and the locations almost full are of every game
  (packing and storage are shared). Alert hrefs are SPA paths.
"""

from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum
from django.db.models.functions import Abs
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .. import shipping
from ..cardmarket import item_guide_price, trend_price
from ..models import MAGIC, CollectionItem, Expansion, Game, Market, Order, OrderItem
from .collection import STALE_DAYS, _views, _with_blueprint
from .common import doc, query
from .locations import location_rows
from .orders import OPEN_STATES, _TAB_FILTERS

TAGS = ["overview"]
WEEKS = 12
MOVERS = 6
STOCK_SETS = 5
PERIODS = ["7d", "30d", "qtd", "ytd"]
FULL_PCT = Decimal("95")  # a location this full (used / capacity, %) is almost full
NOT_REVENUE = [Order.State.CANCELED, Order.State.CLOSED]  # as the orders page's revenue_month
DECIMAL = DecimalField(max_digits=20, decimal_places=6)
VALUE = ExpressionWrapper(F("trend") * F("quantity"), output_field=DECIMAL)
PROFIT = ExpressionWrapper((F("price") - F("listing__item__cost")) * F("quantity"), output_field=DECIMAL)
LINE_VALUE = ExpressionWrapper(F("price") * F("quantity"), output_field=DECIMAL)


def _money(value):
    return None if value is None else f"{value:.2f}"


def _aware(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def period_bounds(period, now):
    """((start, end), (prev_start, prev_end)) of a period, as aware datetimes."""
    if period in ("7d", "30d"):
        span = timedelta(days=int(period[:-1]))
        return (now - span, now), (now - 2 * span, now - span)
    today = timezone.localdate(now)
    if period == "qtd":
        first = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        prev_first = date(first.year - 1, 10, 1) if first.month == 1 else date(first.year, first.month - 3, 1)
    else:
        first, prev_first = date(today.year, 1, 1), date(today.year - 1, 1, 1)
    start, prev_start = _aware(first), _aware(prev_first)
    return (start, now), (prev_start, prev_start + (now - start))


def _share(order, game):
    """The share of `order`'s value in its lines of `game` (0 when it has none): price × quantity, evenly when no
    line has a price. The order's lines must be prefetched with their listing's item's card."""
    lines = list(order.items.all())
    mine = [line for line in lines if line.listing is not None and line.listing.item.card.game_id == game.pk]
    if not mine:
        return Decimal("0")
    values = {line.pk: (line.price or Decimal("0")) * line.quantity for line in lines}
    whole = sum(values.values(), Decimal("0"))
    if whole:
        return sum((values[line.pk] for line in mine), Decimal("0")) / whole
    return Decimal(len(mine)) / len(lines)


def _counted(orders, game, start, end):
    """[(order, share)] of the orders paid in [start, end) with a line of `game`."""
    window = (orders.filter(paid_at__gte=start, paid_at__lt=end)
              .prefetch_related("items__listing__item__card"))
    return [(o, share) for o in window if (share := _share(o, game))]


def _revenue(counted):
    totals = [o.total * share for o, share in counted if o.total is not None]
    return sum(totals, Decimal("0")) if totals else None


def _revenue_weeks(orders, game, today):
    """WEEKS Monday-start weeks up to this one, oldest first, with the game's share of the total of the orders paid in
    each and its lines' value split into singles and sealed."""
    first = today - timedelta(days=today.weekday() + 7 * (WEEKS - 1))
    zero = Decimal("0")
    weeks = {first + timedelta(weeks=i): {"total": zero, "singles": zero, "sealed": zero} for i in range(WEEKS)}
    start = _aware(first)

    def week_of(paid_at):
        day = timezone.localdate(paid_at)
        return weeks.get(day - timedelta(days=day.weekday()))

    for order, share in _counted(orders, game, start, timezone.now() + timedelta(days=1)):
        if order.total is not None and (week := week_of(order.paid_at)) is not None:
            week["total"] += order.total * share
    lines = (OrderItem.objects.filter(order__in=orders, order__paid_at__gte=start, price__isnull=False,
                                      listing__item__card__game=game)
             .values_list("order__paid_at", "listing__item__card__is_product").annotate(v=Sum(LINE_VALUE))
             .order_by())
    for paid_at, is_product, value in lines:
        if (week := week_of(paid_at)) is not None:
            week["sealed" if is_product else "singles"] += value
    return [{"week_start": week.isoformat(), **{k: f"{v:.2f}" for k, v in totals.items()}}
            for week, totals in weeks.items()]


def _stock_by_set(user, game):
    """The game's STOCK_SETS sets with the largest trend value in stock: [{code, name, value}], largest first."""
    totals = {}
    for code, value in (_items(user, game).filter(quantity__gt=0).annotate(trend=trend_price())
                        .values_list("card__set_code", VALUE)):
        if value is not None:
            totals[code] = totals.get(code, Decimal("0")) + value
    top = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:STOCK_SETS]
    names = dict(Expansion.objects.filter(game=game, code__in=[c.lower() for c, _ in top]).values_list("code", "name"))
    return [{"code": code, "name": names.get(code.lower()) or code or "—", "value": _money(value)} for code, value in top]


def _items(user, game):
    return CollectionItem.objects.owned_by(user).filter(card__game=game)


def _movers(user, game):
    """The items in stock whose 7-day average moved most from the 30-day one (foil-aware)."""
    items = (_items(user, game).filter(quantity__gt=0).select_related("card")
             .annotate(trend=trend_price(), avg7=item_guide_price("avg7"), avg30=item_guide_price("avg30"))
             .filter(avg7__isnull=False, avg30__isnull=False)
             .annotate(change=ExpressionWrapper((F("avg7") - F("avg30")) / F("avg30"), output_field=DECIMAL))
             .order_by(Abs("change").desc(), "pk"))[:MOVERS]
    return [
        {
            "id": item.pk,
            "name": item.card.name,
            "set_code": item.card.set_code,
            "quantity": item.quantity,
            "trend": _money(item.trend),
            "change_pct": f"{item.change * 100:+.1f}",
            "exposure": None if item.trend is None else _money(item.quantity * item.trend * item.change),
        }
        for item in items
    ]


def _sold(lines, game, start, end):
    lines = lines.filter(order__paid_at__gte=start, order__paid_at__lt=end, listing__item__card__game=game)
    return lines.aggregate(n=Sum("quantity"))["n"] or 0


def _rate(sold, base):
    return f"{sold / base * 100:.1f}" if base > 0 else None


def _sell_through(user, orders, game, current, prev):
    lines = OrderItem.objects.filter(order__in=orders)
    items = _items(user, game)
    stock = items.aggregate(n=Sum("quantity"))["n"] or 0
    added = items.filter(created_at__gte=current[0], created_at__lt=current[1]).aggregate(n=Sum("quantity"))["n"] or 0
    sold, sold_prev = _sold(lines, game, *current), _sold(lines, game, *prev)
    return _rate(sold, stock + sold), _rate(sold_prev, max(stock + sold - added, 0) + sold_prev)


def _plural(n, word, plural=None):
    return f"{n} {word if n == 1 else plural or word + 's'}"


def _order_alert(user, game, market, today):
    orders = Order.objects.owned_by(user).with_stage().filter(_TAB_FILTERS["to_pack"])
    if market:
        orders = orders.filter(market=market)
    orders = list(orders.only("paid_at", "max_shipping_days", "state"))
    if not orders:
        return None
    dues = [shipping.due(shipping.due_by(o.paid_at, o.max_shipping_days), today) if o.state in OPEN_STATES else None
            for o in orders]
    overdue, due_today = dues.count("overdue"), dues.count("today")
    paid = [o.paid_at for o in orders if o.paid_at is not None]
    parts = []
    if overdue:
        parts.append(f"{overdue} overdue")
    parts.append(f"{due_today} due today")
    if paid:
        parts.append(f"oldest {_plural((today - timezone.localdate(min(paid))).days, 'day')}")
    return {"kind": "orders_to_pack", "title": f"{_plural(len(orders), 'order')} to pack", "sub": " · ".join(parts),
            "href": f"/{game.slug}/orders?tab=to_pack", "tone": "danger" if overdue else "warn"}


def _collection_alerts(user, game):
    views = _views()
    items = _with_blueprint(_items(user, game))
    alerts = []

    below_items = items.filter(views["below_trend"])
    below = list(below_items.values_list("trend", "listed_min", "quantity"))
    if below:
        gaps = [(t - p) / t * 100 for t, p, _ in below]
        euro = sum(((t - p) * q for t, p, q in below), Decimal("0"))
        alerts.append({"kind": "below_trend", "title": f"{_plural(len(below), 'listing')} below trend",
                       "sub": f"avg −{sum(gaps) / len(gaps):.1f}% · €{euro:.2f} under trend",
                       "href": f"/{game.slug}/?view=below_trend", "tone": "warn"})

    unlisted = items.filter(views["not_listed"], quantity__gt=0)
    totals = unlisted.aggregate(copies=Sum("quantity"), value=Sum(VALUE))
    if totals["copies"]:
        sub = f"€{totals['value'] or 0:.2f} at trend"
        lots = Counter(dict(unlisted.filter(lot__isnull=False).values_list("lot__code")
                            .annotate(n=Sum("quantity")).order_by()))
        if lots:
            code, n = sorted(lots.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            sub += f" · {n} from {code}"
        alerts.append({"kind": "not_listed", "title": f"{_plural(totals['copies'], 'copy', 'copies')} not listed",
                       "sub": sub, "href": f"/{game.slug}/?view=not_listed", "tone": "info"})

    stale = items.filter(views["stale"])
    totals = stale.aggregate(n=Count("pk"), value=Sum(VALUE))
    if totals["n"]:
        alerts.append({"kind": "stale", "title": f"{_plural(totals['n'], 'item')} unsold for {STALE_DAYS}+ days",
                       "sub": f"€{totals['value'] or 0:.2f} at trend",
                       "href": f"/{game.slug}/?view=stale", "tone": "info"})
    return alerts


def _location_alert(user, game):
    full = [r for r in location_rows(user)
            if r["capacity"] and Decimal(r["used"]) * 100 >= FULL_PCT * r["capacity"]]
    if not full:
        return None
    href = f"/{game.slug}/?location={full[0]['code']}" if len(full) == 1 else f"/{game.slug}/locations"
    return {"kind": "locations_full", "title": f"{_plural(len(full), 'location')} almost full",
            "sub": " · ".join(f"{r['code']} {r['used']}/{r['capacity']}" for r in full[:5]),
            "href": href, "tone": "warn"}


def alerts(user, game, market, today):
    """[{kind, title, sub, href, tone}] (tone danger|warn|info): orders to pack, listings below trend, copies not
    listed, stale items, locations almost full (used ≥ 95% of the capacity); only those that apply."""
    out = [_order_alert(user, game, market, today), *_collection_alerts(user, game), _location_alert(user, game)]
    return [a for a in out if a is not None]


def _filters(params):
    slug = params.get("game", "").strip() or MAGIC
    try:
        game = Game.by_slug(slug)
    except Game.DoesNotExist:
        raise ValidationError({"game": [f"Unknown game '{slug}'."]}) from None
    market = params.get("market", "").strip()
    if market and market not in Market.values:
        raise ValidationError({"market": ["cm, ct or empty."]})
    period = params.get("period", "").strip() or "30d"
    if period not in PERIODS:
        raise ValidationError({"period": [f"One of {', '.join(PERIODS)}."]})
    return game, market, period


@doc(TAGS, "Dashboard of ?game: revenue, orders, basket and gross profit of ?period with the previous period's revenue "
           "(the orders with a line of the game, for its share; of ?market), weekly revenue with singles and sealed, "
           "sell-through, stock value by set, the price movers and the alerts. See ledger/api/overview.py for the rules",
     parameters=[
         query("game", f"Slug of the game (default {MAGIC})"),
         query("market", "cm or ct: only that market's orders; empty: every market", enum=["cm", "ct", ""]),
         query("period", "7d, 30d (default), qtd (quarter to date) or ytd (year to date)", enum=PERIODS),
     ])
@api_view(["GET"])
def overview(request):
    user = request.user
    game, market, period = _filters(request.query_params)
    now = timezone.now()
    current, prev = period_bounds(period, now)
    orders = Order.objects.owned_by(user).exclude(state__in=NOT_REVENUE)
    if market:
        orders = orders.filter(market=market)
    counted = _counted(orders, game, *current)
    total = _revenue(counted)
    profit = (OrderItem.objects.owned_by(user)
              .filter(order__in=orders, order__paid_at__gte=current[0], order__paid_at__lt=current[1],
                      price__isnull=False, listing__item__cost__isnull=False, listing__item__card__game=game)
              .aggregate(v=Sum(PROFIT))["v"])
    sell_through, sell_through_prev = _sell_through(user, orders, game, current, prev)
    revenue, revenue_prev = _money(total), _money(_revenue(_counted(orders, game, *prev)))
    n = len(counted)
    basket = _money(total / n) if total is not None and n else None
    return Response({
        "period": period,
        "revenue": revenue,
        "revenue_prev": revenue_prev,
        "orders": n,
        "avg_basket": basket,
        "gross_profit": _money(profit),
        "revenue_weeks": _revenue_weeks(orders, game, timezone.localdate(now)),
        "revenue_30d": revenue,
        "revenue_prev_30d": revenue_prev,
        "orders_30d": n,
        "avg_basket_30d": basket,
        "gross_profit_30d": _money(profit),
        "sell_through": sell_through,
        "sell_through_prev": sell_through_prev,
        "stock_by_set": _stock_by_set(user, game),
        "movers": _movers(user, game),
        "alerts": alerts(user, game, market, timezone.localdate(now)),
    })
