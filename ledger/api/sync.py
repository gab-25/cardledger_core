"""The sync API (/api/sync/): the markets' connections, conflicts to resolve and the activity log. Syncing itself is
done by the browser extension (its reports feed the connections, conflicts and log)."""

from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .. import services
from ..models import Listing, MarketConnection, SyncConflict, SyncLog
from .common import body, doc, messages_response, msg

TAGS = ["sync"]
LOG_SIZE = 20


@doc(TAGS, "The markets' connections (Cardmarket, CardTrader: as me's markets, plus when the orders were last read "
           "and the open conflicts), the changes waiting for the next sync, open conflicts and the last activity")
@api_view(["GET"])
def sync(request):
    user = request.user
    orders_synced = dict(MarketConnection.objects.owned_by(user).values_list("market", "orders_synced_at"))
    open_conflicts = dict(
        SyncConflict.objects.owned_by(user).filter(resolution=SyncConflict.Resolution.OPEN)
        .order_by().values_list("market").annotate(n=Count("pk"))
    )
    conflicts = (
        SyncConflict.objects.owned_by(user).filter(resolution=SyncConflict.Resolution.OPEN)
        .select_related("item__card")
    )
    return Response({
        "pending_changes": Listing.objects.owned_by(user).filter(status=Listing.Status.TO_PUBLISH).count(),
        "connections": [
            {**m, "orders_synced_at": orders_synced.get(m["code"]), "open_conflicts": open_conflicts.get(m["code"], 0)}
            for m in services.market_states(user)
        ],
        "conflicts": [
            {
                "id": c.pk, "name": c.item.card.name, "set_code": c.item.card.set_code,
                "condition": c.item.condition, "language": c.item.language, "service": c.get_market_display(),
                "market": c.market,
                "local_value": c.local_value, "remote_value": c.remote_value,
            }
            for c in conflicts
        ],
        "log": [
            {"created_at": l.created_at, "level": l.level, "level_label": l.get_level_display(), "message": l.message}
            for l in SyncLog.objects.visible_to(user)[:LOG_SIZE]
        ],
    })


@doc(TAGS, "Resolve a conflict keeping the Ledger value or the remote one (keep: ledger | remote)",
     request=body("ResolveConflict", {"keep": serializers.ChoiceField(choices=[
         SyncConflict.Resolution.LEDGER, SyncConflict.Resolution.REMOTE])}))
@api_view(["POST"])
def resolve(request, pk):
    conflict = get_object_or_404(
        SyncConflict.objects.owned_by(request.user), pk=pk, resolution=SyncConflict.Resolution.OPEN,
    )
    keep = request.data.get("keep")
    if keep not in (SyncConflict.Resolution.LEDGER, SyncConflict.Resolution.REMOTE):
        raise ValidationError({"detail": "Choose which source to keep.", "code": "invalid"})
    services.resolve_conflict(conflict, keep)
    return messages_response(msg("success", f"Conflict on {conflict.item.card.name} resolved."))
