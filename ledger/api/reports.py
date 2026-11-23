"""The reports API (/api/reports/): sales, profit, fees and postage of a date range, by week, market, set and country.

Rules (every figure is computed from the orders paid in the range, local dates Europe/Rome, canceled and merged
orders left out):

- An order's net is its net, else total − market fee (else 0).
- A line's net is the order's net shared over its lines by their value (price × quantity; evenly when none has a
  price). Gross profit = Σ (line net − cost × quantity) over the lines whose collection item has a cost (null when
  none has); profit_coverage_pct = the share of the net sales coming from those lines; margin_pct = gross profit /
  net of those lines.
- With ?game, an order counts when one of its lines is of that game (line → listing → item → card's game); its net,
  fee and postage count for the share of its value in that game's lines, and the other lines are left out.
- avg_basket = net sales / orders; postage = Σ the shipments' postage cost of the orders counted.
- days_to_sell (by set) = the average days from the listing's listed_at to the order's payment, one decimal (a
  number), null when no line has one.
"""

import csv
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from ..models import Expansion, Game, Market, Order, Shipment
from .common import doc, query

TAGS = ["reports"]
DEFAULT_DAYS = 30
TOP_SETS = 50
NOT_COUNTED = [Order.State.CANCELED, Order.State.CLOSED]
ZERO = Decimal("0")
CSV_COLUMNS = ["name", "code", "game", "units", "net_sales", "profit", "margin_pct", "days_to_sell"]
PARAMS = [
    query("from", "First day, YYYY-MM-DD (default 29 days before to)"),
    query("to", "Last day, YYYY-MM-DD (default today)"),
    query("game", "Slug of a game, or all (default): orders with a line of that game, its lines only"),
    query("market", "cm or ct; empty: every market", enum=["cm", "ct", ""]),
]


def _money(value):
    return None if value is None else f"{value:.2f}"


def _pct(part, whole):
    return f"{part / whole * 100:.1f}" if whole else None


def _date(params, name, default):
    raw = params.get(name, "").strip()
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationError({name: ["A date as YYYY-MM-DD."]}) from None


def parse_filters(params):
    """(from, to, game or None, market or "") of the query, validated."""
    to = _date(params, "to", timezone.localdate())
    start = _date(params, "from", to - timedelta(days=DEFAULT_DAYS - 1))
    if start > to:
        raise ValidationError({"from": ["The first day is after the last."]})
    slug = params.get("game", "").strip()
    game = None
    if slug and slug != "all":
        try:
            game = Game.by_slug(slug)
        except Game.DoesNotExist:
            raise ValidationError({"game": [f"Unknown game '{slug}'."]}) from None
    market = params.get("market", "").strip()
    if market and market not in Market.values:
        raise ValidationError({"market": ["cm, ct or empty."]})
    return start, to, game, market


def _aware(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def _orders(user, start, to, market):
    orders = (Order.objects.owned_by(user).exclude(state__in=NOT_COUNTED)
              .filter(paid_at__gte=_aware(start), paid_at__lt=_aware(to + timedelta(days=1)))
              .prefetch_related("items__listing__item__card__game").select_related("shipment"))
    return orders.filter(market=market) if market else orders


class Line:
    def __init__(self, order_item, net):
        listing = order_item.listing
        item = listing.item if listing is not None else None
        self.item = order_item
        self.card = item.card if item is not None else None
        self.cost = item.cost if item is not None else None
        self.listed_at = listing.listed_at if listing is not None else None
        self.quantity = order_item.quantity
        self.net = net

    @property
    def profit(self):
        return None if self.cost is None else self.net - self.cost * self.quantity


class Counted:
    """An order as counted in a report: its share of net, fee and postage, and its counted lines."""

    def __init__(self, order, game, shipment):
        items = list(order.items.all())
        values = [(i.price or ZERO) * i.quantity for i in items]
        whole = sum(values, ZERO)
        if order.net is not None:
            net = order.net
        elif order.total is not None:
            net = order.total - (order.market_fee or ZERO)
        else:
            net = ZERO

        def share_of(i):
            return values[i] / whole if whole else Decimal(1) / len(items)

        keep = [i for i, it in enumerate(items)
                if game is None or (it.listing is not None and it.listing.item.card.game_id == game.pk)]
        self.counted = bool(keep) if game is not None else True
        share = sum((share_of(i) for i in keep), ZERO) if game is not None else Decimal(1)
        self.order = order
        self.net = net * share
        self.fee = (order.market_fee or ZERO) * share
        self.postage = (shipment.postage_cost or ZERO) * share if shipment is not None else ZERO
        self.lines = [Line(items[i], net * share_of(i)) for i in keep]
        self.day = timezone.localdate(order.paid_at)


def counted_orders(user, start, to, game, market):
    out = []
    for order in _orders(user, start, to, market):
        try:
            shipment = order.shipment
        except Shipment.DoesNotExist:
            shipment = None
        c = Counted(order, game, shipment)
        if c.counted:
            out.append(c)
    return out


def _profit(lines):
    known = [l for l in lines if l.cost is not None]
    if not known:
        return None, ZERO
    return sum((l.profit for l in known), ZERO), sum((l.net for l in known), ZERO)


def _sets(counted):
    """[{name, code, game, units, net_sales, profit, margin_pct, days_to_sell}] by net sales, every set."""
    groups = defaultdict(list)
    for c in counted:
        for line in c.lines:
            if line.card is not None:
                key = (line.card.game.slug, line.card.game_id, line.card.set_code)
            else:
                key = (None, None, line.item.expansion)
            groups[key].append((c, line))
    names = {(e.game_id, e.code): e.name for e in Expansion.objects.filter(
        code__in={k[2].lower() for k in groups if k[1] is not None})}
    rows = []
    for (slug, game_id, code), pairs in groups.items():
        lines = [l for _, l in pairs]
        profit, known_net = _profit(lines)
        net = sum((l.net for l in lines), ZERO)
        days = [(c.order.paid_at - l.listed_at).total_seconds() / 86400 for c, l in pairs if l.listed_at is not None]
        name = names.get((game_id, code.lower())) if game_id is not None else None
        rows.append({
            "name": name or code or "—", "code": code if game_id is not None else "", "game": slug,
            "units": sum(l.quantity for l in lines), "net_sales": net, "profit": profit,
            "margin_pct": _pct(profit, known_net) if profit is not None else None,
            "days_to_sell": round(max(sum(days) / len(days), 0), 1) if days else None,
        })
    rows.sort(key=lambda r: (-r["net_sales"], r["name"]))
    for r in rows:
        r["net_sales"], r["profit"] = _money(r["net_sales"]), _money(r["profit"])
    return rows


def _weeks(counted, start, to):
    first = start - timedelta(days=start.weekday())
    weeks = {}
    week = first
    while week <= to:
        weeks[week] = []
        week += timedelta(weeks=1)
    for c in counted:
        weeks[c.day - timedelta(days=c.day.weekday())].append(c)
    out = []
    for week, cs in weeks.items():
        profit, _ = _profit([l for c in cs for l in c.lines])
        out.append({"week_start": week.isoformat(), "net_sales": _money(sum((c.net for c in cs), ZERO)),
                    "profit": _money(profit)})
    return out


def _by_market(counted, total):
    out = []
    for market in Market.values:
        cs = [c for c in counted if c.order.market == market]
        net = sum((c.net for c in cs), ZERO)
        profit, known_net = _profit([l for c in cs for l in c.lines])
        out.append({"market": market, "share_pct": _pct(net, total), "net_sales": _money(net), "orders": len(cs),
                    "fees": _money(sum((c.fee for c in cs), ZERO)),
                    "margin_pct": _pct(profit, known_net) if profit is not None else None})
    return out


def _by_country(counted):
    counts = defaultdict(int)
    for c in counted:
        counts[(c.order.ship_country_code or "").upper()] += 1
    return [{"code": code, "orders": n, "share_pct": _pct(n, len(counted))}
            for code, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def report(user, start, to, game, market):
    counted = counted_orders(user, start, to, game, market)
    days = (to - start).days + 1
    prev_to = start - timedelta(days=1)
    prev_from = prev_to - timedelta(days=days - 1)
    prev = counted_orders(user, prev_from, prev_to, game, market)
    net = sum((c.net for c in counted), ZERO)
    lines = [l for c in counted for l in c.lines]
    profit, known_net = _profit(lines)
    return {
        "range": {"from": start, "to": to, "prev_from": prev_from, "prev_to": prev_to},
        "kpis": {
            "net_sales": _money(net),
            "net_sales_prev": _money(sum((c.net for c in prev), ZERO)),
            "gross_profit": _money(profit),
            "profit_coverage_pct": _pct(known_net, net) if profit is not None else None,
            "orders": len(counted),
            "avg_basket": _money(net / len(counted)) if counted else None,
            "fees": _money(sum((c.fee for c in counted), ZERO)),
            "postage": _money(sum((c.postage for c in counted), ZERO)),
        },
        "weeks": _weeks(counted, start, to),
        "by_market": _by_market(counted, net),
        "by_set": _sets(counted)[:TOP_SETS],
        "by_country": _by_country(counted),
    }


@doc(TAGS, "Sales report of a date range (default the last 30 days, every game and market): KPIs with the previous "
           "period of the same length, Monday-start weeks, markets, the top 50 sets by net sales and the buyers' "
           "countries. See ledger/api/reports.py for how net, profit and the game filter are counted",
     parameters=PARAMS)
@api_view(["GET"])
def reports(request):
    return Response(report(request.user, *parse_filters(request.query_params)))


@doc(TAGS, "Every set of the report as CSV (same parameters): " + ", ".join(CSV_COLUMNS), parameters=PARAMS)
@api_view(["GET"])
def sets_csv(request):
    start, to, game, market = parse_filters(request.query_params)
    rows = _sets(counted_orders(request.user, start, to, game, market))
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="cardledger-sets-{start}-{to}.csv"'
    writer = csv.writer(response)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow(["" if r[c] is None else r[c] for c in CSV_COLUMNS])
    return response
