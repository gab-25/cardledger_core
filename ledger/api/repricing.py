"""The repricing API (/api/repricing/): the user's rules, the preview and runs of the rules, quotes for new listings
and ad-hoc reprices. Prices are computed here only (no market is called); a changed price on a published listing
goes back to "to publish", so the next extension sync pushes it."""

from dataclasses import replace
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .. import repricer
from ..forms import AdhocForm, RuleForm
from ..models import Blueprint, CollectionItem, Condition, Language, Market, RepricingRule, RepricingRun
from .common import PAGE, PAGE_SIZE, body, doc, form_body, form_errors, messages_response, msg, query

TAGS = ["repricing"]
RULE = form_body("RepricingRule", RuleForm)
RULE_PREVIEW = form_body("RepricingRulePreview", RuleForm, id=serializers.IntegerField(
    required=False, help_text="The saved rule this one would replace"))
REORDER = body("RepricingReorder", {"ids": serializers.ListField(
    child=serializers.IntegerField(), help_text="The user's rule ids in their new order (missing ones follow)")})
ADHOC = form_body("RepricingAdhoc", AdhocForm, ids=serializers.ListField(
    child=serializers.IntegerField(), help_text="Ids of the user's collection items (others are ignored)"))
MARKET = query("market", "Only this market's changes in results (cm, ct)", enum=Market.values)


def _rule(rule, stats):
    return {
        "id": rule.pk, "name": rule.name, "position": rule.position, "kind": rule.kind, "markets": rule.markets,
        "conditions": rule.conditions, "reference": rule.reference, "adjust_pct": f"{rule.adjust_pct:.2f}",
        "adjust_abs": f"{rule.adjust_abs:.2f}", "round_to": rule.round_to, "interval_days": rule.interval_days,
        "enabled": rule.enabled, "draft": rule.draft, "created_at": rule.created_at, "updated_at": rule.updated_at,
        "stats": stats,
    }


def _renumber(user, ordered):
    """Save positions 1..n for the user's rules in this order."""
    for position, rule in enumerate(ordered, start=1):
        if rule.position != position:
            rule.position = position
            rule.save(update_fields=["position"])


def _place(user, rule, position):
    """Move rule (saved) to 1-based position among the user's rules (None: keep its place), renumbering all."""
    others = list(RepricingRule.objects.owned_by(user).exclude(pk=rule.pk).order_by("position", "id"))
    if position is None:
        others.append(rule)
        others.sort(key=lambda r: (r.position, r.pk))
        _renumber(user, others)
        return
    index = max(0, min(position - 1, len(others)))
    _renumber(user, [*others[:index], rule, *others[index:]])


@doc(TAGS, "The repricing rules by position, each with stats: matched (listings it applies to now), last_moved and "
           "last_blocked (in the latest run; null before one) (GET), or create one (POST; position absent: last)",
     request=RULE)
@api_view(["GET", "POST"])
def rules(request):
    user = request.user
    if request.method == "GET":
        rows = list(RepricingRule.objects.owned_by(user).order_by("position", "id"))
        stats = repricer.rule_stats(user, rows)
        return Response({"results": [_rule(r, stats[r.pk]) for r in rows]})
    form = RuleForm(request.data, instance=RepricingRule(user=user))
    if not form.is_valid():
        raise form_errors(form)
    with transaction.atomic():
        rule = form.save(commit=False)
        last = RepricingRule.objects.owned_by(user).aggregate(n=Max("position"))["n"] or 0
        rule.position = last + 1
        rule.save()
        if form.cleaned_data.get("position"):
            _place(user, rule, form.cleaned_data["position"])
    return messages_response(msg("success", f"Rule {rule.name} created."), status=status.HTTP_201_CREATED,
                             id=rule.pk)


@doc(TAGS, "Change a repricing rule (PATCH; a field left out keeps its value; position moves it) or delete it "
           "(DELETE)", request=RULE)
@api_view(["PATCH", "DELETE"])
def rule(request, pk):
    user = request.user
    obj = get_object_or_404(RepricingRule.objects.owned_by(user), pk=pk)
    if request.method == "DELETE":
        with transaction.atomic():
            obj.delete()
            _renumber(user, list(RepricingRule.objects.owned_by(user).order_by("position", "id")))
        return messages_response(msg("success", f"Rule {obj.name} deleted."))
    form = RuleForm(request.data, instance=obj)
    if not form.is_valid():
        raise form_errors(form)
    with transaction.atomic():
        obj = form.save()
        if form.cleaned_data.get("position"):
            _place(user, obj, form.cleaned_data["position"])
    return messages_response(msg("success", f"Rule {obj.name} saved."))


@doc(TAGS, "Reorder the repricing rules: ids in their new order (the user's rules left out follow, in their order)",
     request=REORDER)
@api_view(["POST"])
def reorder(request):
    user = request.user
    ids = request.data.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ValidationError({"ids": ["A list of rule ids."]})
    mine = {r.pk: r for r in RepricingRule.objects.owned_by(user).order_by("position", "id")}
    if any(i not in mine for i in ids) or len(set(ids)) != len(ids):
        raise ValidationError({"ids": ["Unknown or repeated rule ids."]})
    ordered = [mine[i] for i in ids] + [r for pk, r in mine.items() if pk not in set(ids)]
    with transaction.atomic():
        _renumber(user, ordered)
    return messages_response(msg("success", "Rules reordered."))


@doc(TAGS, "Preview an unsaved rule (body: its fields; id: the saved rule it replaces), as if saved at its position "
           "(absent: its current one, or last) and enabled: matched (listings it applies to), moves (prices it "
           "would change), up, down", request=RULE_PREVIEW)
@api_view(["POST"])
def rule_preview(request):
    user = request.user
    replace_id = request.data.get("id")
    instance = RepricingRule(user=user)
    if replace_id is not None:
        if not isinstance(replace_id, int) or isinstance(replace_id, bool):
            raise ValidationError({"id": ["A rule id."]})
        instance = get_object_or_404(RepricingRule.objects.owned_by(user), pk=replace_id)
    data = {k: v for k, v in request.data.items() if k != "id"}
    if not str(data.get("name") or "").strip():
        data["name"] = instance.name or "New rule"  # a rule being drafted may have no name yet
    form = RuleForm(data, instance=instance)
    if not form.is_valid():
        raise form_errors(form)
    draft = form.save(commit=False)
    draft.enabled, draft.draft = True, False
    candidate = repricer.engine_rule(draft)
    if replace_id is None:
        candidate = replace(candidate, id=None)
    position = form.cleaned_data.get("position") or (instance.position if replace_id is not None else None)
    return Response(repricer.preview_rule(user, candidate, position, replace_id=replace_id))


def _page(request, rows):
    page = Paginator(rows, PAGE_SIZE).get_page(request.query_params.get("page"))
    return {"results": list(page), "count": page.paginator.count, "page": page.number,
            "num_pages": page.paginator.num_pages}


def _market(params):
    market = params.get("market", "").strip()
    if market and market not in Market.values:
        raise ValidationError({"market": [f"Unknown market '{market}'."]})
    return market


@doc(TAGS, "Dry run of the rules on every listing (to publish, published, paused): totals (every market, by_market), "
           "the 5 biggest moves, and the changes (?market limits them), paged",
     parameters=[MARKET, PAGE])
@api_view(["GET"])
def preview(request):
    market = _market(request.query_params)
    proposals = repricer.propose(request.user)
    moved = [repricer.change_row(p) for p in proposals
             if p.new is not None and (not market or p.listing.market == market)]
    return Response({"totals": repricer.totals(proposals), "biggest": repricer.biggest(proposals),
                     **_page(request, moved)})


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


@doc(TAGS, "Run the rules now: changed prices are saved (a published listing goes back to to publish)")
@api_view(["POST"])
def run(request):
    done = repricer.run(request.user)
    if done.changes:
        text = (f"Repriced {_plural(done.changes, 'listing')}: {done.raised} up, {done.lowered} down. "
                "The next sync pushes them.")
    else:
        text = "No price to change."
    return messages_response(msg("success", text), run_id=done.pk, changes=done.changes, raised=done.raised,
                             lowered=done.lowered)


@doc(TAGS, f"The latest repricing runs (at most {repricer.KEEP_RUNS} are kept)")
@api_view(["GET"])
def runs(request):
    return Response({"results": [repricer.run_data(r) for r in RepricingRun.objects.owned_by(request.user)]})


@doc(TAGS, "One repricing run and its changes, paged", parameters=[PAGE])
@extend_schema(operation_id="repricing_run_retrieve")
@api_view(["GET"])
def run_detail(request, pk):
    obj = get_object_or_404(RepricingRun.objects.owned_by(request.user), pk=pk)
    changes = obj.price_changes.select_related("rule", "floor_rule")
    page = Paginator(changes, PAGE_SIZE).get_page(request.query_params.get("page"))
    return Response({**repricer.run_data(obj), "results": [repricer.stored_change_row(c) for c in page],
                     "count": page.paginator.count, "page": page.number, "num_pages": page.paginator.num_pages})


def _cost(value):
    if value in (None, ""):
        return None
    try:
        cost = Decimal(value.replace(",", "."))
    except InvalidOperation:
        raise ValidationError({"cost": ["Enter a number."]}) from None
    if not cost.is_finite() or cost < 0:
        raise ValidationError({"cost": ["Enter a number."]})
    return cost.quantize(Decimal("0.01"))


@doc(TAGS, "What the rules would set for a new listing of a catalog product on each market (no matching price rule: "
           "the Cardmarket trend), with the rule, the floor that raised it and a note",
     parameters=[
         OpenApiParameter("blueprint", OpenApiTypes.INT, OpenApiParameter.QUERY, required=True,
                          description="Cardmarket product id of the catalog product"),
         query("condition", "Condition (default NM)", enum=Condition.values),
         query("language", "Language (default EN)", enum=Language.values),
         query("foil", "1: foil", enum=["1"]),
         query("cost", "Purchase price of one copy"),
     ])
@api_view(["GET"])
def quote(request):
    params = request.query_params
    try:
        product = int(params.get("blueprint", ""))
    except ValueError:
        raise ValidationError({"blueprint": ["A Cardmarket product id."]}) from None
    bp = Blueprint.objects.select_related("expansion__game").filter(cm_product_id=product).first()
    if bp is None:
        raise ValidationError({"blueprint": ["Unknown catalog product."]})
    condition = params.get("condition", "") or Condition.NEAR_MINT
    language = params.get("language", "") or Language.EN
    if condition not in Condition.values:
        raise ValidationError({"condition": [f"Unknown condition '{condition}'."]})
    if language not in Language.values:
        raise ValidationError({"language": [f"Unknown language '{language}'."]})
    if bp.is_product:
        condition = Condition.NEAR_MINT
    quotes = repricer.quote(request.user, bp, condition, language, params.get("foil") in ("1", "true"),
                            _cost(params.get("cost")))
    return Response({"markets": [q.data() for q in quotes]})


@doc(TAGS, "Ad-hoc reprice of collection items on markets: trend_pct (basis × (1 + value/100)), change (basis "
           "changed by value, unit pct or eur) or set (value); floors, hold_below_cost and the market minimum apply. "
           "dry_run: the prices per item (now and new by market); else saves them (listings to publish, created "
           "when missing)", request=ADHOC)
@api_view(["POST"])
def adhoc(request):
    user = request.user
    ids = request.data.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ValidationError({"ids": ["A non-empty list of item ids."]})
    form = AdhocForm(request.data)
    if not form.is_valid():
        raise form_errors(form)
    data = form.cleaned_data
    items = CollectionItem.objects.owned_by(user).filter(pk__in=ids)
    lines = repricer.adhoc(user, items, data["mode"], data["value"], data["unit"], data["basis"], data["markets"])
    if data["dry_run"]:
        return Response({"results": [repricer.adhoc_row(line) for line in lines]})
    if not lines:
        return messages_response(msg("warning", "None of the selected items is in your collection."), count=0)
    count = repricer.apply_adhoc(user, lines)
    msgs = [msg("success", f"Repriced {_plural(count, 'item')}. The next sync pushes the prices.")]
    skipped = sum(1 for line in lines if not line.outcomes)
    if skipped:
        msgs.append(msg("warning", f"Skipped {_plural(skipped, 'item')} without a price to set."))
    return messages_response(*msgs, count=count)
