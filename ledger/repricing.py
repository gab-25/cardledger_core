"""The repricing engine: what price the user's rules give one listing. Pure (no database): ledger/repricer.py loads
the rules and the facts of each listing, applies the outcomes and records the runs.

Order of evaluation of one listing (evaluate()):
1. the first rule of kind price or over_time (in the order given: by position) that applies to it (its markets and
   every condition match, and its reference has a value) sets the candidate price; without one the price stays the
   current one (or the fallback, for a new listing's quote);
2. every floor rule that applies raises it to its floor (the highest wins);
3. hold_below_cost: never below the item's cost;
4. the market minimum (Cardmarket 0.02, CardTrader 0.01);
5. rounding last, to the winning rule's step (else the seller's): to the nearest step, or up when a floor, the cost
   or the market minimum is binding, so the result never goes back under it.
A price nothing set nor raised stays exactly the current one (never re-rounded).
"""

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
DEFAULT_STEP = Decimal("0.05")
MARKET_MINIMUM = {"cm": Decimal("0.02"), "ct": Decimal("0.01")}
MARKETS = ("cm", "ct")

PRICE, FLOOR, OVER_TIME = "price", "floor", "over_time"
KINDS = (PRICE, FLOOR, OVER_TIME)
REFERENCES = ("trend", "low", "avg7", "avg30", "cost", "other_market", "current")
ROUND_STEPS = ("0.01", "0.05", "0.10", "0.50", "1.00")

# Condition field -> the operators it takes.
CONDITION_OPS = {
    "game": ("in",), "rarity": ("in",), "condition": ("in",), "language": ("in",), "set": ("in",),
    "foil": ("eq",), "is_product": ("eq",), "both_markets": ("eq",), "cost_known": ("eq",),
    "trend": ("gte", "lte", "between"), "listed_days": ("gte", "lte"),
}


@dataclass(frozen=True)
class Rule:
    """One enabled rule. conditions: [{field, op, value}] as validated by ledger.forms.RuleForm."""

    id: object
    name: str
    kind: str
    markets: str = "all"  # all | cm | ct
    conditions: tuple = ()
    reference: str = "trend"
    adjust_pct: Decimal = Decimal("0")
    adjust_abs: Decimal = Decimal("0")
    round_to: Decimal | None = None  # None: the seller's step
    interval_days: int | None = None
    active: bool = True  # enabled and not a draft; evaluate() skips the others


@dataclass
class Facts:
    """What the rules can see of one listing (or of a listing to create, for a quote)."""

    market: str
    game: str = ""
    rarity: str = ""
    condition: str = ""
    language: str = ""
    foil: bool = False
    is_product: bool = False
    set_code: str = ""
    refs: dict = field(default_factory=dict)  # REFERENCES -> Decimal or None
    listed_days: int | None = None  # days since listed_at, None when never published
    last_step_days: int | None = None  # days since the last over-time step, None when never stepped
    both_markets: bool = False

    @property
    def current(self):
        return self.refs.get("current")

    @property
    def cost(self):
        return self.refs.get("cost")


@dataclass
class Outcome:
    price: Decimal | None  # None: no price (no current one and nothing set it)
    rule: Rule | None = None  # the price/over_time rule that set the candidate
    floor_rule: Rule | None = None  # the floor that raised it
    blocked_by_floor: bool = False  # a floor or hold_below_cost raised the candidate
    held_at_cost: bool = False
    stepped: bool = False  # an over-time step was taken

    def moved(self, current):
        """The new price when it differs from current by a cent or more, else None."""
        if self.price is None or current is not None and abs(self.price - current) < CENT:
            return None
        return self.price


def round_price(price, step, minimum=None):
    """price rounded to the nearest step (half up); up instead when that would fall under minimum."""
    step = Decimal(step)
    rounded = (price / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step
    if minimum is not None and rounded < minimum:
        rounded = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
    return rounded.quantize(CENT)


def adjusted(base, pct, amount):
    """base × (1 + pct/100) + amount."""
    return base * (1 + Decimal(pct) / 100) + Decimal(amount)


# --- matching -----------------------------------------------------------------

def _decimal(value):
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _lower(values):
    return {str(v).strip().lower() for v in values}


def condition_matches(cond, facts):
    """Whether one condition {field, op, value} holds for facts."""
    name, op, value = cond["field"], cond["op"], cond["value"]
    if op == "in":
        actual = {"game": facts.game, "rarity": facts.rarity, "condition": facts.condition,
                  "language": facts.language, "set": facts.set_code}[name]
        return str(actual).strip().lower() in _lower(value)
    if op == "eq":
        actual = {"foil": facts.foil, "is_product": facts.is_product, "both_markets": facts.both_markets,
                  "cost_known": facts.cost is not None}[name]
        return bool(actual) == bool(value)
    if name == "trend":
        actual = facts.refs.get("trend")
        if actual is None:
            return False
    else:  # listed_days: a listing never published has been listed 0 days
        actual = facts.listed_days or 0
    if op == "between":
        low, high = (_decimal(v) for v in value)
        return low <= actual <= high
    bound = _decimal(value)
    return actual >= bound if op == "gte" else actual <= bound


def rule_matches(rule, facts):
    """The rule's markets include the listing's and every condition holds (none: any listing)."""
    if rule.markets not in ("all", facts.market):
        return False
    return all(condition_matches(c, facts) for c in rule.conditions)


def step_due(rule, facts):
    """An over-time rule takes a step once listed for interval_days, then every interval_days after the last."""
    interval = rule.interval_days or 0
    if interval < 1 or facts.listed_days is None or facts.listed_days < interval:
        return False
    return facts.last_step_days is None or facts.last_step_days >= interval


def rule_price(rule, facts):
    """(candidate, stepped) a price/over_time rule gives facts, None when it cannot price it (no reference)."""
    if rule.kind == OVER_TIME:
        if facts.current is None:
            return None
        if not step_due(rule, facts):
            return facts.current, False
        return adjusted(facts.current, rule.adjust_pct, rule.adjust_abs), True
    base = facts.refs.get(rule.reference)
    if base is None:
        return None
    return adjusted(base, rule.adjust_pct, rule.adjust_abs), False


def floor_of(rule, facts):
    base = facts.refs.get(rule.reference)
    return None if base is None else adjusted(base, rule.adjust_pct, rule.adjust_abs)


# --- evaluation ---------------------------------------------------------------

def evaluate(facts, rules, default_step=DEFAULT_STEP, hold_below_cost=False, candidate=None, fallback=None):
    """The Outcome of the rules (in order; inactive ones are skipped) on facts.

    candidate: a price set by hand (ad-hoc repricing): the price rules are skipped, floors and the rest apply.
    fallback: the price when no rule sets one and there is no current price (a quote: the Cardmarket trend).
    """
    rules = [r for r in rules if r.active]
    current = facts.current
    winner, stepped, touched = None, False, False
    price = candidate
    if price is not None:
        touched = True
    else:
        for rule in rules:
            if rule.kind == FLOOR or not rule_matches(rule, facts):
                continue
            found = rule_price(rule, facts)
            if found is None:
                continue
            price, stepped = found
            winner = rule
            touched = rule.kind == PRICE or stepped
            break
        if price is None:
            price = current
            if price is None and fallback is not None:
                price, touched = fallback, True
    if price is None:
        return Outcome(None)
    outcome = Outcome(price, rule=winner, stepped=stepped)

    raised = price
    for rule in rules:
        if rule.kind != FLOOR or not rule_matches(rule, facts):
            continue
        floor = floor_of(rule, facts)
        if floor is not None and floor > raised:
            raised, outcome.floor_rule = floor, rule
    if hold_below_cost and facts.cost is not None and raised < facts.cost:
        raised, outcome.held_at_cost = facts.cost, True
        outcome.floor_rule = None
    outcome.blocked_by_floor = raised > price
    minimum = MARKET_MINIMUM.get(facts.market, CENT)
    if raised < minimum:
        raised = minimum
    constrained = raised > price
    if not touched and not constrained:
        outcome.price = current  # nothing set it nor raised it: left exactly as it is
        return outcome
    step = (winner.round_to if winner is not None and winner.round_to else None) or default_step
    outcome.price = round_price(raised, step, minimum=raised if constrained else minimum)
    return outcome


def insert_rule(rules, rule, position=None, replace_id=None):
    """rules (ordered) with rule at 1-based position (None: last), replacing the rule with id replace_id."""
    rest = [r for r in rules if replace_id is None or r.id != replace_id]
    index = len(rest) if position is None else max(0, min(position - 1, len(rest)))
    return [*rest[:index], rule, *rest[index:]]
