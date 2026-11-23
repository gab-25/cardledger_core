"""The database side of repricing: loads the user's rules and the facts of the listings for the engine
(ledger/repricing.py), and previews, applies and records its results (runs, quotes, ad-hoc reprices).

A changed price on a published listing sends it back to "to publish": the next extension sync pushes it.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from accounts.models import seller_settings_of

from . import repricing
from .models import Blueprint, CardmarketPrice, Listing, Market, RepricingChange, RepricingRule, RepricingRun

KEEP_RUNS = 50
REPRICED_STATUSES = [Listing.Status.TO_PUBLISH, Listing.Status.PUBLISHED, Listing.Status.PAUSED]
GUIDE_REFERENCES = ("trend", "low", "avg7", "avg30")
MARKET_LABELS = dict(Market.choices)


def engine_rule(rule):
    """The engine's Rule of a RepricingRule."""
    return repricing.Rule(
        id=rule.pk, name=rule.name, kind=rule.kind, markets=rule.markets, conditions=tuple(rule.conditions or ()),
        reference=rule.reference, adjust_pct=Decimal(rule.adjust_pct or 0), adjust_abs=Decimal(rule.adjust_abs or 0),
        round_to=Decimal(rule.round_to) if rule.round_to else None, interval_days=rule.interval_days,
        active=rule.enabled and not rule.draft,
    )


def engine_rules(user):
    """All the user's rules as engine rules, by position (inactive ones included: evaluate() skips them)."""
    return [engine_rule(r) for r in RepricingRule.objects.owned_by(user).order_by("position", "id")]


@dataclass
class Settings:
    step: Decimal
    hold_below_cost: bool


def settings_of(user):
    seller = seller_settings_of(user)
    return Settings(Decimal(seller.price_rounding) if seller.price_rounding else repricing.DEFAULT_STEP,
                    seller.hold_below_cost)


def evaluate(facts, rules, settings, **kwargs):
    return repricing.evaluate(facts, rules, default_step=settings.step, hold_below_cost=settings.hold_below_cost,
                              **kwargs)


# --- facts --------------------------------------------------------------------

def _guide(price, foil):
    """{reference: value} of a CardmarketPrice row (the *_foil values for foil), 0.00 as unknown."""
    values = {}
    for name in GUIDE_REFERENCES:
        value = getattr(price, f"{name}_foil" if foil else name, None) if price is not None else None
        values[name] = value or None
    return values


def _days(since, now):
    return None if since is None else max((now - since).days, 0)


class FactsLoader:
    """Facts of listings (or of listings to create), with the price guide and rarities read in bulk."""

    def __init__(self, products):
        products = {p for p in products if p}
        self.prices = {p.pk: p for p in CardmarketPrice.objects.filter(pk__in=products)}
        self.rarities = dict(Blueprint.objects.filter(cm_product_id__in=products)
                             .values_list("cm_product_id", "rarity"))
        self.now = timezone.now()

    def for_item(self, item, market, listing=None):
        """Facts of item's listing on market (listing; None: one to create). item.listings should be prefetched."""
        card = item.card
        foil = item.foil and not card.is_product
        others = [l for l in item.listings.all() if l.is_active and l.market != market]
        mine = listing is not None and listing.is_active
        refs = _guide(self.prices.get(card.cm_product_id), foil)
        refs.update(cost=item.cost, current=listing.price if listing is not None else None,
                    other_market=min((l.price for l in others), default=None))
        return repricing.Facts(
            market=market, game=card.game.slug, rarity=self.rarities.get(card.cm_product_id, "") or "",
            condition=item.condition, language=item.language, foil=item.foil, is_product=card.is_product,
            set_code=card.set_code, refs=refs, both_markets=mine and bool(others),
            listed_days=_days(listing.listed_at, self.now) if listing is not None else None,
            last_step_days=_days(listing.last_step_at, self.now) if listing is not None else None,
        )


def repriced_listings(user, market=None):
    """The user's listings the rules reprice: to publish, published or paused (not error, sold or removed)."""
    listings = (Listing.objects.owned_by(user).filter(status__in=REPRICED_STATUSES, pending_delete=False)
                .select_related("item__card__game").prefetch_related("item__listings")
                .order_by("item__card__name", "item__card__set_code", "market", "pk"))
    if market:
        listings = listings.filter(market=market)
    return list(listings)


@dataclass
class Proposal:
    listing: Listing
    facts: repricing.Facts
    outcome: repricing.Outcome

    @property
    def old(self):
        return self.listing.price

    @property
    def new(self):
        """The new price, None when it does not move."""
        return self.outcome.moved(self.listing.price)


def propose(user, rules=None, market=None, settings=None):
    """The engine's outcome on every repriced listing of the user's (rules: engine rules, default the user's)."""
    rules = engine_rules(user) if rules is None else rules
    settings = settings or settings_of(user)
    listings = repriced_listings(user, market)
    loader = FactsLoader(l.item.card.cm_product_id for l in listings)
    proposals = []
    for listing in listings:
        facts = loader.for_item(listing.item, listing.market, listing)
        proposals.append(Proposal(listing, facts, evaluate(facts, rules, settings)))
    return proposals


# --- preview and runs ----------------------------------------------------------

def money(value):
    return None if value is None else f"{value:.2f}"


def totals(proposals):
    moved = [p for p in proposals if p.new is not None]
    delta = sum(((p.new - p.old) * p.listing.quantity for p in moved), Decimal("0"))
    return {
        "changes": len(moved),
        "raised": sum(1 for p in moved if p.new > p.old),
        "lowered": sum(1 for p in moved if p.new < p.old),
        "value_delta": money(delta),
        "blocked": sum(1 for p in proposals if p.outcome.blocked_by_floor),
        "by_market": {m: sum(1 for p in moved if p.listing.market == m) for m in Market.values},
    }


def change_row(proposal):
    """A preview row (the shape of a run's change)."""
    listing, item, outcome = proposal.listing, proposal.listing.item, proposal.outcome
    return {
        "listing_id": listing.pk, "item_id": item.pk, "name": item.card.name, "set_code": item.card.set_code,
        "condition": item.condition, "language": item.language, "foil": item.foil, "market": listing.market,
        "from": money(proposal.old), "to": money(proposal.new),
        "rule": outcome.rule.name if outcome.rule else None,
        "floor_rule": outcome.floor_rule.name if outcome.floor_rule else None,
        "blocked_by_floor": outcome.blocked_by_floor,
    }


BIGGEST_KEYS = ("listing_id", "name", "set_code", "condition", "foil", "market", "from", "to")


def biggest(proposals, n=5):
    moved = sorted((p for p in proposals if p.new is not None), key=lambda p: -abs(p.new - p.old))[:n]
    return [{k: change_row(p)[k] for k in BIGGEST_KEYS} for p in moved]


def _run_counts(run, changes):
    run.changes = len(changes)
    run.raised = sum(1 for c in changes if c.old_price is not None and c.new_price > c.old_price)
    run.lowered = sum(1 for c in changes if c.old_price is not None and c.new_price < c.old_price)


def _trim_runs(user):
    old = RepricingRun.objects.owned_by(user).order_by("-started_at", "-id").values_list("pk", flat=True)[KEEP_RUNS:]
    RepricingRun.objects.filter(pk__in=list(old)).delete()


def _change(run, listing, old, new, outcome=None):
    item = listing.item
    rule = getattr(outcome, "rule", None)
    floor = getattr(outcome, "floor_rule", None)
    return RepricingChange(
        run=run, listing=listing, market=listing.market, old_price=old, new_price=new,
        rule_id=rule.id if rule else None, floor_rule_id=floor.id if floor else None,
        rule_name=rule.name if rule else "", floor_rule_name=floor.name if floor else "",
        blocked_by_floor=bool(getattr(outcome, "blocked_by_floor", False)), item_pk=item.pk, name=item.card.name,
        set_code=item.card.set_code, condition=item.condition, language=item.language, foil=item.foil,
    )


def set_price(listing, price):
    """Give listing a new price; a published one goes back to "to publish" (the next sync pushes it)."""
    listing.price = price
    if listing.status == Listing.Status.PUBLISHED:
        listing.status = Listing.Status.TO_PUBLISH


def run(user, trigger=RepricingRun.Trigger.MANUAL, market=None):
    """Apply the user's rules to their repriced listings (of market, None: every market) and record the run."""
    with transaction.atomic():
        proposals = propose(user, market=market)
        run = RepricingRun.objects.create(user=user, trigger=trigger)
        changes, delta, now = [], Decimal("0"), timezone.now()
        for p in proposals:
            new, listing = p.new, p.listing
            if new is None:
                continue
            changes.append(_change(run, listing, listing.price, new, p.outcome))
            delta += (new - listing.price) * listing.quantity
            set_price(listing, new)
            fields = ["price", "status", "updated_at"]
            if p.outcome.stepped:
                listing.last_step_at = now
                fields.append("last_step_at")
            listing.save(update_fields=fields)
        RepricingChange.objects.bulk_create(changes)
        _run_counts(run, changes)
        run.value_delta = delta
        run.blocked = sum(1 for p in proposals if p.outcome.blocked_by_floor)
        run.finished_at = timezone.now()
        run.save()
        _trim_runs(user)
    return run


def run_data(run):
    return {
        "id": run.pk, "started_at": run.started_at, "finished_at": run.finished_at, "trigger": run.trigger,
        "dry_run": run.dry_run, "changes": run.changes, "raised": run.raised, "lowered": run.lowered,
        "value_delta": money(run.value_delta), "blocked": run.blocked,
    }


def stored_change_row(change):
    return {
        "listing_id": change.listing_id, "item_id": change.item_pk, "name": change.name, "set_code": change.set_code,
        "condition": change.condition, "language": change.language, "foil": change.foil, "market": change.market,
        "from": money(change.old_price), "to": money(change.new_price),
        "rule": change.rule.name if change.rule else (change.rule_name or None),
        "floor_rule": change.floor_rule.name if change.floor_rule else (change.floor_rule_name or None),
        "blocked_by_floor": change.blocked_by_floor,
    }


# --- rules -----------------------------------------------------------------------

def rule_stats(user, rules):
    """{rule id: {matched, last_moved, last_blocked}}: matched = the repriced listings it applies to now (markets and
    conditions); last_moved / last_blocked = the changes the latest run (manual or before a sync) owes to it (as
    its price rule or its floor) / that its floor held up, None before any run."""
    listings = repriced_listings(user)
    loader = FactsLoader(l.item.card.cm_product_id for l in listings)
    facts = [loader.for_item(l.item, l.market, l) for l in listings]
    last = (RepricingRun.objects.owned_by(user).exclude(trigger=RepricingRun.Trigger.ADHOC)
            .order_by("-started_at", "-id").first())
    changes = list(last.price_changes.values_list("rule_id", "floor_rule_id", "blocked_by_floor")) if last else []
    stats = {}
    for rule in rules:
        er = engine_rule(rule)
        stats[rule.pk] = {
            "matched": sum(1 for f in facts if repricing.rule_matches(er, f)),
            "last_moved": sum(1 for r, fl, _ in changes if rule.pk in (r, fl)) if last else None,
            "last_blocked": sum(1 for _, fl, b in changes if b and fl == rule.pk) if last else None,
        }
    return stats


def preview_rule(user, rule, position=None, replace_id=None):
    """{matched, moves, up, down} of an unsaved rule (an engine Rule) put at position (None: last), replacing the
    rule replace_id: matched = listings it applies to, moves = listings whose price would change because of it."""
    rules = repricing.insert_rule(engine_rules(user), rule, position, replace_id)
    up = down = matched = 0
    for p in propose(user, rules=rules):
        if repricing.rule_matches(rule, p.facts):
            matched += 1
        new = p.new
        if new is None or not (p.outcome.rule is rule or p.outcome.floor_rule is rule):
            continue
        if new > p.old:
            up += 1
        else:
            down += 1
    return {"matched": matched, "moves": up + down, "up": up, "down": down}


# --- quote, listing new copies ----------------------------------------------------

@dataclass
class Quoted:
    market: str
    price: Decimal | None
    rule: object = None
    floor_rule: object = None
    note: str = ""

    def data(self):
        return {"market": self.market, "price": money(self.price), "rule": self.rule.name if self.rule else None,
                "floor_rule": self.floor_rule.name if self.floor_rule else None, "note": self.note}


def _note(outcome, fallback_used):
    notes = []
    if fallback_used:
        notes.append("No rule sets it: Cardmarket trend.")
    if outcome.floor_rule is not None:
        notes.append(f"Raised to the floor of {outcome.floor_rule.name}.")
    if outcome.held_at_cost:
        notes.append("Held at cost.")
    return " ".join(notes)


def quote(user, blueprint, condition, language, foil, cost, markets=None, rules=None, settings=None):
    """[Quoted] per market: what the rules would set for a new listing of blueprint (a catalog Blueprint) with
    these copies. Without a matching price rule the price is the Cardmarket trend (null without one)."""
    is_product = blueprint.is_product
    foil = bool(foil) and not is_product
    price = CardmarketPrice.objects.filter(pk=blueprint.cm_product_id).first()
    refs = _guide(price, foil)
    refs.update(cost=cost, current=None, other_market=None)
    rules = engine_rules(user) if rules is None else rules
    settings = settings or settings_of(user)
    out = []
    for market in markets or Market.values:
        facts = repricing.Facts(
            market=market, game=blueprint.expansion.game.slug, rarity=blueprint.rarity or "",
            condition=condition, language=language, foil=foil, is_product=is_product,
            set_code=blueprint.expansion.code, refs=dict(refs),
        )
        outcome = evaluate(facts, rules, settings, fallback=refs["trend"])
        if outcome.price is None:
            out.append(Quoted(market, None, note="No rule sets a price and there is no Cardmarket trend."))
            continue
        out.append(Quoted(market, outcome.price, outcome.rule, outcome.floor_rule,
                          _note(outcome, outcome.rule is None)))
    return out


def put_on_sale(item, market, price, quantity):
    """Create or reopen item's listing on market (to publish) at price for quantity copies, or add quantity to an
    active one (back to "to publish" unless paused; its price stays). Returns (listing, old price or None)."""
    listing = item.listings.filter(market=market).first()
    if listing is None:
        return Listing.objects.create(item=item, market=market, price=price, quantity=quantity,
                                      status=Listing.Status.TO_PUBLISH), None
    old = listing.price if listing.is_active else None
    if listing.status == Listing.Status.SOLD:  # sold out on the market: a new listing there
        listing.market_listing_id, listing.sold_at, listing.listed_at, listing.last_step_at = "", None, None, None
        listing.price, listing.quantity = price, quantity
    elif listing.pending_delete:  # removed but still on the market: updated there instead
        listing.price, listing.quantity = price, quantity
    else:  # active: more copies (a paused one stays paused)
        listing.quantity = min(listing.quantity + quantity, item.quantity)
        if listing.status == Listing.Status.PAUSED:
            listing.save(update_fields=["quantity", "updated_at"])
            return listing, old
    listing.status, listing.pending_delete, listing.error_message = Listing.Status.TO_PUBLISH, False, ""
    listing.save()
    return listing, old


def list_new_copies(user, item, blueprint, markets, added):
    """List the copies just added to item on markets at the quoted price. Returns (messages tuples, listed)."""
    quotes = quote(user, blueprint, item.condition, item.language, item.foil, item.cost, markets=markets)
    listed, skipped = [], []
    for q in quotes:
        if q.price is None:
            skipped.append(MARKET_LABELS[q.market])
            continue
        put_on_sale(item, q.market, q.price, added)
        listed.append(f"{MARKET_LABELS[q.market]} at €{q.price:.2f}")
    return listed, skipped


# --- ad-hoc ------------------------------------------------------------------------

@dataclass
class AdhocLine:
    item: object
    now: dict
    new: dict
    note: str
    outcomes: dict


def _adhoc_base(mode, value, unit, basis, facts):
    """(candidate price or None, note) of one market's listing."""
    if mode == "set":
        return value, ""
    base = facts.refs.get(basis)
    if base is None:
        label = {"trend": "Cardmarket trend", "low": "Cardmarket low", "avg30": "Cardmarket 30-day average",
                 "current": f"current price on {MARKET_LABELS[facts.market]}"}[basis]
        return None, f"No {label}."
    if mode == "trend_pct" or unit == "pct":
        return repricing.adjusted(base, value, 0), ""
    return base + value, ""


def adhoc(user, items, mode, value, unit, basis, markets):
    """[AdhocLine]: the prices an ad-hoc reprice gives items (queryset of the user's) on markets, floors, the cost
    and the market minimum applied."""
    items = list(items.select_related("card__game").prefetch_related("listings").order_by("card__name", "pk"))
    loader = FactsLoader(i.card.cm_product_id for i in items)
    rules, settings = engine_rules(user), settings_of(user)
    lines = []
    for item in items:
        now = {m: None for m in Market.values}
        for listing in item.active_listings():
            now[listing.market] = listing.price
        new, notes, outcomes = dict(now), [], {}
        for market in markets:
            listing = item.listing_for(market)
            listing = listing if listing is not None and listing.is_active else None
            if listing is None and item.quantity < 1:
                notes.append(f"No copies to list on {MARKET_LABELS[market]}.")
                continue
            facts = loader.for_item(item, market, listing)
            candidate, note = _adhoc_base(mode, value, unit, basis, facts)
            if candidate is None:
                notes.append(note)
                continue
            outcome = evaluate(facts, rules, settings, candidate=candidate)
            new[market], outcomes[market] = outcome.price, outcome
            if outcome.floor_rule is not None:
                notes.append(f"{MARKET_LABELS[market]}: raised to the floor of {outcome.floor_rule.name}.")
            elif outcome.held_at_cost:
                notes.append(f"{MARKET_LABELS[market]}: held at cost.")
        lines.append(AdhocLine(item, now, new, " ".join(dict.fromkeys(notes)), outcomes))
    return lines


def adhoc_row(line):
    return {"item_id": line.item.pk, "name": line.item.card.name,
            "now": {m: money(v) for m, v in line.now.items()}, "new": {m: money(v) for m, v in line.new.items()},
            "note": line.note}


def apply_adhoc(user, lines):
    """Create or update the listings of the ad-hoc lines (to publish), recorded as an ad-hoc run. Returns the number
    of items changed."""
    count = 0
    with transaction.atomic():
        run = RepricingRun.objects.create(user=user, trigger=RepricingRun.Trigger.ADHOC)
        changes, delta = [], Decimal("0")
        for line in lines:
            changed = False
            for market, outcome in line.outcomes.items():
                item, price = line.item, outcome.price
                listing = item.listing_for(market)
                if listing is not None and listing.is_active:
                    if abs(listing.price - price) < repricing.CENT:
                        continue
                    old = listing.price
                    set_price(listing, price)
                    if listing.status == Listing.Status.ERROR:
                        listing.status = Listing.Status.TO_PUBLISH
                    listing.save(update_fields=["price", "status", "updated_at"])
                    delta += (price - old) * listing.quantity
                else:
                    listing, old = put_on_sale(item, market, price, item.quantity)
                changes.append(_change(run, listing, old, price, outcome))
                changed = True
            count += changed
        RepricingChange.objects.bulk_create(changes)
        _run_counts(run, changes)
        run.value_delta = delta
        run.blocked = sum(1 for c in changes if c.blocked_by_floor)
        run.finished_at = timezone.now()
        run.save()
        _trim_runs(user)
    return count
