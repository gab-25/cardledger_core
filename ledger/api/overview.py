"""The overview API (/api/overview/): the dealer's dashboard, revenue, profit, stock value per game and price movers.

Orders are global (market orders are not per game): revenue, orders, basket and gross profit cover every game.
The stock value is given for every game; only the movers are the items of the game of ?game.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, DecimalField, ExpressionWrapper, F, Sum
from django.db.models.functions import Abs
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from ..cardmarket import item_guide_price, trend_price
from ..models import CollectionItem, Game, Order, OrderItem
from .common import GAME, doc, game_param

TAGS = ["overview"]
WEEKS = 12
MOVERS = 6
NOT_REVENUE = [Order.State.CANCELED, Order.State.CLOSED]  # as the orders page's revenue_month
DECIMAL = DecimalField(max_digits=20, decimal_places=6)
VALUE = ExpressionWrapper(F("trend") * F("quantity"), output_field=DECIMAL)
PROFIT = ExpressionWrapper((F("price") - F("listing__item__cost")) * F("quantity"), output_field=DECIMAL)


def _money(value):
    return None if value is None else f"{value:.2f}"


def _revenue_weeks(orders, today):
    """WEEKS Monday-start weeks up to this one, oldest first, with the total of the orders paid in each."""
    first = today - timedelta(days=today.weekday() + 7 * (WEEKS - 1))
    totals = {first + timedelta(weeks=i): Decimal("0") for i in range(WEEKS)}
    start = timezone.make_aware(datetime.combine(first, time.min))
    for paid_at, total in orders.filter(paid_at__gte=start, total__isnull=False).values_list("paid_at", "total"):
        day = timezone.localdate(paid_at)
        week = day - timedelta(days=day.weekday())
        if week in totals:
            totals[week] += total
    return [{"week_start": week.isoformat(), "total": f"{total:.2f}"} for week, total in totals.items()]


def _stock_by_game(user):
    items = CollectionItem.objects.owned_by(user).annotate(trend=trend_price())
    return [
        {"game": g.slug, "name": g.name,
         "value": _money(items.filter(card__game=g).aggregate(v=Sum(VALUE))["v"] or Decimal("0"))}
        for g in Game.objects.order_by("name")
    ]


def _movers(user, game):
    """The items of game in stock whose 7-day average moved most from the 30-day one (foil-aware)."""
    items = (CollectionItem.objects.owned_by(user).filter(card__game=game, quantity__gt=0).select_related("card")
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


@doc(TAGS, "Dashboard: weekly revenue, the last 30 days (orders of every game), stock value per game and the "
           "price movers of the game of ?game", parameters=[GAME])
@api_view(["GET"])
def overview(request):
    user = request.user
    game = game_param(request.query_params)
    now = timezone.now()
    orders = Order.objects.owned_by(user).exclude(state__in=NOT_REVENUE)
    last = orders.filter(paid_at__gte=now - timedelta(days=30)).aggregate(
        revenue=Sum("total"), n=Count("pk"), basket=Avg("total"))
    prev = orders.filter(paid_at__gte=now - timedelta(days=60), paid_at__lt=now - timedelta(days=30)).aggregate(
        revenue=Sum("total"))
    profit = (OrderItem.objects.owned_by(user)
              .filter(order__in=orders, order__paid_at__gte=now - timedelta(days=30),
                      price__isnull=False, listing__item__cost__isnull=False)
              .aggregate(v=Sum(PROFIT))["v"])
    return Response({
        "revenue_weeks": _revenue_weeks(orders, timezone.localdate(now)),
        "revenue_30d": _money(last["revenue"]),
        "revenue_prev_30d": _money(prev["revenue"]),
        "orders_30d": last["n"],
        "avg_basket_30d": _money(last["basket"]),
        "gross_profit_30d": _money(profit),
        "stock_by_game": _stock_by_game(user),
        "movers": _movers(user, game),
    })
