"""Purchase lots: their per-user code, the figures computed from their items, listing them and allocating their cost.

Every count of a lot is in copies (the sum of the items' quantities):

- cards: the copies of its items; while it has none, cards_estimate.
- trend_value: Σ Cardmarket trend × copies of its items (foil-aware); null when no item has a trend.
- return_pct: (trend_value − price_paid) / price_paid × 100, one decimal; null without trend_value or with price 0.
- progress: sorted = copies of its items (sorted into the collection); graded = the same copies (an item gets its
  condition when it is sorted in, so every sorted copy is graded); priced = copies of items with an active listing
  or a cost; listed = copies of items with a published listing; total = the larger of sorted and cards_estimate.
- unlisted: copies of items in stock without an active listing (any market).
- top_card: the item with the highest unit trend ({name, trend}), null when none has one.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max

from . import repricer
from .cardmarket import trend_price
from .models import Blueprint, CollectionItem, Listing, PurchaseLot

CENT = Decimal("0.01")


def code_of(number):
    return f"PL-{number:04d}"


def next_number(user):
    last = PurchaseLot.objects.owned_by(user).aggregate(n=Max("number"))["n"]
    return (last or 0) + 1


def next_code(user):
    return code_of(next_number(user))


def create(user, **fields):
    """A new lot of user's with the next code (the user row is locked so two requests never take the same one)."""
    with transaction.atomic():
        get_user_model().objects.select_for_update().filter(pk=user.pk).first()
        number = next_number(user)
        return PurchaseLot.objects.create(user=user, number=number, code=code_of(number), **fields)


def lot_by_code(user, code):
    """user's lot with this code (case-insensitive), None when there is none."""
    code = (code or "").strip()
    if not code:
        return None
    return PurchaseLot.objects.owned_by(user).filter(code__iexact=code).first()


def money(value):
    return None if value is None else f"{value:.2f}"


@dataclass
class LotStats:
    copies: int = 0
    trend_value: Decimal | None = None
    priced: int = 0
    listed: int = 0
    unlisted: int = 0
    top: object = None  # the item with the highest trend
    items: list = field(default_factory=list)


def lot_items(lot_ids):
    """The items of these lots with trend, their listings prefetched."""
    return (CollectionItem.objects.filter(lot_id__in=lot_ids).select_related("card")
            .prefetch_related("listings").annotate(trend=trend_price()).order_by("pk"))


def stats_of(lots):
    """{lot id: LotStats} of these lots, in one query."""
    stats = {lot.pk: LotStats() for lot in lots}
    for item in lot_items(list(stats)):
        s = stats[item.lot_id]
        s.items.append(item)
        s.copies += item.quantity
        active = item.active_listings()
        if item.trend is not None:
            s.trend_value = (s.trend_value or Decimal("0")) + item.trend * item.quantity
            if s.top is None or item.trend > s.top.trend:
                s.top = item
        if active or item.cost is not None:
            s.priced += item.quantity
        if any(l.status == Listing.Status.PUBLISHED for l in active):
            s.listed += item.quantity
        if not active:
            s.unlisted += item.quantity
    return stats


def cards(lot, s):
    return s.copies if s.items else lot.cards_estimate


def return_pct(lot, s):
    if s.trend_value is None or not lot.price_paid:
        return None
    return f"{(s.trend_value - lot.price_paid) / lot.price_paid * 100:.1f}"


def progress(lot, s):
    return {"sorted": s.copies, "graded": s.copies, "priced": s.priced, "listed": s.listed,
            "total": max(s.copies, lot.cards_estimate or 0)}


def row(lot, s):
    return {
        "id": lot.pk,
        "code": lot.code,
        "type": lot.type,
        "type_label": lot.get_type_display(),
        "seller": lot.seller,
        "where": lot.where,
        "cards": cards(lot, s),
        "cards_estimate": lot.cards_estimate,
        "price_paid": money(lot.price_paid),
        "paid_by": lot.paid_by,
        "bought_on": lot.bought_on,
        "next_step": lot.next_step,
        "status": lot.status,
        "status_label": lot.get_status_display(),
        "trend_value": money(s.trend_value),
        "return_pct": return_pct(lot, s),
        "progress": progress(lot, s),
        "top_card": None if s.top is None else {"name": s.top.card.name, "trend": money(s.top.trend)},
        "unlisted": s.unlisted,
        "created_at": lot.created_at,
    }


def allocate_cost(lot):
    """Set each item's cost per copy to price_paid × its share of the lot's trend value (trend × copies) / copies.
    Items without a trend (or no copies) keep their cost. Returns (items costed, items without a trend)."""
    items = list(lot_items([lot.pk]))
    priced = [i for i in items if i.trend is not None and i.quantity > 0]
    total = sum((i.trend * i.quantity for i in priced), Decimal("0"))
    if not total:
        return 0, len(items)
    for item in priced:
        cost = (lot.price_paid * item.trend / total).quantize(CENT, rounding=ROUND_HALF_UP)
        CollectionItem.objects.filter(pk=item.pk).update(cost=cost)
    return len(priced), len(items) - len(priced)


def list_unlisted(user, lot, markets):
    """List the lot's unlisted items (copies in stock, no active listing on any market) on markets at the price the
    repricing rules quote (Cardmarket trend without a matching rule), as listings to publish.
    Returns (items listed, items skipped: no catalog product or no price)."""
    items = [i for i in lot_items([lot.pk]) if i.quantity > 0 and not i.active_listings()]
    blueprints = {b.cm_product_id: b for b in Blueprint.objects.select_related("expansion__game", "category")
                  .filter(cm_product_id__in={i.card.cm_product_id for i in items if i.card.cm_product_id})}
    rules, settings = repricer.engine_rules(user), repricer.settings_of(user)
    listed = skipped = 0
    with transaction.atomic():
        for item in items:
            bp = blueprints.get(item.card.cm_product_id)
            if bp is None:
                skipped += 1
                continue
            quotes = [q for q in repricer.quote(user, bp, item.condition, item.language, item.foil, item.cost,
                                                markets=markets, rules=rules, settings=settings) if q.price is not None]
            if not quotes:
                skipped += 1
                continue
            for q in quotes:
                repricer.put_on_sale(item, q.market, q.price, item.quantity)
            listed += 1
    return listed, skipped
