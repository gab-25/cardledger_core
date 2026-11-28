"""The purchases API (/api/purchases/): the lots of cards the user bought, their attachments, listing their unlisted
copies and allocating their cost. The figures of a lot are computed from its items (ledger/purchases.py)."""

import os
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.files.storage import default_storage
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.response import Response

from .. import purchases
from ..forms import AttachmentForm, MarketsForm, PurchaseForm
from ..models import Market, PurchaseAttachment, PurchaseLot
from .common import PAGE, doc, form_body, form_errors, messages_response, msg, page_payload, query

TAGS = ["purchases"]
PURCHASE = form_body("Purchase", PurchaseForm)
KPI_DAYS = 30
MARKET_LABELS = dict(Market.choices)
_S = PurchaseLot.Status
TABS = [  # key, label, filter
    ("all", "All", None),
    ("to_grade", "To grade", {"status__in": PurchaseLot.TO_PROCESS}),
    ("listing", "Listing", {"status": _S.LISTING}),
    ("done", "Done", {"status": _S.DONE}),
]
TAB_KEYS = [key for key, _, _ in TABS]


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _attachment(a):
    return {"id": a.pk, "name": a.name, "url": a.file.url, "uploaded_at": a.uploaded_at}


def _kpis(user):
    """The last KPI_DAYS days' lots (by bought_on) and the lots still waiting to be sorted or graded."""
    since = timezone.localdate() - timedelta(days=KPI_DAYS - 1)
    lots = list(PurchaseLot.objects.owned_by(user).filter(bought_on__gte=since))
    waiting = list(PurchaseLot.objects.owned_by(user).filter(status__in=PurchaseLot.TO_PROCESS))
    stats = purchases.stats_of(lots + waiting)
    with_trend = [l for l in lots if stats[l.pk].trend_value]
    paid = sum((l.price_paid for l in with_trend), Decimal("0"))
    trend = sum((stats[l.pk].trend_value for l in with_trend), Decimal("0"))
    return {
        "spent_30d": purchases.money(sum((l.price_paid for l in lots), Decimal("0"))),
        "lots_30d": len(lots),
        "cards_30d": sum(purchases.cards(l, stats[l.pk]) or 0 for l in lots),
        "trend_bought_30d": purchases.money(sum((stats[l.pk].trend_value or Decimal("0") for l in lots),
                                                Decimal("0"))),
        "avg_paid_pct": f"{paid / trend * 100:.1f}" if trend else None,
        "waiting": len(waiting),
        "waiting_cards": sum(purchases.cards(l, stats[l.pk]) or 0 for l in waiting),
    }


def _detail(lot):
    s = purchases.stats_of([lot])[lot.pk]
    return {**purchases.row(lot, s), "notes": lot.notes,
            "attachments": [_attachment(a) for a in lot.attachments.all()]}


@doc(TAGS, "The purchase lots (GET: ?tab all|to_grade (in the post, received, grading)|listing|done, page; newest "
           "bought first) with tab counts and KPIs, or record a new lot (POST: answers its id and code, the next of "
           "the user's PL-0001 sequence). KPIs: the lots bought in the last 30 days (spent, lots, cards, trend value, "
           "avg_paid_pct = paid / trend value × 100 over those with a trend) and the lots waiting (in the post, "
           "received or grading) with their cards. Row figures are in copies; see ledger/purchases.py",
     request=PURCHASE)
@extend_schema(methods=["GET"], parameters=[query("tab", "Which lots (default all)", enum=TAB_KEYS), PAGE])
@api_view(["GET", "POST"])
def lots(request):
    user = request.user
    if request.method == "POST":
        form = PurchaseForm(request.data)
        if not form.is_valid():
            raise form_errors(form)
        fields = {name: form.cleaned_data[name] for name in PurchaseForm.FIELDS}
        lot = purchases.create(user, **fields)
        return messages_response(msg("success", f"Purchase {lot.code} recorded."), status=status.HTTP_201_CREATED,
                                 id=lot.pk, code=lot.code)
    tab = request.query_params.get("tab", "all")
    if tab not in TAB_KEYS:
        tab = "all"
    mine = PurchaseLot.objects.owned_by(user)
    tabs = [{"key": key, "label": label, "count": (mine.filter(**f) if f else mine).count()} for key, label, f in TABS]
    shown = dict((key, f) for key, _, f in TABS)[tab]
    listed = mine.filter(**shown) if shown else mine
    payload = page_payload(request, listed.order_by("-bought_on", "-number"), lambda lot: lot)
    stats = purchases.stats_of(payload["results"])
    payload["results"] = [purchases.row(lot, stats[lot.pk]) for lot in payload["results"]]
    return Response({"tab": tab, "tabs": tabs, "kpis": _kpis(user), **payload})


@doc(TAGS, "The code the next purchase lot will get (PL-0001, PL-0002, ...)")
@api_view(["GET"])
def next_code(request):
    return Response({"code": purchases.next_code(request.user)})


@doc(TAGS, "One purchase lot: read it (GET: its row plus notes and attachments), change it (PATCH: a field left out "
           "keeps its value) or delete it (DELETE: its items stay in the collection without a lot, its attachments "
           "are deleted)", request=PURCHASE)
@extend_schema(methods=["GET"], operation_id="purchase_retrieve")
@api_view(["GET", "PATCH", "DELETE"])
def lot(request, pk):
    obj = get_object_or_404(PurchaseLot.objects.owned_by(request.user), pk=pk)
    if request.method == "DELETE":
        files = [a.file.name for a in obj.attachments.all()]
        obj.delete()
        for name in files:
            default_storage.delete(name)
        return messages_response(msg("success", f"Purchase {obj.code} deleted."))
    if request.method == "PATCH":
        form = PurchaseForm(request.data, instance=obj)
        if not form.is_valid():
            raise form_errors(form)
        form.save()
        return messages_response(msg("success", f"Purchase {obj.code} saved."))
    return Response(_detail(obj))


@doc(TAGS, "Attach a receipt or photo to a purchase lot (multipart 'file': PDF or PNG/JPEG/GIF/WebP image, at most "
           "10 MB); answers the attachment {id, name, url, uploaded_at}",
     request={"multipart/form-data": form_body("PurchaseAttachment", AttachmentForm)})
@api_view(["POST"])
@parser_classes([MultiPartParser])
def attachments(request, pk):
    obj = get_object_or_404(PurchaseLot.objects.owned_by(request.user), pk=pk)
    form = AttachmentForm(data={}, files=request.FILES)
    if not form.is_valid():
        raise form_errors(form)
    upload = form.cleaned_data["file"]
    name = os.path.basename(upload.name)[:200] or "attachment"
    upload.name = f"{uuid.uuid4().hex}{os.path.splitext(name)[1].lower()[:10]}"  # unguessable stored name
    attachment = PurchaseAttachment.objects.create(lot=obj, file=upload, name=name)
    return messages_response(msg("success", f"Attached {name}."), status=status.HTTP_201_CREATED,
                             attachment=_attachment(attachment))


@doc(TAGS, "An attachment of a purchase lot: read it (GET: {id, name, url, uploaded_at}) or remove it and its file "
           "(DELETE)")
@extend_schema(methods=["GET"], operation_id="purchase_attachment_retrieve")
@api_view(["GET", "DELETE"])
def attachment(request, pk, attachment_pk):
    obj = get_object_or_404(PurchaseAttachment.objects.owned_by(request.user), lot_id=pk, pk=attachment_pk)
    if request.method == "GET":
        return Response(_attachment(obj))
    name = obj.file.name
    obj.delete()
    default_storage.delete(name)
    return messages_response(msg("success", f"Removed {obj.name}."))


@doc(TAGS, "List the lot's unlisted items (copies in stock without an active listing on any market) on markets at "
           "the price the repricing rules quote (Cardmarket trend when no price rule matches): listings to publish, "
           "pushed by the next extension sync. Items without a catalog product or a price are skipped",
     request=form_body("PurchaseList", MarketsForm))
@api_view(["POST"])
@parser_classes([JSONParser])
def list_lot(request, pk):
    obj = get_object_or_404(PurchaseLot.objects.owned_by(request.user), pk=pk)
    form = MarketsForm(request.data)
    if not form.is_valid():
        raise form_errors(form)
    markets = form.cleaned_data["markets"]
    listed, skipped = purchases.list_unlisted(request.user, obj, markets)
    where = " and ".join(MARKET_LABELS[m] for m in markets)
    if not listed and not skipped:
        return messages_response(msg("warning", f"Nothing to list: every item of {obj.code} is listed."), count=0)
    msgs = [msg("success", f"Listed {_plural(listed, 'item')} of {obj.code} on {where}.")] if listed else []
    if skipped:
        msgs.append(msg("warning", f"Skipped {_plural(skipped, 'item')} without a price (no rule and no Cardmarket "
                                   "trend)."))
    return messages_response(*msgs, count=listed)


@doc(TAGS, "Allocate the lot's price over its items: each item's cost per copy = price_paid × (its trend × copies / "
           "the lot's trend value) / copies, rounded to the cent; items without a Cardmarket trend keep their cost",
     request=None)
@api_view(["POST"])
def allocate_cost(request, pk):
    obj = get_object_or_404(PurchaseLot.objects.owned_by(request.user), pk=pk)
    with transaction.atomic():
        costed, without = purchases.allocate_cost(obj)
    if not costed:
        return messages_response(msg("warning", f"No item of {obj.code} has a Cardmarket trend: no cost allocated."),
                                 count=0)
    msgs = [msg("success", f"Allocated €{obj.price_paid:.2f} over {_plural(costed, 'item')} of {obj.code}.")]
    if without:
        msgs.append(msg("warning", f"{_plural(without, 'item')} without a Cardmarket trend got no cost."))
    return messages_response(*msgs, count=costed)
