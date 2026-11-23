"""Business operations: CSV import/export, CardTrader sync, market connections, shared stock and orders."""

import csv
import io
import unicodedata
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .cardtrader import (
    CONDITION_FROM_CT,
    CONDITION_TO_CT,
    LANGUAGE_TO_CT,
    CardTraderClient,
    CardTraderError,
    money,
)
from .ct_properties import foil_key, language_key, read_foil, read_language
from .catalog import blueprint_of, clashing_owners, merge_cards, normalize_number
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
    SyncConflict,
    SyncLog,
    SyncService,
)

CSV_FIELDS = ["name", "set", "condition", "language", "foil", "quantity", "number", "cost", "location"]
CARDTRADER = "CardTrader"


def get_cardtrader(user):
    """The user's CardTrader connection (their own token and sync times)."""
    service, _ = SyncService.objects.get_or_create(
        user=user, name=CARDTRADER,
        defaults={"description": "Sale listings and orders", "direction": "Ledger ⇄ CardTrader (API)"},
    )
    return service


def connection(user, market):
    """The user's MarketConnection on market, created (never synced) when missing."""
    return MarketConnection.objects.get_or_create(user=user, market=market)[0]


def mark_synced(user, market, error="", when=None):
    """Record a sync (or publish) of market: ok, or error with its message."""
    conn = connection(user, market)
    conn.last_sync = when or timezone.now()
    conn.status = MarketConnection.Status.ERROR if error else MarketConnection.Status.OK
    conn.last_error = error[:300]
    conn.save(update_fields=["last_sync", "status", "last_error"])
    return conn


def mark_failed(user, market, error):
    """Record a failed sync or publish of market (its last sync time stays)."""
    conn = connection(user, market)
    conn.status, conn.last_error = MarketConnection.Status.ERROR, str(error)[:300]
    conn.save(update_fields=["status", "last_error"])


def market_states(user):
    """me's markets: [{code, name, connected, status, last_sync, handle}] for Cardmarket and CardTrader.

    CardTrader keeps the rule it always had: off without a saved token, never before any sync or publish, error when
    the user's latest "CardTrader:" log entry is an error, else ok. Cardmarket (synced by the extension only) is its
    MarketConnection: off until a pairing or a report creates it, then its stored status.
    """
    conns = {c.market: c for c in MarketConnection.objects.filter(user=user)}
    states = []
    for market in (Market.CARDMARKET, Market.CARDTRADER):
        conn = conns.get(market)
        handle = conn.handle if conn else ""
        if market == Market.CARDTRADER:
            service = SyncService.objects.filter(user=user, name=CARDTRADER).first()
            connected = bool(service and service.api_token)
            times = [t for t in (service and service.last_sync, conn and conn.last_sync) if t]
            last_sync = max(times) if times else None
            if not connected:
                status = MarketConnection.Status.OFF
            elif last_sync is None:
                status = MarketConnection.Status.NEVER
            else:
                latest = (SyncLog.objects.filter(owner=user, message__startswith=f"{CARDTRADER}:")
                          .values_list("level", flat=True).first())
                status = MarketConnection.Status.ERROR if latest == SyncLog.Level.ERROR else MarketConnection.Status.OK
        else:
            status = conn.status if conn else MarketConnection.Status.OFF
            connected = status != MarketConnection.Status.OFF
            last_sync = conn.last_sync if conn else None
        states.append({"code": market, "name": Market(market).label, "connected": connected, "status": status,
                       "last_sync": last_sync, "handle": handle})
    return states


def user_token(user):
    """The user's own CardTrader token: the only one used for their listings, orders and shipments."""
    service = SyncService.objects.filter(user=user, name=CARDTRADER).first()
    return service.api_token if service else ""


def make_client(token):
    """Build the API client (an empty token raises CardTraderError). Tests patch this to inject a mock transport."""
    return CardTraderClient(token)


NO_SELLING_TOKEN = "Connect your CardTrader token in Profile and settings to sell."


def user_client(user):
    """The client for the user's own CardTrader account: never falls back to another user's token."""
    token = user_token(user)
    if not token:
        raise CardTraderError(NO_SELLING_TOKEN)
    return make_client(token)


class TokenInUse(Exception):
    pass


def _ct_user_id(token):
    with make_client(token) as client:
        return (client.info() or {}).get("user_id")


def set_api_token(user, token):
    """Check the token against CardTrader (/info), then store it as the user's own. Returns the app info.

    A CardTrader account can be connected to one user only (TokenInUse otherwise).
    """
    token = token.strip()
    with make_client(token) as client:
        info = client.info() or {}
    ct_user_id = info.get("user_id")
    others = SyncService.objects.filter(name=CARDTRADER).exclude(user=user).exclude(api_token="")
    for other in others.filter(ct_user_id__isnull=True):  # tokens kept from before: learn whose they are
        try:
            other.ct_user_id = _ct_user_id(other.api_token)
        except CardTraderError:
            continue
        if other.ct_user_id is not None:
            other.save(update_fields=["ct_user_id"])
    if ct_user_id is not None and others.filter(ct_user_id=ct_user_id).exists():
        raise TokenInUse("This CardTrader account is already connected to another user.")
    service = get_cardtrader(user)
    service.api_token = token
    service.ct_user_id = ct_user_id
    service.save(update_fields=["api_token", "ct_user_id"])
    conn = connection(user, Market.CARDTRADER)
    conn.status = MarketConnection.Status.NEVER if service.last_sync is None else MarketConnection.Status.OK
    conn.last_error = ""
    conn.save(update_fields=["status", "last_error"])
    _log(f"{CARDTRADER}: API token connected" + (f" (app {info['name']})" if info.get("name") else ""), owner=user)
    return info


def remove_api_token(user):
    service = get_cardtrader(user)
    service.api_token = ""
    service.ct_user_id = None
    service.save(update_fields=["api_token", "ct_user_id"])
    MarketConnection.objects.filter(user=user, market=Market.CARDTRADER).update(status=MarketConnection.Status.OFF)
    _log(f"{CARDTRADER}: API token removed", SyncLog.Level.WARNING, owner=user)


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


# --- Catalog matching ------------------------------------------------------

def _ct_number(bp):
    """Collector number of a CardTrader blueprint (export payload), normalised."""
    return normalize_number((bp.get("fixed_properties") or {}).get("collector_number", ""))


def _ct_properties(bp):
    """Names of the properties CardTrader takes when selling the blueprint; None when it does not say."""
    props = bp.get("editable_properties")
    if props is None:
        return None
    return [p["name"] for p in props if isinstance(p, dict) and p.get("name")]


@dataclass
class Resolution:
    """resolve_blueprints(): cards with no CardTrader blueprint, and what CardTrader takes for each blueprint seen."""

    unmatched: list = field(default_factory=list)
    properties: dict = field(default_factory=dict)  # CardTrader blueprint id -> property names, or None
    not_sold: set = field(default_factory=set)  # pks of the unmatched cards whose game CardTrader does not sell


# Names CardTrader may give a game (GET /games), by slug, besides Game.name; compared normalised (_game_name).
CT_GAME_ALIASES = {
    MAGIC: ["magic", "magic: the gathering"],
    "pokemon": ["pokémon", "pokemon"],
    "lorcana": ["disney lorcana", "lorcana"],
    "onepiece": ["one piece", "one piece card game"],
    "yugioh": ["yu-gi-oh!", "yugioh"],
    "riftbound": ["riftbound", "riftbound: league of legends trading card game"],
}


def _game_name(name):
    """A game name for comparison: casefolded, without accents, punctuation or spaces."""
    text = unicodedata.normalize("NFKD", (name or "").casefold())
    return "".join(c for c in text if c.isalnum())


def ct_game_id(client, game):
    """CardTrader's id of game: the stored one, else looked up once in CardTrader's games and stored. None if absent."""
    if game.ct_game_id:
        return game.ct_game_id
    names = {_game_name(n) for n in [game.name, *CT_GAME_ALIASES.get(game.slug, [])]}
    for entry in client.games():
        if entry.get("id") is None:
            continue
        if names & {_game_name(entry.get("name")), _game_name(entry.get("display_name"))} - {""}:
            game.ct_game_id = entry["id"]
            game.save(update_fields=["ct_game_id"])
            return game.ct_game_id
    return None


def _ct_expansion(card, game_id, by_code, by_name):
    """The CardTrader expansion id of a card of CardTrader's game game_id: by its set code, else by its catalog
    expansion's name."""
    found = by_code.get((game_id, card.set_code.lower()))
    if found is None:
        bp = blueprint_of(card)
        if bp is not None:
            found = by_name.get((game_id, bp.expansion.name.casefold()))
    return found


def _ct_match(card, exported):
    """The CardTrader blueprint of a card: by its Cardmarket product, else by number, else by name."""
    if card.cm_product_id:
        for bp in exported:
            if card.cm_product_id in (bp.get("card_market_ids") or []):
                return bp
    number = normalize_number(card.collector_number)
    if number and not number.startswith("CM"):
        for bp in exported:
            if _ct_number(bp) == number:
                return bp
        return None
    # Several blueprints can share a name (alternate versions): keep the base one.
    by_name = None
    for bp in exported:
        if bp["name"].casefold() == card.name.casefold() and (by_name is None or (by_name.get("version") and not bp.get("version"))):
            by_name = bp
    return by_name


def resolve_blueprints(client, cards):
    """Fill Card.ct_blueprint_id from the seller's CardTrader catalog, and read what CardTrader takes for products.

    Only the CardTrader expansions of these cards, in their own game, are downloaded; a card whose expansion is not
    found, or whose game CardTrader does not sell, stays unmatched. The blueprints of products and of every game but
    Magic are read even when already known, for the property names CardTrader takes (language, foil).
    """
    resolution = Resolution()
    todo = [c for c in cards if not c.ct_blueprint_id or c.is_product or c.game.slug != MAGIC]
    if not todo:
        return resolution
    by_code, by_name = {}, {}
    for e in client.expansions():
        if e.get("code"):
            by_code.setdefault((e.get("game_id"), e["code"].lower()), e["id"])
            by_name.setdefault((e.get("game_id"), (e.get("name") or "").casefold()), e["id"])
    game_ids = {}  # Game pk -> CardTrader game id, looked up once per call
    by_expansion = {}
    for card in todo:
        if card.game_id not in game_ids:
            game_ids[card.game_id] = ct_game_id(client, card.game)
        game_id = game_ids[card.game_id]
        expansion_id = None if game_id is None else _ct_expansion(card, game_id, by_code, by_name)
        if expansion_id is None:
            if not card.ct_blueprint_id:
                resolution.unmatched.append(card)
                if game_id is None:
                    resolution.not_sold.add(card.pk)
            continue
        by_expansion.setdefault(expansion_id, []).append(card)
    for expansion_id, set_cards in by_expansion.items():
        exported = client.blueprints(expansion_id) or []
        for card in set_cards:
            if card.ct_blueprint_id:  # already matched: only what it takes is needed
                known = next((bp for bp in exported if bp["id"] == card.ct_blueprint_id), None)
                if known is not None:
                    resolution.properties[known["id"]] = _ct_properties(known)
                continue
            bp = _ct_match(card, exported)
            if bp is None:
                resolution.unmatched.append(card)
                continue
            resolution.properties[bp["id"]] = _ct_properties(bp)
            if not _link_ct_blueprint(card, bp):
                resolution.unmatched.append(card)
    return resolution


def _link_ct_blueprint(card, bp):
    """Store bp as the card's CardTrader blueprint, folding a duplicate of the same printing. False if it can't."""
    number = normalize_number(card.collector_number) or _ct_number(bp)
    existing = (
        Card.objects.exclude(pk=card.pk)
        .filter(Q(ct_blueprint_id=bp["id"])
                | Q(game_id=card.game_id, set_code=card.set_code, collector_number=number) & ~Q(collector_number=""))
        .first()
    )
    other_printing = existing is not None and (
        existing.ct_blueprint_id not in (None, bp["id"])
        or None not in (existing.cm_product_id, card.cm_product_id) and existing.cm_product_id != card.cm_product_id
    )
    if other_printing:
        _log(f"{card} matches CardTrader blueprint {bp['id']}, already used by {existing} (another printing): "
             f"fix it by hand"[:300], SyncLog.Level.WARNING)
        return False
    if existing is not None:
        clash = clashing_owners(card, existing)
        if clash or not merge_cards(card, existing):
            # The clash is about someone's listings: only they see it, never in the shared log.
            for owner_id in clash:
                SyncLog.objects.create(
                    owner_id=owner_id, level=SyncLog.Level.WARNING,
                    message=f"{card} duplicates {existing} but both have live listings: merge them by hand"[:300],
                )
            return False
        if not existing.ct_blueprint_id:
            existing.ct_blueprint_id = bp["id"]
            existing.save(update_fields=["ct_blueprint_id"])
        return True
    card.ct_blueprint_id = bp["id"]
    card.collector_number = number
    card.save(update_fields=["ct_blueprint_id", "collector_number"])
    return True


# --- Listings ------------------------------------------------------------

def product_payload(listing, accepted=None):
    """The CardTrader product for a listing; products (sealed, accessories) send only what CardTrader takes for them.

    accepted: the property names CardTrader takes for the blueprint, None when unknown. Magic sends what it always
    has (singles: condition, mtg_language, mtg_foil if foil; products: mtg_language unless refused, condition if
    taken); other games send the language and foil properties their blueprint names (see ct_properties). A listing
    of another game that these can't describe is refused by undescribed_reason() before getting here.
    """
    item = listing.item
    game = item.card.game
    if game.slug == MAGIC:
        language, foil = "mtg_language", "mtg_foil"
        if item.card.is_product and accepted is not None and language not in accepted:
            language = None
    else:
        language, foil = language_key(accepted, game), foil_key(accepted, game)
    if item.card.is_product:
        properties = {}
        if language:
            properties[language] = LANGUAGE_TO_CT[item.language]
        if accepted is not None and "condition" in accepted:
            properties["condition"] = CONDITION_TO_CT[item.condition]
    else:
        properties = {"condition": CONDITION_TO_CT[item.condition]}
        if language:
            properties[language] = LANGUAGE_TO_CT[item.language]
        if item.foil and foil:
            properties[foil] = True
    payload = {
        "price": float(listing.price),
        "quantity": listing.quantity,
        "properties": properties,
        "error_mode": "strict",
        "user_data_field": f"cardledger:{listing.pk}",
    }
    if listing.ct_product_id:
        payload["id"] = listing.ct_product_id
    else:
        payload["blueprint_id"] = item.card.ct_blueprint_id
    return payload


PROPERTIES_UNKNOWN = "CardTrader properties unknown"
FOIL_NOT_OFFERED = "foil not offered on CardTrader for this blueprint"


def undescribed_reason(listing, accepted):
    """Why a listing of a game other than Magic can't be put on CardTrader as it is, or None.

    Its language (a single's, or any when CardTrader's properties are unknown) and its foil must have a property,
    or CardTrader would sell it with its defaults (English, not foil).
    """
    item = listing.item
    game = item.card.game
    if game.slug == MAGIC:
        return None
    if accepted is None or not item.card.is_product and language_key(accepted, game) is None:
        return PROPERTIES_UNKNOWN
    if item.foil and foil_key(accepted, game) is None:
        return FOIL_NOT_OFFERED
    return None


def _job_error(result):
    errors = result.get("errors") or {}
    if isinstance(errors, dict):
        return "; ".join(f"{k}: {', '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in errors.items())
    return str(errors) or "unknown error"


@dataclass
class PublishResult:
    published: int = 0
    failed: list = field(default_factory=list)  # [(listing, reason)]


def _push_listings(user, listings, result):
    """Create or update the listings on the user's CardTrader account, filling result."""
    with user_client(user) as client:
        resolution = resolve_blueprints(client, {l.item.card for l in listings})
        unmatched = {c.pk for c in resolution.unmatched}
        # Resolution may merge duplicate printings: reload so listings point at the surviving cards.
        listings = list(
            Listing.objects.filter(pk__in=[l.pk for l in listings]).select_related("item__card__game")
        )
        accepted = {l.pk: resolution.properties.get(l.item.card.ct_blueprint_id) for l in listings}
        to_create, to_update = [], []
        for listing in listings:
            reason = None
            if not listing.ct_product_id:  # a new product needs its blueprint
                if listing.item.card_id in resolution.not_sold:
                    reason = "game not sold on CardTrader"
                elif listing.item.card_id in unmatched or not listing.item.card.ct_blueprint_id:
                    reason = "card not found in the CardTrader catalog"
            reason = reason or undescribed_reason(listing, accepted[listing.pk])
            if reason:
                result.failed.append((listing, reason))
            elif listing.ct_product_id:
                to_update.append(listing)
            else:
                to_create.append(listing)

        for batch, call in ((to_create, client.bulk_create), (to_update, client.bulk_update)):
            if not batch:
                continue
            outcomes = call([product_payload(l, accepted[l.pk]) for l in batch])
            for listing, outcome in zip(batch, outcomes):
                if outcome.get("result") in ("ok", "warning") and outcome.get("product_id"):
                    listing.market_listing_id = str(outcome["product_id"])
                    listing.status = Listing.Status.PUBLISHED
                    listing.error_message = ""
                    listing.listed_at = listing.listed_at or timezone.now()
                    listing.save(update_fields=["market_listing_id", "status", "error_message", "listed_at",
                                                "updated_at"])
                    result.published += 1
                else:
                    result.failed.append((listing, _job_error(outcome)))


def _delete_removed(user, listings):
    """Delete on CardTrader the listings the user removed (pending_delete), then here."""
    with user_client(user) as client:
        for listing in listings:
            try:
                client.delete_product(listing.ct_product_id)
            except CardTraderError as exc:
                if exc.status != 404:  # already gone remotely: fine
                    raise
            listing.delete()


def publish_pending(user, game=None):
    """Push the user's CardTrader listings waiting to be published (of one game, or of all) to their CardTrader
    account, and delete there the ones they removed. Cardmarket listings are pushed by the extension only."""
    service = get_cardtrader(user)
    mine = Listing.objects.owned_by(user).filter(market=Market.CARDTRADER)
    if game is not None:
        mine = mine.filter(item__card__game=game)
    pending = mine.filter(status=Listing.Status.TO_PUBLISH, pending_delete=False)
    listings = list(pending.select_related("item__card__game"))
    removed = list(mine.filter(pending_delete=True).exclude(market_listing_id=""))
    result = PublishResult()
    if not listings and not removed:
        return result

    try:
        if removed:
            _delete_removed(user, removed)
        if listings:
            _push_listings(user, listings, result)
    except CardTraderError as exc:
        _log(f"{CARDTRADER}: publish failed ({exc})", SyncLog.Level.ERROR, owner=user)
        mark_failed(user, Market.CARDTRADER, exc)
        raise

    service.last_sync = timezone.now()
    service.save(update_fields=["last_sync"])
    mark_synced(user, Market.CARDTRADER, when=service.last_sync)
    _log(f"{CARDTRADER}: {result.published} listings published", owner=user)
    for listing, reason in result.failed:
        _log(f"{CARDTRADER}: {listing.item.card.name} not published ({reason})", SyncLog.Level.WARNING, owner=user)
    return result


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


def remove_listing(listing, remote=True):
    """Delete a listing locally and, if it is on its market, there too.

    A CardTrader listing is deleted on CardTrader right away (remote=True, the user's token); any other one still on
    its market (a market id, not sold) is only flagged pending_delete: the next sync deletes it there, then here.
    """
    owner = listing.item.owner
    on_market = bool(listing.market_listing_id) and listing.status != Listing.Status.SOLD
    if on_market and not (remote and listing.market == Market.CARDTRADER):
        listing.pending_delete = True
        listing.save(update_fields=["pending_delete", "updated_at"])
        return
    if on_market:
        with user_client(owner) as client:
            try:
                client.delete_product(listing.ct_product_id)
            except CardTraderError as exc:
                if exc.status != 404:  # already gone remotely: fine
                    raise
        _log(f"{CARDTRADER}: listing for {listing.item.card.name} removed", owner=owner)
    listing.delete()


# --- Orders / shipments ----------------------------------------------------

def _dt(value):
    return parse_datetime(value) if value else None


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
            remove_listing(listing, remote=False)
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
    return order, created


def upsert_order(data, owner):
    """Create/update owner's Order from the CardTrader payload. Returns (order, created)."""
    address = data.get("order_shipping_address") or {}
    method = data.get("order_shipping_method") or {}
    buyer = data.get("buyer") or {}
    total = data.get("seller_total") or {}
    fee = money(data.get("seller_fee_amount"))
    fields = {
        "code": data.get("code") or "",
        "state": data.get("state") or "",
        "via_cardtrader_zero": bool(data.get("via_cardtrader_zero")),
        "buyer_username": buyer.get("username") or "",
        "buyer_email": buyer.get("email") or "",
        "buyer_phone": buyer.get("phone") or "",
        "ship_name": address.get("name") or "",
        "ship_street": address.get("street") or "",
        "ship_zip": address.get("zip") or "",
        "ship_city": address.get("city") or "",
        "ship_province": address.get("state_or_province") or "",
        "ship_country": address.get("country") or "",
        "ship_country_code": address.get("country_code") or "",
        "ship_note": address.get("note") or "",
        "shipping_method": method.get("name") or "",
        "tracked": bool(method.get("tracked")),
        "tracking_code": method.get("tracking_code") or "",
        "max_shipping_days": method.get("max_estimate_shipping_days") or None,
        "packing_number": data.get("packing_number"),
        "size": data.get("size") or 0,
        "items_total": money(data.get("seller_subtotal")),
        "shipping_cost": money(method.get("seller_price")),
        "market_fee": fee,
        "total": money(total),
        "net": None if fee is None or money(total) is None else money(total) - fee,
        "currency": total.get("currency") or "",
        "paid_at": _dt(data.get("paid_at")),
        "sent_at": _dt(data.get("sent_at")),
        "created_at": _dt((data.get("order_items") or [{}])[0].get("created_at")),
        "raw": data,
    }
    lines = []
    for raw in data.get("order_items") or []:
        props = raw.get("properties") or {}
        lines.append({
            "market_item_id": raw["id"],
            "market_listing_id": raw.get("product_id") or "",
            "name": raw.get("name"),
            "expansion": raw.get("expansion"),
            "quantity": raw.get("quantity"),
            "price": money(raw.get("seller_price")),
            "condition": CONDITION_FROM_CT.get(props.get("condition"), props.get("condition") or ""),
            "language": read_language(props),
            "foil": read_foil(props),
        })
    return save_order(owner, Market.CARDTRADER, data["id"], fields, lines)


def import_orders(client, service):
    """Import the orders of service's user (client is their own)."""
    owner = service.user
    since = service.orders_synced_at - timedelta(days=14) if service.orders_synced_at else None
    created = updated = 0
    for data in client.all_orders(date_from=since.date() if since else None):
        with transaction.atomic():
            _, was_created = upsert_order(data, owner)
        created += was_created
        updated += not was_created
    service.orders_synced_at = timezone.now()
    service.save(update_fields=["orders_synced_at"])
    MarketConnection.objects.filter(user=owner, market=Market.CARDTRADER).update(
        orders_synced_at=service.orders_synced_at)
    to_ship = Order.objects.owned_by(owner).filter(state=Order.State.PAID).count()
    _log(f"{CARDTRADER}: {created} new orders, {updated} updated, {to_ship} to ship", owner=owner)
    return created, updated


def diff_listings(user, market, remote, service=None):
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
                defaults={"local_value": local, "remote_value": remote_value, "remote_amount": amount,
                          "service": service},
            )
            seen.add(conflict.pk)
    stale = (SyncConflict.objects.owned_by(user).filter(market=market, resolution=SyncConflict.Resolution.OPEN)
             .exclude(pk__in=seen))
    stale.delete()
    return len(seen)


def _diff_listings(service, products):
    """Open/refresh conflicts between service's user's CardTrader listings and their CardTrader products."""
    remote = {str(p["id"]): (money(p.get("price_cents")), p.get("quantity")) for p in products}
    return diff_listings(service.user, Market.CARDTRADER, remote, service=service)


def sync_all(user):
    """Import the user's orders (applying sold stock), then compare their listings with CardTrader."""
    service = get_cardtrader(user)
    try:
        with user_client(user) as client:
            import_orders(client, service)
            conflicts = _diff_listings(service, client.my_products() or [])
    except CardTraderError as exc:
        _log(f"{CARDTRADER}: sync failed ({exc})", SyncLog.Level.ERROR, owner=user)
        mark_failed(user, Market.CARDTRADER, exc)
        raise
    service.last_sync = timezone.now()
    service.save(update_fields=["last_sync"])
    mark_synced(user, Market.CARDTRADER, when=service.last_sync)
    if conflicts:
        _log(f"{CARDTRADER}: {conflicts} listings differ from Ledger", SyncLog.Level.WARNING, owner=user)
    else:
        _log(f"{CARDTRADER}: listings in sync", owner=user)
    return conflicts


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


NOT_CARDTRADER = "Only CardTrader orders are shipped from here: mark the others as shipped with the CardLedger extension."


def ship_order(order, tracking_code=""):
    """Set the tracking code (if any) and mark the order as shipped on CardTrader."""
    if order.market != Market.CARDTRADER:
        raise CardTraderError(NOT_CARDTRADER)
    tracking_code = tracking_code.strip()
    if (order.via_cardtrader_zero or order.tracked) and not (tracking_code or order.tracking_code):
        kind = "CardTrader Zero orders" if order.via_cardtrader_zero else "Tracked shipments"
        raise CardTraderError(f"{kind} need a tracking code before shipping.")
    with user_client(order.owner) as client:
        if tracking_code and tracking_code != order.tracking_code:
            client.set_tracking_code(order.ct_id, tracking_code)
            order.tracking_code = tracking_code
            order.save(update_fields=["tracking_code", "updated_at"])
        data = client.ship(order.ct_id)
    if isinstance(data, dict) and data.get("id") == order.ct_id:
        order, _ = upsert_order(data, order.owner)
    else:
        order.state = Order.State.SENT
        order.sent_at = timezone.now()
        order.save(update_fields=["state", "sent_at", "updated_at"])
    _log(f"{CARDTRADER}: order #{order.code} shipped", owner=order.owner)
    return order


def save_tracking_code(order, tracking_code):
    if order.market != Market.CARDTRADER:
        raise CardTraderError(NOT_CARDTRADER)
    with user_client(order.owner) as client:
        client.set_tracking_code(order.ct_id, tracking_code.strip())
    order.tracking_code = tracking_code.strip()
    order.save(update_fields=["tracking_code", "updated_at"])
    _log(f"{CARDTRADER}: tracking code set for order #{order.code}", owner=order.owner)
