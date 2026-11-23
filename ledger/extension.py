"""The browser extension: pairing and tokens, the plan of what to push to a market, and applying a run's report.

The extension syncs both markets from the user's browser, in a logged-in tab of each (the seller area's stock
export/import and orders pages; no market token is stored here); the server never calls a market. One run: GET plan
→ the extension applies it on the market and reads the stock and orders there → POST report (idempotent per run_id).
Shipping is done the same way: the extension marks the order shipped on its market, then POST shipped.
"""

import hashlib
import secrets
import string
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import services
from .models import (Card, CollectionItem, ExtensionPairing, ExtensionRun, ExtensionToken, Listing, Market,
                     MarketConnection, Order, SyncLog)

PAIRING_TTL = timedelta(minutes=10)
LAST_USED_EVERY = timedelta(minutes=1)  # last_used_at is written at most this often


# --- Pairing and tokens ------------------------------------------------------

def hash_key(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_code():
    letters = "".join(secrets.choice(string.ascii_uppercase) for _ in range(4))
    digits = "".join(secrets.choice(string.digits) for _ in range(4))
    return f"{letters}-{digits}"


def start_pairing(user):
    """A new pairing code for user (valid PAIRING_TTL); their earlier unclaimed codes stop working."""
    ExtensionPairing.objects.filter(user=user, claimed_at__isnull=True).delete()
    while True:
        try:
            with transaction.atomic():
                return ExtensionPairing.objects.create(user=user, code=_new_code(),
                                                       expires_at=timezone.now() + PAIRING_TTL)
        except IntegrityError:  # the code is taken: draw another
            continue


def normalize_code(code):
    """ "abcd 1234", "ABCD1234" → "ABCD-1234"."""
    code = "".join(c for c in str(code or "").upper() if c.isalnum())
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def claim(code, name):
    """Exchange a valid pairing code for a new token: (raw token, user), or None when the code is unknown, expired
    or already used. Pairing also turns both markets on (never synced, unless synced before) for the user."""
    with transaction.atomic():
        pairing = (ExtensionPairing.objects.select_for_update().select_related("user")
                   .filter(code=normalize_code(code), claimed_at__isnull=True, expires_at__gt=timezone.now()).first())
        if pairing is None:
            return None
        pairing.claimed_at = timezone.now()
        pairing.save(update_fields=["claimed_at"])
        raw = secrets.token_urlsafe(32)
        ExtensionToken.objects.create(user=pairing.user, key=hash_key(raw), name=str(name or "")[:100])
        for market in Market.values:
            conn = services.connection(pairing.user, market)
            if conn.status == MarketConnection.Status.OFF:
                conn.status = MarketConnection.Status.NEVER if conn.last_sync is None else MarketConnection.Status.OK
                conn.save(update_fields=["status"])
    return raw, pairing.user


def user_for_token(raw):
    """(user, token) of a live extension token, or None; notes when it was used."""
    token = (ExtensionToken.objects.select_related("user")
             .filter(key=hash_key(raw), revoked=False, user__is_active=True).first())
    if token is None:
        return None
    now = timezone.now()
    if token.last_used_at is None or now - token.last_used_at > LAST_USED_EVERY:
        ExtensionToken.objects.filter(pk=token.pk).update(last_used_at=now)
        token.last_used_at = now
    return token.user, token


def extension_state(user):
    """me's extension: whether a live token exists and when it was last used."""
    live = ExtensionToken.objects.filter(user=user, revoked=False)
    seen = live.exclude(last_used_at__isnull=True).order_by("-last_used_at").values_list("last_used_at", flat=True)
    return {"paired": live.exists(), "last_seen_at": seen.first()}


# --- Plan ----------------------------------------------------------------------

CREATE, UPDATE, DELETE = "create", "update", "delete"


def action_of(listing):
    """What the next sync must do with a listing on its market: create, update, delete or None (nothing)."""
    on_market = bool(listing.market_listing_id)
    if listing.pending_delete or listing.status == Listing.Status.PAUSED:
        return DELETE if on_market else None
    if listing.status in (Listing.Status.TO_PUBLISH, Listing.Status.ERROR):
        if listing.quantity < 1:
            return DELETE if on_market else None
        return UPDATE if on_market else CREATE
    return None


def _candidates(user, market):
    """The user's listings on market that may need a push (action_of decides)."""
    return (Listing.objects.owned_by(user).filter(market=market)
            .exclude(status__in=[Listing.Status.PUBLISHED, Listing.Status.SOLD], pending_delete=False)
            .select_related("item__card__game").order_by("pk"))


def _item(item):
    card = item.card
    return {
        "id": item.pk, "name": card.name, "set_code": card.set_code, "collector_number": card.collector_number,
        "cm_product_id": card.cm_product_id, "ct_blueprint_id": card.ct_blueprint_id, "game": card.game.slug,
        "condition": item.condition, "language": item.language, "foil": item.foil, "is_product": card.is_product,
    }


def plan(user, market):
    """{market, changes: [...]}: every listing of the user's to create, update or delete on market."""
    changes = []
    for listing in _candidates(user, market):
        action = action_of(listing)
        if action is None:
            continue
        changes.append({
            "listing_id": listing.pk, "action": action, "market_listing_id": listing.market_listing_id,
            "item": _item(listing.item), "quantity": listing.quantity, "price": f"{listing.price:.2f}",
            "comment": "",
        })
    return {"market": market, "changes": changes}


def pending_counts(user):
    """{market: number of changes the next sync of it would push}."""
    counts = {m: 0 for m in Market.values}
    for listing in (Listing.objects.owned_by(user)
                    .exclude(status__in=[Listing.Status.PUBLISHED, Listing.Status.SOLD], pending_delete=False)):
        if action_of(listing) is not None:
            counts[listing.market] += 1
    return counts


# --- Report ------------------------------------------------------------------

def _apply_result(listing, result, now):
    """Apply one result of the plan to its listing. True when something changed."""
    action = action_of(listing)
    remote_id = str(result.get("market_listing_id") or "")
    if not result.get("ok"):
        listing.error_message = (result.get("error") or "Unknown error")[:300]
        if action in (CREATE, UPDATE):
            listing.status = Listing.Status.ERROR
        listing.save(update_fields=["status", "error_message", "updated_at"])
        return True
    if action == DELETE:
        if listing.pending_delete or listing.quantity < 1 and listing.status != Listing.Status.PAUSED:
            listing.delete()
        else:  # paused: off the market, kept here
            listing.market_listing_id, listing.error_message = "", ""
            listing.save(update_fields=["market_listing_id", "error_message", "updated_at"])
        return True
    if action in (CREATE, UPDATE):
        if remote_id:
            listing.market_listing_id = remote_id
        listing.status = Listing.Status.PUBLISHED
        listing.error_message = ""
        listing.listed_at = listing.listed_at or now
        listing.save(update_fields=["market_listing_id", "status", "error_message", "listed_at", "updated_at"])
        return True
    if remote_id and not listing.market_listing_id:  # changed since the plan: only learn its id
        listing.market_listing_id = remote_id
        listing.save(update_fields=["market_listing_id", "updated_at"])
        return True
    return False


def _learn_blueprint(user, card, blueprint_id):
    """Store blueprint_id (reported by a CardTrader run) as the card's CardTrader blueprint, unless another card
    already has it (a warning in the user's log then). True when stored."""
    if not blueprint_id or card.ct_blueprint_id == blueprint_id:
        return False
    if Card.objects.filter(ct_blueprint_id=blueprint_id).exclude(pk=card.pk).exists():
        services._log(f"CardTrader: blueprint {blueprint_id} reported for {card} is already used by another card: "
                      f"fix it by hand"[:300], SyncLog.Level.WARNING, owner=user)
        return False
    card.ct_blueprint_id = blueprint_id
    card.save(update_fields=["ct_blueprint_id"])
    return True


def _suggest_item(user, row):
    """The user's collection item a market listing not known here probably is (same printing and copies), or None."""
    items = CollectionItem.objects.owned_by(user)
    if row.get("cm_product_id"):
        items = items.filter(card__cm_product_id=row["cm_product_id"])
    elif row.get("ct_blueprint_id"):
        items = items.filter(card__ct_blueprint_id=row["ct_blueprint_id"])
    else:
        return None
    for name in ("condition", "language"):
        if row.get(name):
            items = items.filter(**{name: row[name]})
    if "foil" in row:
        items = items.filter(foil=bool(row["foil"]))
    return items.order_by("pk").values_list("pk", flat=True).first()


def _reconcile(user, market, stock):
    """Conflicts for the known listings that differ from the market's stock, and the stock rows not known here."""
    known = {l.market_listing_id: l for l in Listing.objects.owned_by(user).filter(market=market)
             .exclude(market_listing_id="").select_related("item__card")}
    remote, unmatched = {}, []
    for row in stock:
        listing_id = str(row.get("market_listing_id") or "")
        if listing_id in known:
            remote[listing_id] = (row.get("price"), row.get("quantity"))
            if market == Market.CARDTRADER:
                _learn_blueprint(user, known[listing_id].item.card, row.get("ct_blueprint_id"))
        else:
            unmatched.append({**row, "market_listing_id": listing_id, "price": _str_money(row.get("price")),
                              "item_id": _suggest_item(user, row)})
    conflicts = services.diff_listings(user, market, remote)
    return conflicts, unmatched


def _str_money(value):
    return None if value is None else f"{value:.2f}"


ORDER_FIELDS = ["state", "buyer_username", "buyer_name", "buyer_email", "buyer_phone", "ship_name", "ship_street",
                "ship_zip", "ship_city", "ship_province", "ship_country", "ship_country_code", "ship_note",
                "shipping_method", "tracked", "tracking_code", "paid_at", "sent_at", "items_total", "shipping_cost",
                "market_fee", "total", "net", "currency"]


def _save_order(user, market, data):
    fields = {name: data[name] for name in ORDER_FIELDS if name in data}
    if fields.get("net") is None and fields.get("total") is not None and fields.get("market_fee") is not None:
        fields["net"] = fields["total"] - fields["market_fee"]
    items = data.get("items") or []
    fields["code"] = str(data.get("code") or data["market_order_id"])[:40]
    fields["size"] = sum(i.get("quantity") or 1 for i in items)
    if fields.get("paid_at"):
        fields["created_at"] = fields["paid_at"]
    fields["raw"] = _json_safe(data)
    return services.save_order(user, market, data["market_order_id"], fields, items)


def apply_report(user, data):
    """Apply one run of the extension (validated report data) once: a retry with the same run_id answers the same.

    Returns {applied, conflicts, orders_new, unmatched}. A CardTrader run's ct_blueprint_id (in a result, or in a
    stock row of a known listing) is stored on the listing's card (_learn_blueprint).
    """
    market = data["market"]
    label = Market(market).label
    now = timezone.now()
    with transaction.atomic():
        run, created = ExtensionRun.objects.get_or_create(user=user, run_id=data["run_id"], defaults={
            "market": market, "started_at": data.get("started_at"), "finished_at": data.get("finished_at"),
            "error": (data.get("error") or "")[:300],
        })
        if not created:
            return run.response

        listings = {l.pk: l for l in Listing.objects.owned_by(user).filter(
            market=market, pk__in=[r["listing_id"] for r in data.get("results") or []]).select_related("item__card")}
        applied, failed = 0, []
        for result in data.get("results") or []:
            listing = listings.get(result["listing_id"])
            if listing is None:  # another user's, another market's or gone: ignored
                continue
            if not result.get("ok"):
                failed.append((listing.item.card.name, result.get("error") or "unknown error"))
            if market == Market.CARDTRADER:
                _learn_blueprint(user, listing.item.card, result.get("ct_blueprint_id"))
            if _apply_result(listing, result, now):
                applied += 1

        orders_new = 0
        for order in data.get("orders") or []:
            _, was_created = _save_order(user, market, order)
            orders_new += was_created

        conflicts, unmatched = 0, []
        if data.get("stock") is not None:
            conflicts, unmatched = _reconcile(user, market, data["stock"])

        error = (data.get("error") or "").strip()
        conn = services.mark_synced(user, market, error=error, when=data.get("finished_at") or now)
        if data.get("handle"):
            conn.handle = data["handle"][:120]
        if data.get("orders") is not None:
            conn.orders_synced_at = now
        conn.save(update_fields=["handle", "orders_synced_at"])

        for name, reason in failed:
            services._log(f"{label}: {name} not synced ({reason})", SyncLog.Level.WARNING, owner=user)
        summary = (f"{label}: extension sync, {applied} changes applied, {orders_new} new orders, "
                   f"{conflicts} conflicts, {len(unmatched)} unmatched listings")
        if error:
            services._log(f"{label}: extension sync failed ({error})", SyncLog.Level.ERROR, owner=user)
        else:
            level = SyncLog.Level.WARNING if failed or conflicts else SyncLog.Level.OK
            services._log(summary, level, owner=user)

        response = {"applied": applied, "conflicts": conflicts, "orders_new": orders_new, "unmatched": unmatched}
        run.response = _json_safe(response)
        run.save(update_fields=["response"])
    return run.response


def _json_safe(value):
    """The response as stored in JSON (and answered: the same on a retry)."""
    import json

    from django.core.serializers.json import DjangoJSONEncoder

    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def mark_shipped(user, market, market_order_id, tracking_code=""):
    """Record that the extension marked the user's order as shipped on its market. None when there is no such order."""
    order = Order.objects.owned_by(user).filter(market=market, market_order_id=str(market_order_id)).first()
    if order is None:
        return None
    fields = ["updated_at"]
    if tracking_code:
        order.tracking_code = tracking_code[:120]
        fields.append("tracking_code")
    if order.state in (Order.State.PAID, Order.State.HUB_PENDING):
        order.state = Order.State.SENT
        order.sent_at = timezone.now()
        fields += ["state", "sent_at"]
    order.save(update_fields=fields)
    services.record_shipment(order)
    services._log(f"{Market(market).label}: order #{order.code} shipped", owner=user)
    return order
