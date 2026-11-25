"""Business operations: CSV import/export, market connections, shared stock, orders and sync conflicts.

The server never calls a market: both are synced by the browser extension (ledger/extension.py), whose reports land
here (save_order, diff_listings, mark_synced).
"""

import csv
import io
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from .catalog import normalize_number
from .forms import get_or_create_card
from .models import (
    MAGIC,
    Blueprint,
    Card,
    CollectionItem,
    Condition,
    Game,
    Language,
    Listing,
    Market,
    MarketConnection,
    Order,
    OrderItem,
    Shipment,
    SyncConflict,
    SyncLog,
)
from .shipping import carrier_for, expected_by

CSV_FIELDS = ["name", "set", "condition", "language", "foil", "quantity", "number", "cost", "location"]


def connection(user, market):
    """The user's MarketConnection on market, created (never synced) when missing."""
    return MarketConnection.objects.get_or_create(user=user, market=market)[0]


def mark_synced(user, market, error="", when=None):
    """Record a sync of market (an extension report): ok, or error with its message."""
    conn = connection(user, market)
    conn.last_sync = when or timezone.now()
    conn.status = MarketConnection.Status.ERROR if error else MarketConnection.Status.OK
    conn.last_error = error[:300]
    conn.save(update_fields=["last_sync", "status", "last_error"])
    return conn


def market_states(user):
    """me's and the extension's markets: [{code, name, connected, status, last_sync, handle}] for Cardmarket and
    CardTrader, each from the user's MarketConnection: off until pairing the extension (or a report) creates it,
    then its stored status (never, ok, error)."""
    conns = {c.market: c for c in MarketConnection.objects.filter(user=user)}
    states = []
    for market in (Market.CARDMARKET, Market.CARDTRADER):
        conn = conns.get(market)
        status = conn.status if conn else MarketConnection.Status.OFF
        states.append({"code": market, "name": Market(market).label, "connected": status != MarketConnection.Status.OFF,
                       "status": status, "last_sync": conn.last_sync if conn else None,
                       "handle": conn.handle if conn else ""})
    return states



def _log(message, level=SyncLog.Level.OK, owner=None):
    SyncLog.objects.create(message=message[:300], level=level, owner=owner)


# --- CSV -----------------------------------------------------------------

def export_csv(items, stream):
    writer = csv.writer(stream)
    writer.writerow(CSV_FIELDS)
    for item in items.select_related("card"):
        writer.writerow([
            item.card.name,
            item.card.set_code,
            item.condition,
            item.language,
            "1" if item.foil else "0",
            item.quantity,
            item.card.collector_number,
            "" if item.cost is None else f"{item.cost:.2f}",
            item.location,
        ])


class CsvImportError(ValueError):
    pass


def _choice(value, choices, default, field_name, line):
    value = (value or "").strip().upper()
    if not value:
        return default
    if value not in choices.values:
        raise CsvImportError(f"Line {line}: invalid {field_name} '{value}'")
    return value


def _csv_cost(value, line):
    """The cost column: None when blank, else a non-negative amount."""
    if not value:
        return None
    try:
        cost = Decimal(value.replace(",", "."))
    except ArithmeticError as exc:
        raise CsvImportError(f"Line {line}: invalid cost '{value}'") from exc
    if not cost.is_finite() or cost < 0 or cost >= Decimal("1e8"):
        raise CsvImportError(f"Line {line}: invalid cost '{value}'")
    return cost.quantize(Decimal("0.01"))


def link_blueprint(card):
    """Tie a card to its catalog printing by set + collector number, when it has none and one matches in its game."""
    if card.cm_product_id or not card.collector_number:
        return
    bp = Blueprint.objects.filter(
        expansion__game_id=card.game_id, expansion__code=card.set_code.lower(),
        collector_number=normalize_number(card.collector_number),
    ).order_by("cm_product_id").first()
    if bp is not None and not Card.objects.filter(game_id=card.game_id, cm_product_id=bp.cm_product_id).exists():
        card.cm_product_id = bp.cm_product_id
        card.save(update_fields=["cm_product_id"])


def import_csv(uploaded_file, owner, game=None):
    """Create or update owner's collection items from a CSV, as cards of game (None: Magic). Returns (created,
    updated)."""
    if game is None:
        game = Game.by_slug(MAGIC)
    try:
        text = uploaded_file.read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvImportError("The file is not UTF-8 encoded") from exc

    reader = csv.DictReader(io.StringIO(text))
    header = {h.strip().lower() for h in (reader.fieldnames or [])}
    missing = {"name", "set"} - header
    if missing:
        raise CsvImportError(f"Missing required columns: {', '.join(sorted(missing))}")

    created = updated = 0
    with transaction.atomic():
        for line, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            if not row.get("name"):
                continue
            try:
                quantity = int(row.get("quantity") or 1)
            except ValueError as exc:
                raise CsvImportError(f"Line {line}: invalid quantity") from exc
            if quantity < 0:
                raise CsvImportError(f"Line {line}: quantity must be positive")
            # optional columns: blank (or absent) leaves an existing item's value as it is
            extra = {}
            cost = _csv_cost(row.get("cost", ""), line)
            if cost is not None:
                extra["cost"] = cost
            if row.get("location"):
                if len(row["location"]) > 40:
                    raise CsvImportError(f"Line {line}: location longer than 40 characters")
                extra["location"] = row["location"]

            card = get_or_create_card(
                game,
                row["name"],
                row["set"].upper(),
                normalize_number(row.get("number", "")),
            )
            link_blueprint(card)
            item, item_created = CollectionItem.objects.get_or_create(
                owner=owner,
                card=card,
                condition=_choice(row.get("condition"), Condition, Condition.NEAR_MINT, "condition", line),
                language=_choice(row.get("language"), Language, Language.EN, "language", line),
                foil=row.get("foil", "").lower() in {"1", "true", "yes", "y", "foil"},
                defaults={"quantity": quantity, **extra},
            )
            if item_created:
                created += 1
            else:
                item.quantity = quantity
                for name, value in extra.items():
                    setattr(item, name, value)
                item.save(update_fields=["quantity", *extra, "updated_at"])
                updated += 1

        _log(f"CSV import: {created} new cards, {updated} quantities updated", owner=owner)
    return created, updated


# --- Listings ------------------------------------------------------------

PRICE_STEP = Decimal("0.05")


def price_over_trend(trend, pct, step=None):
    """trend × (1 + pct/100), rounded to the nearest step (default 0.05, half up), never under one step."""
    step = step or PRICE_STEP
    price = trend * (1 + Decimal(pct) / 100)
    price = (price / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    return max(price, step).quantize(Decimal("0.01"))


@dataclass
class BulkListResult:
    listed: int = 0
    no_trend: int = 0  # skipped: no Cardmarket trend
    no_copies: int = 0  # skipped (list): no copies left to sell
    no_listing: int = 0  # skipped (reprice): no active listing
    below_cost: int = 0  # skipped (hold_below_cost): the new price would be under the item's cost


def list_at_trend(items, pct, reprice=False, step=None, hold_below_cost=False, markets=(Market.CARDTRADER,)):
    """Put items on sale on markets at price_over_trend(their trend, pct, step), as listings to publish.

    With hold_below_cost an item whose new price would be under its cost is skipped (left as it is).

    items must carry the `trend` annotation (cardmarket.trend_price()). list (reprice=False) creates, on each market,
    the listing of an item without one (all its copies) and reprices an existing one; a sold (or removed) listing is
    reopened. reprice changes only the active listings. A published listing whose price changes goes back to "to
    publish", like an edit of the listing form, so the next sync updates it on its market. Counts are per item.
    """
    result = BulkListResult()
    for item in items.prefetch_related("listings"):
        listings = {m: item.listing_for(m) for m in markets}
        active = {m for m, l in listings.items() if l is not None and l.is_active}
        if reprice and not active:
            result.no_listing += 1
            continue
        if item.trend is None:
            result.no_trend += 1
            continue
        targets = active if reprice else set(markets)
        if not reprice and item.quantity < 1:
            targets = active
        if not targets:
            result.no_copies += 1
            continue
        price = price_over_trend(item.trend, pct, step)
        if hold_below_cost and item.cost is not None and price < item.cost:
            result.below_cost += 1
            continue
        for market in sorted(targets):
            listing = listings[market]
            if listing is None:
                Listing.objects.create(item=item, market=market, price=price, quantity=item.quantity,
                                       status=Listing.Status.TO_PUBLISH)
            elif listing.status == Listing.Status.SOLD:  # sold out on the market: a new listing there
                listing.price, listing.quantity, listing.status = price, item.quantity, Listing.Status.TO_PUBLISH
                listing.market_listing_id, listing.sold_at, listing.listed_at = "", None, None
                listing.pending_delete, listing.error_message = False, ""
                listing.save()
            elif listing.pending_delete:  # removed but still on the market: updated there instead
                listing.price, listing.quantity, listing.status = price, item.quantity, Listing.Status.TO_PUBLISH
                listing.pending_delete = False
                listing.save()
            elif listing.price != price:
                listing.price = price
                if listing.status == Listing.Status.PUBLISHED:
                    listing.status = Listing.Status.TO_PUBLISH
                listing.save(update_fields=["price", "status", "updated_at"])
        result.listed += 1
    return result


def remove_listing(listing):
    """Delete a listing; one still on its market (a market id, not sold) is only flagged pending_delete: the next
    sync deletes it there (the extension), then here."""
    if listing.market_listing_id and listing.status != Listing.Status.SOLD:
        listing.pending_delete = True
        listing.save(update_fields=["pending_delete", "updated_at"])
        return
    listing.delete()


# --- Orders / shipments ----------------------------------------------------

def _decrements_stock(order):
    """CardTrader's rule: CT Zero orders count while hub_pending (later merged/closed);
    the weekly merged CT Zero order and regular orders follow the normal paid flow."""
    if order.via_cardtrader_zero:
        return order.state in (Order.State.HUB_PENDING, Order.State.CLOSED)
    return order.state in (Order.State.PAID, Order.State.SENT, Order.State.ARRIVED, Order.State.DONE)


def share_stock(item, exclude=None):
    """Cap the item's listings on the other markets (all but listing exclude) to its remaining copies.

    Shared stock: a capped listing that was published goes back to "to publish" (the next sync updates it on its
    market); one left with no copies is removed (deleted on its market by the next sync when it is there).
    """
    for listing in item.listings.exclude(pk=getattr(exclude, "pk", None)):
        if not listing.is_active or listing.quantity <= item.quantity:
            continue
        if item.quantity == 0:
            remove_listing(listing)
            continue
        listing.quantity = item.quantity
        if listing.status == Listing.Status.PUBLISHED:
            listing.status = Listing.Status.TO_PUBLISH
        listing.save(update_fields=["quantity", "status", "updated_at"])


def _apply_stock(order):
    for oi in order.items.select_related("listing__item"):
        listing = oi.listing
        if listing is None:
            continue
        item = listing.item
        listing.quantity = max(listing.quantity - oi.quantity, 0)
        item.quantity = max(item.quantity - oi.quantity, 0)
        if listing.quantity == 0:
            listing.status = Listing.Status.SOLD
            listing.sold_at = order.paid_at or timezone.now()
        listing.save(update_fields=["quantity", "status", "sold_at", "updated_at"])
        item.save(update_fields=["quantity", "updated_at"])
        share_stock(item, exclude=listing)
    order.stock_applied = True
    order.save(update_fields=["stock_applied"])


def save_order(owner, market, market_order_id, fields, lines):
    """Create or update owner's order market_order_id on market, with its lines; removes the sold copies once.

    fields: the Order fields to set; lines: dicts with market_item_id, market_listing_id, name, expansion, quantity,
    price, condition, language, foil. A line is tied to the owner's listing with that market id on the market.
    Returns (order, created).
    """
    order, created = Order.objects.update_or_create(
        owner=owner, market=market, market_order_id=str(market_order_id), defaults=fields,
    )
    ids = [str(l.get("market_listing_id") or "") for l in lines]
    listings = {
        l.market_listing_id: l
        for l in Listing.objects.owned_by(owner).filter(market=market, market_listing_id__in=[i for i in ids if i])
    }
    for position, line in enumerate(lines, start=1):
        listing_id = str(line.get("market_listing_id") or "")
        OrderItem.objects.update_or_create(
            order=order,
            market_item_id=str(line.get("market_item_id") or "") or f"line-{position}",
            defaults={
                "market_listing_id": listing_id,
                "listing": listings.get(listing_id),
                "name": (line.get("name") or "")[:200],
                "expansion": (line.get("expansion") or "")[:200],
                "quantity": line.get("quantity") or 1,
                "price": line.get("price"),
                "condition": (line.get("condition") or "")[:40],
                "language": (line.get("language") or "")[:10],
                "foil": bool(line.get("foil")),
            },
        )
    if not order.stock_applied and _decrements_stock(order):
        _apply_stock(order)
    record_shipment(order)
    return order, created


def record_shipment(order):
    """Create or update the Shipment of order once it is shipped (sent, arrived or done on its market); nothing
    before. Created with shipped_at (the order's sent_at, else now), the order's tracking code, the carrier its
    shipping method names and expected_by (shipping.expected_by); later calls fill only what is still blank (the
    seller's edits stay) and set delivered_at the first time the order is seen arrived/done. Returns the shipment or
    None."""
    if order.state not in Order.SHIPPED_STATES:
        return None
    shipment = Shipment.objects.filter(order=order).first() or Shipment(order=order)
    if shipment.shipped_at is None:
        shipment.shipped_at = order.sent_at or timezone.now()
    if order.tracking_code and not shipment.tracking_code:
        shipment.tracking_code = order.tracking_code
    if not shipment.carrier:
        shipment.carrier = carrier_for(order.shipping_method)
    if shipment.expected_by is None:
        shipment.expected_by = expected_by(shipment.shipped_at, order.max_shipping_days,
                                           order.tracked or bool(shipment.tracking_code))
    if order.state in Order.DELIVERED_STATES and shipment.delivered_at is None and shipment.returned_at is None:
        shipment.delivered_at = timezone.now()
    shipment.save()
    return shipment


def diff_listings(user, market, remote):
    """Open/refresh conflicts between user's published listings on market and remote: {market listing id (str):
    (price or None, quantity or None)}, the listings on the market. Returns the number of open conflicts."""
    label = Market(market).label
    seen = set()
    listings = Listing.objects.owned_by(user).filter(
        market=market, status=Listing.Status.PUBLISHED, pending_delete=False,
    ).exclude(market_listing_id="").select_related("item__card")
    for listing in listings:
        product = remote.get(listing.market_listing_id)
        diffs = []
        if product is None:
            diffs.append(("status", "Published", f"Not on {label}", None))
        else:
            remote_price, remote_qty = product
            if remote_price is not None and remote_price != listing.price:
                diffs.append(("price", f"€{listing.price}", f"€{remote_price}", remote_price))
            if remote_qty is not None and remote_qty != listing.quantity:
                diffs.append(("quantity", f"{listing.quantity} for sale", f"{remote_qty} for sale", Decimal(remote_qty)))
        for field_name, local, remote_value, amount in diffs:
            conflict, _ = SyncConflict.objects.update_or_create(
                market=market, item=listing.item, field=field_name, resolution=SyncConflict.Resolution.OPEN,
                defaults={"local_value": local, "remote_value": remote_value, "remote_amount": amount},
            )
            seen.add(conflict.pk)
    stale = (SyncConflict.objects.owned_by(user).filter(market=market, resolution=SyncConflict.Resolution.OPEN)
             .exclude(pk__in=seen))
    stale.delete()
    return len(seen)


def resolve_conflict(conflict, keep):
    """keep is 'ledger' or 'remote'."""
    listing = conflict.listing
    with transaction.atomic():
        if listing is not None:
            if keep == SyncConflict.Resolution.REMOTE:
                if conflict.field == "price" and conflict.remote_amount is not None:
                    listing.price = conflict.remote_amount
                elif conflict.field == "quantity" and conflict.remote_amount is not None:
                    listing.quantity = int(conflict.remote_amount)
                elif conflict.field == "status":
                    # Gone from the market: treat as no longer listed.
                    listing.market_listing_id = ""
                    listing.status = Listing.Status.SOLD
                    listing.sold_at = listing.sold_at or timezone.now()
                listing.save()
            else:
                # Ledger wins: push local values again on next publish.
                if conflict.field == "status":
                    listing.market_listing_id = ""  # recreate the product
                listing.status = Listing.Status.TO_PUBLISH
                listing.save()
        conflict.resolution = keep
        conflict.save(update_fields=["resolution"])
        _log(
            f"{conflict.get_market_display()}: conflict on {conflict.item.card.name} resolved "
            f"({conflict.get_resolution_display().lower()})",
            owner=conflict.item.owner,
        )
